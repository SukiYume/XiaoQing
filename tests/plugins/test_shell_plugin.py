"""Shell 插件的配置、解析、执行与资源回收测试。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins.shell import config as shell_config
from plugins.shell import main as shell_main
from tests.helpers.assertions import text_segments_text
from tests.helpers.settings_snapshot import with_settings_reader

ROOT = Path(__file__).resolve().parent.parent.parent


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
    return with_settings_reader(context)


@pytest.fixture
def mock_event():
    """模拟事件"""
    return {
        "user_id": 12345,
        "message_type": "group",
        "group_id": 67890,
    }


@pytest.fixture
def pending_process():
    """在异步测试的事件循环内创建持续运行的假进程。"""

    def create():
        proc = MagicMock()
        proc.returncode = None
        proc.stdout = asyncio.StreamReader()
        proc.stderr = asyncio.StreamReader()

        async def wait_forever():
            await asyncio.Event().wait()

        proc.wait = wait_forever
        return proc

    return create


def _finish_pending_process(proc, returncode: int = -9) -> None:
    proc.returncode = returncode
    proc.stdout.feed_eof()
    proc.stderr.feed_eof()


class TestShellConfig:
    """Shell 配置测试"""

    def test_frozen_snapshot_config_accepts_tuple_whitelist(self, mock_context):
        from core.config import ConfigSnapshot

        mock_context.secrets = ConfigSnapshot(
            config={},
            secrets={
                "plugins": {
                    "shell": {
                        "whitelist": ["echo", "pwd"],
                        "whitelist_mode": "replace",
                        "timeout": 17,
                    }
                }
            },
        ).secrets

        config = shell_main._get_config(mock_context)
        assert not isinstance(config, dict)
        assert config["whitelist"] == ("echo", "pwd")
        assert shell_main._get_whitelist(mock_context) == {"echo", "pwd"}
        assert shell_main._get_timeout(mock_context) == 17

    def test_default_whitelist_not_empty(self):
        """测试默认白名单不为空"""
        assert len(shell_config.DEFAULT_WHITELIST) > 0
        assert shell_config.DEFAULT_WHITELIST.isdisjoint(shell_config.UNSUPPORTED_SHELL_BUILTINS)

    def test_explicit_empty_replace_list_fails_closed(self, mock_context):
        mock_context.secrets["plugins"]["shell"] = {
            "whitelist": [],
            "whitelist_mode": "replace",
        }

        assert shell_main._get_whitelist(mock_context) == set()

    def test_invalid_whitelist_is_not_split_into_characters(self, mock_context):
        mock_context.secrets["plugins"]["shell"] = {
            "whitelist": "python",
            "whitelist_mode": "replace",
        }

        assert shell_main._get_whitelist(mock_context) == set()

    def test_extend_normalizes_entries_and_filters_shell_builtins(self, mock_context):
        mock_context.secrets["plugins"]["shell"] = {
            "whitelist": [" Custom-Tool ", "CD", None],
            "whitelist_mode": "extend",
        }

        whitelist = shell_main._get_whitelist(mock_context)
        assert "custom-tool" in whitelist
        assert "ls" in whitelist
        assert "cd" not in whitelist

    @pytest.mark.parametrize("value", [None, True, 0, -1, "bad", float("nan"), float("inf")])
    def test_invalid_timeout_falls_back_to_default(self, mock_context, value):
        mock_context.secrets["plugins"]["shell"]["timeout"] = value

        assert shell_main._get_timeout(mock_context) == shell_config.DEFAULT_TIMEOUT

    def test_only_boolean_true_disables_whitelist(self, mock_context):
        mock_context.secrets["plugins"]["shell"]["disable_whitelist"] = "false"
        assert shell_main._is_whitelist_disabled(mock_context) is False

        mock_context.secrets["plugins"]["shell"]["disable_whitelist"] = True
        assert shell_main._is_whitelist_disabled(mock_context) is True

    def test_malformed_secret_tree_uses_defaults(self):
        context = with_settings_reader(SimpleNamespace(secrets={"plugins": []}))

        assert shell_main._get_config(context) == {}
        assert shell_main._get_timeout(context) == shell_config.DEFAULT_TIMEOUT
        assert shell_main._get_whitelist(context) == set(shell_config.DEFAULT_WHITELIST)

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

    def test_dangerous_pattern_matching_is_case_insensitive(self, mock_context):
        error = shell_main._validate_command("MKFS /dev/example", mock_context)

        assert error is not None
        assert "危险" in error

    def test_validate_shell_builtin_rejected(self, mock_context):
        error = shell_main._validate_command("cd /tmp", mock_context)
        assert error is not None
        assert "内建" in error


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

    def test_windows_exe_suffix_matches_extensionless_whitelist(self, monkeypatch, mock_context):
        monkeypatch.setattr(shell_main.sys, "platform", "win32")
        mock_context.secrets["plugins"]["shell"]["whitelist"] = ["python"]

        assert shell_main._validate_command("python.exe --version", mock_context) is None

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

    def test_key_value_path_is_normalized(self):
        result = shell_main._split_command("echo output=C:/Users/testuser/a.txt")
        if shell_main.sys.platform == "win32":
            assert result == ["echo", r"output=C:/Users/testuser\a.txt"]
        else:
            assert result == ["echo", "output=C:/Users/testuser/a.txt"]

    def test_windows_quoted_key_value_stays_one_argument(self, monkeypatch):
        monkeypatch.setattr(shell_main.sys, "platform", "win32")

        result = shell_main._split_command('echo output="C:/Program Files/x.txt"')

        assert result == ["echo", r"output=C:\Program Files\x.txt"]

    def test_windows_quoted_non_path_value_stays_one_argument(self, monkeypatch):
        monkeypatch.setattr(shell_main.sys, "platform", "win32")

        result = shell_main._split_command('echo name="hello world"')

        assert result == ["echo", "name=hello world"]


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
        assert len(result) == 1000
        assert "省略" in result

    def test_truncate_uses_default_max(self):
        """测试使用默认最大长度"""
        text = "a" * 10000
        result = shell_main._truncate(text)
        assert len(result) == shell_config.MAX_OUTPUT_LENGTH

    @pytest.mark.parametrize("max_len", [-1, 0, 1, 5, 20])
    def test_truncate_honors_small_and_non_positive_limits(self, max_len):
        result = shell_main._truncate("abcdefghijklmnopqrstuvwxyz", max_len=max_len)

        assert len(result) <= max(0, max_len)


class TestShellHandle:
    """Shell 主处理函数测试"""

    @pytest.mark.asyncio
    async def test_handle_help(self, mock_context, mock_event):
        """测试帮助命令"""
        result = await shell_main.handle("shell", "help", mock_event, mock_context)
        assert isinstance(result, list)
        assert len(result) > 0
        rendered = text_segments_text(result)
        assert "C:/Users/testuser" not in rendered
        assert "C:/workspace/example.py" in rendered

    @pytest.mark.asyncio
    @pytest.mark.parametrize("alias", ["-h", "--help"])
    async def test_handle_option_help_aliases(self, alias, mock_context, mock_event):
        result = await shell_main.handle("shell", alias, mock_event, mock_context)

        assert "Shell 命令执行插件" in text_segments_text(result)

    @pytest.mark.asyncio
    async def test_handle_list_whitelist(self, mock_context, mock_event):
        """测试列出白名单"""
        result = await shell_main.handle("shell", "list", mock_event, mock_context)
        assert isinstance(result, list)
        assert "echo" in str(result)
        assert "ls" in str(result)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("alias", ["-l", "--list"])
    async def test_handle_option_list_aliases(self, alias, mock_context, mock_event):
        result = await shell_main.handle("shell", alias, mock_event, mock_context)

        text = text_segments_text(result)
        assert "echo" in text
        assert "ls" in text

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

    @pytest.mark.asyncio
    async def test_handle_combines_streams_with_one_output_budget(
        self,
        monkeypatch,
        mock_context,
        mock_event,
    ):
        monkeypatch.setattr(
            shell_main,
            "_execute_command",
            AsyncMock(return_value=(1, "o" * 6000, "e" * 6000)),
        )

        result = await shell_main.handle("shell", "echo ok", mock_event, mock_context)

        text = text_segments_text(result)
        assert "stdout" in text and "stderr" in text
        assert text.count("已省略中间内容") == 2
        assert len(text) <= shell_config.MAX_OUTPUT_LENGTH + 80

    @pytest.mark.asyncio
    async def test_handle_does_not_execute_malformed_quoted_command(
        self,
        monkeypatch,
        mock_context,
        mock_event,
    ):
        execute = AsyncMock()
        monkeypatch.setattr(shell_main, "_execute_command", execute)

        result = await shell_main.handle("shell", 'echo "unterminated', mock_event, mock_context)

        assert "无法解析" in text_segments_text(result)
        execute.assert_not_awaited()

    def test_manifest_entry_and_aliases_match_documentation(self):
        plugin_dir = ROOT / "plugins" / "shell"
        manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
        command = manifest["commands"][0]
        readme = (plugin_dir / "README.md").read_text(encoding="utf-8")

        assert manifest["entry"] == "main.py"
        assert (plugin_dir / manifest["entry"]).is_file()
        assert command["admin_only"] is True
        assert set(command["triggers"]) == {"shell", "sh", "exec"}
        for trigger in command["triggers"]:
            assert f"/{trigger}" in readme


class TestShellExecutionSafety:
    """子进程资源限制和跨平台终止语义。"""

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
        assert captured["stdin"] is asyncio.subprocess.DEVNULL
        if shell_main.sys.platform == "win32":
            assert captured.get("creationflags", 0) != 0
        else:
            assert captured.get("start_new_session") is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("args,timeout", [([], 1), (["echo"], 0), (["echo"], float("inf"))])
    async def test_execute_command_rejects_invalid_runtime_contract(self, args, timeout):
        with pytest.raises(ValueError):
            await shell_main._execute_command(args, timeout)

    @pytest.mark.asyncio
    async def test_execute_command_timeout_terminates_process_tree(self, pending_process):
        proc = pending_process()

        async def terminate(_proc):
            _finish_pending_process(proc)

        with (
            patch.object(
                shell_main.asyncio,
                "create_subprocess_exec",
                new=AsyncMock(return_value=proc),
            ),
            patch.object(
                shell_main, "_terminate_process_tree", new=AsyncMock(side_effect=terminate)
            ) as mock_terminate,
        ):
            code, stdout, stderr = await shell_main._execute_command(["echo", "ok"], 0.01)

        assert code == -1
        assert stdout == ""
        assert "超时" in stderr
        mock_terminate.assert_awaited_once_with(proc)

    @pytest.mark.asyncio
    async def test_execute_command_output_limit_terminates_process_tree(
        self,
        monkeypatch,
        pending_process,
    ):
        proc = pending_process()
        proc.stdout.feed_data(b"x" * (64 * 1024 + 1))

        async def terminate(_proc):
            _finish_pending_process(proc)

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
    async def test_stdout_and_stderr_share_capture_limit(self, monkeypatch, pending_process):
        proc = pending_process()
        proc.stdout.feed_data(b"o" * (40 * 1024))
        proc.stderr.feed_data(b"e" * (40 * 1024))

        async def terminate(_proc):
            _finish_pending_process(proc)

        monkeypatch.setattr(
            shell_main.asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=proc),
        )
        monkeypatch.setattr(
            shell_main,
            "_terminate_process_tree",
            AsyncMock(side_effect=terminate),
        )

        code, stdout, stderr = await shell_main._execute_command(["echo", "ok"], 1)

        assert code == shell_main._EXIT_OUTPUT_LIMIT
        assert len(stdout.encode()) + len(stderr.split("\n", 1)[0].encode()) <= 64 * 1024

    @pytest.mark.asyncio
    async def test_execute_command_cancellation_terminates_process_tree(
        self,
        monkeypatch,
        pending_process,
    ):
        proc = pending_process()

        async def terminate(_proc):
            _finish_pending_process(proc)

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

    @pytest.mark.asyncio
    async def test_repeated_cancellation_still_finishes_process_cleanup(
        self,
        monkeypatch,
        pending_process,
    ):
        proc = pending_process()
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def terminate(_proc):
            cleanup_started.set()
            await release_cleanup.wait()
            _finish_pending_process(proc)

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
        await cleanup_started.wait()
        task.cancel()
        release_cleanup.set()

        with pytest.raises(asyncio.CancelledError):
            await task

        terminate_mock.assert_awaited_once_with(proc)

    @pytest.mark.asyncio
    async def test_windows_taskkill_failure_falls_back_to_direct_kill(self, monkeypatch):
        proc = MagicMock(pid=123, returncode=None)
        proc.wait = AsyncMock(return_value=-9)
        killer = MagicMock(returncode=1)
        killer.communicate = AsyncMock(return_value=(b"", b""))
        monkeypatch.setattr(shell_main.sys, "platform", "win32")
        monkeypatch.setattr(
            shell_main.asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=killer),
        )

        await shell_main._terminate_process_tree(proc)

        proc.kill.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_windows_taskkill_success_does_not_kill_process_twice(self, monkeypatch):
        proc = MagicMock(pid=123, returncode=None)
        proc.wait = AsyncMock(return_value=0)
        killer = MagicMock(returncode=0)
        killer.communicate = AsyncMock(return_value=(b"", b""))
        monkeypatch.setattr(shell_main.sys, "platform", "win32")
        monkeypatch.setattr(
            shell_main.asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=killer),
        )

        await shell_main._terminate_process_tree(proc)

        proc.kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_posix_group_kill_failure_falls_back_to_direct_kill(self, monkeypatch):
        proc = MagicMock(pid=123, returncode=None)
        proc.wait = AsyncMock(return_value=-9)
        monkeypatch.setattr(shell_main.sys, "platform", "linux")
        monkeypatch.setattr(shell_main.signal, "SIGKILL", 9, raising=False)
        monkeypatch.setattr(
            shell_main.os,
            "killpg",
            MagicMock(side_effect=PermissionError),
            raising=False,
        )

        await shell_main._terminate_process_tree(proc)

        proc.kill.assert_called_once_with()


class TestSmartDecode:
    """智能解码测试"""

    def test_decode_utf8(self):
        """测试 UTF-8 解码"""
        data = b"hello"
        result = shell_main._smart_decode(data)
        assert result == "hello"

    def test_decode_gbk(self, monkeypatch):
        """测试 GBK 解码"""
        monkeypatch.setattr(shell_main.sys, "platform", "win32")
        data = "你好".encode("gbk")
        result = shell_main._smart_decode(data)
        assert result == "你好"

    def test_windows_utf8_is_not_misdecoded_as_gbk(self, monkeypatch):
        monkeypatch.setattr(shell_main.sys, "platform", "win32")

        assert shell_main._smart_decode("你好".encode()) == "你好"

    def test_unknown_posix_bytes_fall_back_without_failure(self, monkeypatch):
        monkeypatch.setattr(shell_main.sys, "platform", "linux")

        assert shell_main._smart_decode(b"\xff") == "ÿ"

    def test_decode_empty(self):
        """测试空字节解码"""
        result = shell_main._smart_decode(b"")
        assert result == ""
