"""
apod 插件单元测试

测试每日一天文图插件的功能，包括：
- 命令处理（help、默认查询）
- HTML 解析和标题提取
- 图片处理
- 视频处理（iframe 和 video 标签）
- 受限网络请求
- 定时任务
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.safe_http import SafeHttpError, UnsafeUrlError
from plugins.apod import main as apod
from tests.helpers.settings_snapshot import with_settings_reader

# ============================================================
# Fixtures
# ============================================================


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
            self.current_group_id = 123456789
            self.send_action = AsyncMock()

    return with_settings_reader(MockContext(temp_data_dir))


@pytest.fixture
def mock_event():
    """模拟事件"""
    return {"user_id": 12345, "group_id": 123456789, "message_type": "group"}


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


@pytest.fixture(autouse=True)
def safe_transport_adapter(monkeypatch, mock_context):
    """Adapt historical in-memory sessions to the pinned APOD fetch API."""

    async def response_bytes(url: str) -> bytes | None:
        session = mock_context.http_session
        if session is None:
            return None
        async with session.get(url) as response:
            if response.status != 200:
                return None
            if hasattr(response, "read"):
                value = await response.read()
            else:
                value = (await response.text()).encode("utf-8")
            return value if isinstance(value, bytes) else None

    async def fetch_html(url, **_kwargs):
        body = await response_bytes(url)
        if body is None:
            return None
        return SimpleNamespace(
            url=url,
            status=200,
            body=body,
            headers={"Content-Type": "text/html; charset=utf-8"},
        )

    async def fetch_bytes(url, **_kwargs):
        body = await response_bytes(url)
        if body is None:
            return None
        return SimpleNamespace(
            url=url,
            status=200,
            body=body,
            headers={"Content-Type": "image/jpeg"},
        )

    async def download(url, images_dir, _context):
        body = await response_bytes(url)
        if not body:
            return None
        target = images_dir / "legacy-test-image.jpg"
        target.write_bytes(body)
        return target

    monkeypatch.setattr(apod, "fetch_public_html", fetch_html)
    monkeypatch.setattr(apod, "fetch_public_bytes", fetch_bytes)
    monkeypatch.setattr(apod, "_safe_download_image", download)


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
                self.config = {
                    "plugins": {
                        "apod": {
                            "url": "https://apod.nasa.gov/apod/astropix.html",
                            "allowed_hosts": ["apod.nasa.gov"],
                        }
                    }
                }
                self.http_session = None
                self.logger = MagicMock()

        context = with_settings_reader(MockContext(temp_data_dir))
        config = apod._get_config(context)
        assert config == {
            "url": "https://apod.nasa.gov/apod/astropix.html",
            "allowed_hosts": ["apod.nasa.gov"],
        }

    def test_allowed_hosts_ignores_non_string_and_empty_entries(self, mock_context):
        mock_context.config["plugins"]["apod"]["allowed_hosts"] = [
            "Images.Example.",
            123,
            None,
            "",
        ]

        assert apod._allowed_hosts(mock_context) == {"apod.nasa.gov", "images.example"}


# ============================================================
# Test Title Extraction
# ============================================================


class TestTitleExtraction:
    """测试标题提取功能"""

    def test_extract_title_from_center_b(self):
        """测试从 center 标签中的 b 标签提取标题"""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(SAMPLE_APOD_HTML_WITH_IMAGE, "html.parser")
        title = apod._extract_title(soup, MagicMock())
        assert "Galaxy Center" in title or "Astronomy" in title

    def test_extract_title_no_center(self):
        """测试没有 center 标签时使用 title 标签"""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(SAMPLE_APOD_HTML_NO_TITLE, "html.parser")
        title = apod._extract_title(soup, MagicMock())
        assert title == "Page Title" or title == apod.DEFAULT_FALLBACK_TITLE

    def test_extract_title_fallback(self):
        """测试标题提取失败时使用默认值"""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup("<html><body></body></html>", "html.parser")
        title = apod._extract_title(soup, MagicMock())
        assert title == apod.DEFAULT_FALLBACK_TITLE


def test_image_selection_prefers_apod_image_path(mock_context) -> None:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(
        '<img src="/static/logo.png"><img src="image/apod260201.jpg">',
        "html.parser",
    )
    assert (
        apod._find_image_url(
            soup,
            apod.DEFAULT_APOD_URL,
            mock_context,
            {"apod.nasa.gov"},
        )
        == "https://apod.nasa.gov/apod/image/apod260201.jpg"
    )


def test_image_selection_rejects_ambiguous_allowed_images(mock_context) -> None:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(
        '<img src="/static/logo.png"><img src="/static/banner.png">',
        "html.parser",
    )
    assert (
        apod._find_image_url(
            soup,
            apod.DEFAULT_APOD_URL,
            mock_context,
            {"apod.nasa.gov"},
        )
        is None
    )


# ============================================================
# Test content-addressed cache names
# ============================================================


class TestCacheFilename:
    def test_cache_filename_is_stable_hash_with_verified_mime_extension(self):
        first = apod._cache_filename("https://apod.nasa.gov/image?id=1", ".png")
        second = apod._cache_filename("https://apod.nasa.gov/image?id=1", ".png")
        different = apod._cache_filename("https://apod.nasa.gov/image?id=2", ".png")

        assert first == second
        assert first != different
        assert len(Path(first).stem) == 64
        assert first.endswith(".png")

    def test_cache_filename_does_not_trust_url_extension(self):
        result = apod._cache_filename("https://apod.nasa.gov/payload.exe", ".jpg")

        assert result.endswith(".jpg")
        assert "payload" not in result


# ============================================================
# Test Explanation Extraction
# ============================================================


class TestExplanationExtraction:
    """测试解释文本提取功能"""

    def test_get_explanation_valid(self):
        """测试提取有效的解释文本"""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(SAMPLE_APOD_HTML_WITH_IMAGE, "html.parser")
        explanation = apod.get_explanation(soup, MagicMock())
        assert "test explanation" in explanation.lower()

    def test_get_explanation_no_soup(self):
        """测试空 soup"""
        explanation = apod.get_explanation(None, MagicMock())
        assert explanation == apod.NO_EXPLANATION_TEXT

    def test_get_explanation_no_paragraphs(self):
        """测试没有段落"""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup("<html><body>No content</body></html>", "html.parser")
        explanation = apod.get_explanation(soup, MagicMock())
        assert "No explanation found" in explanation or "unavailable" in explanation.lower()

    def test_get_explanation_removes_tomorrow(self):
        """测试移除 Tomorrow's picture 部分"""
        html = """
        <p>
            <b>Explanation:</b> Today's picture description.
            Tomorrow's picture: Future content
        </p>
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        explanation = apod.get_explanation(soup, MagicMock())
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
        call_count = [0]

        class MockResponse:
            status = 200

            async def text(self):
                return SAMPLE_APOD_HTML_WITH_IMAGE

            async def read(self):
                call_count[0] += 1
                if call_count[0] == 1:
                    return SAMPLE_APOD_HTML_WITH_IMAGE.encode("utf-8")
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
        assert [segment["type"] for segment in result] == ["image", "text"]
        assert result[0]["data"]["file"].startswith("file:")
        assert result[1]["data"]["text"].startswith("The Galaxy Center\n\n")
        assert "test explanation of the astronomy picture" in result[1]["data"]["text"]

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
                    return SAMPLE_APOD_HTML_WITH_IMAGE.encode("utf-8")
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
        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert "图片暂时下载失败" in result[0]["data"]["text"]
        assert "The Galaxy Center" in result[0]["data"]["text"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            SafeHttpError("response is too large"),
            TimeoutError("image download timed out"),
            OSError("invalid image stream"),
            ValueError("image pixel budget exceeded"),
        ],
    )
    async def test_handle_image_download_error_degrades_to_text(
        self,
        mock_context,
        mock_event,
        monkeypatch,
        error,
    ):
        """图片增强失败时仍返回已抓取的标题、说明与原图链接。"""
        monkeypatch.setattr(
            apod,
            "fetch_public_html",
            AsyncMock(
                return_value=SimpleNamespace(
                    url=apod.DEFAULT_APOD_URL,
                    body=SAMPLE_APOD_HTML_WITH_IMAGE,
                    headers={"Content-Type": "text/html"},
                )
            ),
        )
        monkeypatch.setattr(
            apod,
            "_safe_download_image",
            AsyncMock(side_effect=error),
        )

        result = await apod.handle("apod", "", mock_event, mock_context)

        assert len(result) == 1
        assert result[0]["type"] == "text"
        message = result[0]["data"]["text"]
        assert "图片暂时下载失败" in message
        assert "https://apod.nasa.gov/apod/image/apod260201.jpg" in message
        assert "The Galaxy Center" in message
        assert "XQ-PLUGIN-UNEXPECTED" not in message


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

            async def read(self):
                return SAMPLE_APOD_HTML_WITH_IFRAME.encode("utf-8")

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

            async def read(self):
                return SAMPLE_APOD_HTML_WITH_VIDEO.encode("utf-8")

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
        assert "https://apod.nasa.gov/apod/video/apod_video.mp4" in result_text

    @pytest.mark.asyncio
    async def test_relative_iframe_uses_final_redirect_url(
        self,
        mock_context,
        mock_event,
        monkeypatch,
    ):
        html = '<html><body><iframe src="../media/video"></iframe></body></html>'
        monkeypatch.setattr(
            apod,
            "fetch_public_html",
            AsyncMock(
                return_value=SimpleNamespace(
                    url="https://apod.nasa.gov/redirected/day/page.html",
                    body=html,
                    headers={"Content-Type": "text/html"},
                )
            ),
        )

        result = await apod.handle("apod", "", mock_event, mock_context)

        assert result[0]["data"]["text"].startswith("https://apod.nasa.gov/redirected/media/video")


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
        mock_context.http_session = None

        result = await apod.handle("apod", "", mock_event, mock_context)

        assert result == [
            {
                "type": "text",
                "data": {"text": "❌ 获取失败: 网络错误"},
            }
        ]

    @pytest.mark.asyncio
    async def test_handle_dns_safety_rejection_is_actionable(
        self,
        mock_context,
        mock_event,
        monkeypatch,
    ):
        monkeypatch.setattr(
            apod,
            "fetch_public_html",
            AsyncMock(side_effect=UnsafeUrlError("hostname has a non-public DNS result")),
        )

        result = await apod.handle("apod", "", mock_event, mock_context)

        assert result == [
            {
                "type": "text",
                "data": {"text": "❌ APOD 网络安全检查失败，请检查 DNS、代理或 allowed_hosts 配置"},
            }
        ]
        assert "XQ-PLUGIN-UNEXPECTED" not in str(result)

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

            async def read(self):
                return html.encode("utf-8")

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
    async def test_scheduled_task(self, mock_context, monkeypatch):
        """测试定时任务入口"""
        expected = [{"type": "text", "data": {"text": "scheduled APOD"}}]
        handle = AsyncMock(return_value=expected)
        monkeypatch.setattr(apod, "handle", handle)

        result = await apod.scheduled(mock_context)

        assert result == expected
        handle.assert_awaited_once_with(
            command="apod",
            args="",
            event={},
            context=mock_context,
        )


# ============================================================
# Test Help
# ============================================================


class TestHelp:
    """测试帮助信息"""

    def test_show_help(self):
        """测试显示帮助信息"""
        help_text = apod._show_help()
        assert "每日一天文图 (APOD)" in help_text
        assert "/apod" in help_text
        assert "HTTPS、主机、响应字节、MIME 与图片像素受限校验" in help_text


# ============================================================
# Test Image Path Construction
# ============================================================


class TestImagePathConstruction:
    """测试图片路径构造"""

    @pytest.mark.parametrize(
        "image_url",
        [
            pytest.param("image/test.jpg", id="relative"),
            pytest.param("https://example.com/image/test.jpg", id="absolute"),
        ],
    )
    @pytest.mark.asyncio
    async def test_image_url(self, mock_context, mock_event, image_url):
        """相对与绝对图片 URL 均能进入统一下载流程。"""
        html = f"""
            <!DOCTYPE html>
            <html>
            <body>
                <center><b>Test</b></center>
                <img src="{image_url}">
            </body>
            </html>
            """

        class MockResponse:
            status = 200

            async def text(self):
                return html

            async def read(self):
                if not hasattr(self, "count"):
                    self.count = 0
                self.count += 1
                if self.count == 1:
                    return html.encode("utf-8")
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
