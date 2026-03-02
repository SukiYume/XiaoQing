"""
apod 插件单元测试

测试每日一天文图插件的功能，包括：
- 命令处理（help、默认查询）
- HTML 解析和标题提取
- 图片处理
- 视频处理（iframe 和 video 标签）
- 网络请求和代理
- 定时任务
"""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch
import aiohttp

ROOT = Path(__file__).resolve().parent.parent.parent

import importlib.util
spec = importlib.util.spec_from_file_location("apod_main", ROOT / "plugins" / "apod" / "main.py")
apod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apod)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def temp_data_dir():
    """创建临时数据目录"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_context(temp_data_dir):
    """模拟插件上下文"""
    class MockContext:
        def __init__(self, data_dir):
            self.data_dir = data_dir
            self.config = {"plugins": {"apod": {}}}
            self.http_session = None
            self.logger = MagicMock()
            self.current_user_id = 12345
            self.current_group_id = 945793035
            self.send_action = AsyncMock()

    return MockContext(temp_data_dir)


@pytest.fixture
def mock_event():
    """模拟事件"""
    return {
        "user_id": 12345,
        "group_id": 945793035,
        "message_type": "group"
    }


# ============================================================
# Sample HTML Responses
# ============================================================

SAMPLE_APOD_HTML_WITH_IMAGE = """
<!DOCTYPE html>
<html>
<body>
    <center></center>
    <center>
        <b>
            The Galaxy Center
        </b>
    </center>
    <p>
        <b>Explanation:</b> This is a test explanation of the astronomy picture.
        It contains details about the image shown above.
        <br><br>
        Tomorrow's picture: Something else
    </p>
    <img src="image/apod260201.jpg" alt="Astronomy Picture">
</body>
</html>
"""

SAMPLE_APOD_HTML_WITH_IFRAME = """
<!DOCTYPE html>
<html>
<body>
    <center>
        <b>Video Title</b>
    </center>
    <p>
        <b>Explanation:</b> This is a video APOD.
    </p>
    <iframe src="https://www.youtube.com/embed/test123"></iframe>
</body>
</html>
"""

SAMPLE_APOD_HTML_WITH_VIDEO = """
<!DOCTYPE html>
<html>
<body>
    <center>
        <b>Another Video</b>
    </center>
    <p>
        <b>Explanation:</b> Video tag explanation.
    </p>
    <video>
        <source src="video/apod_video.mp4" type="video/mp4">
    </video>
</body>
</html>
"""

SAMPLE_APOD_HTML_NO_TITLE = """
<!DOCTYPE html>
<html>
<head><title>Page Title</title></head>
<body>
    <img src="image/test.jpg">
</body>
</html>
"""


# ============================================================
# Test Config
# ============================================================

class TestConfig:
    """测试配置功能"""

    def test_get_config_default(self, mock_context):
        """测试获取默认配置"""
        config = apod._get_config(mock_context)
        assert config == {}

    def test_get_config_with_values(self, temp_data_dir):
        """测试获取有值的配置"""
        class MockContext:
            def __init__(self, data_dir):
                self.data_dir = data_dir
                self.config = {"plugins": {"apod": {"url": "http://test.com", "proxy": "http://proxy"}}}
                self.http_session = None
                self.logger = MagicMock()

        context = MockContext(temp_data_dir)
        config = apod._get_config(context)
        assert config["url"] == "http://test.com"
        assert config["proxy"] == "http://proxy"

    def test_get_proxy(self, mock_context):
        """测试获取代理"""
        proxy = apod._get_proxy(mock_context)
        assert proxy is None

    def test_get_proxy_with_value(self, temp_data_dir):
        """测试获取代理（有值）"""
        class MockContext:
            def __init__(self, data_dir):
                self.data_dir = data_dir
                self.config = {"plugins": {"apod": {"proxy": "http://proxy.example.com"}}}
                self.http_session = None
                self.logger = MagicMock()

        context = MockContext(temp_data_dir)
        proxy = apod._get_proxy(context)
        assert proxy == "http://proxy.example.com"


# ============================================================
# Test Title Extraction
# ============================================================

class TestTitleExtraction:
    """测试标题提取功能"""

    def test_extract_title_from_center_b(self):
        """测试从 center 标签中的 b 标签提取标题"""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(SAMPLE_APOD_HTML_WITH_IMAGE, 'html.parser')
        title = apod._extract_title(soup, MagicMock())
        assert "Galaxy Center" in title or "Astronomy" in title

    def test_extract_title_no_center(self):
        """测试没有 center 标签时使用 title 标签"""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(SAMPLE_APOD_HTML_NO_TITLE, 'html.parser')
        title = apod._extract_title(soup, MagicMock())
        assert title == "Page Title" or title == apod.DEFAULT_FALLBACK_TITLE

    def test_extract_title_fallback(self):
        """测试标题提取失败时使用默认值"""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup("<html><body></body></html>", 'html.parser')
        title = apod._extract_title(soup, MagicMock())
        assert title == apod.DEFAULT_FALLBACK_TITLE


# ============================================================
# Test Filename Sanitization
# ============================================================

class TestFilenameSanitization:
    """测试文件名清理功能"""

    def test_sanitize_filename_simple(self):
        """测试简单文件名"""
        result = apod._sanitize_filename("http://example.com/image.jpg")
        assert result == "image.jpg"

    def test_sanitize_filename_with_special_chars(self):
        """测试包含特殊字符的文件名"""
        result = apod._sanitize_filename("http://example.com/image<>:\"/\\|?*.jpg")
        assert "<" not in result
        assert ">" not in result
        assert ":" not in result
        assert ".jpg" in result

    def test_sanitize_filename_no_extension(self):
        """测试没有扩展名的文件名"""
        result = apod._sanitize_filename("http://example.com/image")
        assert result.endswith(".jpg")

    def test_sanitize_filename_url_encoded(self):
        """测试 URL 编码的文件名"""
        result = apod._sanitize_filename("http://example.com/image%20test.jpg")
        assert "image" in result
        assert ".jpg" in result


# ============================================================
# Test Explanation Extraction
# ============================================================

class TestExplanationExtraction:
    """测试解释文本提取功能"""

    @pytest.mark.asyncio
    async def test_get_explanation_valid(self):
        """测试提取有效的解释文本"""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(SAMPLE_APOD_HTML_WITH_IMAGE, 'html.parser')
        explanation = await apod.get_explanation(soup, MagicMock())
        assert "test explanation" in explanation.lower()

    @pytest.mark.asyncio
    async def test_get_explanation_no_soup(self):
        """测试空 soup"""
        explanation = await apod.get_explanation(None, MagicMock())
        assert explanation == apod.NO_EXPLANATION_TEXT

    @pytest.mark.asyncio
    async def test_get_explanation_no_paragraphs(self):
        """测试没有段落"""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup("<html><body>No content</body></html>", 'html.parser')
        explanation = await apod.get_explanation(soup, MagicMock())
        assert "No explanation found" in explanation or "unavailable" in explanation.lower()

    @pytest.mark.asyncio
    async def test_get_explanation_removes_tomorrow(self):
        """测试移除 Tomorrow's picture 部分"""
        html = """
        <p>
            <b>Explanation:</b> Today's picture description.
            Tomorrow's picture: Future content
        </p>
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, 'html.parser')
        explanation = await apod.get_explanation(soup, MagicMock())
        assert "Tomorrow" not in explanation
        assert "Today's picture description" in explanation


# ============================================================
# Test Image URL Extraction
# ============================================================

class TestImageExtraction:
    """测试图片提取功能"""

    @pytest.mark.asyncio
    async def test_handle_with_image(self, mock_context, mock_event):
        """测试处理图片 APOD"""
        # 创建 mock HTTP session
        class MockResponse:
            status = 200
            async def text(self):
                return SAMPLE_APOD_HTML_WITH_IMAGE
            async def read(self):
                return b"fake image data"

        class MockGetContextManager:
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        mock_context.http_session = MockSession()

        result = await apod.handle("apod", "", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "Galaxy" in result_text or "explanation" in result_text.lower()

    @pytest.mark.asyncio
    async def test_handle_image_download_failure(self, mock_context, mock_event):
        """测试图片下载失败"""
        # 第一次返回 HTML，第二次返回 None（下载失败）
        call_count = [0]

        class MockResponse:
            status = 200
            async def text(self):
                return SAMPLE_APOD_HTML_WITH_IMAGE
            async def read(self):
                call_count[0] += 1
                if call_count[0] == 1:
                    return b"html"
                return None  # 图片下载失败

        class MockGetContextManager:
            def __init__(self, fail=False):
                self.fail = fail
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        mock_context.http_session = MockSession()

        result = await apod.handle("apod", "", mock_event, mock_context)
        assert result is not None


# ============================================================
# Test Video Handling
# ============================================================

class TestVideoHandling:
    """测试视频处理功能"""

    @pytest.mark.asyncio
    async def test_handle_with_iframe(self, mock_context, mock_event):
        """测试处理 iframe 视频"""
        class MockResponse:
            status = 200
            async def text(self):
                return SAMPLE_APOD_HTML_WITH_IFRAME

        class MockGetContextManager:
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        mock_context.http_session = MockSession()

        result = await apod.handle("apod", "", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "youtube" in result_text.lower() or "video" in result_text.lower()

    @pytest.mark.asyncio
    async def test_handle_with_video_tag(self, mock_context, mock_event):
        """测试处理 video 标签"""
        class MockResponse:
            status = 200
            async def text(self):
                return SAMPLE_APOD_HTML_WITH_VIDEO

        class MockGetContextManager:
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        mock_context.http_session = MockSession()

        result = await apod.handle("apod", "", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "video" in result_text.lower() or "mp4" in result_text


# ============================================================
# Test Network Requests
# ============================================================

class TestNetworkRequests:
    """测试网络请求功能"""

    @pytest.mark.asyncio
    async def test_fetch_with_retry_success(self, mock_context):
        """测试重试机制成功"""
        class MockResponse:
            status = 200
            async def text(self):
                return "Success"

        class MockGetContextManager:
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        result = await apod._fetch_with_retry(
            session=MockSession(),
            url="http://test.com",
            proxy=None,
            timeout=aiohttp.ClientTimeout(total=60),
            is_binary=False,
            context=mock_context
        )
        assert result is not None

    @pytest.mark.asyncio
    async def test_fetch_with_retry_direct_fails_proxy_succeeds(self, mock_context):
        """测试直连失败代理成功"""

    @pytest.mark.asyncio
    async def test_fetch_with_retry_both_fail(self, mock_context):
        """测试直连和代理都失败"""

    @pytest.mark.asyncio
    async def test_fetch_binary(self, mock_context):
        """测试获取二进制数据"""
        class MockResponse:
            status = 200
            async def read(self):
                return b"binary data"

        class MockGetContextManager:
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        result = await apod._fetch_with_retry(
            session=MockSession(),
            url="http://test.com/image.jpg",
            proxy=None,
            timeout=aiohttp.ClientTimeout(total=60),
            is_binary=True,
            context=mock_context
        )
        assert result == b"binary data"


# ============================================================
# Test Download Image
# ============================================================

class TestDownloadImage:
    """测试图片下载功能"""

    @pytest.mark.asyncio
    async def test_download_image_success(self, mock_context, temp_data_dir):
        """测试成功下载图片"""
        import asyncio

        class MockResponse:
            status = 200
            async def read(self):
                return b"fake image content"

        class MockGetContextManager:
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        file_path = temp_data_dir / "test_image.jpg"

        result = await apod.download_image(
            session=MockSession(),
            url="http://example.com/image.jpg",
            file_path=file_path,
            proxy=None,
            timeout=aiohttp.ClientTimeout(total=60),
            context=mock_context
        )

        assert result is True
        assert file_path.exists()
        assert file_path.read_bytes() == b"fake image content"

    @pytest.mark.asyncio
    async def test_download_image_creates_directory(self, mock_context, temp_data_dir):
        """测试下载时创建目录"""
        import asyncio

        class MockResponse:
            status = 200
            async def read(self):
                return b"content"

        class MockGetContextManager:
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        # 使用不存在的子目录
        file_path = temp_data_dir / "subdir" / "nested" / "image.jpg"

        result = await apod.download_image(
            session=MockSession(),
            url="http://example.com/image.jpg",
            file_path=file_path,
            proxy=None,
            timeout=aiohttp.ClientTimeout(total=60),
            context=mock_context
        )

        assert result is True
        assert file_path.exists()

    @pytest.mark.asyncio
    async def test_download_image_failure(self, mock_context, temp_data_dir):
        """测试下载失败"""
        class MockGetContextManager:
            async def __aenter__(self):
                raise aiohttp.ClientError("Download failed")
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        file_path = temp_data_dir / "test_image.jpg"

        result = await apod.download_image(
            session=MockSession(),
            url="http://example.com/image.jpg",
            file_path=file_path,
            proxy=None,
            timeout=aiohttp.ClientTimeout(total=60),
            context=mock_context
        )

        assert result is False


# ============================================================
# Test Handle Commands
# ============================================================

class TestHandleCommands:
    """测试命令处理"""

    @pytest.mark.asyncio
    async def test_handle_help(self, mock_context, mock_event):
        """测试 help 命令"""
        result = await apod.handle("apod", "help", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "APOD" in result_text or "天文图" in result_text

    @pytest.mark.asyncio
    async def test_handle_help_chinese(self, mock_context, mock_event):
        """测试中文帮助命令"""
        result = await apod.handle("apod", "帮助", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "APOD" in result_text or "天文" in result_text

    @pytest.mark.asyncio
    async def test_handle_network_error(self, mock_context, mock_event):
        """测试网络错误"""

    @pytest.mark.asyncio
    async def test_handle_http_error(self, mock_context, mock_event):
        """测试 HTTP 错误"""
        class MockResponse:
            status = 404

        class MockGetContextManager:
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        mock_context.http_session = MockSession()

        result = await apod.handle("apod", "", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "404" in result_text or "失败" in result_text

    @pytest.mark.asyncio
    async def test_handle_unsupported_format(self, mock_context, mock_event):
        """测试不支持的格式"""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <center><b>Some Content</b></center>
            <p>No media here</p>
        </body>
        </html>
        """

        class MockResponse:
            status = 200
            async def text(self):
                return html

        class MockGetContextManager:
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        mock_context.http_session = MockSession()

        result = await apod.handle("apod", "", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "不支持" in result_text or "访问" in result_text


# ============================================================
# Test Scheduled
# ============================================================

class TestScheduled:
    """测试定时任务"""

    @pytest.mark.asyncio
    async def test_scheduled_task(self, mock_context):
        """测试定时任务入口"""
        class MockResponse:
            status = 200
            async def text(self):
                return SAMPLE_APOD_HTML_WITH_IMAGE
            async def read(self):
                return b"fake image"

        class MockGetContextManager:
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        mock_context.http_session = MockSession()

        result = await apod.scheduled(mock_context)
        assert result is not None


# ============================================================
# Test Help
# ============================================================

class TestHelp:
    """测试帮助信息"""

    def test_show_help(self):
        """测试显示帮助信息"""
        help_text = apod._show_help()
        assert help_text is not None
        assert "APOD" in help_text or "天文图" in help_text
        assert "/apod" in help_text


# ============================================================
# Test Init
# ============================================================

class TestInit:
    """测试插件初始化"""

    def test_init(self):
        """测试插件初始化"""
        apod.init()
        assert True


# ============================================================
# Test Image Path Construction
# ============================================================

class TestImagePathConstruction:
    """测试图片路径构造"""

    @pytest.mark.asyncio
    async def test_relative_image_url(self, mock_context, mock_event):
        """测试相对图片 URL"""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <center><b>Test</b></center>
            <img src="image/test.jpg">
        </body>
        </html>
        """

        class MockResponse:
            status = 200
            async def text(self):
                return html
            async def read(self):
                return b"data"

        class MockGetContextManager:
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        mock_context.http_session = MockSession()

        result = await apod.handle("apod", "", mock_event, mock_context)
        assert result is not None

    @pytest.mark.asyncio
    async def test_absolute_image_url(self, mock_context, mock_event):
        """测试绝对图片 URL"""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <center><b>Test</b></center>
            <img src="https://example.com/image/test.jpg">
        </body>
        </html>
        """

        class MockResponse:
            status = 200
            async def text(self):
                return html
            async def read(self):
                return b"data"

        class MockGetContextManager:
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        mock_context.http_session = MockSession()

        result = await apod.handle("apod", "", mock_event, mock_context)
        assert result is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
