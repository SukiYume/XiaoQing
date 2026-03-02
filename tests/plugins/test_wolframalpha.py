"""测试wolframalpha插件 - Wolfram|Alpha计算引擎"""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent

import importlib.util
spec = importlib.util.spec_from_file_location("wolframalpha_main", ROOT / "plugins" / "wolframalpha" / "main.py")
wolframalpha = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wolframalpha)


class TestWolframAlphaPlugin:
    """测试wolframalpha插件"""

    @pytest.fixture
    def mock_context(self):
        """模拟插件上下文"""
        context = MagicMock()
        context.secrets = {
            "plugins": {
                "wolframalpha": {
                    "appid": "test_appid_123"
                }
            }
        }
        context.logger = MagicMock()
        context.http_session = MagicMock()

        # 创建成功的HTTP响应mock
        class MockSuccessResponse:
            status = 200
            async def text(self):
                return "42"

        class MockSuccessContextManager:
            async def __aenter__(self):
                return MockSuccessResponse()
            async def __aexit__(self, *args):
                pass

        class MockPostSuccessResponse:
            status = 200
            async def text(self):
                return '<?xml version="1.0"?><queryresult><pod><plaintext>Step 1</plaintext></pod></queryresult>'

        class MockPostSuccessContextManager:
            async def __aenter__(self):
                return MockPostSuccessResponse()
            async def __aexit__(self, *args):
                pass

        class MockHttpSession:
            def get(self, *args, **kwargs):
                return MockSuccessContextManager()
            def post(self, *args, **kwargs):
                return MockPostSuccessContextManager()

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
        wolframalpha.init()
        assert True

    def test_get_appid_valid(self, mock_context):
        """测试获取有效的App ID"""
        appid = wolframalpha._get_appid(mock_context)
        assert appid == "test_appid_123"

    def test_get_appid_missing(self):
        """测试缺失App ID"""
        context = MagicMock()
        context.secrets = {}
        appid = wolframalpha._get_appid(context)
        assert appid == ""

    def test_get_appid_empty_plugin(self):
        """测试空的插件配置"""
        context = MagicMock()
        context.secrets = {"plugins": {}}
        appid = wolframalpha._get_appid(context)
        assert appid == ""

    def test_show_help(self):
        """测试帮助信息"""
        help_text = wolframalpha._show_help()
        assert help_text is not None
        assert "Wolfram" in help_text or "计算" in help_text
        assert "/alpha" in help_text

    @pytest.mark.asyncio
    async def test_handle_help_command(self, mock_context, mock_event):
        """测试帮助命令"""
        result = await wolframalpha.handle("alpha", "help", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "Wolfram" in result_text or "计算" in result_text

    @pytest.mark.asyncio
    async def test_handle_help_chinese(self, mock_context, mock_event):
        """测试中文帮助命令"""
        result = await wolframalpha.handle("alpha", "帮助", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "Wolfram" in result_text or "计算" in result_text

    @pytest.mark.asyncio
    async def test_handle_empty_question(self, mock_context, mock_event):
        """测试空问题"""
        result = await wolframalpha.handle("alpha", "", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "请输入" in result_text or "问题" in result_text

    @pytest.mark.asyncio
    async def test_handle_no_appid(self, mock_event):
        """测试缺少App ID"""
        context = MagicMock()
        context.secrets = {}
        context.logger = MagicMock()
        context.http_session = MagicMock()

        result = await wolframalpha.handle("alpha", "1+1", mock_event, context)
        assert result is not None
        result_text = str(result)
        assert "未配置" in result_text or "appid" in result_text

    @pytest.mark.asyncio
    async def test_handle_simple_calculation(self, mock_context, mock_event):
        """测试简单计算"""
        result = await wolframalpha.handle("alpha", "1+1", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "42" in result_text or "计算" in result_text

    @pytest.mark.asyncio
    async def test_get_answer_success(self, mock_context):
        """测试成功获取答案"""
        result = await wolframalpha._get_answer("2+2", "test_appid", mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_get_answer_no_session(self):
        """测试无HTTP会话"""
        context = MagicMock()
        context.http_session = None
        context.logger = MagicMock()

        result = await wolframalpha._get_answer("test", "appid", context)
        assert result is not None
        result_text = str(result)
        assert "HTTP" in result_text or "会话" in result_text

    @pytest.mark.asyncio
    async def test_get_answer_timeout(self, mock_context):
        """测试查询超时"""
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

        result = await wolframalpha._get_answer("test", "appid", mock_context)
        assert result is not None
        result_text = str(result)
        assert "超时" in result_text or "重试" in result_text

    @pytest.mark.asyncio
    async def test_get_answer_api_error(self, mock_context):
        """测试API错误"""
        class MockErrorResponse:
            status = 400
            async def text(self):
                return "Bad Request"

        class MockErrorContextManager:
            async def __aenter__(self):
                return MockErrorResponse()
            async def __aexit__(self, *args):
                pass

        class MockErrorSession:
            def get(self, *args, **kwargs):
                return MockErrorContextManager()

        mock_context.http_session = MockErrorSession()

        result = await wolframalpha._get_answer("test", "appid", mock_context)
        assert result is not None
        result_text = str(result)
        assert "失败" in result_text or "错误" in result_text

    @pytest.mark.asyncio
    async def test_query_step_success(self, mock_context):
        """测试步骤解答"""
        class MockStepResponse:
            status = 200
            async def text(self):
                return '<?xml version="1.0"?><queryresult><pod><plaintext>Step 1: Do this</plaintext><plaintext>Step 2: Do that</plaintext></pod></queryresult>'

        class MockStepContextManager:
            async def __aenter__(self):
                return MockStepResponse()
            async def __aexit__(self, *args):
                pass

        class MockStepSession:
            def post(self, *args, **kwargs):
                return MockStepContextManager()

        result = await wolframalpha._query_step("integrate x^2", "appid", MockStepSession())
        assert result is not None
        assert "Step" in result

    @pytest.mark.asyncio
    async def test_query_step_no_result(self, mock_context):
        """测试步骤解答无结果"""
        class MockEmptyResponse:
            status = 200
            async def text(self):
                return '<?xml version="1.0"?><queryresult></queryresult>'

        class MockEmptyContextManager:
            async def __aenter__(self):
                return MockEmptyResponse()
            async def __aexit__(self, *args):
                pass

        class MockEmptySession:
            def post(self, *args, **kwargs):
                return MockEmptyContextManager()

        result = await wolframalpha._query_step("test", "appid", MockEmptySession())
        assert result == "未找到步骤解答"

    @pytest.mark.asyncio
    async def test_query_step_error(self, mock_context):
        """测试步骤解答API错误"""
        class MockStepErrorResponse:
            status = 500
            async def text(self):
                return "Internal Server Error"

        class MockStepErrorContextManager:
            async def __aenter__(self):
                return MockStepErrorResponse()
            async def __aexit__(self, *args):
                pass

        class MockStepErrorSession:
            def post(self, *args, **kwargs):
                return MockStepErrorContextManager()

        with pytest.raises(ValueError):
            await wolframalpha._query_step("test", "appid", MockStepErrorSession())

    @pytest.mark.asyncio
    async def test_query_complete_success(self, mock_context):
        """测试完整结果查询"""
        class MockCompleteResponse:
            status = 200
            async def json(self):
                return {
                    "queryresult": {
                        "pods": [
                            {
                                "subpods": [
                                    {"plaintext": "42"}
                                ]
                            }
                        ]
                    }
                }

        class MockCompleteContextManager:
            async def __aenter__(self):
                return MockCompleteResponse()
            async def __aexit__(self, *args):
                pass

        class MockCompleteSession:
            def post(self, *args, **kwargs):
                return MockCompleteContextManager()

        result = await wolframalpha._query_complete("1+1", "appid", MockCompleteSession())
        assert result == "42"

    @pytest.mark.asyncio
    async def test_query_complete_no_result(self, mock_context):
        """测试完整结果查询无结果"""
        class MockNoResultResponse:
            status = 200
            async def json(self):
                return {
                    "queryresult": {
                        "pods": [
                            {
                                "subpods": [
                                    {"plaintext": ""}
                                ]
                            }
                        ]
                    }
                }

        class MockNoResultContextManager:
            async def __aenter__(self):
                return MockNoResultResponse()
            async def __aexit__(self, *args):
                pass

        class MockNoResultSession:
            def post(self, *args, **kwargs):
                return MockNoResultContextManager()

        result = await wolframalpha._query_complete("test", "appid", MockNoResultSession())
        assert result == "未找到结果"

    @pytest.mark.asyncio
    async def test_query_complete_parse_error(self, mock_context):
        """测试完整结果解析错误"""
        class MockParseErrorResponse:
            status = 200
            async def json(self):
                return {"queryresult": {}}

        class MockParseErrorContextManager:
            async def __aenter__(self):
                return MockParseErrorResponse()
            async def __aexit__(self, *args):
                pass

        class MockParseErrorSession:
            def post(self, *args, **kwargs):
                return MockParseErrorContextManager()

        result = await wolframalpha._query_complete("test", "appid", MockParseErrorSession())
        assert result == "结果解析失败"

    @pytest.mark.asyncio
    async def test_handle_step_suffix(self, mock_context, mock_event):
        """测试step后缀"""
        class MockStepSuffixResponse:
            status = 200
            async def text(self):
                return '<?xml version="1.0"?><queryresult><pod><plaintext>Step details</plaintext></pod></queryresult>'

        class MockStepSuffixContextManager:
            async def __aenter__(self):
                return MockStepSuffixResponse()
            async def __aexit__(self, *args):
                pass

        class MockStepSuffixSession:
            def post(self, *args, **kwargs):
                return MockStepSuffixContextManager()

        mock_context.http_session = MockStepSuffixSession()

        result = await wolframalpha.handle("alpha", "integrate x^2 step", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "步骤" in result_text

    @pytest.mark.asyncio
    async def test_handle_cp_suffix(self, mock_context, mock_event):
        """测试cp后缀"""
        class MockCpSuffixResponse:
            status = 200
            async def json(self):
                return {
                    "queryresult": {
                        "pods": [
                            {
                                "subpods": [
                                    {"plaintext": "Complete result"}
                                ]
                            }
                        ]
                    }
                }

        class MockCpSuffixContextManager:
            async def __aenter__(self):
                return MockCpSuffixResponse()
            async def __aexit__(self, *args):
                pass

        class MockCpSuffixSession:
            def post(self, *args, **kwargs):
                return MockCpSuffixContextManager()

        mock_context.http_session = MockCpSuffixSession()

        result = await wolframalpha.handle("alpha", "test cp", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "计算结果" in result_text

    @pytest.mark.asyncio
    async def test_handle_network_error(self, mock_context, mock_event):
        """测试网络错误"""
        import aiohttp

        class MockNetworkErrorContextManager:
            async def __aenter__(self):
                raise aiohttp.ClientError("Network error")
            async def __aexit__(self, *args):
                pass

        class MockNetworkErrorSession:
            def get(self, *args, **kwargs):
                return MockNetworkErrorContextManager()

        mock_context.http_session = MockNetworkErrorSession()

        result = await wolframalpha.handle("alpha", "test", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "网络" in result_text or "错误" in result_text

    @pytest.mark.asyncio
    async def test_handle_exception(self, mock_context, mock_event):
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

        result = await wolframalpha.handle("alpha", "test", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "失败" in result_text or "错误" in result_text

    def test_command_triggers(self):
        """测试支持的命令触发词"""
        # plugin.json中定义的触发词: alpha, wolfram, wa, 计算
        # 这里只是确认插件导出了handle函数
        assert hasattr(wolframalpha, 'handle')
        assert callable(wolframalpha.handle)
