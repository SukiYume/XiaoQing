"""
Shell 插件单元测试
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins.shell import config as shell_config
from plugins.shell import main as shell_main

ROOT = Path(__file__).resolve().parent.parent.parent


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

    def test_validate_shell_builtin_rejected(self, mock_context):
        error = shell_main._validate_command("cd /tmp", mock_context)
        assert error is not None
        assert "内建" in error

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

    def test_split_path_with_spaces(self):
        """测试带空格路径仍作为单个参数"""
        result = shell_main._split_command('echo "C:/Users/testuser/Desktop/my file.py"')
        assert result[0] == "echo"
        assert "my file.py" in result[1]

    def test_split_windows_backslash_path_not_mangled(self):
        """Windows 反斜杠路径不应被 shlex 当转义吞掉"""
        result = shell_main._split_command(r"echo C:/Users/testuser\Desktop\a.py")
        if shell_main.sys.platform == "win32":
            assert result == ["echo", r"C:/Users/testuser\Desktop\a.py"]
        else:
            assert result == ["echo", "C:UserstorchDesktopa.py"]

    def test_forward_slash_path_normalized_for_current_platform(self):
        """用户统一输入 / 路径，后端按当前系统规范化"""
        result = shell_main._split_command("echo C:/Users/testuser/Desktop/a.py")
        if shell_main.sys.platform == "win32":
            assert result == ["echo", r"C:/Users/testuser\Desktop\a.py"]
        else:
            assert result == ["echo", "C:/Users/testuser/Desktop/a.py"]

    def test_windows_options_are_not_paths(self):
        """cmd /c copy /Y 中的 /c 和 /Y 不是路径"""
        result = shell_main._split_command("cmd /c copy /Y C:/Users/testuser/a.py C:/Users/testuser/b.py")
        assert result[1] == "/c"
        assert result[3] == "/Y"

    def test_url_is_not_normalized_as_path(self):
        """URL 参数不应被路径规范化破坏"""
        result = shell_main._split_command("curl https://example.com/a/b")
        assert result == ["curl", "https://example.com/a/b"]

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
        assert "echo" in str(result)
        assert "ls" in str(result)

    @pytest.mark.asyncio
    async def test_handle_invalid_command(self, mock_context, mock_event):
        """测试无效命令"""
        result = await shell_main.handle("shell", "invalid_cmd", mock_event, mock_context)
        assert isinstance(result, list)
        assert "拒绝" in str(result)

    @pytest.mark.asyncio
    async def test_handle_list_whitelist_hides_unsupported_builtins(self, mock_context, mock_event):
        result = await shell_main.handle("shell", "list", mock_event, mock_context)
        text = str(result)
        assert "cd" not in text
        assert "copy" not in text


class TestShellExecutionSafety:
    @pytest.mark.asyncio
    async def test_execute_command_starts_in_own_process_group(self):
        captured = {}

        class _Proc:
            returncode = 0

            def __init__(self):
                self.stdout = asyncio.StreamReader()
                self.stderr = asyncio.StreamReader()
                self.stdout.feed_data(b"ok")
                self.stdout.feed_eof()
                self.stderr.feed_eof()

            async def wait(self):
                return 0

        async def fake_create_subprocess_exec(*args, **kwargs):
            captured.update(kwargs)
            return _Proc()

        with patch.object(
            shell_main.asyncio,
            "create_subprocess_exec",
            new=AsyncMock(side_effect=fake_create_subprocess_exec),
        ):
            code, stdout, stderr = await shell_main._execute_command(["echo", "ok"], 1)

        assert code == 0
        assert stdout == "ok"
        assert stderr == ""
        if shell_main.sys.platform == "win32":
            assert captured.get("creationflags", 0) != 0
        else:
            assert captured.get("start_new_session") is True

    @pytest.mark.asyncio
    async def test_execute_command_timeout_terminates_process_tree(self):
        proc = MagicMock()
        proc.returncode = None
        proc.stdout = asyncio.StreamReader()
        proc.stderr = asyncio.StreamReader()

        async def wait_forever():
            await asyncio.Event().wait()

        proc.wait = wait_forever

        async def terminate(_proc):
            proc.returncode = -9
            proc.stdout.feed_eof()
            proc.stderr.feed_eof()

        with (
            patch.object(
                shell_main.asyncio,
                "create_subprocess_exec",
                new=AsyncMock(return_value=proc),
            ),
            patch.object(shell_main, "_terminate_process_tree", new=AsyncMock(side_effect=terminate)) as mock_terminate,
        ):
            code, stdout, stderr = await shell_main._execute_command(["echo", "ok"], 0.01)

        assert code == -1
        assert stdout == ""
        assert "超时" in stderr
        mock_terminate.assert_awaited_once_with(proc)

    @pytest.mark.asyncio
    async def test_execute_command_output_limit_terminates_process_tree(self, monkeypatch):
        proc = MagicMock()
        proc.returncode = None
        proc.stdout = asyncio.StreamReader()
        proc.stderr = asyncio.StreamReader()
        proc.stdout.feed_data(b"x" * (64 * 1024 + 1))

        async def wait_forever():
            await asyncio.Event().wait()

        proc.wait = wait_forever

        async def terminate(_proc):
            proc.returncode = -9
            proc.stdout.feed_eof()
            proc.stderr.feed_eof()

        monkeypatch.setattr(
            shell_main.asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=proc),
        )
        terminate_mock = AsyncMock(side_effect=terminate)
        monkeypatch.setattr(shell_main, "_terminate_process_tree", terminate_mock)

        code, stdout, stderr = await shell_main._execute_command(["echo", "ok"], 1)

        assert code == -2
        assert len(stdout.encode()) <= 64 * 1024
        assert "安全上限" in stderr
        terminate_mock.assert_awaited_once_with(proc)

    @pytest.mark.asyncio
    async def test_execute_command_cancellation_terminates_process_tree(self, monkeypatch):
        proc = MagicMock()
        proc.returncode = None
        proc.stdout = asyncio.StreamReader()
        proc.stderr = asyncio.StreamReader()

        async def wait_forever():
            await asyncio.Event().wait()

        proc.wait = wait_forever

        async def terminate(_proc):
            proc.returncode = -9
            proc.stdout.feed_eof()
            proc.stderr.feed_eof()

        monkeypatch.setattr(
            shell_main.asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=proc),
        )
        terminate_mock = AsyncMock(side_effect=terminate)
        monkeypatch.setattr(shell_main, "_terminate_process_tree", terminate_mock)
        task = asyncio.create_task(shell_main._execute_command(["sleep", "10"], 30))
        await asyncio.sleep(0)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        terminate_mock.assert_awaited_once_with(proc)

# ============================================================
# 智能解码测试
# ============================================================

class TestSmartDecode:
    """智能解码测试"""

    def test_decode_utf8(self):
        """测试 UTF-8 解码"""
        data = b"hello"
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
