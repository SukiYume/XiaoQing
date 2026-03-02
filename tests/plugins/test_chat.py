"""测试chat插件 - AI对话助手"""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent

import importlib.util
spec = importlib.util.spec_from_file_location("chat_main", ROOT / "plugins" / "chat" / "main.py")
chat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chat)


class TestChatPlugin:
    """测试chat插件"""

    @pytest.fixture
    def mock_context(self, tmp_path):
        """模拟插件上下文"""
        context = MagicMock()
        context.secrets = {
            "plugins": {
                "chat": {
                    "token": "test_token_123",
                    "bot_id": "test_bot_456",
                    "user": "test_user",
                    "stream": False
                }
            }
        }
        context.logger = MagicMock()
        context.plugin_dir = ROOT / "plugins" / "chat"
        context.data_dir = tmp_path / "data"

        # 创建成功的HTTP响应mock
        class MockSuccessResponse:
            status = 200
            async def text(self):
                return "{}"
            async def json(self):
                return {
                    "messages": [
                        {"type": "answer", "content": "测试回答"}
                    ]
                }

        class MockSuccessContextManager:
            async def __aenter__(self):
                return MockSuccessResponse()
            async def __aexit__(self, *args):
                pass

        class MockHttpSession:
            def post(self, *args, **kwargs):
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
        chat.init()
        assert True

    def test_get_config_valid(self, mock_context):
        """测试获取有效配置"""
        config = chat.get_config(mock_context)
        assert config is not None
        assert config.get("token") == "test_token_123"
        assert config.get("bot_id") == "test_bot_456"

    def test_get_config_empty(self):
        """测试空配置"""
        context = MagicMock()
        context.secrets = {}
        context.logger = MagicMock()

        config = chat.get_config(context)
        assert config == {}

    def test_validate_config_valid(self):
        """测试有效配置验证"""
        config = {
            "token": "test_token",
            "bot_id": "test_bot"
        }
        is_valid, error = chat.validate_config(config)
        assert is_valid is True
        assert error is None

    def test_validate_config_empty(self):
        """测试空配置验证"""
        config = {}
        is_valid, error = chat.validate_config(config)
        assert is_valid is False
        assert "配置为空" in error

    def test_validate_config_no_token(self):
        """测试缺少token"""
        config = {"bot_id": "test_bot"}
        is_valid, error = chat.validate_config(config)
        assert is_valid is False
        assert "token" in error

    def test_validate_config_no_bot_id(self):
        """测试缺少bot_id"""
        config = {"token": "test_token"}
        is_valid, error = chat.validate_config(config)
        assert is_valid is False
        assert "bot_id" in error

    def test_extract_answer_valid(self, mock_context):
        """测试提取有效答案"""
        data = {
            "messages": [
                {"type": "answer", "content": "这是答案"}
            ]
        }
        answer = chat.extract_answer(data, mock_context)
        assert answer == "这是答案"

    def test_extract_answer_multiple_messages(self, mock_context):
        """测试从多条消息中提取答案"""
        data = {
            "messages": [
                {"type": "question", "content": "问题"},
                {"type": "answer", "content": "答案"},
                {"type": "other", "content": "其他"}
            ]
        }
        answer = chat.extract_answer(data, mock_context)
        assert answer == "答案"

    def test_extract_answer_no_answer(self, mock_context):
        """测试没有答案的情况"""
        data = {
            "messages": [
                {"type": "question", "content": "问题"}
            ]
        }
        answer = chat.extract_answer(data, mock_context)
        assert answer is None

    def test_extract_answer_invalid_type(self, mock_context):
        """测试无效响应类型"""
        answer = chat.extract_answer("not a dict", mock_context)
        assert answer is None

    def test_extract_answer_invalid_messages(self, mock_context):
        """测试无效messages字段"""
        data = {
            "messages": "not a list"
        }
        answer = chat.extract_answer(data, mock_context)
        assert answer is None

    def test_extract_answer_empty_content(self, mock_context):
        """测试空答案内容"""
        data = {
            "messages": [
                {"type": "answer", "content": "   "}
            ]
        }
        answer = chat.extract_answer(data, mock_context)
        assert answer is None

    @pytest.mark.asyncio
    async def test_call_coze_api_success(self, mock_context):
        """测试成功的API调用"""
        query = "测试问题"
        config = {
            "token": "test_token",
            "bot_id": "test_bot",
            "user": "test_user"
        }

        result = await chat.call_coze_api(query, config, mock_context)
        assert result is not None
        assert "messages" in result

    @pytest.mark.asyncio
    async def test_call_coze_api_with_proxy(self, mock_context):
        """测试带代理的API调用"""
        query = "测试问题"
        config = {
            "token": "test_token",
            "bot_id": "test_bot",
            "proxy": "http://proxy.example.com"
        }

        result = await chat.call_coze_api(query, config, mock_context)
        assert result is not None

    @pytest.mark.asyncio
    async def test_call_coze_api_error_response(self, mock_context):
        """测试API错误响应"""
        class MockErrorResponse:
            status = 401
            async def text(self):
                return "Unauthorized"

        class MockErrorContextManager:
            async def __aenter__(self):
                return MockErrorResponse()
            async def __aexit__(self, *args):
                pass

        class MockErrorSession:
            def post(self, *args, **kwargs):
                return MockErrorContextManager()

        mock_context.http_session = MockErrorSession()

        query = "测试问题"
        config = {
            "token": "invalid_token",
            "bot_id": "test_bot"
        }

        result = await chat.call_coze_api(query, config, mock_context)
        assert result is None

    @pytest.mark.asyncio
    async def test_call_coze_api_timeout(self, mock_context):
        """测试API超时"""
        import asyncio

        class MockTimeoutContextManager:
            async def __aenter__(self):
                raise asyncio.TimeoutError()
            async def __aexit__(self, *args):
                pass

        class MockTimeoutSession:
            def post(self, *args, **kwargs):
                return MockTimeoutContextManager()

        mock_context.http_session = MockTimeoutSession()

        query = "测试问题"
        config = {
            "token": "test_token",
            "bot_id": "test_bot"
        }

        result = await chat.call_coze_api(query, config, mock_context)
        assert result is None

    @pytest.mark.asyncio
    async def test_call_coze_api_exception(self, mock_context):
        """测试API异常"""
        class MockExceptionContextManager:
            async def __aenter__(self):
                raise Exception("Network error")
            async def __aexit__(self, *args):
                pass

        class MockExceptionSession:
            def post(self, *args, **kwargs):
                return MockExceptionContextManager()

        mock_context.http_session = MockExceptionSession()

        query = "测试问题"
        config = {
            "token": "test_token",
            "bot_id": "test_bot"
        }

        result = await chat.call_coze_api(query, config, mock_context)
        assert result is None

    @pytest.mark.asyncio
    async def test_handle_help_command(self, mock_context, mock_event):
        """测试帮助命令"""
        result = await chat.handle("chat", "help", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "AI" in result_text or "对话" in result_text

    @pytest.mark.asyncio
    async def test_handle_empty_query(self, mock_context, mock_event):
        """测试空查询"""
        result = await chat.handle("chat", "", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "请输入" in result_text or "用法" in result_text

    @pytest.mark.asyncio
    async def test_handle_invalid_config(self, mock_context, mock_event):
        """测试无效配置"""
        mock_context.secrets = {}
        result = await chat.handle("chat", "测试问题", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "配置错误" in result_text or "配置" in result_text

    @pytest.mark.asyncio
    async def test_handle_query_too_long(self, mock_context, mock_event):
        """测试查询过长"""
        long_query = "a" * 2500
        result = await chat.handle("chat", long_query, mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "过长" in result_text or "字符" in result_text

    @pytest.mark.asyncio
    async def test_handle_success(self, mock_context, mock_event):
        """测试成功的对话处理"""
        result = await chat.handle("chat", "你好", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_handle_gpt_alias(self, mock_context, mock_event):
        """测试gpt命令别名"""
        result = await chat.handle("gpt", "测试", mock_event, mock_context)
        assert result is not None

    @pytest.mark.asyncio
    async def test_handle_ai_alias(self, mock_context, mock_event):
        """测试ai命令别名"""
        result = await chat.handle("ai", "测试", mock_event, mock_context)
        assert result is not None

    def test_show_help(self):
        """测试帮助信息显示"""
        help_text = chat._show_help()
        assert help_text is not None
        assert "AI" in help_text or "对话" in help_text
        assert "/chat" in help_text

    def test_constants(self):
        """测试常量定义"""
        assert hasattr(chat, 'COZE_API_URL')
        assert hasattr(chat, 'DEFAULT_USER_ID')
        assert hasattr(chat, 'REQUEST_TIMEOUT')
        assert hasattr(chat, 'MAX_QUERY_LENGTH')

        assert chat.COZE_API_URL == "https://api.coze.com/open_api/v2/chat"
        assert chat.DEFAULT_USER_ID == "123223"
        assert chat.REQUEST_TIMEOUT == 30
        assert chat.MAX_QUERY_LENGTH == 2000
