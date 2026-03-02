"""
Shell 插件单元测试
"""

import asyncio
import pytest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, AsyncMock, patch
import sys
import importlib.util
import types

ROOT = Path(__file__).resolve().parent.parent.parent

# 动态加载 shell 插件配置模块
spec_config = importlib.util.spec_from_file_location("shell_config", ROOT / "plugins" / "shell" / "config.py")
shell_config = importlib.util.module_from_spec(spec_config)
spec_config.loader.exec_module(shell_config)

# 将配置模块添加到 sys.modules 以便导入
sys.modules["shell_config"] = shell_config

# 读取 shell main.py 源代码并修改相对导入
with open(ROOT / "plugins" / "shell" / "main.py", "r", encoding="utf-8") as f:
    main_source = f.read()

# 替换相对导入为绝对导入
main_source = main_source.replace("from .config import", "from shell_config import")
main_source = main_source.replace("from __future__ import annotations\n", "")

# 动态执行修改后的代码
shell_main = types.ModuleType("shell_main")
exec(main_source, shell_main.__dict__)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def mock_context():
    """模拟上下文"""
    context = MagicMock()
    context.secrets = {
        "plugins": {
            "shell": {
                "whitelist": ["ls", "pwd", "echo"],
                "timeout": 30,
            }
        }
    }
    context.logger = MagicMock()
    return context


@pytest.fixture
def mock_event():
    """模拟事件"""
    return {
        "user_id": 12345,
        "message_type": "group",
        "group_id": 67890,
    }

# ============================================================
# 配置测试
# ============================================================

class TestShellConfig:
    """Shell 配置测试"""

    def test_default_whitelist_not_empty(self):
        """测试默认白名单不为空"""
        assert len(shell_config.DEFAULT_WHITELIST) > 0

    def test_dangerous_patterns_defined(self):
        """测试危险模式已定义"""
        assert len(shell_config.DANGEROUS_PATTERNS) > 0
        # 应该包含常见的命令注入模式
        assert any("&&" in p or "||" in p or ";" in p for p in shell_config.DANGEROUS_PATTERNS)

    def test_default_timeout_positive(self):
        """测试默认超时为正数"""
        assert shell_config.DEFAULT_TIMEOUT > 0

    def test_max_output_length_positive(self):
        """测试最大输出长度为正数"""
        assert shell_config.MAX_OUTPUT_LENGTH > 0

# ============================================================
# 命令验证测试
# ============================================================

class TestCommandValidation:
    """命令验证测试"""

    def test_validate_empty_command(self, mock_context):
        """测试空命令验证"""
        error = shell_main._validate_command("", mock_context)
        assert error is not None
        assert "不能为空" in error

    def test_validate_whitelisted_command(self, mock_context):
        """测试白名单命令验证通过"""
        error = shell_main._validate_command("ls -la", mock_context)
        assert error is None

    def test_validate_non_whitelisted_command(self, mock_context):
        """测试非白名单命令验证失败"""
        error = shell_main._validate_command("rm -rf /", mock_context)
        assert error is not None

    def test_validate_dangerous_pattern(self, mock_context):
        """测试危险模式被检测"""
        error = shell_main._validate_command("ls && rm file", mock_context)
        assert error is not None
        assert "危险" in error

# ============================================================
# 命令拆分测试
# ============================================================

class TestCommandSplit:
    """命令拆分测试"""

    def test_split_simple_command(self):
        """测试简单命令拆分"""
        result = shell_main._split_command("ls -la")
        assert result == ["ls", "-la"]

    def test_split_with_quotes(self):
        """测试带引号的命令拆分"""
        result = shell_main._split_command('echo "hello world"')
        assert result == ["echo", "hello world"]

    def test_extract_command_name(self):
        """测试提取命令名"""
        result = shell_main._extract_command("/usr/bin/ls -la")
        assert result == "ls"

    def test_extract_command_simple(self):
        """测试简单命令名提取"""
        result = shell_main._extract_command("ls -la")
        assert result == "ls"

# ============================================================
# 输出截断测试
# ============================================================

class TestOutputTruncate:
    """输出截断测试"""

    def test_truncate_short_text(self):
        """测试短文本不截断"""
        text = "short"
        result = shell_main._truncate(text, max_len=100)
        assert result == text

    def test_truncate_long_text(self):
        """测试长文本截断"""
        text = "a" * 5000
        result = shell_main._truncate(text, max_len=1000)
        assert len(result) < len(text)
        assert "省略" in result

    def test_truncate_uses_default_max(self):
        """测试使用默认最大长度"""
        text = "a" * 10000
        result = shell_main._truncate(text)
        assert len(result) <= shell_config.MAX_OUTPUT_LENGTH + 100  # 加上省略信息

# ============================================================
# 主处理函数测试
# ============================================================

class TestShellHandle:
    """Shell 主处理函数测试"""

    @pytest.mark.asyncio
    async def test_handle_help(self, mock_context, mock_event):
        """测试帮助命令"""
        result = await shell_main.handle("shell", "help", mock_event, mock_context)
        assert isinstance(result, list)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_handle_list_whitelist(self, mock_context, mock_event):
        """测试列出白名单"""
        result = await shell_main.handle("shell", "list", mock_event, mock_context)
        assert isinstance(result, list)
        assert "允许" in str(result) or "白名单" in str(result)

    @pytest.mark.asyncio
    async def test_handle_invalid_command(self, mock_context, mock_event):
        """测试无效命令"""
        result = await shell_main.handle("shell", "invalid_cmd", mock_event, mock_context)
        assert isinstance(result, list)
        assert "拒绝" in str(result)

# ============================================================
# 智能解码测试
# ============================================================

class TestSmartDecode:
    """智能解码测试"""

    def test_decode_utf8(self):
        """测试 UTF-8 解码"""
        data = "hello".encode("utf-8")
        result = shell_main._smart_decode(data)
        assert result == "hello"

    def test_decode_gbk(self):
        """测试 GBK 解码"""
        data = "你好".encode("gbk")
        result = shell_main._smart_decode(data)
        assert result == "你好"

    def test_decode_empty(self):
        """测试空字节解码"""
        result = shell_main._smart_decode(b"")
        assert result == ""

# ============================================================
# 运行测试
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
