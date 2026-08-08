"""Minecraft 插件的命令、配置、连接和日志游标契约。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, ClassVar, cast
from unittest.mock import AsyncMock

import pytest

from core.interfaces import DeliveryTarget, PluginContextProtocol
from plugins.minecraft import main as mc_main
from plugins.minecraft.connection import ConnectionManager, McConnection
from plugins.minecraft.log_monitor import LogBatch, LogEventType, LogMonitor
from plugins.minecraft.rcon import (
    RconCommandResult,
    RconConnectResult,
    RconErrorKind,
)
from tests.helpers.settings_snapshot import settings_snapshot

ROOT = Path(__file__).resolve().parent.parent.parent
PRIVATE_TARGET = DeliveryTarget("private", 10001)
GROUP_TARGET = DeliveryTarget("group", 20001)


class _Context:
    def __init__(
        self,
        plugin_dir: Path,
        *,
        password: Any = "secret-pass",
        profile: str = "default",
    ) -> None:
        self.plugin_dir = plugin_dir
        self.data_dir = plugin_dir / "runtime-data"
        self.request_id = "mc-test-request"
        self.send_action = AsyncMock(return_value=True)
        self.secrets = {"plugins": {"minecraft": {profile: password}}}

    def get_settings_snapshot(self):
        return settings_snapshot(secrets=self.secrets)


class _FakeRconClient:
    instances: ClassVar[list[_FakeRconClient]] = []
    next_connect_result = RconConnectResult(success=True)

    def __init__(self, host: str, port: int, password: str) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.connected = False
        self.disconnected = False
        self.commands: list[str] = []
        self.command_result = RconCommandResult(success=True, response="ok")
        type(self).instances.append(self)

    async def connect(self) -> RconConnectResult:
        result = type(self).next_connect_result
        self.connected = result.success
        return result

    async def command(self, command: str) -> RconCommandResult:
        self.commands.append(command)
        return self.command_result

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnected = True


class _FailingCleanupClient:
    async def disconnect(self) -> None:
        raise RuntimeError("close failed")


@pytest.fixture(autouse=True)
def reset_minecraft_runtime(monkeypatch: pytest.MonkeyPatch) -> ConnectionManager:
    manager = ConnectionManager()
    monkeypatch.setattr(mc_main, "_manager", manager)
    monkeypatch.setattr(mc_main, "_schedule_lock", asyncio.Lock())
    mc_main._event_buckets.clear()  # noqa: SLF001 - 隔离进程内限流状态。
    mc_main._delivery_cursor = 0  # noqa: SLF001
    _FakeRconClient.instances.clear()
    _FakeRconClient.next_connect_result = RconConnectResult(success=True)
    yield manager
    mc_main._event_buckets.clear()  # noqa: SLF001


def _context(
    tmp_path: Path,
    *,
    password: Any = "secret-pass",
    profile: str = "default",
) -> PluginContextProtocol:
    return cast(
        PluginContextProtocol,
        cast(object, _Context(tmp_path, password=password, profile=profile)),
    )


def _write_config(tmp_path: Path, server: dict[str, Any], *, profile: str = "default") -> None:
    (tmp_path / "config.json").write_text(
        json.dumps({profile: server}),
        encoding="utf-8",
    )


def _server_config(**overrides: Any) -> dict[str, Any]:
    server: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 25575,
        "log_file": "",
    }
    server.update(overrides)
    return server


def _connection(
    target: DeliveryTarget,
    *,
    client: Any = None,
    monitor: Any = None,
    host: str = "127.0.0.1",
    port: int = 25575,
) -> McConnection:
    return McConnection(
        host=host,
        port=port,
        target=target,
        rcon_client=cast(Any, client),
        log_monitor=cast(Any, monitor),
    )


class TestMinecraftMetadata:
    def test_entrypoints_and_help_match_rcon_scope(self) -> None:
        assert callable(mc_main.handle)
        assert callable(mc_main.scheduled)
        help_text = mc_main._show_help()
        assert "Minecraft RCON" in help_text
        assert "/mc connect <配置名>" in help_text
        assert "/mc say <消息>" in help_text
        assert "/mc say 大家好" in help_text
        assert "/mc tell <玩家名> <消息>" in help_text
        assert "玩家聊天、加入和离开等事件会转发到当前 QQ 私聊" in help_text
        assert "start" not in help_text

    def test_manifest_commands_are_admin_only_and_scheduled(self) -> None:
        content = json.loads(
            (ROOT / "plugins" / "minecraft" / "plugin.json").read_text(encoding="utf-8")
        )
        assert content["name"] == "minecraft"
        assert all(command["admin_only"] is True for command in content["commands"])
        assert {command["name"] for command in content["commands"]} == {
            "mc",
            "mcconnect",
            "mcdisconnect",
        }
        schedule = next(item for item in content["schedule"] if item["id"] == "check_log")
        assert schedule["handler"] == "scheduled"

    def test_removed_test_only_connection_wrappers_stay_absent(self) -> None:
        for name in ("add_connection", "remove_connection", "has_connection", "connection_count"):
            assert not hasattr(ConnectionManager, name)


class TestMinecraftConfiguration:
    @pytest.mark.asyncio
    async def test_connect_reads_local_profile_without_exposing_password(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        reset_minecraft_runtime: ConnectionManager,
    ) -> None:
        _write_config(tmp_path, _server_config())
        monkeypatch.setattr(mc_main, "RconClient", _FakeRconClient)

        result = await mc_main._handle_connect("default", PRIVATE_TARGET, _context(tmp_path))

        text = result[0]["data"]["text"]
        conn = reset_minecraft_runtime.get_connection(PRIVATE_TARGET)
        assert "已连接到 127.0.0.1:25575" in text
        assert "secret-pass" not in text
        assert conn is not None
        assert not hasattr(conn, "password")
        assert not hasattr(conn, "log_file")
        assert _FakeRconClient.instances[0].password == "secret-pass"

    @pytest.mark.asyncio
    async def test_missing_config_returns_fixed_error(self, tmp_path: Path) -> None:
        result = await mc_main._handle_connect("default", PRIVATE_TARGET, _context(tmp_path))
        assert "未找到 config.json" in result[0]["data"]["text"]

    @pytest.mark.asyncio
    async def test_connect_without_profile_returns_usage(self, tmp_path: Path) -> None:
        result = await mc_main._handle_connect("", PRIVATE_TARGET, _context(tmp_path))
        assert "用法: /mc connect <配置名>" in result[0]["data"]["text"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("profile", ["default C:/sensitive.log", "服务器"])
    async def test_invalid_profile_name_is_rejected(
        self,
        tmp_path: Path,
        profile: str,
    ) -> None:
        _write_config(tmp_path, _server_config())
        result = await mc_main._handle_connect(
            profile,
            PRIVATE_TARGET,
            _context(tmp_path),
        )
        assert "配置名必须" in result[0]["data"]["text"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "server, expected",
        [
            (_server_config(host=" bad"), "host 无效"),
            (_server_config(port=True), "port 必须"),
            (_server_config(port=0), "port 必须"),
            (_server_config(port=65536), "port 必须"),
            (_server_config(log_file=7), "log_file 无效"),
            (_server_config(log_file=" bad.log"), "log_file 无效"),
            (_server_config(log_file="bad\0.log"), "log_file 无效"),
        ],
    )
    async def test_invalid_profile_fields_are_rejected(
        self,
        tmp_path: Path,
        server: dict[str, Any],
        expected: str,
    ) -> None:
        _write_config(tmp_path, server)
        result = await mc_main._handle_connect("default", PRIVATE_TARGET, _context(tmp_path))
        assert expected in result[0]["data"]["text"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "password",
        ["", "bad\0secret", pytest.param("x" * 4097, id="too-long"), 7],
    )
    async def test_invalid_secret_values_are_rejected(
        self,
        tmp_path: Path,
        password: Any,
    ) -> None:
        _write_config(tmp_path, _server_config())
        result = await mc_main._handle_connect(
            "default",
            PRIVATE_TARGET,
            _context(tmp_path, password=password),
        )
        assert "password 为空" in result[0]["data"]["text"]

    @pytest.mark.asyncio
    async def test_public_profile_cannot_contain_password(self, tmp_path: Path) -> None:
        _write_config(tmp_path, _server_config(password="legacy-password"))
        result = await mc_main._handle_connect("default", PRIVATE_TARGET, _context(tmp_path))
        assert "必须写入 config/secrets.json" in result[0]["data"]["text"]

    @pytest.mark.asyncio
    async def test_missing_secret_is_rejected(self, tmp_path: Path) -> None:
        _write_config(tmp_path, _server_config())
        result = await mc_main._handle_connect(
            "default",
            PRIVATE_TARGET,
            _context(tmp_path, password=None),
        )
        assert "未找到 config/secrets.json" in result[0]["data"]["text"]

    @pytest.mark.asyncio
    async def test_non_object_and_invalid_json_are_rejected(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        config_path.write_text("[]", encoding="utf-8")
        non_object = await mc_main._handle_connect("default", PRIVATE_TARGET, _context(tmp_path))
        config_path.write_text("{invalid", encoding="utf-8")
        malformed = await mc_main._handle_connect("default", PRIVATE_TARGET, _context(tmp_path))
        assert "顶层必须是对象" in non_object[0]["data"]["text"]
        assert "不是有效 JSON" in malformed[0]["data"]["text"]

    @pytest.mark.asyncio
    async def test_missing_profile_and_oversized_config_are_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text("{}", encoding="utf-8")
        missing = await mc_main._handle_connect("default", PRIVATE_TARGET, _context(tmp_path))
        (tmp_path / "config.json").write_bytes(b" " * (mc_main.MC_MAX_CONFIG_BYTES + 1))
        oversized = await mc_main._handle_connect("default", PRIVATE_TARGET, _context(tmp_path))
        assert "未找到指定服务器配置" in missing[0]["data"]["text"]
        assert "64 KiB" in oversized[0]["data"]["text"]

    @pytest.mark.asyncio
    async def test_invalid_optional_log_path_does_not_block_rcon(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        reset_minecraft_runtime: ConnectionManager,
    ) -> None:
        _write_config(tmp_path, _server_config(log_file="missing.log"))
        monkeypatch.setattr(mc_main, "RconClient", _FakeRconClient)

        result = await mc_main._handle_connect("default", PRIVATE_TARGET, _context(tmp_path))

        conn = reset_minecraft_runtime.get_connection(PRIVATE_TARGET)
        assert "仅连接 RCON" in result[0]["data"]["text"]
        assert conn is not None and conn.log_monitor is None
        assert _FakeRconClient.instances[0].connected is True

    @pytest.mark.asyncio
    async def test_relative_log_path_is_resolved_from_plugin_directory(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        reset_minecraft_runtime: ConnectionManager,
    ) -> None:
        log_path = tmp_path / "latest.log"
        log_path.write_text("old history\n", encoding="utf-8")
        _write_config(tmp_path, _server_config(log_file="latest.log"))
        monkeypatch.setattr(mc_main, "RconClient", _FakeRconClient)

        result = await mc_main._handle_connect("default", GROUP_TARGET, _context(tmp_path))

        conn = reset_minecraft_runtime.get_connection(GROUP_TARGET)
        assert "已启用" in result[0]["data"]["text"]
        assert conn is not None and conn.log_monitor is not None
        assert conn.log_monitor.log_path == log_path.resolve()
        assert conn.log_monitor._last_position == log_path.stat().st_size  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_typed_connect_failure_is_returned_without_publishing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        reset_minecraft_runtime: ConnectionManager,
    ) -> None:
        _write_config(tmp_path, _server_config())
        _FakeRconClient.next_connect_result = RconConnectResult(
            False,
            error_kind=RconErrorKind.AUTH,
            error_message="RCON 认证失败，请检查密码",
        )
        monkeypatch.setattr(mc_main, "RconClient", _FakeRconClient)

        result = await mc_main._handle_connect("default", PRIVATE_TARGET, _context(tmp_path))

        assert "认证失败" in result[0]["data"]["text"]
        assert reset_minecraft_runtime.get_connection(PRIVATE_TARGET) is None

    @pytest.mark.asyncio
    async def test_connect_constructor_error_is_fixed_and_not_published(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        reset_minecraft_runtime: ConnectionManager,
    ) -> None:
        _write_config(tmp_path, _server_config())

        def explode(*_args: Any) -> Any:
            raise RuntimeError("SECRET-CONSTRUCTOR-DETAIL")

        monkeypatch.setattr(mc_main, "RconClient", explode)
        result = await mc_main._handle_connect("default", PRIVATE_TARGET, _context(tmp_path))
        text = result[0]["data"]["text"]
        assert text == "❌ RCON 连接初始化失败"
        assert "SECRET" not in text
        assert reset_minecraft_runtime.get_connection(PRIVATE_TARGET) is None

    @pytest.mark.asyncio
    async def test_log_monitor_setup_error_falls_back_to_rcon_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "latest.log").write_bytes(b"")
        _write_config(tmp_path, _server_config(log_file="latest.log"))
        monkeypatch.setattr(mc_main, "RconClient", _FakeRconClient)

        def explode(*_args: Any, **_kwargs: Any) -> Any:
            raise OSError("cursor init failed")

        monkeypatch.setattr(mc_main, "LogMonitor", explode)
        result = await mc_main._handle_connect("default", PRIVATE_TARGET, _context(tmp_path))
        assert "仅连接 RCON" in result[0]["data"]["text"]


class TestMinecraftCommands:
    def test_target_resolution_prefers_group_and_rejects_invalid_ids(self) -> None:
        assert mc_main._target_from_event({"group_id": 3, "user_id": 4}) == DeliveryTarget(
            "group", 3
        )
        assert mc_main._target_from_event({"user_id": 4}) == DeliveryTarget("private", 4)
        assert mc_main._target_from_event({"group_id": 0, "user_id": 4}) is None
        assert mc_main._target_from_event({"user_id": True}) is None
        assert mc_main._target_from_event({}) is None

    @pytest.mark.asyncio
    async def test_help_does_not_require_event_identity(self, tmp_path: Path) -> None:
        result = await mc_main.handle("mc", "help", {}, _context(tmp_path))
        assert "Minecraft RCON" in result[0]["data"]["text"]

    @pytest.mark.asyncio
    async def test_legacy_connect_and_disconnect_route_to_same_target(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        reset_minecraft_runtime: ConnectionManager,
    ) -> None:
        _write_config(tmp_path, _server_config())
        monkeypatch.setattr(mc_main, "RconClient", _FakeRconClient)
        context = _context(tmp_path)

        connected = await mc_main.handle("mcconnect", "default", {"user_id": 9}, context)
        status = await mc_main.handle("mc", "status", {"user_id": 9}, context)
        disconnected = await mc_main.handle("mcdisconnect", "", {"user_id": 9}, context)

        assert "已连接" in connected[0]["data"]["text"]
        assert "连接状态" in status[0]["data"]["text"]
        assert "已断开" in disconnected[0]["data"]["text"]
        assert reset_minecraft_runtime.all_connections() == []

    @pytest.mark.asyncio
    async def test_primary_mc_entry_routes_connect_command_and_disconnect(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _write_config(tmp_path, _server_config())
        monkeypatch.setattr(mc_main, "RconClient", _FakeRconClient)
        context = _context(tmp_path)

        connected = await mc_main.handle("mc", "connect default", {"user_id": 10}, context)
        client = _FakeRconClient.instances[0]
        client.command_result = RconCommandResult(True, response="players: 1")
        command = await mc_main.handle("mc", "list", {"user_id": 10}, context)
        disconnected = await mc_main.handle("mc", "disconnect", {"user_id": 10}, context)

        assert "已连接" in connected[0]["data"]["text"]
        assert "players: 1" in command[0]["data"]["text"]
        assert "已断开" in disconnected[0]["data"]["text"]

    @pytest.mark.asyncio
    async def test_connect_cancellation_propagates(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _write_config(tmp_path, _server_config())

        class CancelConnect(_FakeRconClient):
            async def connect(self) -> RconConnectResult:
                raise asyncio.CancelledError

        monkeypatch.setattr(mc_main, "RconClient", CancelConnect)
        with pytest.raises(asyncio.CancelledError):
            await mc_main._handle_connect("default", PRIVATE_TARGET, _context(tmp_path))

    @pytest.mark.asyncio
    async def test_unknown_command_and_invalid_target_are_fixed_errors(
        self, tmp_path: Path
    ) -> None:
        context = _context(tmp_path)
        unknown = await mc_main.handle("other", "", {"user_id": 9}, context)
        invalid = await mc_main.handle("mc", "status", {"user_id": 0}, context)
        assert unknown[0]["data"]["text"] == "未知命令"
        assert "无法识别" in invalid[0]["data"]["text"]

    @pytest.mark.asyncio
    async def test_legacy_command_rejects_invalid_target(self, tmp_path: Path) -> None:
        result = await mc_main.handle("mcconnect", "default", {"user_id": 0}, _context(tmp_path))
        assert "无法识别" in result[0]["data"]["text"]

    @pytest.mark.asyncio
    async def test_command_success_empty_failure_and_target_isolation(
        self,
        reset_minecraft_runtime: ConnectionManager,
        tmp_path: Path,
    ) -> None:
        private_client = _FakeRconClient("private", 25575, "p")
        group_client = _FakeRconClient("group", 25576, "p")
        private_client.command_result = RconCommandResult(True, response="players: 3")
        group_client.command_result = RconCommandResult(True, response="")
        await reset_minecraft_runtime.replace_connection(
            _connection(PRIVATE_TARGET, client=private_client)
        )
        await reset_minecraft_runtime.replace_connection(
            _connection(GROUP_TARGET, client=group_client)
        )
        context = _context(tmp_path)

        private = await mc_main._handle_mc_command("list", PRIVATE_TARGET, context)
        empty = await mc_main._handle_mc_command("save-all", GROUP_TARGET, context)
        private_client.command_result = RconCommandResult(
            False,
            error_kind=RconErrorKind.TIMEOUT,
            error_message="RCON 操作超时，未收到完整响应",
        )
        failed = await mc_main._handle_mc_command("list", PRIVATE_TARGET, context)

        assert "players: 3" in private[0]["data"]["text"]
        assert "空响应" in empty[0]["data"]["text"]
        assert "操作超时" in failed[0]["data"]["text"]
        assert private_client.commands == ["list", "list"]
        assert group_client.commands == ["save-all"]

    @pytest.mark.asyncio
    async def test_command_requires_connection_client_and_nonempty_input(
        self,
        reset_minecraft_runtime: ConnectionManager,
        tmp_path: Path,
    ) -> None:
        context = _context(tmp_path)
        missing = await mc_main._handle_mc_command("list", PRIVATE_TARGET, context)
        await reset_minecraft_runtime.replace_connection(_connection(PRIVATE_TARGET))
        closed = await mc_main._handle_mc_command("list", PRIVATE_TARGET, context)
        client = _FakeRconClient("private", 25575, "p")
        await reset_minecraft_runtime.replace_connection(_connection(PRIVATE_TARGET, client=client))
        empty = await mc_main._handle_mc_command("   ", PRIVATE_TARGET, context)
        assert "未连接" in missing[0]["data"]["text"]
        assert "已关闭" in closed[0]["data"]["text"]
        assert "请提供" in empty[0]["data"]["text"]

    @pytest.mark.asyncio
    async def test_missing_disconnect_and_status_are_explicit(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        disconnected = await mc_main._handle_disconnect(PRIVATE_TARGET, context)
        status = mc_main._handle_status_command(PRIVATE_TARGET, context)
        assert "当前无连接" in disconnected[0]["data"]["text"]
        assert "未连接到任何服务器" in status[0]["data"]["text"]

    @pytest.mark.asyncio
    async def test_command_exception_is_fixed_and_cancellation_propagates(
        self,
        reset_minecraft_runtime: ConnectionManager,
        tmp_path: Path,
    ) -> None:
        class ExplodingClient:
            async def command(self, _command: str) -> RconCommandResult:
                raise RuntimeError("SECRET-COMMAND-DETAIL")

        await reset_minecraft_runtime.replace_connection(
            _connection(PRIVATE_TARGET, client=cast(Any, ExplodingClient()))
        )
        failed = await mc_main._handle_mc_command("list", PRIVATE_TARGET, _context(tmp_path))
        assert failed[0]["data"]["text"] == "❌ RCON 命令执行失败，请重新连接后重试"

        class CancelledClient:
            async def command(self, _command: str) -> RconCommandResult:
                raise asyncio.CancelledError

        await reset_minecraft_runtime.replace_connection(
            _connection(PRIVATE_TARGET, client=cast(Any, CancelledClient()))
        )
        with pytest.raises(asyncio.CancelledError):
            await mc_main._handle_mc_command("list", PRIVATE_TARGET, _context(tmp_path))

    @pytest.mark.asyncio
    async def test_response_strips_controls_and_obeys_dual_budget(
        self,
        reset_minecraft_runtime: ConnectionManager,
        tmp_path: Path,
    ) -> None:
        client = _FakeRconClient("private", 25575, "p")
        client.command_result = RconCommandResult(
            True,
            response="\x1b[31mSECRET\x1b[0m\x00" + "🧱" * 5000,
        )
        await reset_minecraft_runtime.replace_connection(_connection(PRIVATE_TARGET, client=client))

        result = await mc_main._handle_mc_command("list", PRIVATE_TARGET, _context(tmp_path))
        text = result[0]["data"]["text"]

        assert "\x1b" not in text and "\x00" not in text
        assert "响应已截断" in text
        assert len(text) <= mc_main.MC_MAX_RESPONSE_CHARS + 2
        assert len(text.encode("utf-8")) <= mc_main.MC_MAX_RESPONSE_BYTES + len("📤 ".encode())

    @pytest.mark.asyncio
    async def test_truncated_rcon_response_is_labeled(
        self,
        reset_minecraft_runtime: ConnectionManager,
        tmp_path: Path,
    ) -> None:
        client = _FakeRconClient("private", 25575, "p")
        client.command_result = RconCommandResult(
            True,
            response="partial response",
            truncated=True,
        )
        await reset_minecraft_runtime.replace_connection(_connection(PRIVATE_TARGET, client=client))

        result = await mc_main._handle_mc_command("list", PRIVATE_TARGET, _context(tmp_path))

        assert "响应可能不完整" in result[0]["data"]["text"]

    @pytest.mark.asyncio
    async def test_unexpected_errors_never_echo_exception_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        async def explode(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("TOP-SECRET-INTERNAL")

        monkeypatch.setattr(mc_main, "_dispatch_command", explode)
        result = await mc_main.handle("mc", "status", {"user_id": 1}, _context(tmp_path))
        text = result[0]["data"]["text"]
        assert "处理 Minecraft 请求失败" in text
        assert "TOP-SECRET-INTERNAL" not in text

    @pytest.mark.asyncio
    async def test_handle_cancellation_propagates(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        async def cancel(*_args: Any, **_kwargs: Any) -> Any:
            raise asyncio.CancelledError

        monkeypatch.setattr(mc_main, "_dispatch_command", cancel)
        with pytest.raises(asyncio.CancelledError):
            await mc_main.handle("mc", "status", {"user_id": 1}, _context(tmp_path))


class TestConnectionManager:
    @pytest.mark.asyncio
    async def test_replace_is_atomic_and_closes_old_client(self) -> None:
        manager = ConnectionManager()
        old_client = _FakeRconClient("old", 25575, "p")
        new_client = _FakeRconClient("new", 25575, "p")
        old = _connection(PRIVATE_TARGET, client=old_client, host="old")
        new = _connection(PRIVATE_TARGET, client=new_client, host="new")
        await manager.replace_connection(old)

        replaced = await manager.replace_connection(new)

        assert replaced is old
        assert old_client.disconnected is True
        assert old.rcon_client is None
        assert manager.get_connection(PRIVATE_TARGET) is new

    @pytest.mark.asyncio
    async def test_disconnect_missing_and_snapshot_are_safe(self) -> None:
        manager = ConnectionManager()
        conn = _connection(PRIVATE_TARGET)
        await manager.replace_connection(conn)
        snapshot = manager.all_connections()
        snapshot.clear()
        assert manager.get_connection(PRIVATE_TARGET) is conn
        assert await manager.disconnect_connection(GROUP_TARGET) is None
        assert await manager.disconnect_connection(PRIVATE_TARGET) is conn

    @pytest.mark.asyncio
    async def test_cleanup_all_continues_after_one_client_fails(self) -> None:
        manager = ConnectionManager()
        healthy = _FakeRconClient("healthy", 25575, "p")
        await manager.replace_connection(
            _connection(PRIVATE_TARGET, client=cast(Any, _FailingCleanupClient()))
        )
        await manager.replace_connection(_connection(GROUP_TARGET, client=healthy))

        await manager.cleanup_all()

        assert healthy.disconnected is True
        assert manager.all_connections() == []

    @pytest.mark.asyncio
    async def test_cleanup_cancellation_propagates_after_registry_is_cleared(self) -> None:
        class CancelCleanupClient:
            async def disconnect(self) -> None:
                raise asyncio.CancelledError

        manager = ConnectionManager()
        await manager.replace_connection(
            _connection(PRIVATE_TARGET, client=cast(Any, CancelCleanupClient()))
        )
        with pytest.raises(asyncio.CancelledError):
            await manager.cleanup_all()
        assert manager.all_connections() == []

    @pytest.mark.asyncio
    async def test_shutdown_clears_connections_and_rate_state(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = ConnectionManager()
        client = _FakeRconClient("host", 25575, "p")
        await manager.replace_connection(_connection(PRIVATE_TARGET, client=client))
        monkeypatch.setattr(mc_main, "_manager", manager)
        mc_main._event_buckets[("host", 25575, "private", 10001)] = mc_main._EventTokenBucket()
        mc_main._delivery_cursor = 4

        await mc_main.shutdown(None)

        assert client.disconnected is True
        assert manager.all_connections() == []
        assert mc_main._event_buckets == {}
        assert mc_main._delivery_cursor == 0


class TestLogMonitor:
    def test_parse_supported_events_and_keep_full_death_message(self, tmp_path: Path) -> None:
        log_path = tmp_path / "latest.log"
        log_path.write_bytes(b"")
        monitor = LogMonitor(str(log_path))
        assert monitor.initialize() is True
        lines = [
            "[12:00:00] [Server thread/INFO]: <Steve> hello\n",
            "[12:00:01] [Server thread/INFO]: Alex joined the game\n",
            "[12:00:02] [Server thread/INFO]: Alex left the game\n",
            "[12:00:03] [Server thread/INFO]: Steve was slain by Zombie\n",
            "[12:00:04] [Server thread/INFO]: Steve has made the advancement [Stone Age]\n",
            "[12:00:05] [Server thread/INFO]: server tick complete\n",
            "[12:00:05] [Server thread/WARN]: ignored\n",
        ]
        with log_path.open("a", encoding="utf-8") as file:
            file.writelines(lines)

        batch = monitor.check_updates()

        assert [event.event_type for event in batch.events] == [
            LogEventType.CHAT,
            LogEventType.JOIN,
            LogEventType.LEAVE,
            LogEventType.DEATH,
            LogEventType.ADVANCEMENT,
        ]
        assert batch.events[3].message == "was slain by Zombie"
        assert not hasattr(batch.events[0], "raw_line")
        assert not hasattr(batch.events[0], "timestamp")

    def test_unterminated_line_is_delivered_only_after_newline(self, tmp_path: Path) -> None:
        log_path = tmp_path / "latest.log"
        log_path.write_bytes(b"")
        monitor = LogMonitor(str(log_path))
        assert monitor.initialize()
        with log_path.open("ab") as file:
            file.write(b"[12:00:00] [Server thread/INFO]: <Steve> partial")

        incomplete = monitor.check_updates()
        assert incomplete.events == []
        assert monitor.commit(incomplete) is True
        with log_path.open("ab") as file:
            file.write(b" message\n")
        complete = monitor.check_updates()
        assert [event.message for event in complete.events] == ["partial message"]

    def test_bounded_tail_reports_exact_skip_and_retains_latest_events(
        self, tmp_path: Path
    ) -> None:
        log_path = tmp_path / "latest.log"
        payload = (b"ignored line\n" * 80) + b"[12:00:00] [Server thread/INFO]: <Steve> bounded\n"
        log_path.write_bytes(payload)
        monitor = LogMonitor(str(log_path))
        monitor._initialized = True  # noqa: SLF001
        monitor._last_position = 0  # noqa: SLF001
        monitor.MAX_READ_BYTES = 128
        read_start = len(payload) - monitor.MAX_READ_BYTES
        partial = read_start > 0 and payload[read_start - 1 : read_start] != b"\n"
        partial_end = payload.find(b"\n", read_start) + 1 if partial else read_start

        batch = monitor.check_updates()

        assert batch.skipped_bytes == partial_end
        assert batch.skipped_lines == payload[:read_start].count(b"\n") + int(partial)
        assert [event.message for event in batch.events] == ["bounded"]
        assert monitor._last_position == 0  # noqa: SLF001
        assert monitor.commit(batch) is True
        assert monitor._last_position == log_path.stat().st_size  # noqa: SLF001

    def test_event_retention_is_bounded_and_counts_all_matches(self, tmp_path: Path) -> None:
        log_path = tmp_path / "latest.log"
        lines = "".join(
            f"[12:00:00] [Server thread/INFO]: <Player{index % 100}> message-{index}\n"
            for index in range(1500)
        )
        log_path.write_text(lines, encoding="utf-8")
        monitor = LogMonitor(str(log_path))
        monitor._initialized = True  # noqa: SLF001
        monitor.MAX_READ_BYTES = log_path.stat().st_size + 1

        batch = monitor.check_updates()

        assert batch.matched_total == 1500
        assert batch.dropped_events == 500
        assert len(batch.events) == 1000
        assert batch.events[0].message == "message-500"
        assert batch.events[-1].message == "message-1499"

    def test_truncation_with_same_identity_restarts_from_zero(self, tmp_path: Path) -> None:
        log_path = tmp_path / "latest.log"
        log_path.write_bytes(b"")
        monitor = LogMonitor(str(log_path))
        assert monitor.initialize()
        log_path.write_text(
            "[12:00:00] [Server thread/INFO]: <Steve> a very long first message\n",
            encoding="utf-8",
        )
        first = monitor.check_updates()
        assert monitor.commit(first)
        log_path.write_text(
            "[1:00:00] [Server thread/INFO]: A joined the game\n",
            encoding="utf-8",
        )

        rotated = monitor.check_updates()

        assert [event.player for event in rotated.events] == ["A"]
        assert rotated.cursor_after == log_path.stat().st_size

    def test_persisted_cursor_survives_restart(self, tmp_path: Path) -> None:
        log_path = tmp_path / "latest.log"
        state_path = tmp_path / "state" / "cursor.json"
        log_path.write_bytes(b"")
        first_monitor = LogMonitor(str(log_path), state_path=state_path)
        assert first_monitor.initialize()
        with log_path.open("a", encoding="utf-8") as file:
            file.write("[12:00:00] [Server thread/INFO]: A joined the game\n")
        first_batch = first_monitor.check_updates()
        assert first_monitor.commit(first_batch)

        second_monitor = LogMonitor(str(log_path), state_path=state_path)
        assert second_monitor.initialize()
        with log_path.open("a", encoding="utf-8") as file:
            file.write("[12:00:01] [Server thread/INFO]: B joined the game\n")
        second_batch = second_monitor.check_updates()

        assert [event.player for event in second_batch.events] == ["B"]

    def test_corrupt_cursor_starts_at_current_end(self, tmp_path: Path) -> None:
        log_path = tmp_path / "latest.log"
        state_path = tmp_path / "cursor.json"
        log_path.write_text(
            "[12:00:00] [Server thread/INFO]: Old joined the game\n",
            encoding="utf-8",
        )
        state_path.write_text('{"version": 2, "position": 0}', encoding="utf-8")
        monitor = LogMonitor(str(log_path), state_path=state_path)
        assert monitor.initialize()
        with log_path.open("a", encoding="utf-8") as file:
            file.write("[12:00:01] [Server thread/INFO]: New joined the game\n")

        batch = monitor.check_updates()

        assert [event.player for event in batch.events] == ["New"]

    def test_cursor_identity_mismatch_or_position_past_eof_replays_from_zero(
        self,
        tmp_path: Path,
    ) -> None:
        log_path = tmp_path / "latest.log"
        state_path = tmp_path / "cursor.json"
        log_path.write_text(
            "[12:00:00] [Server thread/INFO]: Old joined the game\n",
            encoding="utf-8",
        )
        identity = LogMonitor._identity(log_path.stat())  # noqa: SLF001
        state_path.write_text(
            json.dumps({"version": 1, "position": 9999, "file_identity": identity}),
            encoding="utf-8",
        )
        past_eof = LogMonitor(str(log_path), state_path=state_path)
        assert past_eof.initialize()
        assert past_eof._last_position == 0  # noqa: SLF001

        state_path.write_text(
            json.dumps({"version": 1, "position": 1, "file_identity": "other"}),
            encoding="utf-8",
        )
        mismatch = LogMonitor(str(log_path), state_path=state_path)
        assert mismatch.initialize()
        assert mismatch._last_position == 0  # noqa: SLF001

    def test_invalid_cursor_position_and_directory_path_fail_closed(self, tmp_path: Path) -> None:
        log_path = tmp_path / "latest.log"
        state_path = tmp_path / "cursor.json"
        log_path.write_text("history\n", encoding="utf-8")
        state_path.write_text(
            json.dumps({"version": 1, "position": True, "file_identity": "x"}),
            encoding="utf-8",
        )
        monitor = LogMonitor(str(log_path), state_path=state_path)
        assert monitor.initialize()
        assert monitor._last_position == log_path.stat().st_size  # noqa: SLF001
        assert LogMonitor(str(tmp_path)).initialize() is False

    def test_empty_file_and_skip_scan_extremes(self, tmp_path: Path) -> None:
        log_path = tmp_path / "latest.log"
        log_path.write_bytes(b"")
        monitor = LogMonitor(str(log_path))
        assert monitor.initialize()
        empty = monitor.check_updates()
        assert empty.events == []
        assert empty.cursor_before == empty.cursor_after == 0
        assert monitor.commit(empty)
        with log_path.open("rb") as file:
            assert monitor._count_skipped_lines(file, 0, 0) == 0  # noqa: SLF001
            monitor.MAX_SKIPPED_LINE_SCAN_BYTES = 1
            assert monitor._count_skipped_lines(file, 0, 2) is None  # noqa: SLF001
        assert LogMonitor._keep_complete_lines(b"", content_start=7) == (b"", 7)  # noqa: SLF001

    def test_large_unscanned_prefix_keeps_unknown_line_count(self, tmp_path: Path) -> None:
        log_path = tmp_path / "latest.log"
        event = b"[12:00:00] [Server thread/INFO]: <Steve> bounded\n"
        payload = b"x" * 200 + b"\n" + event
        log_path.write_bytes(payload)
        monitor = LogMonitor(str(log_path))
        monitor._initialized = True  # noqa: SLF001
        monitor.MAX_READ_BYTES = len(event) + 50
        monitor.MAX_SKIPPED_LINE_SCAN_BYTES = 1

        batch = monitor.check_updates()

        assert batch.skipped_lines is None
        assert [item.message for item in batch.events] == ["bounded"]

    def test_stale_and_uncommittable_batches_are_rejected(self, tmp_path: Path) -> None:
        log_path = tmp_path / "latest.log"
        log_path.write_bytes(b"")
        monitor = LogMonitor(str(log_path))
        assert monitor.initialize()
        assert monitor.commit(LogBatch(events=[])) is False
        stale = LogBatch(
            events=[],
            cursor_before=99,
            cursor_after=100,
            file_identity="1:2",
        )
        assert monitor.commit(stale) is False

    def test_missing_file_and_poll_error_return_uncommittable_batch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        missing = LogMonitor(str(tmp_path / "missing.log"))
        assert missing.initialize() is False
        assert missing.check_updates().cursor_before is None

        log_path = tmp_path / "latest.log"
        log_path.write_bytes(b"")
        monitor = LogMonitor(str(log_path))
        assert monitor.initialize()

        def fail() -> Any:
            raise OSError("disk failure")

        monkeypatch.setattr(monitor, "_read_window", fail)
        batch = monitor.check_updates()
        assert batch.events == []
        assert batch.cursor_before is None
        assert monitor._initialized is False  # noqa: SLF001
