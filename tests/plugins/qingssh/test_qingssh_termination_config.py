"""远程终止、配置事务和文件清理。"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from tests.helpers.filesystem_faults import fail_replace_onto
from tests.helpers.qingssh_test_support import (
    FakeChannel,
    FakeClient,
    SessionKeys,
    SessionStub,
    StreamChannel,
    StreamClient,
    qingssh_main,
    ssh_manager_module,
    ssh_session_handlers,
)


@pytest.mark.asyncio
async def test_remote_stop_uses_control_channel_and_process_group(tmp_path):
    class Channel:
        active = True

        def __init__(self):
            self.closed = False

        def exit_status_ready(self):
            return True

        def close(self):
            self.closed = True

    class Client:
        def __init__(self):
            self.commands = []

        def exec_command(self, command):
            self.commands.append(command)
            return None, None, None

    manager                      = ssh_manager_module.SSHManager(tmp_path)
    key                          = manager.build_connection_key("1", "2", "srv")
    channel                      = Channel()
    client                       = Client()
    manager.connections[key]     = cast(Any, client)
    manager.active_channels[key] = {"channel": channel, "remote_pid": 321}

    termination = await manager.stop_command("1", "2", "srv")

    assert termination.remote_confirmed is True
    assert termination.local_cleaned is True
    assert client.commands == ["kill -TERM -- -321 2>/dev/null || kill -TERM 321"]
    assert channel.closed is True
    assert key not in manager.active_channels


@pytest.mark.asyncio
async def test_shutdown_closes_connections_off_event_loop(tmp_path):
    class Client:
        def __init__(self):
            self.close_threads = []

        def close(self):
            self.close_threads.append(threading.current_thread())

    manager                        = ssh_manager_module.SSHManager(tmp_path)
    client                         = Client()
    manager.connections["1:2:srv"] = cast(Any, client)

    await manager.shutdown()

    assert len(client.close_threads) == 1
    assert client.close_threads[0] is not threading.current_thread()
    assert manager.connections == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("active", ["bare_channel", "missing_pid"])
async def test_remote_stop_without_pid_always_closes_and_unregisters(
    tmp_path,
    active,
):
    manager                      = ssh_manager_module.SSHManager(tmp_path)
    key                          = manager.build_connection_key("1", "2", "srv")
    channel                      = FakeChannel()
    manager.active_channels[key] = (
        channel if active == "bare_channel" else {"channel": channel, "remote_pid": None}
    )

    termination = await manager.stop_command("1", "2", "srv")

    assert termination.found is True
    assert termination.local_cleaned is True
    assert termination.remote_unknown is True
    assert termination.signal_attempted is False
    assert channel.closed is True
    assert key not in manager.active_channels


@pytest.mark.asyncio
async def test_remote_stop_without_client_reports_unknown_but_cleans_local_state(tmp_path):
    manager                      = ssh_manager_module.SSHManager(tmp_path)
    key                          = manager.build_connection_key("1", "2", "srv")
    channel                      = FakeChannel()
    manager.active_channels[key] = {"channel": channel, "remote_pid": 321}

    termination = await manager.stop_command("1", "2", "srv")

    assert termination.local_cleaned is True
    assert termination.remote_unknown is True
    assert "SSH client" in str(termination.error)
    assert channel.closed is True
    assert key not in manager.active_channels


@pytest.mark.asyncio
async def test_channel_close_failure_is_reported_but_registry_is_still_cleared(tmp_path):
    class BrokenChannel:
        def close(self):
            raise OSError("close failed")

    manager                      = ssh_manager_module.SSHManager(tmp_path)
    key                          = manager.build_connection_key("1", "2", "srv")
    manager.active_channels[key] = {
        "channel"   : BrokenChannel(),
        "remote_pid": None,
    }

    termination = await manager.stop_command("1", "2", "srv")

    assert termination.local_cleaned is False
    assert termination.remote_unknown is True
    assert key not in manager.active_channels


@pytest.mark.asyncio
async def test_remote_stop_signal_failures_still_close_and_unregister(tmp_path):
    class Client:
        def __init__(self):
            self.commands = []

        def exec_command(self, command):
            self.commands.append(command)
            raise OSError("control channel unavailable")

    manager                      = ssh_manager_module.SSHManager(tmp_path)
    key                          = manager.build_connection_key("1", "2", "srv")
    channel                      = FakeChannel()
    client                       = Client()
    manager.connections[key]     = cast(Any, client)
    manager.active_channels[key] = {"channel": channel, "remote_pid": 321}

    termination = await manager.stop_command("1", "2", "srv")

    assert termination.remote_unknown is True
    assert termination.signal_attempted is True
    assert client.commands == [
        "kill -TERM -- -321 2>/dev/null || kill -TERM 321",
        "kill -KILL -- -321 2>/dev/null || kill -KILL 321",
    ]
    assert channel.closed is True
    assert key not in manager.active_channels


@pytest.mark.asyncio
async def test_old_termination_cannot_remove_replacement_command_record(tmp_path):
    class ReadyChannel(FakeChannel):
        active = True

        def exit_status_ready(self):
            return True

    manager     = ssh_manager_module.SSHManager(tmp_path)
    key         = manager.build_connection_key("1", "2", "srv")
    old_channel = ReadyChannel()
    new_channel = ReadyChannel()
    old_record  = {"channel": old_channel, "remote_pid": 321}
    new_record  = {"channel": new_channel, "remote_pid": 654}

    class Client:
        def exec_command(self, _command):
            manager.active_channels[key] = new_record
            return None, None, None

    manager.connections[key]     = cast(Any, Client())
    manager.active_channels[key] = old_record

    termination = await manager.stop_command("1", "2", "srv")

    assert termination.remote_confirmed is True
    assert old_channel.closed is True
    assert new_channel.closed is False
    assert manager.active_channels[key] is new_record


@pytest.mark.asyncio
async def test_stop_command_is_idempotent_after_unknown_remote_cleanup(tmp_path):
    manager                      = ssh_manager_module.SSHManager(tmp_path)
    key                          = manager.build_connection_key("1", "2", "srv")
    channel                      = FakeChannel()
    manager.active_channels[key] = {"channel": channel, "remote_pid": None}

    first  = await manager.stop_command("1", "2", "srv")
    second = await manager.stop_command("1", "2", "srv")

    assert first.found is True and first.remote_unknown is True
    assert second.found is False and second.local_cleaned is True
    assert channel.closed is True


@pytest.mark.asyncio
async def test_concurrent_termination_keeps_one_lock_until_all_waiters_finish(
    monkeypatch,
    tmp_path,
):
    manager          = ssh_manager_module.SSHManager(tmp_path)
    key              = manager.build_connection_key("1", "2", "srv")
    first_entered    = asyncio.Event()
    release_first    = asyncio.Event()
    second_entered   = asyncio.Event()
    release_second   = asyncio.Event()
    third_entered    = asyncio.Event()
    call_count       = 0
    active_calls     = 0
    max_active_calls = 0

    async def controlled_termination(_key):
        nonlocal call_count, active_calls, max_active_calls
        call_count += 1
        ordinal = call_count
        active_calls += 1
        max_active_calls = max(max_active_calls, active_calls)
        try:
            if ordinal == 1:
                first_entered.set()
                await asyncio.wait_for(release_first.wait(), timeout=10)
            elif ordinal == 2:
                second_entered.set()
                await asyncio.wait_for(release_second.wait(), timeout=10)
            else:
                third_entered.set()
            return ssh_manager_module.CommandTerminationResult(
                found            = False,
                local_cleaned    = True,
                remote_confirmed = False,
            )
        finally:
            active_calls -= 1

    monkeypatch.setattr(manager, "_terminate_active_command_locked", controlled_termination)

    first = asyncio.create_task(manager._terminate_active_command(key))
    await asyncio.wait_for(first_entered.wait(), timeout=10)
    second = asyncio.create_task(manager._terminate_active_command(key))
    await asyncio.sleep(0)
    release_first.set()
    await asyncio.wait_for(second_entered.wait(), timeout=10)

    third = asyncio.create_task(manager._terminate_active_command(key))
    await asyncio.sleep(0)
    assert third_entered.is_set() is False

    release_second.set()
    await asyncio.gather(first, second, third)

    assert third_entered.is_set() is True
    assert max_active_calls == 1
    assert manager._termination_locks == {}
    assert manager._termination_lock_users == {}


@pytest.mark.asyncio
async def test_stream_task_cancellation_before_pid_marker_leaves_no_active_channel(tmp_path):
    manager                  = ssh_manager_module.SSHManager(tmp_path)
    key                      = manager.build_connection_key("1", "2", "srv")
    channel                  = StreamChannel()
    manager.connections[key] = cast(Any, StreamClient(channel))
    task                     = asyncio.create_task(
        manager.execute_command_stream("1", "2", "srv", "sleep 1", AsyncMock())
    )
    async with asyncio.timeout(10):
        while key not in manager.active_channels:
            await asyncio.sleep(0.01)
    assert manager.active_channels[key]["remote_pid"] is None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert channel.closed is True
    assert key not in manager.active_channels


@pytest.mark.asyncio
async def test_remove_server_disconnects_every_actor_and_deletes_secret(tmp_path):
    deleted               = []
    context               = Mock()
    context.delete_secret = lambda ref: deleted.append(ref) or True
    manager = ssh_manager_module.SSHManager(tmp_path, context=context)
    manager.servers["srv"]          = {"password_ref": "passwords.ref"}
    first                           = FakeClient()
    second                          = FakeClient()
    manager.connections["1:10:srv"] = first
    manager.connections["2:20:srv"] = second

    assert await manager.remove_server("srv") is True

    assert first.closed and second.closed
    assert deleted == ["passwords.ref"]
    assert manager.connections == {}


@pytest.mark.asyncio
async def test_add_server_does_not_overwrite_an_existing_name(tmp_path):
    """重复名称必须保持原配置，避免静默替换连接凭据。"""

    manager                = ssh_manager_module.SSHManager(tmp_path)
    manager.servers["srv"] = {"host": "old.example", "port": 22}

    assert await manager.add_server("srv", "new.example", auth_type="agent") is False
    assert manager.servers["srv"] == {"host": "old.example", "port": 22}


@pytest.mark.asyncio
async def test_add_server_rolls_back_secret_when_config_write_fails(monkeypatch, tmp_path):
    """配置未落盘时不能留下不可达的孤立密码。"""

    stored : dict[str, str] = {}
    deleted: list[str]      = []
    context                 = Mock()
    context.set_secret      = lambda key, value: stored.__setitem__(key, value)

    def delete_secret(key):
        deleted.append(key)
        stored.pop(key, None)
        return True

    context.delete_secret = delete_secret
    manager = ssh_manager_module.SSHManager(tmp_path, context=context)
    fail_replace_onto(monkeypatch, "servers.json", "disk full")

    with pytest.raises(OSError, match="disk full"):
        await manager.add_server("srv", "example.com", password="top-secret")

    assert manager.servers == {}
    assert stored == {}
    assert len(deleted) == 1


@pytest.mark.asyncio
async def test_remove_server_preserves_state_when_config_write_fails(monkeypatch, tmp_path):
    """删除写盘失败时，内存配置、密钥和现有连接都应保持可用。"""

    deleted: list[str]    = []
    context               = Mock()
    context.delete_secret = lambda ref: deleted.append(ref) or True
    manager = ssh_manager_module.SSHManager(tmp_path, context=context)
    manager.servers["srv"] = {
        "host"        : "example.com",
        "port"        : 22,
        "password_ref": "passwords.ref",
    }
    client                         = FakeClient()
    manager.connections["1:2:srv"] = client
    fail_replace_onto(monkeypatch, "servers.json", "disk full")

    with pytest.raises(OSError, match="disk full"):
        await manager.remove_server("srv")

    assert "srv" in manager.servers
    assert deleted == []
    assert client.closed is False


@pytest.mark.asyncio
async def test_legacy_password_migration_rolls_back_secret_on_save_failure(
    monkeypatch,
    tmp_path,
):
    """旧明文迁移必须以配置写盘成功为提交点。"""

    (tmp_path / "servers.json").write_text(
        json.dumps(
            {
                "srv": {
                    "host"     : "example.com",
                    "port"     : 22,
                    "username" : "root",
                    "auth_type": "password",
                    "password" : "dummy-secret",
                }
            }
        ),
        encoding="utf-8",
    )
    stored: dict[str, str] = {}
    context                = Mock()
    context.set_secret     = lambda key, value: stored.__setitem__(key, value)
    context.delete_secret  = lambda key: stored.pop(key, None) is not None
    manager = ssh_manager_module.SSHManager(tmp_path, context=context)
    fail_replace_onto(monkeypatch, "servers.json", "disk full")

    await manager._load_servers()

    assert manager.servers == {}
    assert stored == {}


def test_server_accessors_return_defensive_copies(tmp_path):
    """调用方不能绕过配置锁直接修改管理器内部状态。"""

    manager                = ssh_manager_module.SSHManager(tmp_path)
    manager.servers["srv"] = {"host": "example.com", "port": 22}

    server  = manager.get_server("srv")
    servers = manager.list_servers()
    assert server is not None
    server["host"]         = "mutated.example"
    servers["srv"]["port"] = 2200

    assert manager.servers["srv"] == {"host": "example.com", "port": 22}


@pytest.mark.asyncio
async def test_reload_ssh_config_clears_cache_after_file_removal(monkeypatch, tmp_path):
    """config 被删除后不能继续展示上一次解析出的主机。"""

    if not ssh_manager_module.PARAMIKO_AVAILABLE:
        pytest.skip("paramiko is not installed")
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    config_path = ssh_dir / "config"
    config_path.write_text(
        "Host demo\n  HostName example.com\n  ProxyJump jumpuser@jump-host:2222\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ssh_manager_module.Path,
        "home",
        classmethod(lambda _cls: tmp_path),
    )
    manager = ssh_manager_module.SSHManager(tmp_path / "data")

    await manager.reload_ssh_config()
    assert manager.get_ssh_config_hosts() == ["demo"]
    assert manager.get_ssh_config_for_host("demo")["proxyjump"] == "jumpuser@jump-host:2222"
    assert manager.get_ssh_config_for_host("undeclared.example") is None

    config_path.unlink()
    await manager.reload_ssh_config()
    assert manager.get_ssh_config_hosts() == []


@pytest.mark.asyncio
async def test_download_file_removes_partial_temporary_file_on_failure(tmp_path):
    """SFTP 中途失败时不能在目标目录残留半文件。"""

    class SFTP:
        closed = False

        def stat(self, _path):
            return Mock(st_size=5)

        def get(self, _remote, local):
            Path(local).write_bytes(b"part")
            raise OSError(r"DOWNLOAD_PRIVATE_CANARY C:\private\download.part")

        def close(self):
            self.closed = True

    class Transport:
        def is_active(self):
            return True

    class Client:
        def __init__(self, sftp):
            self.sftp = sftp

        def get_transport(self):
            return Transport()

        def open_sftp(self):
            return self.sftp

    context            = MagicMock()
    context.request_id = "req-qingssh-download"
    context.secrets    = {"private_exception_canary": "DOWNLOAD_PRIVATE_CANARY"}
    manager = ssh_manager_module.SSHManager(tmp_path / "data", context=context)
    sftp                     = SFTP()
    key                      = manager.build_connection_key("1", "2", "srv")
    manager.connections[key] = cast(Any, Client(sftp))
    target                   = tmp_path / "image.png"

    success, message = await manager.download_file(
        "1",
        "2",
        "srv",
        "/remote/image.png",
        str(target),
    )

    assert success is False
    assert "XQ-PLUGIN-UNEXPECTED" in message
    assert "req-qingssh-download" in message
    assert "DOWNLOAD_PRIVATE_CANARY" not in message
    assert "download.part" not in message
    assert target.exists() is False
    assert list(tmp_path.glob("*.part")) == []
    assert sftp.closed is True


@pytest.mark.asyncio
async def test_cancelled_config_write_publishes_the_committed_snapshot(monkeypatch, tmp_path):
    """原子写入已经开始后，取消只能延迟交付，不能制造磁盘与内存分叉。"""

    started      = threading.Event()
    release      = threading.Event()
    real_replace = os.replace

    def delayed_replace(source, destination):
        # 临时文件已写好、尚未替换到目标：写入正在进行中。
        if Path(destination).name == "servers.json":
            started.set()
            assert release.wait(timeout=2)
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", delayed_replace)
    manager = ssh_manager_module.SSHManager(tmp_path)
    task = asyncio.create_task(manager.add_server("srv", "example.com", auth_type="agent"))
    assert await asyncio.to_thread(started.wait, 2)

    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    payload = json.loads((tmp_path / "servers.json").read_text(encoding="utf-8"))
    assert payload == manager.servers
    assert payload["srv"]["host"] == "example.com"


@pytest.mark.asyncio
async def test_execute_command_reports_nonzero_exit_as_failure(tmp_path):
    """完整输出接口不能把远端非零退出码误报成成功。"""

    manager = ssh_manager_module.SSHManager(tmp_path)
    channel = StreamChannel((b"__XQ_PID__7\nremote error",), exit_status=7)
    manager.connections[manager.build_connection_key("1", "2", "srv")] = cast(
        Any, StreamClient(channel)
    )

    success, output = await manager.execute_command("1", "2", "srv", "false")

    assert success is False
    assert output == "remote error"


@pytest.mark.asyncio
async def test_guided_add_rejects_unsafe_username_without_advancing():
    """聊天输入的用户名不能进入 ProxyJump 展开或后续连接参数。"""

    context = Mock(current_user_id=10001, current_group_id=None)
    context.end_session = AsyncMock()
    session             = SessionStub(
        {
            SessionKeys.STEP         : "username",
            SessionKeys.SERVER_CONFIG: {
                "name": "srv",
                "host": "example.com",
                "port": 22,
            },
        }
    )

    result = await ssh_session_handlers._handle_adding_session(
        "bad user",
        context,
        session,
        MagicMock(),
    )

    assert "用户名只能" in result[0]["data"]["text"]
    assert session.get(SessionKeys.STEP) == "username"
    context.end_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_session_state_ends_without_initializing_manager(tmp_path):
    """损坏状态应直接关闭，不能为了报错反向创建 SSH 管理器。"""

    context = SimpleNamespace(
        state       = {},
        data_dir    = tmp_path,
        end_session = AsyncMock(),
    )
    session = SessionStub({SessionKeys.STATE: "corrupt"})

    result = await ssh_session_handlers.handle_session("pwd", {}, context, session)

    assert "状态无效" in result[0]["data"]["text"]
    context.end_session.assert_awaited_once_with()
    assert "ssh_manager" not in context.state


@pytest.mark.asyncio
async def test_status_omits_group_label_for_private_connections():
    context                                     = Mock()
    manager                                     = MagicMock()
    manager.get_active_connections.return_value = [
        {"user_id": "10001", "group_id": None, "server_name": "srv"}
    ]

    result = await qingssh_main.handle_ssh_status("", {}, context, manager)
    text   = result[0]["data"]["text"]

    assert "[用户: 10001]" in text
    assert "[群:" not in text


@pytest.mark.asyncio
async def test_import_all_uses_one_atomic_snapshot(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "config").write_text(
        "Host first\n  HostName first.example\n  User root\n"
        "Host second\n  HostName second.example\n  User root\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    real_replace  = os.replace
    config_writes = []

    def counting_replace(source, destination):
        if Path(destination).name == "servers.json":
            config_writes.append(Path(destination))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", counting_replace)
    manager = ssh_manager_module.SSHManager(tmp_path / "data")
    await manager.reload_ssh_config()

    count, imported = await manager.import_all_from_ssh_config()

    assert count == 2
    assert imported == ["first", "second"]
    assert set(manager.servers) == {"first", "second"}
    assert len(config_writes) == 1
    assert json.loads(config_writes[0].read_text(encoding="utf-8")) == manager.servers
