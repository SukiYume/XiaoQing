"""后台作业、会话代际和清理。"""

from __future__ import annotations

from tests.helpers.qingssh_test_support import (
    Any,
    AsyncMock,
    Mock,
    Path,
    SessionKeys,
    SessionManager,
    _command_parent,
    _CommandGateManager,
    _connected_session,
    _DisconnectManagerStub,
    _ManagerStub,
    _SessionStub,
    _TransactionalContext,
    _wait_for,
    asyncio,
    cast,
    inspect,
    pytest,
    qingssh_main,
    ssh_manager_module,
    ssh_session_handlers,
)
from tests.helpers.settings_snapshot import with_settings_reader


def test_qingssh_session_does_not_store_task_object():
    async def _run():
        manager                  = _ManagerStub()
        context                  = with_settings_reader(Mock())
        context.current_user_id  = 10001
        context.current_group_id = 50001
        context.send_action      = AsyncMock()
        context.end_session      = AsyncMock()

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
    manager                  = _ManagerStub()
    context                  = with_settings_reader(Mock())
    context.current_user_id  = 10001
    context.current_group_id = 50001
    context.send_action      = AsyncMock()
    context.end_session      = AsyncMock()
    context.config           = {}
    context.request_id       = "sensitive-history-test"
    session                  = _connected_session()

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
    context = _TransactionalContext(_connected_session())
    manager = _CommandGateManager(tmp_path)

    try:
        await context.run_parent(_command_parent("export X='a\"b'", context, manager))
        await manager.started.wait()
        assert context.session is not None
        assert context.session.get(SessionKeys.ENV_VARS) == {"X": 'a"b'}
        manager.release.set()
        await _wait_for(lambda: not ssh_session_handlers._COMMAND_JOBS)

        invalid = await ssh_session_handlers._handle_connected_session(
            "export X='unterminated",
            context,
            _connected_session(),
            cast(Any, manager),
        )
        assert "引号" in invalid[0]["data"]["text"]
    finally:
        manager.release.set()
        await ssh_session_handlers.shutdown_tasks()


@pytest.mark.asyncio
async def test_existing_sensitive_history_is_scrubbed_before_display_or_replay() -> None:
    await ssh_session_handlers.shutdown_tasks()
    manager                  = _ManagerStub()
    context                  = with_settings_reader(Mock())
    context.current_user_id  = 10001
    context.current_group_id = 50001
    context.end_session      = AsyncMock()
    session                  = _connected_session()
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
    manager                  = _ManagerStub()
    context                  = with_settings_reader(Mock())
    context.current_user_id  = 10001
    context.current_group_id = 50001
    context.send_action      = AsyncMock()
    context.end_session      = AsyncMock()
    context.config           = {}
    context.request_id       = "ordinary-history-test"
    session                  = _connected_session()

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


def test_background_worker_has_no_session_or_legacy_execution_parameter() -> None:
    parameters = inspect.signature(ssh_session_handlers._run_background_command).parameters

    assert "session" not in parameters
    assert "legacy_session" not in parameters
    assert list(parameters)[:2] == ["update_session", "send_action"]


@pytest.mark.asyncio
async def test_background_waits_for_parent_commit_before_opening_ssh(tmp_path: Path) -> None:
    await ssh_session_handlers.shutdown_tasks()
    context = _TransactionalContext(_connected_session())
    manager = _CommandGateManager(tmp_path)

    async def parent(working: _SessionStub) -> None:
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
        await _wait_for(
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
    context = _TransactionalContext(_connected_session())
    manager = _CommandGateManager(tmp_path)

    async def parent(working: _SessionStub) -> None:
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
        await _wait_for(lambda: not ssh_session_handlers._COMMAND_JOBS)
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
    context  = _TransactionalContext(_connected_session())
    manager  = _CommandGateManager(tmp_path)
    launched = asyncio.Event()

    async def parent(working: _SessionStub) -> None:
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
        await _wait_for(lambda: not ssh_session_handlers._COMMAND_JOBS)
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
    context                 = _TransactionalContext(_connected_session())
    manager                 = _CommandGateManager(tmp_path)
    replacement             = _SessionStub({SessionKeys.STATE: "other"})
    replacement.plugin_name = "other-plugin"

    try:
        await context.run_parent(
            _command_parent("hostname", context, manager), replacement=replacement
        )
        await _wait_for(lambda: not ssh_session_handlers._COMMAND_JOBS)
        assert manager.calls == []
        assert context.session is replacement
    finally:
        manager.release.set()
        await ssh_session_handlers.shutdown_tasks()


@pytest.mark.asyncio
async def test_old_job_cannot_overwrite_new_generation_or_unregister_it(tmp_path: Path) -> None:
    await ssh_session_handlers.shutdown_tasks()
    context = _TransactionalContext(_connected_session())
    manager = _CommandGateManager(tmp_path, output="/old/cwd\n")

    await context.run_parent(_command_parent("cd /old/cwd", context, manager))
    await asyncio.wait_for(manager.started.wait(), timeout=1)
    assert context.session is not None
    old_job_id = context.session.get(SessionKeys.CURRENT_TASK)
    assert isinstance(old_job_id, str)

    new_job_id = "f" * 32

    def install_new_generation(current: _SessionStub) -> None:
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
        await _wait_for(lambda: old_job_id not in ssh_session_handlers._COMMAND_JOBS)
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
    context = _TransactionalContext(_connected_session())
    manager = _CommandGateManager(tmp_path)

    await context.run_parent(_command_parent("sleep 60", context, manager))
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
            SessionKeys.STATE: "other",
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
    context         = _TransactionalContext(_connected_session())
    manager         = _CommandGateManager(tmp_path)
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

    await context.run_parent(_command_parent("sleep 60", context, manager))
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
    monkeypatch: pytest.MonkeyPatch,
    termination: str,
) -> None:
    await ssh_session_handlers.shutdown_tasks()
    context = _TransactionalContext(_connected_session())
    manager = _CommandGateManager(tmp_path)
    monkeypatch.setattr(ssh_session_handlers, "get_manager", AsyncMock(return_value=manager))

    await context.run_parent(_command_parent("sleep 60", context, manager))
    await asyncio.wait_for(manager.started.wait(), timeout=1)
    close_returned = asyncio.Event()

    async def close_then_fail(working: _SessionStub) -> None:
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

        await asyncio.wait_for(
            _wait_for(
                lambda: (
                    context.session is not None
                    and context.session.get(SessionKeys.STATE) == "connected"
                    and context.session.get(SessionKeys.CURRENT_TASK) is None
                    and not ssh_session_handlers._COMMAND_JOBS
                ),
                attempts=1000,
            ),
            timeout=1,
        )
        assert manager.disconnects == [("10001", "50001", "srv1")]
        assert manager.close_operations == ["stop", "disconnect"]
    finally:
        if not transaction.done():
            transaction.cancel()
            await asyncio.gather(transaction, return_exceptions=True)
        manager.release.set()
        await ssh_session_handlers.shutdown_tasks()


@pytest.mark.asyncio
async def test_close_cancels_only_the_exact_session_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await ssh_session_handlers.shutdown_tasks()
    context = with_settings_reader(Mock(current_user_id=10001, current_group_id=50001))
    manager = _DisconnectManagerStub()
    monkeypatch.setattr(ssh_session_handlers, "get_manager", AsyncMock(return_value=manager))
    key          = (context.current_user_id, context.current_group_id)
    never        = asyncio.Event()
    old_task     = asyncio.create_task(never.wait())
    current_task = asyncio.create_task(never.wait())
    old_job      = ssh_session_handlers._CommandJob(key, "srv1", "a" * 32, old_task)
    current_job  = ssh_session_handlers._CommandJob(key, "srv1", "b" * 32, current_task)
    ssh_session_handlers._register_job(old_job)
    ssh_session_handlers._register_job(current_job)
    session = _SessionStub(
        {
            SessionKeys.SERVER_NAME: "srv1",
            SessionKeys.STATE: "executing",
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
        # This synthetic task has no QingSSH finalizer; production workers
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
async def test_close_cancels_exact_job_before_manager_initialization_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await ssh_session_handlers.shutdown_tasks()
    context = with_settings_reader(Mock(current_user_id=10001, current_group_id=50001))
    key  = (context.current_user_id, context.current_group_id)
    task = asyncio.create_task(asyncio.Event().wait())
    job  = ssh_session_handlers._CommandJob(key, "srv1", "c" * 32, task)
    ssh_session_handlers._register_job(job)
    session = _SessionStub(
        {
            SessionKeys.SERVER_NAME: "srv1",
            SessionKeys.STATE: "executing",
            SessionKeys.CURRENT_TASK: job.job_id,
        }
    )
    monkeypatch.setattr(
        ssh_session_handlers,
        "get_manager",
        AsyncMock(side_effect=RuntimeError("manager unavailable")),
    )

    try:
        with pytest.raises(RuntimeError, match="manager unavailable"):
            await ssh_session_handlers.close_session(context, session)
        await asyncio.sleep(0)
        assert task.cancelled()
        assert session.get(SessionKeys.STATE) == "connected"
        assert session.get(SessionKeys.CURRENT_TASK) is None
    finally:
        ssh_session_handlers._remove_job_if_current(job)
        await ssh_session_handlers.shutdown_tasks()


@pytest.mark.asyncio
async def test_plugin_cleanup_drains_jobs_before_manager_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.qingssh import main as qingssh_main

    drain = AsyncMock()
    monkeypatch.setattr(ssh_session_handlers, "shutdown_tasks", drain)
    monkeypatch.setattr(
        qingssh_main,
        "get_manager",
        AsyncMock(side_effect=RuntimeError("manager unavailable")),
    )

    await qingssh_main.cleanup(Mock())

    drain.assert_awaited_once_with()


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
    session             = _SessionStub(
        {
            SessionKeys.STATE: "executing",
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
        manager                  = _DisconnectManagerStub()
        context                  = with_settings_reader(Mock())
        context.current_user_id  = 10001
        context.current_group_id = 50001
        context.end_session      = AsyncMock()
        context.get_session      = AsyncMock(
            return_value=_SessionStub({SessionKeys.SERVER_NAME: "current-srv"})
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

        def get_server(self, _name):
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
async def test_qingssh_main_does_not_replace_another_plugin_session(
    monkeypatch: pytest.MonkeyPatch,
):
    from plugins.qingssh import handlers as handlers_module

    session             = _SessionStub()
    session.plugin_name = "pendo"
    context = with_settings_reader(Mock(current_user_id=10001, current_group_id=50001))
    context.get_session = AsyncMock(return_value=session)
    manager = Mock()
    monkeypatch.setattr(handlers_module, "PARAMIKO_AVAILABLE", True)

    result = await handlers_module.handle_ssh_main("srv", {}, context, manager)

    assert "先结束当前会话" in result[0]["data"]["text"]
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
async def test_qingssh_main_preserves_dash_prefixed_quick_add_host(
    monkeypatch: pytest.MonkeyPatch,
):
    """主路由不得把快速添加的位置参数误解析成 Core 选项。"""
    context = with_settings_reader(Mock())
    context.get_session = AsyncMock(return_value=None)
    manager = Mock()
    manager.add_server = AsyncMock(return_value=True)
    monkeypatch.setattr(qingssh_main, "get_manager", AsyncMock(return_value=manager))

    result = await qingssh_main.handle(
        "ssh",
        "add srv -host.internal 22 root",
        {},
        context,
    )

    assert "主机地址格式无效" in result[0]["data"]["text"]
    manager.add_server.assert_not_awaited()
