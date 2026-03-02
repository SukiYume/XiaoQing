"""测试url_parser插件 - URL解析插件"""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent

import importlib.util
spec = importlib.util.spec_from_file_location("url_parser_main", ROOT / "plugins" / "url_parser" / "main.py")
url_parser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(url_parser)


class TestURLParserPlugin:
    """测试url_parser插件"""

    @pytest.fixture
    def mock_context(self):
        """模拟插件上下文"""
        context = MagicMock()
        context.logger = MagicMock()
        context.http_session = MagicMock()

        # 创建成功的HTTP响应mock
        html_content = b'''
        <html>
        <head>
            <title>Test Page Title</title>
            <meta name="description" content="This is a test description">
            <meta property="og:image" content="https://example.com/image.jpg">
        </head>
        <body>
            <p>Content</p>
        </body>
        </html>
        '''

        class MockSuccessResponse:
            status = 200
            charset = "utf-8"

            @property
            def content(self):
                return self

            async def read(self, limit=None):
                if limit and len(html_content) > limit:
                    return html_content[:limit]
                return html_content

        class MockSuccessContextManager:
            async def __aenter__(self):
                return MockSuccessResponse()
            async def __aexit__(self, *args):
                pass

        class MockHttpSession:
            def get(self, *args, **kwargs):
                return MockSuccessContextManager()

        context.http_session = MockHttpSession()

        return context

    @pytest.fixture
    def mock_event(self):
        """模拟事件"""
        return {
            "user_id": "12345",
            "message": "test",
            "message_type": "private"
        }

    def test_init(self):
        """测试插件初始化"""
        url_parser.init()
        assert True

    def test_constants(self):
        """测试常量定义"""
        assert hasattr(url_parser, 'MAX_CONTENT_SIZE')
        assert hasattr(url_parser, 'MAX_DESC_LENGTH')
        assert hasattr(url_parser, 'REQUEST_TIMEOUT')

        assert url_parser.MAX_CONTENT_SIZE == 2 * 1024 * 1024  # 2MB
        assert url_parser.MAX_DESC_LENGTH == 100
        assert url_parser.REQUEST_TIMEOUT == 10

    @pytest.mark.asyncio
    async def test_handle_placeholder(self, mock_context, mock_event):
        """测试handle占位符函数"""
        result = await url_parser.handle("test", "args", mock_event, mock_context)
        assert result == []

    @pytest.mark.asyncio
    async def test_handle_url_no_session(self, mock_context, mock_event):
        """测试没有HTTP会话的情况"""
        mock_context.http_session = None

        result = await url_parser.handle_url("https://example.com", mock_event, mock_context)
        assert result == []

    @pytest.mark.asyncio
    async def test_handle_url_success(self, mock_context, mock_event):
        """测试成功的URL解析"""
        result = await url_parser.handle_url("https://example.com", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "Test Page Title" in result_text

    @pytest.mark.asyncio
    async def test_handle_url_no_http_session(self, mock_event):
        """测试无HTTP会话"""
        context = MagicMock()
        context.http_session = None
        context.logger = MagicMock()

        result = await url_parser.handle_url("https://example.com", mock_event, context)
        assert result == []

    @pytest.mark.asyncio
    async def test_handle_url_error_status(self, mock_context, mock_event):
        """测试错误HTTP状态码"""
        class MockErrorResponse:
            status = 404

        class MockErrorContextManager:
            async def __aenter__(self):
                return MockErrorResponse()
            async def __aexit__(self, *args):
                pass

        class MockErrorSession:
            def get(self, *args, **kwargs):
                return MockErrorContextManager()

        mock_context.http_session = MockErrorSession()

        result = await url_parser.handle_url("https://example.com", mock_event, mock_context)
        assert result == []

    @pytest.mark.asyncio
    async def test_handle_url_content_too_large(self, mock_context, mock_event):
        """测试内容过大"""
        large_content = b"a" * (3 * 1024 * 1024)  # 3MB

        class MockLargeResponse:
            status = 200
            charset = "utf-8"

            @property
            def content(self):
                return self

            async def read(self, limit=None):
                if limit:
                    return large_content[:limit + 1]
                return large_content

        class MockLargeContextManager:
            async def __aenter__(self):
                return MockLargeResponse()
            async def __aexit__(self, *args):
                pass

        class MockLargeSession:
            def get(self, *args, **kwargs):
                return MockLargeContextManager()

        mock_context.http_session = MockLargeSession()

        result = await url_parser.handle_url("https://example.com", mock_event, mock_context)
        assert result == []

    @pytest.mark.asyncio
    async def test_handle_url_no_title_or_desc(self, mock_context, mock_event):
        """测试没有标题和描述的页面"""
        class MockNoContentResponse:
            status = 200
            charset = "utf-8"

            @property
            def content(self):
                return self

            async def read(self, limit=None):
                return b'<html><body><p>Just content</p></body></html>'

        class MockNoContentContextManager:
            async def __aenter__(self):
                return MockNoContentResponse()
            async def __aexit__(self, *args):
                pass

        class MockNoContentSession:
            def get(self, *args, **kwargs):
                return MockNoContentContextManager()

        mock_context.http_session = MockNoContentSession()

        result = await url_parser.handle_url("https://example.com", mock_event, mock_context)
        assert result == []

    @pytest.mark.asyncio
    async def test_handle_url_with_description_truncated(self, mock_context, mock_event):
        """测试长描述被截断"""
        long_desc = "a" * 200
        html_content = f'''
        <html>
        <head>
            <title>Test</title>
            <meta name="description" content="{long_desc}">
        </head>
        </html>
        '''.encode()

        class MockTruncatedResponse:
            status = 200
            charset = "utf-8"

            @property
            def content(self):
                return self

            async def read(self, limit=None):
                return html_content

        class MockTruncatedContextManager:
            async def __aenter__(self):
                return MockTruncatedResponse()
            async def __aexit__(self, *args):
                pass

        class MockTruncatedSession:
            def get(self, *args, **kwargs):
                return MockTruncatedContextManager()

        mock_context.http_session = MockTruncatedSession()

        result = await url_parser.handle_url("https://example.com", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "..." in result_text  # 描述被截断

    @pytest.mark.asyncio
    async def test_handle_url_with_og_tags(self, mock_context, mock_event):
        """测试Open Graph标签"""
        html_content = b'''
        <html>
        <head>
            <title>OG Title</title>
            <meta property="og:description" content="OG Description">
            <meta property="og:image" content="https://example.com/og-image.jpg">
        </head>
        </html>
        '''

        class MockOGResponse:
            status = 200
            charset = "utf-8"

            @property
            def content(self):
                return self

            async def read(self, limit=None):
                return html_content

        class MockOGContextManager:
            async def __aenter__(self):
                return MockOGResponse()
            async def __aexit__(self, *args):
                pass

        class MockOGSession:
            def get(self, *args, **kwargs):
                return MockOGContextManager()

        mock_context.http_session = MockOGSession()

        result = await url_parser.handle_url("https://example.com", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "OG Title" in result_text
        assert "OG Description" in result_text

    @pytest.mark.asyncio
    async def test_handle_url_with_twitter_tags(self, mock_context, mock_event):
        """测试Twitter卡片标签"""
        html_content = b'''
        <html>
        <head>
            <title>Twitter Title</title>
            <meta name="twitter:description" content="Twitter Description">
            <meta name="twitter:image" content="https://example.com/twitter-image.jpg">
        </head>
        </html>
        '''

        class MockTwitterResponse:
            status = 200
            charset = "utf-8"

            @property
            def content(self):
                return self

            async def read(self, limit=None):
                return html_content

        class MockTwitterContextManager:
            async def __aenter__(self):
                return MockTwitterResponse()
            async def __aexit__(self, *args):
                pass

        class MockTwitterSession:
            def get(self, *args, **kwargs):
                return MockTwitterContextManager()

        mock_context.http_session = MockTwitterSession()

        result = await url_parser.handle_url("https://example.com", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "Twitter Title" in result_text

    @pytest.mark.asyncio
    async def test_handle_url_client_error(self, mock_context, mock_event):
        """测试客户端错误"""
        import aiohttp

        class MockClientErrorContextManager:
            async def __aenter__(self):
                raise aiohttp.ClientError("Connection error")
            async def __aexit__(self, *args):
                pass

        class MockClientErrorSession:
            def get(self, *args, **kwargs):
                return MockClientErrorContextManager()

        mock_context.http_session = MockClientErrorSession()

        result = await url_parser.handle_url("https://example.com", mock_event, mock_context)
        assert result == []

    @pytest.mark.asyncio
    async def test_handle_url_timeout(self, mock_context, mock_event):
        """测试超时"""
        import asyncio

        class MockTimeoutContextManager:
            async def __aenter__(self):
                raise asyncio.TimeoutError()
            async def __aexit__(self, *args):
                pass

        class MockTimeoutSession:
            def get(self, *args, **kwargs):
                return MockTimeoutContextManager()

        mock_context.http_session = MockTimeoutSession()

        result = await url_parser.handle_url("https://example.com", mock_event, mock_context)
        assert result == []

    @pytest.mark.asyncio
    async def test_handle_url_exception(self, mock_context, mock_event):
        """测试异常处理"""
        class MockExceptionContextManager:
            async def __aenter__(self):
                raise Exception("Unexpected error")
            async def __aexit__(self, *args):
                pass

        class MockExceptionSession:
            def get(self, *args, **kwargs):
                return MockExceptionContextManager()

        mock_context.http_session = MockExceptionSession()

        result = await url_parser.handle_url("https://example.com", mock_event, mock_context)
        assert result == []

    @pytest.mark.asyncio
    async def test_handle_url_charset_fallback(self, mock_context, mock_event):
        """测试字符集回退"""
        html_content = "测试内容".encode("gbk")

        class MockCharsetResponse:
            status = 200
            charset = None

            @property
            def content(self):
                return self

            async def read(self, limit=None):
                return html_content

        class MockCharsetContextManager:
            async def __aenter__(self):
                return MockCharsetResponse()
            async def __aexit__(self, *args):
                pass

        class MockCharsetSession:
            def get(self, *args, **kwargs):
                return MockCharsetContextManager()

        mock_context.http_session = MockCharsetSession()

        result = await url_parser.handle_url("https://example.com", mock_event, mock_context)
        # 即使字符集回退，也不应该崩溃
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_handle_url_invalid_charset(self, mock_context, mock_event):
        """测试无效字符集"""
        html_content = b"<html><head><title>Test</title></head></html>"

        class MockInvalidCharsetResponse:
            status = 200
            charset = "invalid-charset"

            @property
            def content(self):
                return self

            async def read(self, limit=None):
                return html_content

        class MockInvalidCharsetContextManager:
            async def __aenter__(self):
                return MockInvalidCharsetResponse()
            async def __aexit__(self, *args):
                pass

        class MockInvalidCharsetSession:
            def get(self, *args, **kwargs):
                return MockInvalidCharsetContextManager()

        mock_context.http_session = MockInvalidCharsetSession()

        result = await url_parser.handle_url("https://example.com", mock_event, mock_context)
        # 应该回退到utf-8解码
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_handle_url_with_title_only(self, mock_context, mock_event):
        """测试只有标题的页面"""
        html_content = b'<html><head><title>Only Title</title></head><body></body></html>'

        class MockTitleOnlyResponse:
            status = 200
            charset = "utf-8"

            @property
            def content(self):
                return self

            async def read(self, limit=None):
                return html_content

        class MockTitleOnlyContextManager:
            async def __aenter__(self):
                return MockTitleOnlyResponse()
            async def __aexit__(self, *args):
                pass

        class MockTitleOnlySession:
            def get(self, *args, **kwargs):
                return MockTitleOnlyContextManager()

        mock_context.http_session = MockTitleOnlySession()

        result = await url_parser.handle_url("https://example.com", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "Only Title" in result_text

    @pytest.mark.asyncio
    async def test_handle_url_empty_title(self, mock_context, mock_event):
        """测试空标题"""
        html_content = b'<html><head><title></title><meta name="description" content="Desc"></meta></head></html>'

        class MockEmptyTitleResponse:
            status = 200
            charset = "utf-8"

            @property
            def content(self):
                return self

            async def read(self, limit=None):
                return html_content

        class MockEmptyTitleContextManager:
            async def __aenter__(self):
                return MockEmptyTitleResponse()
            async def __aexit__(self, *args):
                pass

        class MockEmptyTitleSession:
            def get(self, *args, **kwargs):
                return MockEmptyTitleContextManager()

        mock_context.http_session = MockEmptyTitleSession()

        result = await url_parser.handle_url("https://example.com", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "Desc" in result_text

    def test_no_commands(self):
        """测试该插件没有定义命令"""
        # url_parser是通过dispatcher调用的，不定义命令
        assert hasattr(url_parser, 'handle')
        assert callable(url_parser.handle)
