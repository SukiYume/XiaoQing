"""入站监听候选切换、回滚和取消收敛。"""

from __future__ import annotations

import tests.helpers.app_test_support as _fixture_support
from tests.helpers.app_test_support import (
    ApplicationLifecycleFatalError,
    AsyncMock,
    ClientSession,
    InboundManager,
    InboundReconcileError,
    Mock,
    Path,
    XiaoQingApp,
    asyncio,
    patch,
    pytest,
)

mock_dependencies = _fixture_support.mock_dependencies
temp_app_root     = _fixture_support.temp_app_root


@pytest.mark.asyncio
async def test_reconcile_inbound_restarts_when_proxy_security_declaration_changes(
    temp_app_root: Path,
):
    app         = XiaoQingApp(temp_app_root)
    old_manager = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
        trusted_tls_proxy = False,
    )
    new_manager = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
        trusted_tls_proxy = True,
    )
    events: list[str] = []

    async def stop_old() -> None:
        events.append("old.stop")

    async def start_new() -> None:
        events.append("new.start")

    old_manager.stop = AsyncMock(side_effect=stop_old)
    new_manager.start = AsyncMock(side_effect=start_new)
    app.inbound_manager = old_manager

    with patch("core.app_ingress.InboundManager.from_config", return_value=new_manager):
        await app._reconcile_inbound_manager({}, {"inbound_token": "token"})

    old_manager.stop.assert_awaited_once()
    new_manager.start.assert_awaited_once()
    assert events == ["old.stop", "new.start"]
    assert app.inbound_manager is new_manager
    app.scheduler.shutdown()


@pytest.mark.asyncio
async def test_invalid_inbound_config_never_stops_current(temp_app_root: Path):
    app     = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    current.stop        = AsyncMock()
    app.inbound_manager = current

    with (
        patch(
            "core.app_ingress.InboundManager.from_config",
            side_effect=ValueError("invalid listener"),
        ),
        pytest.raises(ValueError, match="invalid listener"),
    ):
        await app._reconcile_inbound_manager({}, {"inbound_token": "token"})

    current.stop.assert_not_awaited()
    assert app.inbound_manager is current
    app.scheduler.shutdown()


@pytest.mark.asyncio
async def test_disjoint_candidate_bind_failure_keeps_current_running(temp_app_root: Path):
    app     = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base = "http://127.0.0.1:12001",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    current.stop  = AsyncMock()
    current.start = AsyncMock()
    desired.start = AsyncMock(side_effect=OSError("bind failed"))
    desired.stop        = AsyncMock()
    app.inbound_manager = current

    with (
        patch("core.app_ingress.InboundManager.from_config", return_value=desired),
        pytest.raises(OSError, match="bind failed"),
    ):
        await app._reconcile_inbound_manager({}, {"inbound_token": "token"})

    desired.stop.assert_awaited_once()
    current.stop.assert_not_awaited()
    current.start.assert_not_awaited()
    assert app.inbound_manager is current
    app.scheduler.shutdown()


@pytest.mark.asyncio
async def test_failed_candidate_cleanup_remains_owned_until_terminal_cleanup(
    temp_app_root: Path,
):
    app     = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base = "http://127.0.0.1:12001",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    current.stop = AsyncMock()
    desired.start = AsyncMock(side_effect=OSError("bind failed"))
    desired.stop = AsyncMock(side_effect=[RuntimeError("cleanup failed"), None])
    app.inbound_manager = current

    with (
        patch("core.app_ingress.InboundManager.from_config", return_value=desired),
        pytest.raises(OSError, match="bind failed"),
    ):
        await app._reconcile_inbound_manager({}, {"inbound_token": "token"})

    assert app.inbound_manager is current
    assert app._inbound_cleanup_pending == [desired]

    await app.stop()

    assert app._inbound_cleanup_pending == []
    assert desired.stop.await_count == 2
    current.stop.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_disjoint_occupied_port_switch_keeps_old_health_endpoint_live(
    temp_app_root: Path,
    unused_tcp_port_factory,
):
    old_port      = unused_tcp_port_factory()
    occupied_port = unused_tcp_port_factory()
    app           = XiaoQingApp(temp_app_root)
    current       = InboundManager(
        inbound_http_base = f"http://127.0.0.1:{old_port}",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    await current.start()
    app.inbound_manager = current
    occupied            = await asyncio.start_server(
        lambda _reader, writer: writer.close(), "127.0.0.1", occupied_port
    )

    try:
        with pytest.raises(OSError):
            await app._reconcile_inbound_manager(
                {
                    "enable_inbound_server": True,
                    "inbound_http_base": f"http://127.0.0.1:{occupied_port}",
                    "inbound_ws_uri": "",
                },
                {"inbound_token": "token"},
            )

        assert app.inbound_manager is current
        async with ClientSession() as session:
            response = await session.get(
                f"http://127.0.0.1:{old_port}/health",
                headers={"Authorization": "Bearer token"},
            )
            # The old socket remains owned and responsive, but a structural
            # endpoint change synchronously revokes its credential before the
            # replacement bind is attempted.
            assert response.status == 401
            assert current._token == ""
    finally:
        occupied.close()
        await occupied.wait_closed()
        await current.stop()
        app.scheduler.shutdown()


@pytest.mark.asyncio
async def test_disjoint_inbound_switch_starts_candidate_before_stopping_current(
    temp_app_root: Path,
):
    app     = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base = "http://127.0.0.1:12001",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    events: list[str] = []
    desired.start     = AsyncMock(
        side_effect=lambda *, accept_events=True: events.append(f"new.start:{accept_events}")
    )
    desired.commit_admission = Mock(side_effect=lambda: events.append("new.commit"))
    current.stop = AsyncMock(side_effect=lambda: events.append("old.stop"))
    app.inbound_manager = current

    with patch("core.app_ingress.InboundManager.from_config", return_value=desired):
        await app._reconcile_inbound_manager({}, {"inbound_token": "token"})

    assert events == ["new.start:False", "old.stop", "new.commit"]
    assert app.inbound_manager is desired
    app.scheduler.shutdown()


@pytest.mark.asyncio
async def test_overlapping_candidate_failure_restores_current(temp_app_root: Path):
    app     = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
        trusted_tls_proxy = True,
    )
    events: list[str] = []
    current.stop = AsyncMock(side_effect=lambda: events.append("old.stop"))
    current.start = AsyncMock(side_effect=lambda: events.append("old.start"))

    async def fail_candidate() -> None:
        events.append("new.start")
        raise OSError("candidate failed")

    desired.start = AsyncMock(side_effect=fail_candidate)
    desired.stop = AsyncMock(side_effect=lambda: events.append("new.stop"))
    app.inbound_manager = current

    with (
        patch("core.app_ingress.InboundManager.from_config", return_value=desired),
        pytest.raises(OSError, match="candidate failed"),
    ):
        await app._reconcile_inbound_manager({}, {"inbound_token": "token"})

    assert events == ["old.stop", "new.start", "new.stop", "old.start"]
    assert app.inbound_manager is current
    app.scheduler.shutdown()


@pytest.mark.asyncio
async def test_overlapping_candidate_and_restore_failure_clears_current(temp_app_root: Path):
    app     = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
        trusted_tls_proxy = True,
    )
    current.stop = AsyncMock()
    current.start = AsyncMock(side_effect=OSError("restore failed"))
    desired.start = AsyncMock(side_effect=OSError("candidate failed"))
    desired.stop        = AsyncMock()
    app.inbound_manager = current

    with (
        patch("core.app_ingress.InboundManager.from_config", return_value=desired),
        pytest.raises(InboundReconcileError, match="could not be restored"),
    ):
        await app._reconcile_inbound_manager({}, {"inbound_token": "token"})

    assert app.inbound_manager is None
    assert app._inbound_cleanup_pending == [current]
    app.scheduler.shutdown()


@pytest.mark.asyncio
async def test_restore_fatal_and_cleanup_failure_remain_owned_until_stop_retry(
    temp_app_root: Path,
):
    class RestoreFatal(BaseException):
        pass

    app     = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
        trusted_tls_proxy = True,
    )
    fatal = RestoreFatal("restore fatal")
    current.stop = AsyncMock(side_effect=[None, RuntimeError("old cleanup failed"), None])
    current.start = AsyncMock(side_effect=fatal)
    desired.start = AsyncMock(side_effect=OSError("candidate failed"))
    desired.stop        = AsyncMock()
    app.inbound_manager = current

    with patch("core.app_ingress.InboundManager.from_config", return_value=desired):
        task = asyncio.create_task(app._reconcile_inbound_manager({}, {"inbound_token": "token"}))
        with pytest.raises(InboundReconcileError) as exc_info:
            await task

    assert exc_info.value.restore_error is fatal
    assert app.inbound_manager is None
    assert app._inbound_cleanup_pending == [current]

    await app.stop()
    assert app._lifecycle_state.value == "failed"
    assert app._inbound_cleanup_pending == [current]

    await app.stop()
    assert app._lifecycle_state.value == "stopped"
    assert app._inbound_cleanup_pending == []
    assert current.stop.await_count == 3


@pytest.mark.asyncio
async def test_inbound_candidate_fatal_is_task_safe_and_keeps_current(temp_app_root: Path):
    class CandidateFatal(BaseException):
        pass

    app     = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base = "http://127.0.0.1:12001",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    fatal        = CandidateFatal("candidate fatal")
    current.stop = AsyncMock()
    desired.start = AsyncMock(side_effect=fatal)
    desired.stop        = AsyncMock()
    app.inbound_manager = current

    with patch("core.app_ingress.InboundManager.from_config", return_value=desired):
        task = asyncio.create_task(app._reconcile_inbound_manager({}, {"inbound_token": "token"}))
        with pytest.raises(ApplicationLifecycleFatalError) as exc_info:
            await task

    assert exc_info.value.original is fatal
    desired.stop.assert_awaited_once()
    current.stop.assert_not_awaited()
    assert app.inbound_manager is current
    app.scheduler.shutdown()


@pytest.mark.asyncio
async def test_inbound_reconcile_cancellation_finishes_rollback(temp_app_root: Path):
    app     = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
        trusted_tls_proxy = True,
    )
    entered       = asyncio.Event()
    blocker       = asyncio.Event()
    current.stop  = AsyncMock()
    current.start = AsyncMock()
    desired.stop  = AsyncMock()

    async def block_candidate() -> None:
        entered.set()
        await blocker.wait()

    desired.start = AsyncMock(side_effect=block_candidate)
    app.inbound_manager = current

    with patch("core.app_ingress.InboundManager.from_config", return_value=desired):
        task = asyncio.create_task(app._reconcile_inbound_manager({}, {"inbound_token": "token"}))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    desired.stop.assert_awaited_once()
    current.start.assert_awaited_once()
    assert app.inbound_manager is current
    app.scheduler.shutdown()


@pytest.mark.asyncio
async def test_repeated_cancellation_during_restore_wins_after_rollback(temp_app_root: Path):
    app     = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
        trusted_tls_proxy = True,
    )
    restore_entered = asyncio.Event()
    restore_release = asyncio.Event()
    current.stop    = AsyncMock()

    async def restore_current() -> None:
        restore_entered.set()
        await restore_release.wait()

    current.start = AsyncMock(side_effect=restore_current)
    desired.start = AsyncMock(side_effect=OSError("candidate failed"))
    desired.stop        = AsyncMock()
    app.inbound_manager = current

    with patch("core.app_ingress.InboundManager.from_config", return_value=desired):
        task = asyncio.create_task(app._reconcile_inbound_manager({}, {"inbound_token": "token"}))
        await restore_entered.wait()
        task.cancel()
        task.cancel()
        await asyncio.sleep(0)
        restore_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert task.cancelling() >= 2
    assert app.inbound_manager is current
    desired.stop.assert_awaited_once()
    current.start.assert_awaited_once()
    app.scheduler.shutdown()
