"""QingSSH 输入、清单和路径契约。"""

from __future__ import annotations

from tests.helpers.qingssh_test_support import (
    EXIT_CODE_TIMEOUT,
    ROOT,
    SessionKeys,
    json,
    pytest,
    qingssh_main,
    ssh_manager_module,
    validate_command,
    validate_hostname,
    validate_port,
    validate_server_name,
    validate_username,
)


@pytest.mark.parametrize("name", ["server", "server_01", "jump-host"])
def test_server_name_validator_accepts_only_complete_ascii_names(name: str) -> None:
    assert validate_server_name(name) == (True, "")


@pytest.mark.parametrize(
    "name",
    ["", "safe\n", "safe name", "服务器", "x" * 51, "server/other"],
)
def test_server_name_validator_rejects_ambiguous_or_non_ascii_names(name: str) -> None:
    assert validate_server_name(name)[0] is False


@pytest.mark.parametrize("text, expected", [("1", 1), ("22", 22), ("65535", 65535)])
def test_port_validator_accepts_only_bounded_ascii_decimal(text: str, expected: int) -> None:
    assert validate_port(text) == (True, expected, "")


@pytest.mark.parametrize(
    "text",
    ["", "0", "65536", " 22", "22 ", "+22", "22\n", "２２", "22.0"],
)
def test_port_validator_rejects_coercion_and_whitespace(text: str) -> None:
    valid, port, _error = validate_port(text)
    assert valid is False
    assert port == 0


@pytest.mark.parametrize(
    "hostname",
    ["localhost", "example.com", "sub-domain.example", "127.0.0.1", "2001:db8::1"],
)
def test_hostname_validator_accepts_valid_dns_and_ip_values(hostname: str) -> None:
    assert validate_hostname(hostname) == (True, "")


@pytest.mark.parametrize(
    "hostname",
    [
        "",
        "safe\n",
        " evil.example",
        "evil:$(touch x)",
        "2001:db8::zz",
        "256.1.1.1",
        "192.168.001.1",
        "example..com",
        "-example.com",
        "example.com-",
        "example_com",
        "example.com.",
        f"{'x' * 64}.example",
    ],
)
def test_hostname_validator_rejects_malformed_and_injection_values(hostname: str) -> None:
    assert validate_hostname(hostname)[0] is False


def test_command_validator_rejects_empty_or_whitespace_only_commands() -> None:
    assert validate_command("")[0] is False
    assert validate_command(" \t\r\n")[0] is False
    assert validate_command("  pwd  ") == (True, "")


@pytest.mark.parametrize("username", ["root", "user.name", "svc_account", "build-bot"])
def test_username_validator_accepts_safe_ssh_names(username: str) -> None:
    assert validate_username(username) == (True, "")


@pytest.mark.parametrize("username", ["", "bad user", "user@host", "$(id)", "x" * 65])
def test_username_validator_rejects_ambiguous_values(username: str) -> None:
    assert validate_username(username)[0] is False


class TestQingsshRuntimeContract:
    """使用真实导入对象验证 SSH 插件公开契约。"""

    def test_entrypoints_and_help(self):
        assert callable(qingssh_main.handle)
        assert callable(qingssh_main.handle_session)
        assert callable(qingssh_main.cleanup)
        assert callable(qingssh_main.shutdown)
        assert callable(qingssh_main.cleanup_orphans)
        help_text = qingssh_main._show_help()
        assert "SSH 远程控制" in help_text
        assert "/ssh disconnect" in help_text
        assert "showimg <路径或通配符> [--page N]" in help_text
        assert "每页 5 张" in help_text

    def test_runtime_manager_and_config_contract(self):
        assert ssh_manager_module.SSHManager is not None
        assert callable(ssh_manager_module.get_manager)
        assert SessionKeys.SERVER_NAME == "server_name"
        assert SessionKeys.STEP == "step"
        assert SessionKeys.SERVER_CONFIG == "server_config"
        assert EXIT_CODE_TIMEOUT < 0

    def test_proxyjump_parser_rejects_local_shells(self):
        assert ssh_manager_module._parse_proxyjump_command(
            "ssh -W %h:%p -p 2222 -l jumpuser jump-host"
        ) == {
            "jump_host": "jump-host",
            "jump_port": 2222,
            "jump_user": "jumpuser",
        }
        assert ssh_manager_module._parse_proxyjump_command("bash -lc 'nc %h %p'") is None
        assert ssh_manager_module._parse_proxyjump_spec("jumpuser@jump-host:2222") == {
            "jump_host": "jump-host",
            "jump_port": 2222,
            "jump_user": "jumpuser",
        }
        assert ssh_manager_module._parse_proxyjump_spec("first,second") is None
        assert ssh_manager_module._parse_proxyjump_spec("jump:70000") is None


class TestQingsshPluginJson:
    """测试 QingSSH plugin.json 配置"""

    def test_plugin_json_exists(self):
        """测试 plugin.json 存在"""
        plugin_json = ROOT / "plugins" / "qingssh" / "plugin.json"
        assert plugin_json.exists()

    def test_plugin_json_content(self):
        """测试 plugin.json 内容"""
        plugin_json = ROOT / "plugins" / "qingssh" / "plugin.json"
        content = json.loads(plugin_json.read_text(encoding="utf-8"))

        assert content["name"] == "qingssh"
        assert "commands" in content
        assert "schedule" in content

    def test_main_command(self):
        """测试主命令配置"""
        plugin_json = ROOT / "plugins" / "qingssh" / "plugin.json"
        content = json.loads(plugin_json.read_text(encoding="utf-8"))

        ssh_cmd = next((cmd for cmd in content["commands"] if cmd["name"] == "ssh"), None)
        assert ssh_cmd is not None
        assert "ssh" in ssh_cmd["triggers"]
        assert "admin_only" in ssh_cmd
        assert ssh_cmd["admin_only"] is True
        assert "showimg" in ssh_cmd["help"]
        help_command = next(
            subcommand for subcommand in ssh_cmd["subcommands"] if subcommand["name"] == "help"
        )
        assert "showimg" in help_command["help"]

    def test_legacy_commands(self):
        """测试旧命令存在"""
        plugin_json = ROOT / "plugins" / "qingssh" / "plugin.json"
        content = json.loads(plugin_json.read_text(encoding="utf-8"))

        commands = [cmd["name"] for cmd in content["commands"]]
        assert "ssh断开" in commands or "sshdisconnect" in commands
        assert "ssh列表" in commands or "sshlist" in commands
        assert "ssh添加" in commands or "sshadd" in commands
        assert "ssh删除" in commands or "sshremove" in commands or "sshdel" in commands
        assert "ssh导入" in commands or "sshimport" in commands
        assert "sshconfig" in commands or "ssh配置" in commands
        assert "ssh状态" in commands or "sshstatus" in commands

    def test_schedule_config(self):
        """测试定时任务配置"""
        plugin_json = ROOT / "plugins" / "qingssh" / "plugin.json"
        content = json.loads(plugin_json.read_text(encoding="utf-8"))

        assert "schedule" in content
        assert len(content["schedule"]) > 0

        # 检查清理任务
        cleanup_task = next((s for s in content["schedule"] if s["id"] == "cleanup"), None)
        assert cleanup_task is not None
        assert cleanup_task["handler"] == "cleanup_orphans"

    def test_concurrency_setting(self):
        """测试并发设置"""
        plugin_json = ROOT / "plugins" / "qingssh" / "plugin.json"
        content = json.loads(plugin_json.read_text(encoding="utf-8"))

        assert "concurrency" in content
        assert content["concurrency"] == "sequential"

    def test_manifest_routes_and_schedule_handlers_match_code(self):
        """清单触发词和定时入口必须与实际导出保持一致。"""
        plugin_json = ROOT / "plugins" / "qingssh" / "plugin.json"
        content = json.loads(plugin_json.read_text(encoding="utf-8"))
        commands     = content["commands"]
        main_names   = {command["name"] for command in commands[:1]}
        legacy_names = {command["name"] for command in commands[1:]}

        assert main_names == qingssh_main._MAIN_COMMANDS
        assert legacy_names == set(qingssh_main._LEGACY_ROUTES)
        assert (plugin_json.parent / content["entry"]).is_file()
        for schedule in content["schedule"]:
            assert callable(getattr(qingssh_main, schedule["handler"], None))


class TestQingsshPathResolver:
    """测试路径解析器"""

    def _import_path_resolver(self):
        """动态导入 path_resolver 模块"""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "path_resolver",
            ROOT / "plugins" / "qingssh" / "path_resolver.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_is_cd_command(self):
        """测试 cd 命令检测"""
        pr = self._import_path_resolver()
        assert pr.is_cd_command("cd") is True
        assert pr.is_cd_command("cd /tmp") is True
        assert pr.is_cd_command("cd\t/tmp") is True
        assert pr.is_cd_command("cd FRB121102") is True
        assert pr.is_cd_command("  cd ..  ") is True
        assert pr.is_cd_command("cd /tmp && find / -name x") is False
        assert pr.is_cd_command("cd /tmp\npwd") is False
        assert pr.is_cd_command("ls") is False
        assert pr.is_cd_command("echo cd") is False
        assert pr.is_cd_command("cdf") is False

    def test_build_command_no_cwd(self):
        """测试无 CWD 时的命令构建"""
        pr = self._import_path_resolver()
        # 普通命令
        assert pr.build_command("ls", None) == "ls"
        assert pr.build_command("pwd", None) == "pwd"

    def test_build_command_with_cwd(self):
        """测试有 CWD 时的命令构建"""
        pr     = self._import_path_resolver()
        result = pr.build_command("ls", "/home/user/data")
        assert result == "cd /home/user/data && ls"

    def test_build_command_cd_appends_pwd(self):
        """测试 cd 命令会附加隐藏的工作目录探针"""
        pr = self._import_path_resolver()
        import shlex

        # cd 无 CWD
        result = pr.build_command("cd /tmp", None)
        assert result == "cd /tmp && printf '%s%s\\n' '__XQ_CWD__' \"$(pwd -P)\""

        # cd 有 CWD
        cwd    = "/home/user/low.iops.files"
        result = pr.build_command("cd FRB121102", cwd)
        assert result == (
            f"cd {shlex.quote(cwd)} && cd FRB121102 && printf '%s%s\\n' '__XQ_CWD__' \"$(pwd -P)\""
        )

        # bare cd 有 CWD
        cwd2   = "/home/user/data"
        result = pr.build_command("cd", cwd2)
        assert result == (
            f"cd {shlex.quote(cwd2)} && cd && printf '%s%s\\n' '__XQ_CWD__' \"$(pwd -P)\""
        )
        assert "__XQ_CWD__" not in pr.build_command("cd /tmp && find / -name x")

    def test_build_command_with_env_vars(self):
        """测试带环境变量的命令构建"""
        pr = self._import_path_resolver()
        import shlex

        cwd    = "/home/user"
        result = pr.build_command("echo $FOO", cwd, {"FOO": "bar"})
        assert f"cd {shlex.quote(cwd)}" in result
        assert "export FOO=" in result
        assert "echo $FOO" in result

    def test_extract_cwd_from_output(self):
        """测试从输出中提取 CWD"""
        pr = self._import_path_resolver()
        # 只接受内部唯一标记后的绝对路径
        assert pr.extract_cwd_from_output("__XQ_CWD__/home/user/data\n") == "/home/user/data"

        # 普通输出中的路径不能伪造 CWD
        assert pr.extract_cwd_from_output("some output\n/home/user/data\n") is None
        assert (
            pr.extract_cwd_from_output("some output\n__XQ_CWD__/home/user/data\n")
            == "/home/user/data"
        )
        assert pr.extract_cwd_from_output("__XQ_CWD__not-absolute\n") is None

        # 无有效路径
        assert pr.extract_cwd_from_output("error message\n") is None
        assert pr.extract_cwd_from_output("") is None
        assert pr.extract_cwd_from_output(None) is None

    def test_extract_cwd_dots_in_path(self):
        """测试路径中包含点号的情况（核心 bug 场景）"""
        pr = self._import_path_resolver()
        assert (
            pr.extract_cwd_from_output("__XQ_CWD__/home/user/low.iops.files\n")
            == "/home/user/low.iops.files"
        )
        assert (
            pr.extract_cwd_from_output("__XQ_CWD__/home/user/low.iops.files/FRB121102\n")
            == "/home/user/low.iops.files/FRB121102"
        )

    def test_strip_cwd_markers(self):
        pr = self._import_path_resolver()

        assert pr.strip_cwd_markers("before\n__XQ_CWD__/tmp\nafter\n") == "before\nafter\n"

    def test_resolve_remote_path(self):
        """测试远程路径解析"""
        pr = self._import_path_resolver()
        # 绝对路径直接返回
        assert pr.resolve_remote_path("/tmp/file.png") == "/tmp/file.png"

        # 相对路径拼接 CWD
        assert pr.resolve_remote_path("image.png", "/home/user/data") == "/home/user/data/image.png"

        # 无 CWD 时直接返回文件名
        assert pr.resolve_remote_path("image.png") == "image.png"

        # CWD 末尾有斜杠
        assert (
            pr.resolve_remote_path("image.png", "/home/user/data/") == "/home/user/data/image.png"
        )
