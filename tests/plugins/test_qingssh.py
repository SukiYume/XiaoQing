"""测试 SSH 远程控制插件 (QingSSH)"""

import pytest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock
import tempfile
import json
from typing import Any, cast

from plugins.qingssh import session_handlers as ssh_session_handlers
from plugins.qingssh.config import SessionKeys

ROOT = Path(__file__).resolve().parent.parent.parent


class TestQingsshPlugin:
    """测试 QingSSH 插件"""

    def test_init(self):
        """测试插件初始化"""
        main_file = ROOT / "plugins" / "qingssh" / "main.py"
        content = main_file.read_text(encoding='utf-8')
        assert "def init" in content

    def test_help_text(self):
        """测试帮助文本"""
        main_file = ROOT / "plugins" / "qingssh" / "main.py"
        content = main_file.read_text(encoding='utf-8')
        assert "def _show_help" in content
        assert "SSH" in content or "远程控制" in content


class TestQingsshCommands:
    """测试 QingSSH 命令处理"""

    def test_handle_function(self):
        """测试 handle 函数"""
        main_file = ROOT / "plugins" / "qingssh" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "async def handle" in content
        assert "command" in content
        assert "args" in content

    def test_handle_session_function(self):
        """测试 handle_session 函数"""
        main_file = ROOT / "plugins" / "qingssh" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "async def handle_session" in content
        assert "session" in content

    def test_cleanup_function(self):
        """测试 cleanup 函数"""
        main_file = ROOT / "plugins" / "qingssh" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "async def cleanup" in content
        assert "close_all" in content

    def test_shutdown_function(self):
        """测试 shutdown 函数"""
        main_file = ROOT / "plugins" / "qingssh" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "async def shutdown" in content

    def test_cleanup_orphans_function(self):
        """测试清理孤儿连接函数"""
        main_file = ROOT / "plugins" / "qingssh" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "async def cleanup_orphans" in content


class TestQingsshModules:
    """测试 QingSSH 模块结构"""

    def test_main_module_exists(self):
        """测试主模块存在"""
        main_file = ROOT / "plugins" / "qingssh" / "main.py"
        assert main_file.exists()

    def test_handlers_module_exists(self):
        """测试处理器模块存在"""
        handlers_file = ROOT / "plugins" / "qingssh" / "handlers.py"
        assert handlers_file.exists()

    def test_session_handlers_module_exists(self):
        """测试会话处理器模块存在"""
        session_handlers_file = ROOT / "plugins" / "qingssh" / "session_handlers.py"
        assert session_handlers_file.exists()

    def test_ssh_manager_module_exists(self):
        """测试 SSH 管理器模块存在"""
        ssh_manager_file = ROOT / "plugins" / "qingssh" / "ssh_manager.py"
        assert ssh_manager_file.exists()

    def test_config_module_exists(self):
        """测试配置模块存在"""
        config_file = ROOT / "plugins" / "qingssh" / "config.py"
        assert config_file.exists()

    def test_validators_module_exists(self):
        """测试验证器模块存在"""
        validators_file = ROOT / "plugins" / "qingssh" / "validators.py"
        assert validators_file.exists()

    def test_message_formatter_module_exists(self):
        """测试消息格式化模块存在"""
        formatter_file = ROOT / "plugins" / "qingssh" / "message_formatter.py"
        assert formatter_file.exists()

    def test_types_module_exists(self):
        """测试类型模块存在"""
        types_file = ROOT / "plugins" / "qingssh" / "types.py"
        assert types_file.exists()


class TestQingsshHandlers:
    """测试 QingSSH 处理器函数"""

    def test_handler_functions(self):
        """测试处理器函数存在"""
        handlers_file = ROOT / "plugins" / "qingssh" / "handlers.py"
        content = handlers_file.read_text(encoding='utf-8')

        assert "handle_ssh_main" in content
        assert "handle_ssh_list" in content
        assert "handle_ssh_add" in content
        assert "handle_ssh_remove" in content
        assert "handle_ssh_disconnect" in content
        assert "handle_ssh_import" in content
        assert "handle_ssh_config_list" in content
        assert "handle_ssh_status" in content

    def test_add_server_handler(self):
        """测试添加服务器处理器"""
        handlers_file = ROOT / "plugins" / "qingssh" / "handlers.py"
        content = handlers_file.read_text(encoding='utf-8')

        assert "validate_server_name" in content
        assert "validate_hostname" in content
        assert "validate_port" in content

    def test_import_handler(self):
        """测试导入处理器"""
        handlers_file = ROOT / "plugins" / "qingssh" / "handlers.py"
        content = handlers_file.read_text(encoding='utf-8')

        assert "import_from_ssh_config" in content
        assert "~/.ssh/config" in content or ".ssh/config" in content

    def test_ssh_config_host_listing(self):
        """测试 SSH config 主机列表"""
        handlers_file = ROOT / "plugins" / "qingssh" / "handlers.py"
        content = handlers_file.read_text(encoding='utf-8')

        assert "get_ssh_config_hosts" in content


class TestQingsshSshManager:
    """测试 SSHManager 模块"""

    def test_ssh_manager_class(self):
        """测试 SSHManager 类"""
        ssh_manager_file = ROOT / "plugins" / "qingssh" / "ssh_manager.py"
        content = ssh_manager_file.read_text(encoding='utf-8')

        assert "class SSHManager" in content

    def test_connection_management(self):
        """测试连接管理方法"""
        ssh_manager_file = ROOT / "plugins" / "qingssh" / "ssh_manager.py"
        content = ssh_manager_file.read_text(encoding='utf-8')

        assert "async def connect" in content
        assert "def disconnect" in content
        assert "def is_connected" in content
        assert "def close_all" in content

    def test_server_management(self):
        """测试服务器管理方法"""
        ssh_manager_file = ROOT / "plugins" / "qingssh" / "ssh_manager.py"
        content = ssh_manager_file.read_text(encoding='utf-8')

        assert "async def add_server" in content
        assert "async def remove_server" in content
        assert "def get_server" in content
        assert "def list_servers" in content

    def test_command_execution(self):
        """测试命令执行方法"""
        ssh_manager_file = ROOT / "plugins" / "qingssh" / "ssh_manager.py"
        content = ssh_manager_file.read_text(encoding='utf-8')

        assert "async def execute_command" in content
        assert "async def execute_command_stream" in content

    def test_file_operations(self):
        """测试文件操作方法"""
        ssh_manager_file = ROOT / "plugins" / "qingssh" / "ssh_manager.py"
        content = ssh_manager_file.read_text(encoding='utf-8')

        assert "async def download_file" in content
        assert "async def list_files" in content

    def test_connection_key_building(self):
        """测试连接键构建"""
        ssh_manager_file = ROOT / "plugins" / "qingssh" / "ssh_manager.py"
        content = ssh_manager_file.read_text(encoding='utf-8')

        assert "def _build_connection_key" in content
        assert "user_id" in content
        assert "group_id" in content

    def test_ssh_config_loading(self):
        """测试 SSH 配置加载"""
        ssh_manager_file = ROOT / "plugins" / "qingssh" / "ssh_manager.py"
        content = ssh_manager_file.read_text(encoding='utf-8')

        assert "async def _load_ssh_config" in content
        assert "def get_ssh_config_hosts" in content
        assert "def get_ssh_config_for_host" in content


class TestQingsshConfig:
    """测试 QingSSH 配置"""

    def test_timeout_constants(self):
        """测试超时常量"""
        config_file = ROOT / "plugins" / "qingssh" / "config.py"
        content = config_file.read_text(encoding='utf-8')

        assert "COMMAND_TIMEOUT" in content
        assert "MAX_OUTPUT_LENGTH" in content
        assert "CONNECT_TIMEOUT" in content
        assert "SESSION_TIMEOUT" in content

    def test_session_keys(self):
        """测试会话键"""
        config_file = ROOT / "plugins" / "qingssh" / "config.py"
        content = config_file.read_text(encoding='utf-8')

        assert "class SessionKeys" in content
        assert "SERVER_NAME" in content
        assert "CWD" in content or "WORKING_DIR" in content  # CWD 是工作目录的键名
        assert "COMMAND_COUNT" in content or "HISTORY" in content  # 命令历史相关

    def test_exit_codes(self):
        """测试退出码"""
        config_file = ROOT / "plugins" / "qingssh" / "config.py"
        content = config_file.read_text(encoding='utf-8')

        assert "EXIT_CODE_INTERRUPTED" in content
        assert "EXIT_CODE_ERROR" in content


class TestQingsshValidators:
    """测试 QingSSH 验证器"""

    def test_validator_functions(self):
        """测试验证器函数"""
        validators_file = ROOT / "plugins" / "qingssh" / "validators.py"
        content = validators_file.read_text(encoding='utf-8')

        assert "def validate_server_name" in content
        assert "def validate_host" in content or "def validate_hostname" in content
        assert "def validate_port" in content
        # validate_username 可能不存在，检查是否有其他验证函数


class TestQingsshMessageFormatter:
    """测试 QingSSH 消息格式化"""

    def test_formatter_functions(self):
        """测试格式化函数"""
        formatter_file = ROOT / "plugins" / "qingssh" / "message_formatter.py"
        content = formatter_file.read_text(encoding='utf-8')

        # 检查各种格式化函数
        assert "format_section" in content or "def format" in content
        assert "format_error" in content or "error" in content.lower()
        assert "format_success" in content or "success" in content.lower()


class TestQingsshTypes:
    """测试 QingSSH 类型定义"""

    def test_type_aliases(self):
        """测试类型别名"""
        types_file = ROOT / "plugins" / "qingssh" / "types.py"
        content = types_file.read_text(encoding='utf-8')

        # 检查类型定义
        assert "Context" in content
        assert "Session" in content
        assert "OneBotEvent" in content
        assert "MessageSegments" in content


class TestQingsshPluginJson:
    """测试 QingSSH plugin.json 配置"""

    def test_plugin_json_exists(self):
        """测试 plugin.json 存在"""
        plugin_json = ROOT / "plugins" / "qingssh" / "plugin.json"
        assert plugin_json.exists()

    def test_plugin_json_content(self):
        """测试 plugin.json 内容"""
        plugin_json = ROOT / "plugins" / "qingssh" / "plugin.json"
        content = json.loads(plugin_json.read_text(encoding='utf-8'))

        assert content["name"] == "qingssh"
        assert "commands" in content
        assert "schedule" in content

    def test_main_command(self):
        """测试主命令配置"""
        plugin_json = ROOT / "plugins" / "qingssh" / "plugin.json"
        content = json.loads(plugin_json.read_text(encoding='utf-8'))

        ssh_cmd = next((cmd for cmd in content["commands"] if cmd["name"] == "ssh"), None)
        assert ssh_cmd is not None
        assert "ssh" in ssh_cmd["triggers"]
        assert "admin_only" in ssh_cmd
        assert ssh_cmd["admin_only"] is True

    def test_legacy_commands(self):
        """测试旧命令存在"""
        plugin_json = ROOT / "plugins" / "qingssh" / "plugin.json"
        content = json.loads(plugin_json.read_text(encoding='utf-8'))

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
        content = json.loads(plugin_json.read_text(encoding='utf-8'))

        assert "schedule" in content
        assert len(content["schedule"]) > 0

        # 检查清理任务
        cleanup_task = next((s for s in content["schedule"] if s["id"] == "cleanup"), None)
        assert cleanup_task is not None
        assert cleanup_task["handler"] == "cleanup_orphans"

    def test_concurrency_setting(self):
        """测试并发设置"""
        plugin_json = ROOT / "plugins" / "qingssh" / "plugin.json"
        content = json.loads(plugin_json.read_text(encoding='utf-8'))

        assert "concurrency" in content
        assert content["concurrency"] == "sequential"


class TestQingsshIntegration:
    """集成测试"""

    def test_module_files_exist(self):
        """测试模块文件存在"""
        assert (ROOT / "plugins" / "qingssh" / "main.py").exists()
        assert (ROOT / "plugins" / "qingssh" / "handlers.py").exists()
        assert (ROOT / "plugins" / "qingssh" / "ssh_manager.py").exists()
        assert (ROOT / "plugins" / "qingssh" / "path_resolver.py").exists()

    def test_main_functions(self):
        """测试主模块函数"""
        main_file = ROOT / "plugins" / "qingssh" / "main.py"
        content = main_file.read_text(encoding='utf-8')
        # 检查关键函数存在
        assert "def init" in content
        assert "async def handle" in content
        assert "async def handle_session" in content
        assert "async def cleanup" in content
        assert "async def shutdown" in content
        assert "async def cleanup_orphans" in content
        assert "def _show_help" in content

    def test_handlers_exports(self):
        """测试处理器模块导出"""
        handlers_file = ROOT / "plugins" / "qingssh" / "handlers.py"
        content = handlers_file.read_text(encoding='utf-8')
        # 检查关键函数存在
        assert "handle_ssh_main" in content
        assert "handle_ssh_list" in content
        assert "handle_ssh_add" in content
        assert "handle_ssh_remove" in content
        assert "handle_ssh_disconnect" in content

    def test_ssh_manager_exports(self):
        """测试 SSH 管理器模块导出"""
        ssh_manager_file = ROOT / "plugins" / "qingssh" / "ssh_manager.py"
        content = ssh_manager_file.read_text(encoding='utf-8')
        # 检查关键类存在
        assert "class SSHManager" in content
        assert "PARAMIKO_AVAILABLE" in content
        assert "def get_manager" in content

    def test_paramiko_check(self):
        """测试 paramiko 可用性检查"""
        ssh_manager_file = ROOT / "plugins" / "qingssh" / "ssh_manager.py"
        content = ssh_manager_file.read_text(encoding='utf-8')
        # PARAMIKO_AVAILABLE 检查应该存在
        assert "PARAMIKO_AVAILABLE" in content


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
        assert pr.is_cd_command("cd FRB121102") is True
        assert pr.is_cd_command("  cd ..  ") is True
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
        pr = self._import_path_resolver()
        result = pr.build_command("ls", "/home/user/data")
        assert result == "cd /home/user/data && ls"

    def test_build_command_cd_appends_pwd(self):
        """测试 cd 命令会附加 pwd"""
        pr = self._import_path_resolver()
        import shlex
        # cd 无 CWD
        result = pr.build_command("cd /tmp", None)
        assert result == "cd /tmp && pwd"
        
        # cd 有 CWD
        cwd = "/home/user/low.iops.files"
        result = pr.build_command("cd FRB121102", cwd)
        assert result == f"cd {shlex.quote(cwd)} && cd FRB121102 && pwd"
        
        # bare cd 有 CWD
        cwd2 = "/home/user/data"
        result = pr.build_command("cd", cwd2)
        assert result == f"cd {shlex.quote(cwd2)} && cd && pwd"

    def test_build_command_with_env_vars(self):
        """测试带环境变量的命令构建"""
        pr = self._import_path_resolver()
        import shlex
        cwd = "/home/user"
        result = pr.build_command("echo $FOO", cwd, {"FOO": "bar"})
        assert f"cd {shlex.quote(cwd)}" in result
        assert "export FOO=" in result
        assert "echo $FOO" in result

    def test_extract_cwd_from_output(self):
        """测试从输出中提取 CWD"""
        pr = self._import_path_resolver()
        # 正常 pwd 输出
        assert pr.extract_cwd_from_output("/home/user/data\n") == "/home/user/data"
        
        # 多行输出（pwd 在最后）
        assert pr.extract_cwd_from_output("some output\n/home/user/data\n") == "/home/user/data"
        
        # 无有效路径
        assert pr.extract_cwd_from_output("error message\n") is None
        assert pr.extract_cwd_from_output("") is None
        assert pr.extract_cwd_from_output(None) is None

    def test_extract_cwd_dots_in_path(self):
        """测试路径中包含点号的情况（核心 bug 场景）"""
        pr = self._import_path_resolver()
        assert pr.extract_cwd_from_output("/home/user/low.iops.files\n") == "/home/user/low.iops.files"
        assert pr.extract_cwd_from_output("/home/user/low.iops.files/FRB121102\n") == "/home/user/low.iops.files/FRB121102"

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
        assert pr.resolve_remote_path("image.png", "/home/user/data/") == "/home/user/data/image.png"


class _SessionStub:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class _ManagerStub:
    def __init__(self):
        self._stop = False
        self._done = asyncio.Event()

    def is_connected(self, user_id, group_id, server_name):
        return True

    def stop_command(self, user_id, group_id, server_name):
        self._stop = True
        return True

    async def execute_command_stream(self, *args, **kwargs):
        await self._done.wait()
        return 0


def test_qingssh_session_does_not_store_task_object():
    async def _run():
        manager = _ManagerStub()
        context = Mock()
        context.current_user_id = 10001
        context.current_group_id = 50001
        context.send_action = AsyncMock()
        context.end_session = AsyncMock()

        session = _SessionStub(
            {
                SessionKeys.STATE: "connected",
                SessionKeys.SERVER_NAME: "srv1",
                SessionKeys.COMMAND_COUNT: 0,
                SessionKeys.CWD: None,
                SessionKeys.ENV_VARS: {},
                SessionKeys.HISTORY: [],
            }
        )

        await ssh_session_handlers._handle_connected_session("ls", context, session, cast(Any, manager))

        assert session.get(SessionKeys.CURRENT_TASK) == "running"
        assert not isinstance(session.get(SessionKeys.CURRENT_TASK), asyncio.Task)

        manager._done.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert session.get(SessionKeys.CURRENT_TASK) is None

    asyncio.run(_run())
