"""QingSSH 给 xiaoqing_agent 的一次性命令入口：独立连接、执行完即断开、不被孤儿清理误杀。"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tests.helpers.qingssh_test_support import (
    qingssh_main,
    ssh_manager_module,
)
from tests.helpers.settings_snapshot import with_settings_reader

_SERVERS = {
    "lab": {
        "host"        : "10.0.0.5",
        "port"        : 22,
        "username"    : "astro",
        "password_ref": "secret-ref-should-not-leak",
    },
    "edge": {"host": "edge.example", "port": 2222, "username": "ops", "proxyjump": "bastion"},
}


class _FakeChannel:
    """paramiko 通道替身：远端先打印 PID 标记再输出；``hang`` 时一直不退出，直到收到 kill。"""

    def __init__(self, remote: _FakeRemote) -> None:
        self.closed      = False
        self.exited      = not remote.hang
        self.exit_status = 0
        self._chunks     = [b"__XQ_PID__4242\n" + remote.output]

    def set_combine_stderr(self, _flag: bool) -> None:
        return None

    def exec_command(self, _command: str) -> None:
        return None

    def exit_status_ready(self) -> bool:
        return self.exited

    def recv_ready(self) -> bool:
        return bool(self._chunks)

    def recv(self, _size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def send_ready(self) -> bool:
        return False

    def close(self) -> None:
        self.closed = True


class _FakeClient:
    """paramiko.SSHClient 替身：握手由 ``_FakeRemote`` 决定成败；收到 kill 命令后通道退出。"""

    def __init__(self, remote: _FakeRemote) -> None:
        self.remote                       = remote
        self.closed                       = False
        self.channel: _FakeChannel | None = None

    def set_missing_host_key_policy(self, _policy: object) -> None:
        return None

    def load_system_host_keys(self) -> None:
        return None

    def load_host_keys(self, _path: str) -> None:
        return None

    def connect(self, **_kwargs: Any) -> None:
        self.remote.on_connect()
        if self.remote.connect_error is not None:
            raise self.remote.connect_error

    def get_transport(self) -> SimpleNamespace:
        return SimpleNamespace(is_active=lambda: True, open_session=self._open_session)

    def _open_session(self) -> _FakeChannel:
        self.channel = _FakeChannel(self.remote)
        self.remote.channels.append(self.channel)
        return self.channel

    def exec_command(self, command: str) -> None:
        self.remote.commands.append(command)
        if command.startswith("kill") and self.channel is not None:
            self.channel.exited = True

    def close(self) -> None:
        self.closed = True


class _FakeRemote:
    """把 ``paramiko.SSHClient`` 换成替身，并记录创建出来的客户端、通道和远端收到的命令。"""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        output: bytes = b"",
        hang: bool    = False,
    ) -> None:
        self.output                          = output
        self.hang                            = hang
        self.connect_error: Exception | None = None
        self.on_connect: Callable[[], None]  = lambda: None
        self.clients: list[_FakeClient]      = []
        self.channels: list[_FakeChannel]    = []
        self.commands: list[str]             = []
        monkeypatch.setattr(ssh_manager_module.paramiko, "SSHClient", self._new_client)

    def _new_client(self) -> _FakeClient:
        client = _FakeClient(self)
        self.clients.append(client)
        return client


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """known_hosts 和 ~/.ssh/config 都从临时目录读，不碰开发机的真实文件。"""

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))


async def _ready_manager(tmp_path) -> ssh_manager_module.SSHManager:
    (tmp_path / "servers.json").write_text(json.dumps(_SERVERS), encoding="utf-8")
    manager = ssh_manager_module.SSHManager(tmp_path)
    await manager.initialize()
    return manager


async def _wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition did not become true")


def _command_is_running(manager: ssh_manager_module.SSHManager) -> bool:
    """远端命令已经启动且 PID 标记已被读到（终止时会向远端发 kill）。"""

    return any(
        isinstance(active, dict) and active["remote_pid"]
        for active in manager.active_channels.values()
    )


@pytest.mark.asyncio
async def test_service_command_uses_a_private_connection_and_always_disconnects(
    monkeypatch, tmp_path
):
    remote = _FakeRemote(monkeypatch, output=b"line 1\nline 2\n")
    manager = await _ready_manager(tmp_path)
    seen    = {}
    remote.on_connect = lambda: seen.update(keys=set(manager.service_connection_keys))

    result = await manager.run_service_command("lab", "uptime", timeout=12.5)

    assert result == {
        "ok"       : True,
        "exit_code": 0,
        "output"   : "line 1\nline 2\n",
        "truncated": False,
        "timed_out": False,
    }
    (key,) = seen["keys"]
    owner, group_id, name = manager.parse_connection_key(key)
    assert owner.startswith("agent-") and ":" not in owner
    assert (group_id, name) == (None, "lab")
    (client,) = remote.clients
    assert client.closed is True
    assert manager.connections == {}
    assert manager.service_connection_keys == set()


@pytest.mark.asyncio
async def test_service_command_truncates_output_and_reports_timeout(monkeypatch, tmp_path):
    remote = _FakeRemote(monkeypatch, output=b"a" * 30 + b"b" * 30, hang=True)
    manager = await _ready_manager(tmp_path)

    result = await manager.run_service_command("lab", "yes", timeout=0.2, max_output_chars=40)

    assert result["output"] == "a" * 30 + "b" * 10
    assert result["truncated"] is True
    assert result["timed_out"] is True
    assert result["ok"] is False
    # 超时后向远端进程组发出终止信号，并关掉本地通道和连接。
    assert any(command.startswith("kill -TERM -- -4242") for command in remote.commands)
    assert remote.channels[0].closed is True
    assert remote.clients[0].closed is True


@pytest.mark.asyncio
async def test_service_command_reports_connect_failure_and_unpins(monkeypatch, tmp_path):
    remote               = _FakeRemote(monkeypatch)
    remote.connect_error = ssh_manager_module.paramiko.AuthenticationException("rejected")
    manager              = await _ready_manager(tmp_path)

    result = await manager.run_service_command("lab", "uptime", timeout=5)

    assert set(result) == {"ok", "exit_code", "output", "timed_out"}
    assert result["ok"] is False
    assert result["exit_code"] is None
    assert result["timed_out"] is False
    assert result["output"].startswith("❌ 认证失败")
    assert remote.clients[0].closed is True
    assert manager.connections == {}
    assert manager.service_connection_keys == set()


@pytest.mark.asyncio
async def test_service_command_cancellation_still_disconnects(monkeypatch, tmp_path):
    remote = _FakeRemote(monkeypatch, hang=True)
    manager = await _ready_manager(tmp_path)

    running = asyncio.create_task(manager.run_service_command("lab", "sleep 99", timeout=60))
    await _wait_until(lambda: _command_is_running(manager))
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert remote.channels[0].closed is True
    assert remote.clients[0].closed is True
    assert manager.connections == {}
    assert manager.service_connection_keys == set()


@pytest.mark.asyncio
async def test_orphan_cleanup_keeps_agent_connections(monkeypatch, tmp_path):
    remote              = _FakeRemote(monkeypatch)
    manager             = await _ready_manager(tmp_path)
    pinned              = manager.build_connection_key("agent-abc", None, "lab")
    orphan              = manager.build_connection_key("10001", None, "lab")
    pinned_client       = _FakeClient(remote)
    orphan_client       = _FakeClient(remote)
    manager.connections = {pinned: pinned_client, orphan: orphan_client}
    manager.service_connection_keys.add(pinned)
    context = with_settings_reader(
        SimpleNamespace(
            state    = {"ssh_manager": manager},
            data_dir = tmp_path,
            session_manager=SimpleNamespace(get_all_sessions=AsyncMock(return_value=[])),
        )
    )

    await qingssh_main.cleanup_orphans(context)

    assert orphan_client.closed is True
    assert pinned_client.closed is False
    assert set(manager.connections) == {pinned}


@pytest.mark.asyncio
async def test_agent_command_service_lists_summaries_and_runs(monkeypatch, tmp_path):
    remote = _FakeRemote(monkeypatch, output=b"up")
    manager = await _ready_manager(tmp_path)
    context = with_settings_reader(
        SimpleNamespace(state={"ssh_manager": manager}, data_dir=tmp_path)
    )

    servers = await qingssh_main.agent_command_service({"op": "list"}, context)
    assert servers == [
        {"name": "edge", "host": "edge.example", "port": 2222, "username": "ops", "via_jump": True},
        {"name": "lab", "host": "10.0.0.5", "port": 22, "username": "astro", "via_jump": False},
    ]
    assert "secret-ref" not in repr(servers)

    missing = await qingssh_main.agent_command_service(
        {"op": "run", "server": "nope", "command": "ls", "timeout_seconds": 5},
        context,
    )
    assert missing["ok"] is False and "edge, lab" in missing["output"]
    assert remote.clients == []

    # 执行只在插件通道里启动；返回的 Task 由 Core 在通道外等待，其他 /ssh 请求不被挡住。
    pending = await qingssh_main.agent_command_service(
        {"op": "run", "server": "lab", "command": "uptime", "timeout_seconds": 7},
        context,
    )
    assert isinstance(pending, asyncio.Task)
    result = await pending
    assert result["ok"] is True
    assert result["output"] == "up"

    # 请求里的超时秒数生效：远端一直不退出时按它超时。
    remote.hang = True
    pending     = await qingssh_main.agent_command_service(
        {"op": "run", "server": "lab", "command": "sleep 99", "timeout_seconds": 0.2},
        context,
    )
    assert (await pending)["timed_out"] is True

    with pytest.raises(ValueError):
        await qingssh_main.agent_command_service({"op": "drop"}, context)


@pytest.mark.asyncio
async def test_manager_shutdown_cancels_agent_commands_still_running(monkeypatch, tmp_path):
    remote = _FakeRemote(monkeypatch, hang=True)
    manager = await _ready_manager(tmp_path)
    pending = manager.start_service_command("lab", "sleep 3600", timeout=60)
    await _wait_until(lambda: _command_is_running(manager))

    await manager.shutdown()

    assert pending.cancelled()
    assert manager._service_tasks == set()
    assert remote.channels[0].closed is True
    assert remote.clients[0].closed is True
    assert manager.connections == {}


def test_remote_command_wrapper_falls_back_when_setsid_is_missing():
    class Channel:
        def __init__(self):
            self.command = ""

        def set_combine_stderr(self, _flag):
            return None

        def exec_command(self, command):
            self.command = command

        def close(self):
            return None

    channel = Channel()
    transport = SimpleNamespace(open_session=lambda: channel)
    ssh_manager_module._open_command_channel(transport, "echo 'a b'", threading.Event())

    assert channel.command == (
        "if command -v setsid >/dev/null 2>&1; then setsid sh -c 'echo '\"'\"'a b'\"'\"'' & "
        "else sh -c 'echo '\"'\"'a b'\"'\"'' & fi; pid=$!; "
        'printf \'__XQ_PID__%s\\n\' "$pid"; wait "$pid"'
    )
