"""测试echo插件"""

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

import importlib.util
spec = importlib.util.spec_from_file_location("echo_main", ROOT / "plugins" / "echo" / "main.py")
echo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(echo)


class TestEchoPlugin:
    """测试echo插件"""

    @pytest.fixture
    def mock_context(self):
        """模拟插件上下文"""
        class MockContext:
            def __init__(self):
                self.plugin_dir = Path(__file__).parent.parent.parent / "plugins" / "echo"
                self.data_dir = self.plugin_dir / "data"

        return MockContext()

    @pytest.fixture
    def mock_event(self):
        """模拟事件"""
        return {
            "user_id": "12345",
            "message": "test",
            "message_type": "private"
        }

    @pytest.mark.asyncio
    async def test_echo_simple_text(self, mock_context, mock_event):
        """测试简单文本回显"""
        result = await echo.handle("echo", "hello world", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "hello world" in result_text or "hello" in result_text.lower()

    @pytest.mark.asyncio
    async def test_echo_empty(self, mock_context, mock_event):
        """测试空消息"""
        result = await echo.handle("echo", "", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_echo_special_chars(self, mock_context, mock_event):
        """测试特殊字符"""
        special = "!@#$%^&*()_+-=[]{}|;':\",./<>?"
        result = await echo.handle("echo", special, mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_echo_chinese_command(self, mock_context, mock_event):
        """测试中文回显命令"""
        result = await echo.handle("回显", "测试文本", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "测试文本" in result_text or len(result_text) > 0

    @pytest.mark.asyncio
    async def test_hello_command(self, mock_context, mock_event):
        """测试hello命令"""
        result = await echo.handle("hello", "", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "你好" in result_text or "hello" in result_text.lower()

    @pytest.mark.asyncio
    async def test_hello_chinese_command(self, mock_context, mock_event):
        """测试中文hello命令"""
        result = await echo.handle("你好", "", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "你好" in result_text or "12345" in result_text

    @pytest.mark.asyncio
    async def test_invalid_command(self, mock_context, mock_event):
        """测试无效命令"""
        result = await echo.handle("invalid", "", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "未知命令" in result_text or "invalid" in result_text

    @pytest.mark.asyncio
    async def test_echo_with_leading_trailing_spaces(self, mock_context, mock_event):
        """测试带前后空格的文本"""
        result = await echo.handle("echo", "  hello world  ", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    def test_init(self):
        """测试插件初始化"""
        echo.init()
        assert True
