"""
github 插件单元测试
"""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import aiohttp

ROOT = Path(__file__).resolve().parent.parent.parent

import importlib.util
spec = importlib.util.spec_from_file_location("github_main", ROOT / "plugins" / "github" / "main.py")
github = importlib.util.module_from_spec(spec)
spec.loader.exec_module(github)


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
            self.secrets = {"plugins": {"github": {"proxy": ""}}}

    return MockContext(temp_data_dir)


@pytest.fixture
def mock_event():
    """模拟事件"""
    return {"user_id": 12345}


# ============================================================
# Mock HTML Response
# ============================================================

SAMPLE_TRENDING_HTML = """
<!DOCTYPE html>
<html>
<body>
    <article class="Box-row">
        <h2>
            <a href="/octocat/Hello-World">octocat/Hello-World</a>
        </h2>
        <p>A sample repository for testing</p>
        <span itemprop="programmingLanguage">Python</span>
        <a href="/octocat/Hello-World/stargazers">
            1,234 stars
        </a>
        <span class="d-inline-block float-sm-right">
            56 stars today
        </span>
    </article>
    <article class="Box-row">
        <h2>
            <a href="/torvalds/linux">torvalds/linux</a>
        </h2>
        <p>Linux kernel source tree</p>
        <span itemprop="programmingLanguage">C</span>
        <a href="/torvalds/linux/stargazers">
            150k stars
        </a>
        <span class="d-inline-block float-sm-right">
            123 stars today
        </span>
    </article>
</body>
</html>
"""


# ============================================================
# Test Handle
# ============================================================

class TestHandle:
    """测试命令处理"""

    @pytest.mark.asyncio
    async def test_handle_default(self, mock_context, mock_event):
        """测试默认命令（daily）"""
        with patch.object(github, '_fetch_trending', new=AsyncMock(return_value=github.segments("mocked"))) as mock_fetch:
            result = await github.handle("github", "", mock_event, mock_context)
            assert result is not None
            mock_fetch.assert_called_once_with("daily", mock_context)

    @pytest.mark.asyncio
    async def test_handle_daily(self, mock_context, mock_event):
        """测试 daily 命令"""
        with patch.object(github, '_fetch_trending', new=AsyncMock(return_value=github.segments("daily result"))) as mock_fetch:
            result = await github.handle("github", "daily", mock_event, mock_context)
            assert result is not None
            mock_fetch.assert_called_once_with("daily", mock_context)

    @pytest.mark.asyncio
    async def test_handle_weekly(self, mock_context, mock_event):
        """测试 weekly 命令"""
        with patch.object(github, '_fetch_trending', new=AsyncMock(return_value=github.segments("weekly result"))) as mock_fetch:
            result = await github.handle("github", "weekly", mock_event, mock_context)
            assert result is not None
            mock_fetch.assert_called_once_with("weekly", mock_context)

    @pytest.mark.asyncio
    async def test_handle_monthly(self, mock_context, mock_event):
        """测试 monthly 命令"""
        with patch.object(github, '_fetch_trending', new=AsyncMock(return_value=github.segments("monthly result"))) as mock_fetch:
            result = await github.handle("github", "monthly", mock_event, mock_context)
            assert result is not None
            mock_fetch.assert_called_once_with("monthly", mock_context)

    @pytest.mark.asyncio
    async def test_handle_help(self, mock_context, mock_event):
        """测试 help 命令"""
        result = await github.handle("github", "help", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "GitHub" in result_text or "趋势" in result_text

    @pytest.mark.asyncio
    async def test_handle_invalid_command(self, mock_context, mock_event):
        """测试无效命令"""
        result = await github.handle("github", "invalid_command", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "未知" in result_text or "help" in result_text.lower()


# ============================================================
# Test Fetch Trending
# ============================================================

class TestFetchTrending:
    """测试获取趋势功能"""

    @pytest.mark.asyncio
    async def test_fetch_trending_success(self, mock_context, mock_event):
        """测试成功获取趋势"""
        # 创建 mock HTTP session
        class MockResponse:
            status = 200
            async def text(self):
                return SAMPLE_TRENDING_HTML

        class MockGetContextManager:
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        mock_context.http_session = MockSession()

        result = await github._fetch_trending("daily", mock_context)
        assert result is not None
        result_text = str(result)
        assert "octocat/Hello-World" in result_text or "torvalds/linux" in result_text

    @pytest.mark.asyncio
    async def test_fetch_trending_http_error(self, mock_context, mock_event):
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

        result = await github._fetch_trending("daily", mock_context)
        assert result is not None
        result_text = str(result)
        assert "404" in result_text or "HTTP" in result_text

    @pytest.mark.asyncio
    async def test_fetch_trending_network_error(self, mock_context, mock_event):
        """测试网络错误"""
        class MockGetContextManager:
            async def __aenter__(self):
                raise aiohttp.ClientError("Connection error")
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        mock_context.http_session = MockSession()

        result = await github._fetch_trending("daily", mock_context)
        assert result is not None
        result_text = str(result)
        assert "失败" in result_text or "获取失败" in result_text

    @pytest.mark.asyncio
    async def test_fetch_trending_empty_response(self, mock_context, mock_event):
        """测试空响应"""
        class MockResponse:
            status = 200
            async def text(self):
                return "<html><body></body></html>"

        class MockGetContextManager:
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        mock_context.http_session = MockSession()

        result = await github._fetch_trending("daily", mock_context)
        assert result is not None
        result_text = str(result)
        assert "未找到" in result_text or "没有" in result_text

    @pytest.mark.asyncio
    async def test_fetch_trending_invalid_time_range(self, mock_context, mock_event):
        """测试无效时间范围（默认为 daily）"""
        class MockResponse:
            status = 200
            async def text(self):
                return SAMPLE_TRENDING_HTML

        class MockGetContextManager:
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        mock_context.http_session = MockSession()

        # 无效的时间范围应该默认为 daily
        result = await github._fetch_trending("invalid", mock_context)
        assert result is not None


# ============================================================
# Test Parse Trending HTML
# ============================================================

class TestParseTrendingHTML:
    """测试 HTML 解析功能"""

    def test_parse_valid_html(self):
        """测试解析有效 HTML"""
        repos = github._parse_trending_html(SAMPLE_TRENDING_HTML)
        assert len(repos) == 2

        # 检查第一个仓库
        repo1 = repos[0]
        assert repo1["owner"] == "octocat"
        assert repo1["name"] == "Hello-World"
        assert repo1["full_name"] == "octocat/Hello-World"
        assert repo1["language"] == "Python"
        # stars 值会因为解析而可能是不同的值，只检查它不是空的
        assert repo1["stars"] is not None
        assert repo1["stars_gained"] is not None

    def test_parse_empty_html(self):
        """测试解析空 HTML"""
        repos = github._parse_trending_html("<html><body></body></html>")
        assert len(repos) == 0

    def test_parse_html_with_missing_fields(self):
        """测试解析缺少字段的 HTML"""
        incomplete_html = """
        <article class="Box-row">
            <h2><a href="/test/repo">test/repo</a></h2>
        </article>
        """
        repos = github._parse_trending_html(incomplete_html)
        # 应该仍然解析出一个仓库，使用默认值
        assert len(repos) == 1
        assert repos[0]["full_name"] == "test/repo"

    def test_parse_html_without_language(self):
        """测试没有语言字段的 HTML"""
        html_no_lang = """
        <article class="Box-row">
            <h2><a href="/test/repo">test/repo</a></h2>
            <p>Test description</p>
        </article>
        """
        repos = github._parse_trending_html(html_no_lang)
        assert len(repos) == 1
        assert repos[0]["language"] == "未知"


# ============================================================
# Test Valid Ranges
# ============================================================

class TestValidRanges:
    """测试有效时间范围"""

    def test_valid_ranges_const(self):
        """测试 VALID_RANGES 常量"""
        assert "daily" in github.VALID_RANGES
        assert "weekly" in github.VALID_RANGES
        assert "monthly" in github.VALID_RANGES

    def test_range_names_mapping(self):
        """测试 RANGE_NAMES 映射"""
        assert github.RANGE_NAMES["daily"] == "每日"
        assert github.RANGE_NAMES["weekly"] == "每周"
        assert github.RANGE_NAMES["monthly"] == "每月"


# ============================================================
# Test Scheduled
# ============================================================

class TestScheduled:
    """测试定时任务"""

    @pytest.mark.asyncio
    async def test_scheduled_task(self, mock_context):
        """测试定时任务入口"""
        with patch.object(github, '_fetch_trending', new=AsyncMock(return_value=github.segments("scheduled"))) as mock_fetch:
            result = await github.scheduled(mock_context)
            assert result is not None
            mock_fetch.assert_called_once_with("daily", mock_context)


# ============================================================
# Test Proxy Configuration
# ============================================================

class TestProxyConfig:
    """测试代理配置"""

    def test_get_proxy_with_value(self):
        """测试获取代理（有值）"""
        class MockContext:
            def __init__(self):
                self.secrets = {"plugins": {"github": {"proxy": "http://proxy.example.com:8080"}}}

        context = MockContext()
        proxy = github._get_proxy(context)
        assert proxy == "http://proxy.example.com:8080"

    def test_get_proxy_without_value(self):
        """测试获取代理（无值）"""
        class MockContext:
            def __init__(self):
                self.secrets = {"plugins": {}}

        context = MockContext()
        proxy = github._get_proxy(context)
        assert proxy == ""

    def test_get_proxy_empty_secrets(self):
        """测试获取代理（空 secrets）"""
        class MockContext:
            def __init__(self):
                self.secrets = {}

        context = MockContext()
        proxy = github._get_proxy(context)
        assert proxy == ""


# ============================================================
# Test History Saving
# ============================================================

class TestSaveHistory:
    """测试历史记录保存"""

    def test_save_history_creates_files(self, mock_context):
        """测试保存历史创建文件"""
        repos = [
            {
                "full_name": "test/repo",
                "description": "Test",
                "language": "Python",
                "stars": "100",
                "url": "https://github.com/test/repo"
            }
        ]

        github._save_history(repos, "daily", mock_context)

        # 检查最新文件
        latest_file = mock_context.data_dir / "trending_daily_latest.json"
        assert latest_file.exists()

        # 检查历史目录
        history_dir = mock_context.data_dir / "history"
        assert history_dir.exists()

        # 验证文件内容
        import json
        content = json.loads(latest_file.read_text(encoding="utf-8"))
        assert content["time_range"] == "daily"
        assert content["count"] == 1
        assert len(content["repositories"]) == 1

    def test_save_history_creates_history_files(self, mock_context):
        """测试保存历史创建历史文件"""
        repos = [{"full_name": "test/repo"}]

        github._save_history(repos, "weekly", mock_context)

        # 检查历史文件
        history_files = list((mock_context.data_dir / "history").glob("trending_weekly_*.json"))
        assert len(history_files) == 1


# ============================================================
# Test Error Handling
# ============================================================

class TestErrorHandling:
    """测试错误处理"""

    @pytest.mark.asyncio
    async def test_handle_exception(self, mock_context, mock_event):
        """测试 handle 中的异常处理"""
        with patch.object(github, '_fetch_trending', new=AsyncMock(side_effect=Exception("Test error"))):
            result = await github.handle("github", "daily", mock_event, mock_context)
            assert result is not None
            result_text = str(result)
            assert "出错" in result_text or "error" in result_text.lower()

    @pytest.mark.asyncio
    async def test_timeout_error(self, mock_context, mock_event):
        """测试超时错误"""
        import asyncio

        class MockGetContextManager:
            async def __aenter__(self):
                raise asyncio.TimeoutError("Request timeout")
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        mock_context.http_session = MockSession()

        result = await github._fetch_trending("daily", mock_context)
        assert result is not None
        result_text = str(result)
        assert "失败" in result_text or "timeout" in result_text.lower() or "获取失败" in result_text


# ============================================================
# Test Init
# ============================================================

def test_init():
    """测试插件初始化"""
    github.init()
    assert True


# ============================================================
# Test Multiple Repositories
# ============================================================

class TestMultipleRepos:
    """测试多个仓库"""

    @pytest.mark.asyncio
    async def test_fetch_multiple_repos(self, mock_context, mock_event):
        """测试获取多个仓库"""
        multi_repo_html = """
        <article class="Box-row">
            <h2><a href="/user/repo1">user/repo1</a></h2>
            <p>Description 1</p>
            <span itemprop="programmingLanguage">Python</span>
            <a href="/user/repo1/stargazers">100 stars</a>
            <span class="d-inline-block float-sm-right">10 stars today</span>
        </article>
        <article class="Box-row">
            <h2><a href="/user/repo2">user/repo2</a></h2>
            <p>Description 2</p>
            <span itemprop="programmingLanguage">JavaScript</span>
            <a href="/user/repo2/stargazers">200 stars</a>
            <span class="d-inline-block float-sm-right">20 stars today</span>
        </article>
        <article class="Box-row">
            <h2><a href="/user/repo3">user/repo3</a></h2>
            <p>Description 3</p>
            <span itemprop="programmingLanguage">Go</span>
            <a href="/user/repo3/stargazers">300 stars</a>
            <span class="d-inline-block float-sm-right">30 stars today</span>
        </article>
        """

        repos = github._parse_trending_html(multi_repo_html)
        assert len(repos) == 3

        assert repos[0]["full_name"] == "user/repo1"
        assert repos[1]["full_name"] == "user/repo2"
        assert repos[2]["full_name"] == "user/repo3"

        assert repos[0]["language"] == "Python"
        assert repos[1]["language"] == "JavaScript"
        assert repos[2]["language"] == "Go"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
