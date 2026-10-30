"""QingSSH 给 xiaoqing_agent 的一次性命令入口：独立连接、执行完即断开、不被孤儿清理误杀。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.helpers.qingssh_test_support import (
    EXIT_CODE_TIMEOUT,
    qingssh_main,
    ssh_manager_module,
)


def _manager(tmp_path) -> ssh_manager_module.SSHManager:
    manager         = ssh_manager_module.SSHManager(tmp_path)
    manager.servers = {
        "lab": {
            "host"        : "10.0.0.5",
            "port"        : 22,
            "username"    : "astro",
            "password_ref": "secret-ref-should-not-leak",
        },
        "edge": {"host": "edge.example", "port": 2222, "username": "ops", "proxyjump": "bastion"},
    }
    return manager


@pytest.mark.asyncio
async def test_service_command_uses_a_private_connection_and_always_disconnects(
    monkeypatch, tmp_path
):
    manager = _manager(tmp_path)
    seen    = {}

    async def connect(owner, group_id, name):
        seen["owner"] = owner
        seen["key"]   = manager.build_connection_key(owner, group_id, name)
        assert seen["key"] in manager.service_connection_keys
        return True, "ok"

    async def stream(owner, group_id, name, command, collector, *, timeout):
        seen["timeout"] = timeout
        await collector("line 1\n")
        await collector("line 2\n")
        return 0

    disconnect = Mock(return_value=True)
    monkeypatch.setattr(manager, "connect", connect)
    monkeypatch.setattr(manager, "execute_command_stream", stream)
    monkeypatch.setattr(manager, "disconnect", disconnect)

    result = await manager.run_service_command("lab", "uptime", timeout=12.5)

    assert result == {
        "ok"       : True,
        "exit_code": 0,
        "output"   : "line 1\nline 2\n",
        "truncated": False,
        "timed_out": False,
    }
    assert seen["owner"].startswith("agent-") and ":" not in seen["owner"]
    assert seen["timeout"] == 12.5
    disconnect.assert_called_once_with(seen["owner"], None, "lab")
    assert manager.service_connection_keys == set()


@pytest.mark.asyncio
async def test_service_command_truncates_output_and_reports_timeout(monkeypatch, tmp_path):
    manager = _manager(tmp_path)

    async def stream(owner, group_id, name, command, collector, *, timeout):
        await collector("a" * 30)
        await collector("b" * 30)
        return EXIT_CODE_TIMEOUT

    monkeypatch.setattr(manager, "connect", AsyncMock(return_value=(True, "ok")))
    monkeypatch.setattr(manager, "execute_command_stream", stream)
    monkeypatch.setattr(manager, "disconnect", Mock())

    result = await manager.run_service_command("lab", "yes", timeout=1, max_output_chars=40)

    assert result["output"] == "a" * 30 + "b" * 10
    assert result["truncated"] is True
    assert result["timed_out"] is True
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_service_command_reports_connect_failure_and_unpins(monkeypatch, tmp_path):
    manager    = _manager(tmp_path)
    disconnect = Mock()
    monkeypatch.setattr(manager, "connect", AsyncMock(return_value=(False, "❌ 认证失败")))
    monkeypatch.setattr(manager, "disconnect", disconnect)

    result = await manager.run_service_command("lab", "uptime", timeout=5)

    assert result == {"ok": False, "exit_code": None, "output": "❌ 认证失败", "timed_out": False}
    disconnect.assert_called_once()
    assert manager.service_connection_keys == set()


@pytest.mark.asyncio
async def test_service_command_cancellation_still_disconnects(monkeypatch, tmp_path):
    manager    = _manager(tmp_path)
    started    = asyncio.Event()
    disconnect = Mock()

    async def stream(*_args, **_kwargs):
        started.set()
        await asyncio.wait_for(asyncio.Event().wait(), timeout=10)

    monkeypatch.setattr(manager, "connect", AsyncMock(return_value=(True, "ok")))
    monkeypatch.setattr(manager, "execute_command_stream", stream)
    monkeypatch.setattr(manager, "disconnect", disconnect)

    running = asyncio.create_task(manager.run_service_command("lab", "sleep 99", timeout=60))
    await asyncio.wait_for(started.wait(), timeout=10)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    disconnect.assert_called_once()
    assert manager.service_connection_keys == set()


@pytest.mark.asyncio
async def test_orphan_cleanup_keeps_agent_connections(monkeypatch, tmp_path):
    manager             = _manager(tmp_path)
    pinned              = manager.build_connection_key("agent-abc", None, "lab")
    orphan              = manager.build_connection_key("10001", None, "lab")
    manager.connections = {pinned: object(), orphan: object()}
    manager.service_connection_keys.add(pinned)
    disconnected: list[tuple] = []

    def disconnect(user_id, group_id, name):
        disconnected.append((user_id, group_id, name))
        return True

    monkeypatch.setattr(manager, "disconnect", disconnect)
    monkeypatch.setattr(manager, "initialize", AsyncMock())
    context = SimpleNamespace(
        state    = {"ssh_manager": manager},
        data_dir = tmp_path,
        session_manager=SimpleNamespace(get_all_sessions=AsyncMock(return_value=[])),
    )

    await qingssh_main.cleanup_orphans(context)

    assert disconnected == [("10001", None, "lab")]


@pytest.mark.asyncio
async def test_agent_command_service_lists_summaries_and_runs(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    monkeypatch.setattr(manager, "initialize", AsyncMock())
    run = AsyncMock(return_value={"ok": True, "exit_code": 0, "output": "up"})
    monkeypatch.setattr(manager, "run_service_command", run)
    context = SimpleNamespace(state={"ssh_manager": manager}, data_dir=tmp_path)

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
    run.assert_not_awaited()

    # 执行只在插件通道里启动；返回的 Task 由 Core 在通道外等待，其他 /ssh 请求不被挡住。
    pending = await qingssh_main.agent_command_service(
        {"op": "run", "server": "lab", "command": "uptime", "timeout_seconds": 7},
        context,
    )
    assert isinstance(pending, asyncio.Task)
    result = await pending
    assert result["output"] == "up"
    run.assert_awaited_once_with("lab", "uptime", timeout=7.0)

    with pytest.raises(ValueError):
        await qingssh_main.agent_command_service({"op": "drop"}, context)


@pytest.mark.asyncio
async def test_manager_shutdown_cancels_agent_commands_still_running(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    started = asyncio.Event()

    async def hang(_name, _command, *, timeout):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(manager, "run_service_command", hang)
    monkeypatch.setattr(manager, "close_all", lambda: None)
    pending = manager.start_service_command("lab", "sleep 3600", timeout=60)
    await asyncio.wait_for(started.wait(), timeout=10)

    await manager.shutdown()

    assert pending.cancelled()
    assert manager._service_tasks == set()


def test_remote_command_wrapper_falls_back_when_setsid_is_missing():
    import threading

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
    ssh_manager_module._open_command_channel(transport, "echo 'a b'", False, threading.Event())

    assert channel.command == (
        "if command -v setsid >/dev/null 2>&1; then setsid sh -c 'echo '\"'\"'a b'\"'\"'' & "
        "else sh -c 'echo '\"'\"'a b'\"'\"'' & fi; pid=$!; "
        'printf \'__XQ_PID__%s\\n\' "$pid"; wait "$pid"'
    )
