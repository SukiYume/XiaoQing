"""Shell 插件的配置、解析、执行与资源回收测试。"""

import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.process_tree import ProcessResult
from plugins.shell import config as shell_config
from plugins.shell import main as shell_main
from tests.helpers.assertions import text_segments_text
from tests.helpers.asyncio_tools import run_with_subprocess_loop
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.settings_snapshot import with_settings_reader

ROOT = REPOSITORY_ROOT


@pytest.fixture
def mock_context():
    """模拟上下文"""
    context         = MagicMock()
    context.config  = {}
    context.secrets = {
        "plugins": {
            "shell": {
                "whitelist": ["ls", "pwd", "echo"],
                "timeout"  : 30,
            }
        }
    }
    context.logger = MagicMock()
    return with_settings_reader(context)


@pytest.fixture
def mock_event():
    """模拟事件"""
    return {
        "user_id"     : 12345,
        "message_type": "group",
        "group_id"    : 67890,
    }


class TestShellConfig:
    """Shell 配置测试"""

    def test_frozen_snapshot_config_accepts_tuple_whitelist(self, mock_context):
        from core.config import ConfigSnapshot

        mock_context.secrets = ConfigSnapshot(
            config  = {},
            secrets = {
                "plugins": {
                    "shell": {
                        "whitelist"     : ["echo", "pwd"],
                        "whitelist_mode": "replace",
                        "timeout"       : 17,
                    }
                }
            },
        ).secrets

        config = shell_main._get_config(mock_context)
        assert not isinstance(config, dict)
        assert config["whitelist"] == ("echo", "pwd")
        assert shell_main._get_whitelist(mock_context) == {"echo", "pwd"}
        assert shell_main._get_timeout(mock_context) == 17

    def test_public_git_bash_terminal_config(self, mock_context):
        mock_context.config = {
            "plugins": {
                "shell": {
                    "terminal": {
                        "backend"   : "git-bash",
                        "executable": "C:/Program Files/Git/bin/bash.exe",
                    }
                }
            }
        }

        settings = shell_main._get_terminal_settings(mock_context)

        assert settings.backend == "git-bash"
        assert settings.executable == "C:/Program Files/Git/bin/bash.exe"
        assert settings.error is None

    @pytest.mark.parametrize(
        "terminal,error_fragment",
        [
            ("git-bash", "必须是对象"),
            ({"backend": "unknown"}, "direct 或 git-bash"),
            ({"backend": "git-bash"}, "需要非空"),
        ],
    )
    def test_invalid_public_terminal_config_fails_closed(
        self, mock_context, terminal, error_fragment
    ):
        mock_context.config = {"plugins": {"shell": {"terminal": terminal}}}

        settings = shell_main._get_terminal_settings(mock_context)

        assert settings.error is not None
        assert error_fragment in settings.error

    def test_default_whitelist_not_empty(self):
        """测试默认白名单不为空"""
        assert len(shell_config.DEFAULT_WHITELIST) > 0
        assert shell_config.DEFAULT_WHITELIST.isdisjoint(shell_config.UNSUPPORTED_SHELL_BUILTINS)

    def test_explicit_empty_replace_list_fails_closed(self, mock_context):
        mock_context.secrets["plugins"]["shell"] = {
            "whitelist"     : [],
            "whitelist_mode": "replace",
        }

        assert shell_main._get_whitelist(mock_context) == set()

    def test_invalid_whitelist_is_not_split_into_characters(self, mock_context):
        mock_context.secrets["plugins"]["shell"] = {
            "whitelist"     : "python",
            "whitelist_mode": "replace",
        }

        assert shell_main._get_whitelist(mock_context) == set()

    def test_extend_normalizes_entries_and_filters_shell_builtins(self, mock_context):
        mock_context.secrets["plugins"]["shell"] = {
            "whitelist"     : [" Custom-Tool ", "CD", None],
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
        result = shell_main._split_command(r"echo C:\Users\testuser\Desktop\a.py")
        if shell_main.sys.platform == "win32":
            assert result == ["echo", r"C:\Users\testuser\Desktop\a.py"]
        else:
            assert result == ["echo", "C:UserstestuserDesktopa.py"]

    def test_forward_slash_path_normalized_for_current_platform(self):
        """用户统一输入 / 路径，后端按当前系统规范化"""
        result = shell_main._split_command("echo C:/Users/testuser/Desktop/a.py")
        if shell_main.sys.platform == "win32":
            assert result == ["echo", r"C:\Users\testuser\Desktop\a.py"]
        else:
            assert result == ["echo", "C:/Users/testuser/Desktop/a.py"]

    def test_windows_options_are_not_paths(self):
        """cmd /c copy /Y 中的 /c 和 /Y 不是路径"""
        result = shell_main._split_command(
            "cmd /c copy /Y C:/Users/testuser/a.py C:/Users/testuser/b.py"
        )
        assert result[1] == "/c"
        assert result[3] == "/Y"

    def test_url_is_not_normalized_as_path(self):
        """URL 参数不应被路径规范化破坏"""
        result = shell_main._split_command("curl https://example.com/a/b")
        assert result == ["curl", "https://example.com/a/b"]

    def test_key_value_path_is_normalized(self):
        result = shell_main._split_command("echo output=C:/Users/testuser/a.txt")
        if shell_main.sys.platform == "win32":
            assert result == ["echo", r"output=C:\Users\testuser\a.txt"]
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
        text   = "a" * 10000
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
    async def test_handle_help_uses_windows_native_examples(
        self, monkeypatch, mock_context, mock_event
    ):
        monkeypatch.setattr(shell_main.sys, "platform", "win32")

        result = await shell_main.handle("shell", "help", mock_event, mock_context)

        rendered = text_segments_text(result)
        assert "/shell cmd /c dir" in rendered
        assert "/shell cmd /c cd" in rendered
        assert "/shell ls -la" not in rendered

    @pytest.mark.asyncio
    async def test_handle_help_uses_posix_examples(self, monkeypatch, mock_context, mock_event):
        monkeypatch.setattr(shell_main.sys, "platform", "linux")

        result = await shell_main.handle("shell", "help", mock_event, mock_context)

        rendered = text_segments_text(result)
        assert "/shell ls -la" in rendered
        assert "/shell pwd" in rendered
        assert "/shell cmd /c dir" not in rendered

    @pytest.mark.asyncio
    async def test_handle_help_uses_git_bash_examples_on_windows(
        self, monkeypatch, mock_context, mock_event
    ):
        monkeypatch.setattr(shell_main.sys, "platform", "win32")
        mock_context.config = {
            "plugins": {
                "shell": {
                    "terminal": {
                        "backend"   : "git-bash",
                        "executable": "C:/Program Files/Git/bin/bash.exe",
                    }
                }
            }
        }

        result = await shell_main.handle("shell", "help", mock_event, mock_context)

        rendered = text_segments_text(result)
        assert "默认终端: Git Bash" in rendered
        assert "/shell ls -la" in rendered
        assert "/shell pwd" in rendered
        assert "/shell cmd /c dir" not in rendered

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
    async def test_handle_list_distinguishes_enabled_from_available(
        self, monkeypatch, mock_context, mock_event
    ):
        monkeypatch.setattr(shell_main, "_command_available", lambda name: name == "echo")

        result = await shell_main.handle("shell", "list", mock_event, mock_context)

        rendered = text_segments_text(result)
        assert "当前 Bot PATH 可执行（1）" in rendered
        assert "当前 Bot PATH 未找到（2）" in rendered
        assert "echo" in rendered
        assert "ls" in rendered and "pwd" in rendered
        assert "启用列表不负责安装程序" in rendered

    @pytest.mark.asyncio
    async def test_handle_list_queries_git_bash_once(self, monkeypatch, mock_context, mock_event):
        mock_context.config = {
            "plugins": {
                "shell": {
                    "terminal": {
                        "backend"   : "git-bash",
                        "executable": "C:/Program Files/Git/bin/bash.exe",
                    }
                }
            }
        }
        monkeypatch.setattr(
            shell_main,
            "_resolve_terminal_executable",
            lambda _settings: r"C:\Program Files\Git\bin\bash.exe",
        )
        execute = AsyncMock(return_value=(0, "ls\npwd\n", ""))
        monkeypatch.setattr(shell_main, "_execute_command", execute)

        result = await shell_main.handle("shell", "list", mock_event, mock_context)

        rendered = text_segments_text(result)
        assert "当前 Git Bash 可执行（2）" in rendered
        assert "当前 Git Bash 未找到（1）" in rendered
        assert "ls, pwd" in rendered
        assert "echo" in rendered
        execute.assert_awaited_once()
        queried = execute.await_args.args[0]
        assert queried[:4] == [
            r"C:\Program Files\Git\bin\bash.exe",
            "--noprofile",
            "--norc",
            "-c",
        ]
        assert queried[-3:] == ["echo", "ls", "pwd"]

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
    async def test_handle_missing_executable_returns_actionable_error(
        self, monkeypatch, mock_context, mock_event
    ):
        execute = AsyncMock(side_effect=FileNotFoundError(2, "not found"))
        audit = MagicMock()
        monkeypatch.setattr(shell_main, "_execute_command", execute)
        monkeypatch.setattr(shell_main, "_log_command_audit", audit)
        monkeypatch.setattr(shell_main.sys, "platform", "win32")

        result = await shell_main.handle("shell", "ls", mock_event, mock_context)

        rendered = text_segments_text(result)
        assert "找不到可执行命令 'ls'" in rendered
        assert "启用列表只控制允许执行的入口" in rendered
        assert "/shell cmd /c dir" in rendered
        assert "XQ-PLUGIN-UNEXPECTED" not in rendered
        execute.assert_awaited_once_with(["ls"], 30)
        assert [call.kwargs["status"] for call in audit.call_args_list] == [
            "started",
            "unavailable",
        ]

    @pytest.mark.asyncio
    async def test_handle_executes_original_command_through_configured_git_bash(
        self, monkeypatch, mock_context, mock_event
    ):
        mock_context.config = {
            "plugins": {
                "shell": {
                    "terminal": {
                        "backend"   : "git-bash",
                        "executable": "C:/Program Files/Git/bin/bash.exe",
                    }
                }
            }
        }
        bash = r"C:\Program Files\Git\bin\bash.exe"
        monkeypatch.setattr(shell_main, "_resolve_terminal_executable", lambda _settings: bash)
        execute = AsyncMock(return_value=(0, "ok", ""))
        monkeypatch.setattr(shell_main, "_execute_command", execute)
        command = 'echo "C:/Program Files/example.txt"'

        result = await shell_main.handle("shell", command, mock_event, mock_context)

        assert "返回码: 0" in text_segments_text(result)
        execute.assert_awaited_once_with(
            [bash, "--noprofile", "--norc", "-c", command],
            30,
        )

    @pytest.mark.asyncio
    async def test_handle_missing_configured_git_bash_fails_before_audit(
        self, monkeypatch, mock_context, mock_event
    ):
        mock_context.config = {
            "plugins": {
                "shell": {
                    "terminal": {
                        "backend"   : "git-bash",
                        "executable": "C:/missing/bash.exe",
                    }
                }
            }
        }
        monkeypatch.setattr(shell_main, "_resolve_terminal_executable", lambda _settings: None)
        execute = AsyncMock()
        audit   = MagicMock()
        monkeypatch.setattr(shell_main, "_execute_command", execute)
        monkeypatch.setattr(shell_main, "_log_command_audit", audit)

        result = await shell_main.handle("shell", "ls", mock_event, mock_context)

        rendered = text_segments_text(result)
        assert "Shell 终端配置不可用" in rendered
        assert "plugins.shell.terminal" in rendered
        execute.assert_not_awaited()
        audit.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_list_whitelist_hides_unsupported_builtins(self, mock_context, mock_event):
        result = await shell_main.handle("shell", "list", mock_event, mock_context)
        text   = str(result)
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

    def test_manifest_entry_and_command_authorization(self):
        plugin_dir = ROOT / "plugins" / "shell"
        manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
        command = manifest["commands"][0]

        assert manifest["entry"] == "main.py"
        assert (plugin_dir / manifest["entry"]).is_file()
        assert command["admin_only"] is True
        assert set(command["triggers"]) == {"shell", "sh", "exec"}


class TestShellExecution:
    """进程树归属、输出预算和超时回收在 ``core.process_tree``（见 tests/core/test_process_tree.py）；
    这里只验证 /shell 怎样把执行结局写成返回码和提示。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("result", "expected_code", "notice"),
        [
            (ProcessResult(3, "out", "err"), 3, None),
            (ProcessResult(0, "out", ""), 0, None),
            (
                ProcessResult(None, "out", "err", timed_out=True),
                shell_main._EXIT_TIMEOUT,
                "命令执行超时（2秒）",
            ),
            (
                ProcessResult(None, "out", "", output_limited=True),
                shell_main._EXIT_OUTPUT_LIMIT,
                "字节安全上限，已终止进程树",
            ),
        ],
    )
    async def test_results_map_to_codes_and_notices(
        self, monkeypatch, result, expected_code, notice
    ):
        run = AsyncMock(return_value=result)
        monkeypatch.setattr(shell_main, "run_process", run)

        code, stdout, stderr = await shell_main._execute_command(["echo", "ok"], 2)

        assert code == expected_code and stdout == "out"
        assert run.call_args.kwargs["max_capture_bytes"] == shell_main._MAX_CAPTURE_BYTES
        if notice is None:
            assert stderr == result.stderr
        else:
            assert notice in stderr

    def test_a_real_command_runs_end_to_end(self):
        async def exercise():
            code, stdout, stderr = await shell_main._execute_command(
                [sys.executable, "-c", "print('ok')"], 30
            )
            assert (code, stdout.strip(), stderr) == (0, "ok", "")

        run_with_subprocess_loop(exercise)
