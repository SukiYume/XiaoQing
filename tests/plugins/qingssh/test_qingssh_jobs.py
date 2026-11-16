"""后台作业、会话代际和清理。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from tests.helpers.qingssh_test_support import (
    CommandGateManager,
    DisconnectManagerStub,
    ManagerStub,
    SessionKeys,
    SessionManager,
    SessionStub,
    TransactionalContext,
    command_parent,
    connected_session,
    qingssh_main,
    ssh_manager_module,
    ssh_session_handlers,
    wait_for,
)
from tests.helpers.settings_snapshot import with_settings_reader


class _HangingChannel:
    """paramiko 通道替身：远端命令一直不退出，直到客户端收到 kill 命令。"""

    def __init__(self, remote: _HangingRemote) -> None:
        self.remote  = remote
        self.exited  = False
        self._chunks = [b"__XQ_PID__4242\n"]

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
        self.remote.events.append("channel.close")


class _HangingClient:
    """paramiko.SSHClient 替身；``remote.events`` 按发生顺序记下终止信号和关闭。"""

    def __init__(self, remote: _HangingRemote) -> None:
        self.remote                          = remote
        self.channel: _HangingChannel | None = None

    def get_transport(self) -> SimpleNamespace:
        return SimpleNamespace(is_active=lambda: True, open_session=self._open_session)

    def _open_session(self) -> _HangingChannel:
        self.channel = _HangingChannel(self.remote)
        return self.channel

    def exec_command(self, command: str) -> None:
        if command.startswith("kill"):
            self.remote.events.append("kill")
            if self.channel is not None:
                self.channel.exited = True

    def close(self) -> None:
        self.remote.events.append("client.close")


class _HangingRemote:
    def __init__(self) -> None:
        self.events: list[str] = []


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """known_hosts 和 ~/.ssh/config 都从临时目录读，不碰开发机的真实文件。"""

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))


def test_qingssh_session_does_not_store_task_object():
    async def _run():
        manager                  = ManagerStub()
        context                  = with_settings_reader(Mock())
        context.current_user_id  = 10001
        context.current_group_id = 50001
        context.send_action      = AsyncMock()
        context.end_session      = AsyncMock()

        session = SessionStub(
            {
                SessionKeys.STATE        : "connected",
                SessionKeys.SERVER_NAME  : "srv1",
                SessionKeys.COMMAND_COUNT: 0,
                SessionKeys.CWD          : None,
                SessionKeys.ENV_VARS     : {},
                SessionKeys.HISTORY      : [],
            }
        )

        async def update_session(callback):
            return callback(session)

        context.update_session = update_session

        await ssh_session_handlers._handle_connected_session(
            "ls", context, session, cast(Any, manager)
        )

        job_id = session.get(SessionKeys.CURRENT_TASK)
        assert isinstance(job_id, str)
        assert len(job_id) == 32
        assert not isinstance(job_id, asyncio.Task)

        manager._done.set()
        for _ in range(20):
            if session.get(SessionKeys.CURRENT_TASK) is None:
                break
            await asyncio.sleep(0)

        assert session.get(SessionKeys.CURRENT_TASK) is None

    asyncio.run(_run())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "export OPENAI_API_KEY=sk-history-canary",
        "curl -H 'Authorization: Bearer history-canary' https://example.test",
        "deploy --token history-canary",
    ],
)
async def test_sensitive_commands_are_not_retained_in_session_history(
    command: str,
) -> None:
    await ssh_session_handlers.shutdown_tasks()
    manager                  = ManagerStub()
    context                  = with_settings_reader(Mock())
    context.current_user_id  = 10001
    context.current_group_id = 50001
    context.send_action      = AsyncMock()
    context.end_session      = AsyncMock()
    context.config           = {}
    context.request_id       = "sensitive-history-test"
    session                  = connected_session()

    async def update_session(callback):
        return callback(session)

    context.update_session = update_session
    try:
        await ssh_session_handlers._handle_connected_session(
            command,
            context,
            session,
            cast(Any, manager),
        )

        assert session.get(SessionKeys.HISTORY) == []
    finally:
        manager._done.set()
        await ssh_session_handlers.shutdown_tasks()


@pytest.mark.asyncio
async def test_export_value_uses_shell_quoting_and_rejects_unclosed_quotes(tmp_path: Path) -> None:
    await ssh_session_handlers.shutdown_tasks()
    context = TransactionalContext(connected_session())
    manager = CommandGateManager(tmp_path)

    try:
        await context.run_parent(command_parent("export X='a\"b'", context, manager))
        await manager.started.wait()
        assert context.session is not None
        assert context.session.get(SessionKeys.ENV_VARS) == {"X": 'a"b'}
        manager.release.set()
        await wait_for(lambda: not ssh_session_handlers._COMMAND_JOBS)

        invalid = await ssh_session_handlers._handle_connected_session(
            "export X='unterminated",
            context,
            connected_session(),
            cast(Any, manager),
        )
        assert "引号" in invalid[0]["data"]["text"]
    finally:
        manager.release.set()
        await ssh_session_handlers.shutdown_tasks()


@pytest.mark.asyncio
async def test_existing_sensitive_history_is_scrubbed_before_display_or_replay() -> None:
    await ssh_session_handlers.shutdown_tasks()
    manager                  = ManagerStub()
    context                  = with_settings_reader(Mock())
    context.current_user_id  = 10001
    context.current_group_id = 50001
    context.end_session      = AsyncMock()
    session                  = connected_session()
    session.set(
        SessionKeys.HISTORY,
        [
            "export GITHUB_TOKEN=legacy-history-canary",
            "git status",
        ],
    )

    history_result = await ssh_session_handlers._handle_connected_session(
        "历史",
        context,
        session,
        cast(Any, manager),
    )

    assert "git status" in history_result[0]["data"]["text"]
    assert "legacy-history-canary" not in history_result[0]["data"]["text"]
    assert session.get(SessionKeys.HISTORY) == ["git status"]


@pytest.mark.asyncio
async def test_ordinary_commands_remain_available_in_session_history() -> None:
    await ssh_session_handlers.shutdown_tasks()
    manager                  = ManagerStub()
    context                  = with_settings_reader(Mock())
    context.current_user_id  = 10001
    context.current_group_id = 50001
    context.send_action      = AsyncMock()
    context.end_session      = AsyncMock()
    context.config           = {}
    context.request_id       = "ordinary-history-test"
    session                  = connected_session()

    async def update_session(callback):
        return callback(session)

    context.update_session = update_session
    try:
        await ssh_session_handlers._handle_connected_session(
            "git status",
            context,
            session,
            cast(Any, manager),
        )

        assert session.get(SessionKeys.HISTORY) == ["git status"]
    finally:
        manager._done.set()
        await ssh_session_handlers.shutdown_tasks()


@pytest.mark.asyncio
async def test_background_waits_for_parent_commit_before_opening_ssh(tmp_path: Path) -> None:
    await ssh_session_handlers.shutdown_tasks()
    context = TransactionalContext(connected_session())
    manager = CommandGateManager(tmp_path)

    async def parent(working: SessionStub) -> None:
        await ssh_session_handlers._handle_connected_session(
            "hostname",
            context,
            working,
            cast(Any, manager),
        )
        await asyncio.sleep(0)
        assert manager.calls == []

    try:
        await context.run_parent(parent)
        await asyncio.wait_for(manager.started.wait(), timeout=1)
        assert len(manager.calls) == 1
        manager.release.set()
        await wait_for(
            lambda: (
                context.session is not None
                and context.session.get(SessionKeys.CURRENT_TASK) is None
            )
        )
    finally:
        manager.release.set()
        await ssh_session_handlers.shutdown_tasks()


@pytest.mark.asyncio
async def test_parent_rollback_never_opens_ssh(tmp_path: Path) -> None:
    await ssh_session_handlers.shutdown_tasks()
    context = TransactionalContext(connected_session())
    manager = CommandGateManager(tmp_path)

    async def parent(working: SessionStub) -> None:
        await ssh_session_handlers._handle_connected_session(
            "hostname",
            context,
            working,
            cast(Any, manager),
        )
        await asyncio.sleep(0)
        assert manager.calls == []
        raise RuntimeError("force rollback")

    try:
        with pytest.raises(RuntimeError, match="force rollback"):
            await context.run_parent(parent)
        await wait_for(lambda: not ssh_session_handlers._COMMAND_JOBS)
        assert manager.calls == []
        assert context.session is not None
        assert context.session.get(SessionKeys.STATE) == "connected"
        assert context.session.get(SessionKeys.CURRENT_TASK) is None
    finally:
        manager.release.set()
        await ssh_session_handlers.shutdown_tasks()


@pytest.mark.asyncio
async def test_parent_cancellation_never_opens_ssh(tmp_path: Path) -> None:
    await ssh_session_handlers.shutdown_tasks()
    context  = TransactionalContext(connected_session())
    manager  = CommandGateManager(tmp_path)
    launched = asyncio.Event()

    async def parent(working: SessionStub) -> None:
        await ssh_session_handlers._handle_connected_session(
            "hostname",
            context,
            working,
            cast(Any, manager),
        )
        launched.set()
        await asyncio.Event().wait()

    transaction = asyncio.create_task(context.run_parent(parent))
    try:
        await asyncio.wait_for(launched.wait(), timeout=1)
        await asyncio.sleep(0)
        transaction.cancel()
        with pytest.raises(asyncio.CancelledError):
            await transaction
        await wait_for(lambda: not ssh_session_handlers._COMMAND_JOBS)
        assert manager.calls == []
        assert context.session is not None
        assert context.session.get(SessionKeys.STATE) == "connected"
        assert context.session.get(SessionKeys.CURRENT_TASK) is None
    finally:
        manager.release.set()
        await ssh_session_handlers.shutdown_tasks()


@pytest.mark.asyncio
async def test_session_replacement_before_commit_never_opens_ssh(tmp_path: Path) -> None:
    await ssh_session_handlers.shutdown_tasks()
    context                 = TransactionalContext(connected_session())
    manager                 = CommandGateManager(tmp_path)
    replacement             = SessionStub({SessionKeys.STATE: "other"})
    replacement.plugin_name = "other-plugin"

    try:
        await context.run_parent(
            command_parent("hostname", context, manager), replacement=replacement
        )
        await wait_for(lambda: not ssh_session_handlers._COMMAND_JOBS)
        assert manager.calls == []
        assert context.session is replacement
    finally:
        manager.release.set()
        await ssh_session_handlers.shutdown_tasks()


@pytest.mark.asyncio
async def test_old_job_cannot_overwrite_new_generation_or_unregister_it(tmp_path: Path) -> None:
    await ssh_session_handlers.shutdown_tasks()
    context = TransactionalContext(connected_session())
    manager = CommandGateManager(tmp_path, output="__XQ_CWD__/old/cwd\n")

    await context.run_parent(command_parent("cd /old/cwd", context, manager))
    await asyncio.wait_for(manager.started.wait(), timeout=1)
    assert context.session is not None
    old_job_id = context.session.get(SessionKeys.CURRENT_TASK)
    assert isinstance(old_job_id, str)

    new_job_id = "f" * 32

    def install_new_generation(current: SessionStub) -> None:
        current.set(SessionKeys.CURRENT_TASK, new_job_id)
        current.set(SessionKeys.CWD, "/new/cwd")
        current.set(SessionKeys.STATE, "executing")

    await context.update_session(install_new_generation)
    never    = asyncio.Event()
    new_task = asyncio.create_task(never.wait())
    new_job  = ssh_session_handlers._CommandJob(
        key         = (context.current_user_id, context.current_group_id),
        server_name = "srv1",
        job_id      = new_job_id,
        task        = cast("asyncio.Task[None]", new_task),
    )
    ssh_session_handlers._register_job(new_job)

    try:
        manager.release.set()
        await wait_for(lambda: old_job_id not in ssh_session_handlers._COMMAND_JOBS)
        assert context.session.get(SessionKeys.CURRENT_TASK) == new_job_id
        assert context.session.get(SessionKeys.STATE) == "executing"
        assert context.session.get(SessionKeys.CWD) == "/new/cwd"
        assert (
            ssh_session_handlers._find_job(
                (context.current_user_id, context.current_group_id),
                "srv1",
                new_job_id,
            )
            is new_job
        )
    finally:
        new_task.cancel()
        await asyncio.gather(new_task, return_exceptions=True)
        ssh_session_handlers._remove_job_if_current(new_job)
        await ssh_session_handlers.shutdown_tasks()


@pytest.mark.asyncio
async def test_shutdown_cancels_all_jobs_and_restores_owned_session(tmp_path: Path) -> None:
    await ssh_session_handlers.shutdown_tasks()
    context = TransactionalContext(connected_session())
    manager = CommandGateManager(tmp_path)

    await context.run_parent(command_parent("sleep 60", context, manager))
    await asyncio.wait_for(manager.started.wait(), timeout=1)
    await ssh_session_handlers.shutdown_tasks()

    assert context.session is not None
    assert context.session.get(SessionKeys.STATE) == "connected"
    assert context.session.get(SessionKeys.CURRENT_TASK) is None
    assert ssh_session_handlers._COMMAND_JOBS == {}
    assert ssh_session_handlers._CURRENT_JOB_BY_KEY == {}


@pytest.mark.asyncio
async def test_stale_job_checks_do_not_touch_replacement_session_metadata() -> None:
    manager     = SessionManager()
    replacement = await manager.create(
        10001,
        50001,
        "qingssh",
        {
            SessionKeys.STATE      : "other",
            SessionKeys.SERVER_NAME: "new-server",
        },
    )

    async def update_session(callback: Any) -> Any:
        return await manager.update(10001, 50001, callback)

    assert (
        await ssh_session_handlers._session_job_is_current(
            update_session,
            server_name = "old-server",
            job_id      = "a" * 32,
        )
        is False
    )
    after_guard = await manager.peek(10001, 50001)
    assert after_guard == replacement

    assert (
        await ssh_session_handlers._commit_job_result(
            update_session,
            server_name = "old-server",
            job_id      = "a" * 32,
            cwd         = "/must-not-commit",
        )
        is False
    )
    assert await manager.peek(10001, 50001) == replacement


@pytest.mark.asyncio
async def test_repeated_worker_cancellation_cannot_interrupt_final_session_cas(
    tmp_path: Path,
) -> None:
    await ssh_session_handlers.shutdown_tasks()
    context         = TransactionalContext(connected_session())
    manager         = CommandGateManager(tmp_path)
    real_update     = context.update_session
    cleanup_entered = asyncio.Event()
    allow_cleanup   = asyncio.Event()
    update_calls    = 0

    async def gated_update(callback: Any) -> Any:
        nonlocal update_calls
        update_calls += 1
        if update_calls == 2:
            cleanup_entered.set()
            await allow_cleanup.wait()
        return await real_update(callback)

    context.update_session = gated_update

    await context.run_parent(command_parent("sleep 60", context, manager))
    await asyncio.wait_for(manager.started.wait(), timeout=1)
    assert context.session is not None
    job_id = context.session.get(SessionKeys.CURRENT_TASK)
    job    = ssh_session_handlers._COMMAND_JOBS[job_id]

    try:
        job.task.cancel()
        await asyncio.wait_for(cleanup_entered.wait(), timeout=1)
        job.task.cancel()
        await asyncio.sleep(0)
        assert not job.task.done()
        assert ssh_session_handlers._COMMAND_JOBS.get(job_id) is job

        allow_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await job.task

        assert context.session.get(SessionKeys.STATE) == "connected"
        assert context.session.get(SessionKeys.CURRENT_TASK) is None
        assert job_id not in ssh_session_handlers._COMMAND_JOBS
        assert ssh_session_handlers._CURRENT_JOB_BY_KEY == {}
    finally:
        allow_cleanup.set()
        manager.release.set()
        await ssh_session_handlers.shutdown_tasks()


@pytest.mark.parametrize("termination", ["rollback", "cancel"])
@pytest.mark.asyncio
async def test_close_parent_failure_does_not_deadlock_or_leave_executing_session(
    tmp_path: Path,
    termination: str,
) -> None:
    await ssh_session_handlers.shutdown_tasks()
    context                                 = TransactionalContext(connected_session())
    remote                                  = _HangingRemote()
    manager                                 = ssh_manager_module.SSHManager(tmp_path)
    manager.connections["10001:50001:srv1"] = cast(Any, _HangingClient(remote))
    context.state                           = {"ssh_manager": manager}
    context.data_dir                        = tmp_path

    await context.run_parent(command_parent("sleep 60", context, manager))
    for _ in range(500):
        if any(
            isinstance(active, dict) and active["remote_pid"]
            for active in manager.active_channels.values()
        ):
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("remote command did not start")
    close_returned = asyncio.Event()
    assert context.session is not None
    job = ssh_session_handlers._COMMAND_JOBS[context.session.get(SessionKeys.CURRENT_TASK)]

    async def close_then_fail(working: SessionStub) -> None:
        await ssh_session_handlers.close_session(context, working)
        close_returned.set()
        if termination == "rollback":
            raise RuntimeError("rollback close")
        await asyncio.Event().wait()

    transaction = asyncio.create_task(context.run_parent(close_then_fail))
    try:
        await asyncio.wait_for(close_returned.wait(), timeout=1)
        if termination == "cancel":
            transaction.cancel()
            with pytest.raises(asyncio.CancelledError):
                await transaction
        else:
            with pytest.raises(RuntimeError, match="rollback close"):
                await transaction

        await asyncio.wait_for(asyncio.gather(job.task, return_exceptions=True), timeout=1)
        assert context.session is not None
        assert context.session.get(SessionKeys.STATE) == "connected"
        assert context.session.get(SessionKeys.CURRENT_TASK) is None
        assert not ssh_session_handlers._COMMAND_JOBS
        # 先终止远端命令并关掉通道，再断开这个会话的连接。
        assert manager.connections == {}
        assert remote.events[0] == "kill"
        assert remote.events.index("channel.close") < remote.events.index("client.close")
        assert remote.events.count("client.close") == 1
    finally:
        if not transaction.done():
            transaction.cancel()
            await asyncio.gather(transaction, return_exceptions=True)
        await ssh_session_handlers.shutdown_tasks()


@pytest.mark.asyncio
async def test_close_cancels_only_the_exact_session_generation(tmp_path: Path) -> None:
    await ssh_session_handlers.shutdown_tasks()
    context = with_settings_reader(Mock(current_user_id=10001, current_group_id=50001))
    context.state    = {"ssh_manager": ssh_manager_module.SSHManager(tmp_path)}
    context.data_dir = tmp_path
    key              = (context.current_user_id, context.current_group_id)
    never            = asyncio.Event()
    old_task         = asyncio.create_task(never.wait())
    current_task     = asyncio.create_task(never.wait())
    old_job          = ssh_session_handlers._CommandJob(key, "srv1", "a" * 32, old_task)
    current_job      = ssh_session_handlers._CommandJob(key, "srv1", "b" * 32, current_task)
    ssh_session_handlers._register_job(old_job)
    ssh_session_handlers._register_job(current_job)
    session = SessionStub(
        {
            SessionKeys.SERVER_NAME : "srv1",
            SessionKeys.STATE       : "executing",
            SessionKeys.CURRENT_TASK: old_job.job_id,
        }
    )

    try:
        await ssh_session_handlers.close_session(context, session)
        await asyncio.sleep(0)
        assert old_task.cancelled()
        assert not current_task.done()
        assert session.get(SessionKeys.STATE) == "connected"
        assert session.get(SessionKeys.CURRENT_TASK) is None
        # This synthetic task has no QingSSH finalizer. Production workers
        # compare-and-remove their own entry after cancellation.
        assert old_job.job_id in ssh_session_handlers._COMMAND_JOBS
        assert current_job.job_id in ssh_session_handlers._COMMAND_JOBS
    finally:
        current_task.cancel()
        await asyncio.gather(current_task, return_exceptions=True)
        ssh_session_handlers._remove_job_if_current(old_job)
        ssh_session_handlers._remove_job_if_current(current_job)
        await ssh_session_handlers.shutdown_tasks()


@pytest.mark.asyncio
async def test_close_cancels_exact_job_before_manager_initialization_failure() -> None:
    await ssh_session_handlers.shutdown_tasks()
    context = with_settings_reader(Mock(current_user_id=10001, current_group_id=50001))
    context.state = None  # 没有可用的管理器状态：获取管理器时失败。
    key           = (context.current_user_id, context.current_group_id)
    task          = asyncio.create_task(asyncio.Event().wait())
    job           = ssh_session_handlers._CommandJob(key, "srv1", "c" * 32, task)
    ssh_session_handlers._register_job(job)
    session = SessionStub(
        {
            SessionKeys.SERVER_NAME : "srv1",
            SessionKeys.STATE       : "executing",
            SessionKeys.CURRENT_TASK: job.job_id,
        }
    )

    try:
        with pytest.raises(RuntimeError, match="must be a dictionary"):
            await ssh_session_handlers.close_session(context, session)
        await asyncio.sleep(0)
        assert task.cancelled()
        assert session.get(SessionKeys.STATE) == "connected"
        assert session.get(SessionKeys.CURRENT_TASK) is None
    finally:
        ssh_session_handlers._remove_job_if_current(job)
        await ssh_session_handlers.shutdown_tasks()


@pytest.mark.asyncio
async def test_plugin_cleanup_drains_jobs_before_manager_initialization() -> None:
    from plugins.qingssh import main as qingssh_main

    await ssh_session_handlers.shutdown_tasks()
    task = asyncio.create_task(asyncio.Event().wait())
    job  = ssh_session_handlers._CommandJob((10001, 50001), "srv1", "d" * 32, task)
    ssh_session_handlers._register_job(job)

    try:
        # 管理器从未创建：插件清理仍然先排空后台任务。
        await qingssh_main.shutdown(Mock())

        assert task.cancelled()
        assert ssh_session_handlers._COMMAND_JOBS == {}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        ssh_session_handlers._remove_job_if_current(job)
        await ssh_session_handlers.shutdown_tasks()


@pytest.mark.asyncio
async def test_stop_reply_does_not_claim_remote_exit_when_only_local_cleanup_succeeded():
    class UnknownStopManager:
        def is_connected(self, *_args):
            return True

        async def stop_command(self, *_args):
            return ssh_manager_module.CommandTerminationResult(
                found            = True,
                local_cleaned    = True,
                remote_confirmed = False,
                error            = "missing remote PID",
            )

    context = with_settings_reader(Mock(current_user_id=10001, current_group_id=50001))
    context.end_session = AsyncMock()
    session             = SessionStub(
        {
            SessionKeys.STATE      : "executing",
            SessionKeys.SERVER_NAME: "srv1",
        }
    )

    result = await ssh_session_handlers._handle_connected_session(
        "停止",
        context,
        session,
        cast(Any, UnknownStopManager()),
    )

    text = result[0]["data"]["text"]
    assert "远端进程状态未知" in text
    assert "已确认停止" not in text


def test_qingssh_disconnect_respects_explicit_target_without_ending_current_session():
    async def _run():
        manager                  = DisconnectManagerStub()
        context                  = with_settings_reader(Mock())
        context.current_user_id  = 10001
        context.current_group_id = 50001
        context.end_session      = AsyncMock()
        context.get_session      = AsyncMock(
            return_value=SessionStub({SessionKeys.SERVER_NAME: "current-srv"})
        )

        from plugins.qingssh import handlers as handlers_module

        segments = await handlers_module.handle_ssh_disconnect(
            "other-srv",
            {},
            context,
            cast(Any, manager),
        )

        assert manager.calls == [("10001", "50001", "other-srv")]
        context.end_session.assert_not_awaited()
        assert "other-srv" in segments[0]["data"]["text"]

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_qingssh_connect_rolls_back_client_when_session_creation_fails():
    from plugins.qingssh import handlers as handlers_module

    class Manager:
        def __init__(self):
            self.disconnected: list[tuple[str, str, str]] = []

        def resolve_server(self, _name, *, username_override=None):
            return {"host": "host.internal", "port": 22, "username": "root"}

        async def connect(self, *_args, **_kwargs):
            return True, "connected"

        def disconnect(self, user_id, group_id, server_name):
            self.disconnected.append((user_id, group_id, server_name))
            return True

    manager = Manager()
    context = with_settings_reader(Mock(current_user_id=10001, current_group_id=50001))
    context.create_session = AsyncMock(side_effect=RuntimeError("session unavailable"))

    with pytest.raises(RuntimeError, match="session unavailable"):
        await handlers_module._connect_to_server("srv", context, cast(Any, manager))

    assert manager.disconnected == [("10001", "50001", "srv")]


@pytest.mark.asyncio
async def test_qingssh_main_does_not_replace_another_plugin_session():
    from plugins.qingssh import handlers as handlers_module

    session             = SessionStub()
    session.plugin_name = "pendo"
    context = with_settings_reader(Mock(current_user_id=10001, current_group_id=50001))
    context.get_session = AsyncMock(return_value=session)
    manager = Mock()

    result = await handlers_module.handle_ssh_main("srv", {}, context, manager)

    assert "先结束当前会话" in result[0]["data"]["text"]
    manager.connect.assert_not_called()


@pytest.mark.asyncio
async def test_qingssh_interactive_connect_refuses_the_agent_command_runner():
    """交互连接按“用户 + 会话 + 服务器”登记，替 agent 连接会顶掉本人正在用的同名连接。"""

    from core.interfaces import COMMAND_RUNNER_EVENT_KEY
    from plugins.qingssh import handlers as handlers_module

    context = with_settings_reader(Mock(current_user_id=10001, current_group_id=50001))
    context.get_session = AsyncMock(return_value=None)
    manager = Mock()

    result = await handlers_module.handle_ssh_main(
        "srv", {COMMAND_RUNNER_EVENT_KEY: "xiaoqing_agent"}, context, manager
    )

    assert "ssh_run" in result[0]["data"]["text"]
    manager.connect.assert_not_called()


@pytest.mark.asyncio
async def test_qingssh_quick_add_rejects_surplus_arguments():
    from plugins.qingssh import handlers as handlers_module

    context = with_settings_reader(Mock())
    context.get_session = AsyncMock(return_value=None)
    manager            = Mock()
    manager.add_server = AsyncMock()

    result = await handlers_module.handle_ssh_add(
        "srv host.internal 22 root ignored",
        {},
        context,
        cast(Any, manager),
    )

    assert "参数过多" in result[0]["data"]["text"]
    manager.add_server.assert_not_awaited()


@pytest.mark.asyncio
async def test_qingssh_quick_add_rejects_unclosed_quote():
    from plugins.qingssh import handlers as handlers_module

    context = with_settings_reader(Mock())
    context.get_session = AsyncMock(return_value=None)
    manager            = Mock()
    manager.add_server = AsyncMock()

    result = await handlers_module.handle_ssh_add(
        '"srv host.internal 22 root',
        {},
        context,
        cast(Any, manager),
    )

    assert "引号没有闭合" in result[0]["data"]["text"]
    manager.add_server.assert_not_awaited()


@pytest.mark.asyncio
async def test_qingssh_main_preserves_dash_prefixed_quick_add_host(tmp_path: Path):
    """主路由不得把快速添加的位置参数误解析成 Core 选项。"""
    context = with_settings_reader(Mock())
    context.get_session = AsyncMock(return_value=None)
    manager          = ssh_manager_module.SSHManager(tmp_path)
    context.state    = {"ssh_manager": manager}
    context.data_dir = tmp_path

    result = await qingssh_main.handle(
        "ssh",
        "add srv -host.internal 22 root",
        {},
        context,
    )

    assert "主机地址格式无效" in result[0]["data"]["text"]
    assert manager.servers == {}
    assert not manager.servers_file.exists()
