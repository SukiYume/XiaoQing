"""测试 Minecraft 服务器通信插件"""

import pytest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock
import json
from typing import Any, cast

from plugins.minecraft import main as mc_main
from plugins.minecraft.rcon import PacketType, RconClient, RconPacket
from core.interfaces import PluginContextProtocol

ROOT = Path(__file__).resolve().parent.parent.parent


class TestMinecraftPlugin:
    """测试 Minecraft 插件"""

    def test_init(self):
        """测试插件初始化"""
        main_file = ROOT / "plugins" / "minecraft" / "main.py"
        content = main_file.read_text(encoding='utf-8')
        assert "def init" in content

    def test_help_text(self):
        """测试帮助文本"""
        main_file = ROOT / "plugins" / "minecraft" / "main.py"
        content = main_file.read_text(encoding='utf-8')
        assert "def _show_help" in content
        assert "Minecraft" in content or "RCON" in content


class TestMinecraftCommands:
    """测试 Minecraft 命令处理"""

    def test_handle_function(self):
        """测试 handle 函数"""
        main_file = ROOT / "plugins" / "minecraft" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "async def handle" in content
        assert "command" in content
        assert "args" in content

    def test_help_command(self):
        """测试帮助命令"""
        main_file = ROOT / "plugins" / "minecraft" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "help" in content
        assert "帮助" in content

    def test_connect_command(self):
        """测试连接命令"""
        main_file = ROOT / "plugins" / "minecraft" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "connect" in content
        assert "连接" in content

    def test_disconnect_command(self):
        """测试断开命令"""
        main_file = ROOT / "plugins" / "minecraft" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "disconnect" in content
        assert "断开" in content

    def test_status_command(self):
        """测试状态命令"""
        main_file = ROOT / "plugins" / "minecraft" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "status" in content
        assert "状态" in content


class TestMinecraftConnection:
    """测试 Minecraft 连接管理"""

    def test_connection_module(self):
        """测试连接模块"""
        connection_file = ROOT / "plugins" / "minecraft" / "connection.py"
        assert connection_file.exists()

    def test_mc_connection_class(self):
        """测试 McConnection 类"""
        connection_file = ROOT / "plugins" / "minecraft" / "connection.py"
        content = connection_file.read_text(encoding='utf-8')

        assert "class McConnection" in content
        assert "@dataclass" in content

    def test_connection_manager_class(self):
        """测试 ConnectionManager 类"""
        connection_file = ROOT / "plugins" / "minecraft" / "connection.py"
        content = connection_file.read_text(encoding='utf-8')

        assert "class ConnectionManager" in content
        assert "def get_connection" in content
        assert "def add_connection" in content
        assert "def remove_connection" in content
        assert "def has_connection" in content

    def test_connection_key_generation(self):
        """测试连接键生成"""
        connection_file = ROOT / "plugins" / "minecraft" / "connection.py"
        content = connection_file.read_text(encoding='utf-8')

        assert "connection_key" in content
        assert "target_type" in content
        assert "target_id" in content


class TestMinecraftRcon:
    """测试 Minecraft RCON 模块"""

    def test_rcon_module(self):
        """测试 RCON 模块存在"""
        rcon_file = ROOT / "plugins" / "minecraft" / "rcon.py"
        assert rcon_file.exists()

    def test_rcon_client_class(self):
        """测试 RconClient 类"""
        rcon_file = ROOT / "plugins" / "minecraft" / "rcon.py"
        content = rcon_file.read_text(encoding='utf-8')

        assert "class RconClient" in content
        assert "def connect" in content
        assert "def disconnect" in content
        assert "def command" in content

    def test_rcon_packet(self):
        """测试 RCON 数据包"""
        rcon_file = ROOT / "plugins" / "minecraft" / "rcon.py"
        content = rcon_file.read_text(encoding='utf-8')

        assert "class RconPacket" in content
        assert "PacketType" in content
        assert "def encode" in content
        assert "def decode" in content

    def test_packet_types(self):
        """测试数据包类型"""
        rcon_file = ROOT / "plugins" / "minecraft" / "rcon.py"
        content = rcon_file.read_text(encoding='utf-8')

        assert "RESPONSE" in content
        assert "COMMAND" in content
        assert "LOGIN" in content

    def test_rcon_protocol(self):
        """测试 RCON 协议实现"""
        rcon_file = ROOT / "plugins" / "minecraft" / "rcon.py"
        content = rcon_file.read_text(encoding='utf-8')

        # 检查协议相关
        assert "struct" in content
        assert "asyncio" in content
        assert "open_connection" in content or "Stream" in content


class TestMinecraftLogMonitor:
    """测试 Minecraft 日志监控"""

    def test_log_monitor_module(self):
        """测试日志监控模块存在"""
        log_monitor_file = ROOT / "plugins" / "minecraft" / "log_monitor.py"
        assert log_monitor_file.exists()

    def test_log_monitor_class(self):
        """测试 LogMonitor 类"""
        log_monitor_file = ROOT / "plugins" / "minecraft" / "log_monitor.py"
        content = log_monitor_file.read_text(encoding='utf-8')

        assert "class LogMonitor" in content
        assert "def initialize" in content
        assert "def check_updates" in content

    def test_log_event_types(self):
        """测试日志事件类型"""
        log_monitor_file = ROOT / "plugins" / "minecraft" / "log_monitor.py"
        content = log_monitor_file.read_text(encoding='utf-8')

        assert "class LogEventType" in content
        assert "CHAT" in content
        assert "JOIN" in content
        assert "LEAVE" in content
        assert "DEATH" in content
        assert "ADVANCEMENT" in content

    def test_log_patterns(self):
        """测试日志匹配模式"""
        log_monitor_file = ROOT / "plugins" / "minecraft" / "log_monitor.py"
        content = log_monitor_file.read_text(encoding='utf-8')

        assert "CHAT_PATTERN" in content
        assert "JOIN_PATTERN" in content
        assert "LEAVE_PATTERN" in content
        assert "DEATH_PATTERNS" in content
        assert "ADVANCEMENT_PATTERN" in content

    def test_log_event_class(self):
        """测试 LogEvent 类"""
        log_monitor_file = ROOT / "plugins" / "minecraft" / "log_monitor.py"
        content = log_monitor_file.read_text(encoding='utf-8')

        assert "class LogEvent" in content
        assert "event_type" in content
        assert "player" in content
        assert "message" in content


class TestMinecraftScheduled:
    """测试 Minecraft 定时任务"""

    def test_scheduled_function(self):
        """测试定时任务函数"""
        main_file = ROOT / "plugins" / "minecraft" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "async def scheduled" in content
        assert "check_updates" in content

    def test_event_formatting(self):
        """测试事件格式化"""
        main_file = ROOT / "plugins" / "minecraft" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "_format_event_message" in content
        assert "LogEventType" in content


class TestMinecraftPluginJson:
    """测试 Minecraft plugin.json 配置"""

    def test_plugin_json_exists(self):
        """测试 plugin.json 存在"""
        plugin_json = ROOT / "plugins" / "minecraft" / "plugin.json"
        assert plugin_json.exists()

    def test_plugin_json_content(self):
        """测试 plugin.json 内容"""
        plugin_json = ROOT / "plugins" / "minecraft" / "plugin.json"
        content = json.loads(plugin_json.read_text(encoding='utf-8'))

        assert content["name"] == "minecraft"
        assert "commands" in content
        assert "schedule" in content

    def test_command_triggers(self):
        """测试命令触发器"""
        plugin_json = ROOT / "plugins" / "minecraft" / "plugin.json"
        content = json.loads(plugin_json.read_text(encoding='utf-8'))

        mc_cmd = next((cmd for cmd in content["commands"] if cmd["name"] == "mc"), None)
        assert mc_cmd is not None
        assert "mc" in mc_cmd["triggers"]
        assert "minecraft" in mc_cmd["triggers"]

    def test_legacy_commands(self):
        """测试旧命令存在"""
        plugin_json = ROOT / "plugins" / "minecraft" / "plugin.json"
        content = json.loads(plugin_json.read_text(encoding='utf-8'))

        commands = [cmd["name"] for cmd in content["commands"]]
        assert "mcconnect" in commands
        assert "mcdisconnect" in commands

    def test_schedule_config(self):
        """测试定时任务配置"""
        plugin_json = ROOT / "plugins" / "minecraft" / "plugin.json"
        content = json.loads(plugin_json.read_text(encoding='utf-8'))

        assert "schedule" in content
        assert len(content["schedule"]) > 0

        # 检查日志检查任务
        log_task = next((s for s in content["schedule"] if s["id"] == "check_log"), None)
        assert log_task is not None
        assert log_task["handler"] == "scheduled"


class TestMinecraftIntegration:
    """集成测试"""

    def test_module_files_exist(self):
        """测试模块文件存在"""
        assert (ROOT / "plugins" / "minecraft" / "main.py").exists()
        assert (ROOT / "plugins" / "minecraft" / "rcon.py").exists()
        assert (ROOT / "plugins" / "minecraft" / "connection.py").exists()
        assert (ROOT / "plugins" / "minecraft" / "log_monitor.py").exists()

    def test_main_functions(self):
        """测试主模块函数"""
        main_file = ROOT / "plugins" / "minecraft" / "main.py"
        content = main_file.read_text(encoding='utf-8')
        # 检查关键函数存在
        assert "def init" in content
        assert "async def handle" in content
        assert "def _show_help" in content
        assert "async def scheduled" in content

    def test_rcon_exports(self):
        """测试 RCON 模块导出"""
        rcon_file = ROOT / "plugins" / "minecraft" / "rcon.py"
        content = rcon_file.read_text(encoding='utf-8')
        # 检查关键类存在
        assert "class RconClient" in content
        assert "class RconPacket" in content
        assert "class PacketType" in content or "PacketType" in content

    def test_connection_exports(self):
        """测试连接模块导出"""
        connection_file = ROOT / "plugins" / "minecraft" / "connection.py"
        content = connection_file.read_text(encoding='utf-8')
        # 检查关键类存在
        assert "class McConnection" in content
        assert "class ConnectionManager" in content

    def test_log_monitor_exports(self):
        """测试日志监控模块导出"""
        log_monitor_file = ROOT / "plugins" / "minecraft" / "log_monitor.py"
        content = log_monitor_file.read_text(encoding='utf-8')
        # 检查关键类存在
        assert "class LogMonitor" in content
        assert "class LogEventType" in content
        assert "class LogEvent" in content


class _MinecraftTestContext:
    def __init__(self, secrets=None, plugin_dir=None):
        self.secrets = secrets or {}
        self.plugin_dir = plugin_dir or Path(".")


class _FakeRconClient:
    def __init__(self, host, port, password):
        self.host = host
        self.port = port
        self.password = password

    async def connect(self):
        return True


class _FakeDisconnectRcon:
    def __init__(self):
        self.disconnected = False

    async def disconnect(self):
        self.disconnected = True


@pytest.mark.asyncio
async def test_mc_connect_default_reads_secrets_config(monkeypatch, tmp_path):
    monkeypatch.setattr(mc_main, "_manager", mc_main.ConnectionManager())
    monkeypatch.setattr(mc_main, "RconClient", _FakeRconClient)

    # Write a config.json in the temp plugin dir
    config_data = {
        "default": {
            "host": "127.0.0.1",
            "port": 25575,
            "password": "secret-pass",
            "log_file": "",
        }
    }
    (tmp_path / "config.json").write_text(json.dumps(config_data), encoding="utf-8")

    context = cast(
        PluginContextProtocol,
        cast(object, _MinecraftTestContext(plugin_dir=tmp_path)),
    )

    msg = await mc_main._handle_connect("default", group_id=None, user_id=10001, context=context)
    text = msg[0]["data"]["text"]

    assert "已连接到 127.0.0.1:25575" in text


@pytest.mark.asyncio
async def test_mc_connect_default_missing_config_returns_usage(monkeypatch, tmp_path):
    monkeypatch.setattr(mc_main, "_manager", mc_main.ConnectionManager())
    monkeypatch.setattr(mc_main, "RconClient", _FakeRconClient)

    # No config.json in tmp_path, so it should return usage
    context = cast(
        PluginContextProtocol,
        cast(object, _MinecraftTestContext(plugin_dir=tmp_path)),
    )
    msg = await mc_main._handle_connect("default", group_id=None, user_id=10002, context=context)
    text = msg[0]["data"]["text"]

    assert "config.json" in text


@pytest.mark.asyncio
async def test_shutdown_cleans_all_mc_connections(monkeypatch):
    manager = mc_main.ConnectionManager()
    fake_rcon = _FakeDisconnectRcon()
    conn = mc_main.McConnection(
        host="127.0.0.1",
        port=25575,
        password="p",
        log_file="",
        target_type="private",
        target_id=10003,
        rcon_client=fake_rcon,
        log_monitor=None,
    )
    manager.add_connection(conn)
    monkeypatch.setattr(mc_main, "_manager", manager)

    await mc_main.shutdown(None)

    assert fake_rcon.disconnected is True
    assert manager.connection_count() == 0


class _FakeRconReader:
    def __init__(self, payload: bytes):
        self._payload = payload

    async def readexactly(self, n: int) -> bytes:
        if n > len(self._payload):
            raise asyncio.IncompleteReadError(partial=self._payload, expected=n)
        chunk = self._payload[:n]
        self._payload = self._payload[n:]
        return chunk


class _FakeRconWriter:
    def __init__(self):
        self.written = b""

    def write(self, data: bytes):
        self.written += data

    async def drain(self):
        return None

    def is_closing(self) -> bool:
        return False


def test_rcon_send_packet_reads_full_large_response():
    large_payload = "x" * 5000
    response_packet = RconPacket(request_id=1, packet_type=PacketType.RESPONSE, payload=large_payload)
    encoded = response_packet.encode()

    client = RconClient("127.0.0.1", 25575, "pw")
    client._reader = cast(Any, _FakeRconReader(encoded))
    client._writer = cast(Any, _FakeRconWriter())

    response = asyncio.run(client._send_packet(PacketType.COMMAND, "list"))

    assert response.payload == large_payload
