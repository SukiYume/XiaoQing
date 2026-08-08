"""应用启动和入站切换。"""

from __future__ import annotations

import tests.helpers.app_test_support as _fixture_support
from tests.helpers.app_test_support import (
    Any,
    ApplicationLifecycleFatalError,
    AsyncMock,
    ClientSession,
    InboundManager,
    InboundReconcileError,
    MagicMock,
    Mock,
    Path,
    XiaoQingApp,
    asyncio,
    json,
    patch,
    pytest,
)

mock_dependencies = _fixture_support.mock_dependencies
temp_app_root = _fixture_support.temp_app_root


@pytest.mark.asyncio
async def test_reconcile_inbound_restarts_when_proxy_security_declaration_changes(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    old_manager = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
        trusted_tls_proxy=False,
    )
    new_manager = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
        trusted_tls_proxy=True,
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
    app = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
    )
    current.stop = AsyncMock()
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
    app = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base="http://127.0.0.1:12001",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
    )
    current.stop = AsyncMock()
    current.start = AsyncMock()
    desired.start = AsyncMock(side_effect=OSError("bind failed"))
    desired.stop = AsyncMock()
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
    app = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base="http://127.0.0.1:12001",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
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
    old_port = unused_tcp_port_factory()
    occupied_port = unused_tcp_port_factory()
    app = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base=f"http://127.0.0.1:{old_port}",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
    )
    await current.start()
    app.inbound_manager = current
    occupied = await asyncio.start_server(
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
    app = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base="http://127.0.0.1:12001",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
    )
    events: list[str] = []
    desired.start = AsyncMock(
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
    app = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
        trusted_tls_proxy=True,
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
    app = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
        trusted_tls_proxy=True,
    )
    current.stop = AsyncMock()
    current.start = AsyncMock(side_effect=OSError("restore failed"))
    desired.start = AsyncMock(side_effect=OSError("candidate failed"))
    desired.stop = AsyncMock()
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

    app = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
        trusted_tls_proxy=True,
    )
    fatal = RestoreFatal("restore fatal")
    current.stop = AsyncMock(side_effect=[None, RuntimeError("old cleanup failed"), None])
    current.start = AsyncMock(side_effect=fatal)
    desired.start = AsyncMock(side_effect=OSError("candidate failed"))
    desired.stop = AsyncMock()
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

    app = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base="http://127.0.0.1:12001",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
    )
    fatal = CandidateFatal("candidate fatal")
    current.stop = AsyncMock()
    desired.start = AsyncMock(side_effect=fatal)
    desired.stop = AsyncMock()
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
    app = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
        trusted_tls_proxy=True,
    )
    entered = asyncio.Event()
    blocker = asyncio.Event()
    current.stop = AsyncMock()
    current.start = AsyncMock()
    desired.stop = AsyncMock()

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
    app = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="token",
        handler=app._handle_inbound_event,
        trusted_tls_proxy=True,
    )
    restore_entered = asyncio.Event()
    restore_release = asyncio.Event()
    current.stop = AsyncMock()

    async def restore_current() -> None:
        restore_entered.set()
        await restore_release.wait()

    current.start = AsyncMock(side_effect=restore_current)
    desired.start = AsyncMock(side_effect=OSError("candidate failed"))
    desired.stop = AsyncMock()
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


@pytest.mark.asyncio
@pytest.mark.integration
async def test_app_start(temp_app_root: Path):
    """Test app start initializes components"""
    app = XiaoQingApp(temp_app_root)

    # Mock plugin manager methods
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])

    await app.start()

    # Verify HTTP session is created
    assert app.http_session is not None

    # Verify plugins are loaded
    app.plugin_manager.load_all.assert_called_once()
    await app.plugin_manager.wait_inits()

    # Verify session cleanup task is created
    assert app._session_cleanup_task is not None
    assert app._plugin_watch_task is None

    # Cleanup
    await app.stop()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_app_start_defers_plugin_schedule_callbacks_until_one_startup_reconcile(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    app.plugin_manager.schedule_definitions = Mock(return_value=[])
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.load_all = Mock(side_effect=lambda: app._reschedule("test_plugin"))
    app.scheduler.replace_prefix = Mock()

    await app.start()

    app.scheduler.replace_prefix.assert_called_once_with("plugin.", [])
    await app.stop()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_startup_factory_reentrant_security_update_revokes_provisional_candidate(
    temp_app_root: Path,
):
    from core.config import ConfigSnapshot

    app = XiaoQingApp(temp_app_root)
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])
    app.plugin_manager.list_runtime_plugins = Mock(return_value=[])
    old_config = {
        **app.config,
        "onebot_http_base": "",
        "enable_ws_client": False,
        "onebot_ws_uri": "",
        "enable_inbound_server": True,
        "inbound_http_base": "http://127.0.0.1:12000",
        "inbound_ws_uri": "",
    }
    new_config = {
        **old_config,
        "enable_inbound_server": False,
        "inbound_http_base": "",
    }
    old_snapshot = ConfigSnapshot(
        config=old_config,
        secrets={**app.secrets, "inbound_token": "rev1-old"},
        revision=1,
    )
    new_snapshot = ConfigSnapshot(
        config=new_config,
        secrets={**app.secrets, "inbound_token": "rev2-new"},
        revision=2,
    )
    candidate = MagicMock(
        config_key=("http://127.0.0.1:12000", "", 8, 200, 5.0, False),
        start=AsyncMock(),
        stop=AsyncMock(),
    )
    factory_calls = 0

    def build_candidate(*, config, token, handler):
        nonlocal factory_calls
        factory_calls += 1
        assert handler == app._handle_inbound_event
        if factory_calls == 1:
            assert token == "rev1-old"
            app._apply_security_snapshot(new_snapshot)
            return candidate
        assert config["enable_inbound_server"] is False
        assert token == "rev2-new"
        return None

    session = MagicMock(close=AsyncMock())
    with (
        patch.object(app.config_manager, "snapshot", return_value=old_snapshot),
        patch("core.app_lifecycle.aiohttp.ClientSession", return_value=session),
        patch("core.app_ingress.InboundManager.from_config", side_effect=build_candidate),
    ):
        await app.start()

        assert app.inbound_manager is None
        assert factory_calls == 2
        candidate.update_token.assert_called_with("")
        candidate.start.assert_not_awaited()
        assert app._active_inbound_candidates() == ()

        await app.stop()

    session.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    ("max_attempts", "timeout_seconds", "expected_attempts"),
    [(3, 60.0, 3), (100, 0.0, 1)],
    ids=["attempt-limit", "deadline"],
)
async def test_startup_ownership_retry_is_bounded_and_rolls_back(
    temp_app_root: Path,
    caplog,
    max_attempts: int,
    timeout_seconds: float,
    expected_attempts: int,
):
    app = XiaoQingApp(temp_app_root)
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])
    app.plugin_manager.list_runtime_plugins = Mock(return_value=[])
    app._claim_or_reuse_startup_owner = Mock(return_value=None)
    session = MagicMock(close=AsyncMock())
    caplog.set_level("ERROR", logger="core.app_lifecycle")

    with (
        patch("core.app_lifecycle.aiohttp.ClientSession", return_value=session),
        patch("core.app_lifecycle._STARTUP_OWNERSHIP_MAX_ATTEMPTS", max_attempts),
        patch("core.app_lifecycle._STARTUP_OWNERSHIP_TIMEOUT_SECONDS", timeout_seconds),
        patch("core.app_lifecycle._STARTUP_OWNERSHIP_RETRY_BASE_DELAY_SECONDS", 0.0),
    ):
        with pytest.raises(
            RuntimeError,
            match="startup authentication ownership did not stabilize",
        ):
            await app.start()

    assert app._claim_or_reuse_startup_owner.call_count == expected_attempts
    assert app._lifecycle_state.value == "new"
    assert app.http_session is None
    assert app.scheduler.scheduler is None
    assert "startup ownership did not stabilize" in caplog.text
    session.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_start_failure_rolls_back_every_resource_and_allows_retry(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])
    app.plugin_manager.list_runtime_plugins = Mock(return_value=[])
    first_session = MagicMock(close=AsyncMock())
    second_session = MagicMock(close=AsyncMock())
    failed_manager = MagicMock()
    failed_manager.start = AsyncMock(side_effect=OSError("inbound bind failed"))
    failed_manager.stop = AsyncMock()
    healthy_manager = MagicMock()
    healthy_manager.start = AsyncMock()
    healthy_manager.stop = AsyncMock()

    with (
        patch(
            "core.app_lifecycle.aiohttp.ClientSession", side_effect=[first_session, second_session]
        ),
        patch(
            "core.app_ingress.InboundManager.from_config",
            side_effect=[failed_manager, healthy_manager],
        ),
    ):
        with pytest.raises(OSError, match="inbound bind failed"):
            await app.start()

        first_session.close.assert_awaited_once()
        failed_manager.stop.assert_awaited_once()
        assert app.http_session is None
        assert app.inbound_manager is None
        assert app._session_cleanup_task is None
        assert app._config_watch_task is None
        assert app._plugin_watch_task is None
        assert app.scheduler.scheduler is None
        assert app._lifecycle_state.value == "new"
        assert app._stopping is False

        await app.start()
        assert app._lifecycle_state.value == "running"
        assert app.http_session is second_session
        assert app.inbound_manager is healthy_manager
        await app.stop()

    healthy_manager.start.assert_awaited_once()
    healthy_manager.stop.assert_awaited_once()
    second_session.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_timezone_reload_after_clean_start_rollback_is_used_on_retry(
    temp_app_root: Path,
):
    from core.config import ConfigSnapshot

    app = XiaoQingApp(temp_app_root)
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])
    app.plugin_manager.list_runtime_plugins = Mock(return_value=[])
    first_session = MagicMock(close=AsyncMock())
    second_session = MagicMock(close=AsyncMock())
    failed_manager = MagicMock(
        start=AsyncMock(side_effect=OSError("inbound bind failed")),
        stop=AsyncMock(),
    )
    healthy_manager = MagicMock(start=AsyncMock(), stop=AsyncMock())

    with (
        patch(
            "core.app_lifecycle.aiohttp.ClientSession", side_effect=[first_session, second_session]
        ),
        patch(
            "core.app_ingress.InboundManager.from_config",
            side_effect=[failed_manager, healthy_manager],
        ),
    ):
        with pytest.raises(OSError, match="inbound bind failed"):
            await app.start()

        assert app._lifecycle_state.value == "new"
        assert app.http_session is None
        assert app.scheduler.scheduler is None

        app._apply_config(
            ConfigSnapshot(
                config={**app.config, "timezone": "UTC"},
                secrets=app.secrets,
                revision=app.config_manager.revision + 1,
            )
        )
        config_task = app._config_apply_task
        assert config_task is not None
        await config_task
        await asyncio.sleep(0)

        assert app.scheduler.timezone == "UTC"
        assert app.scheduler.scheduler is not None
        assert str(app.scheduler.scheduler.timezone) == "UTC"

        await app.start()
        assert app._lifecycle_state.value == "running"
        assert str(app.scheduler.scheduler.timezone) == "UTC"
        await app.stop()

    healthy_manager.start.assert_awaited_once()
    healthy_manager.stop.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_start_rollback_cleanup_failure_retains_ownership_and_blocks_retry(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])
    app.plugin_manager.list_runtime_plugins = Mock(return_value=[])
    session = MagicMock(close=AsyncMock())
    manager = MagicMock()
    manager.start = AsyncMock(side_effect=OSError("bind failed"))
    manager.stop = AsyncMock(side_effect=[RuntimeError("stop failed"), None])

    with (
        patch("core.app_lifecycle.aiohttp.ClientSession", return_value=session),
        patch("core.app_ingress.InboundManager.from_config", return_value=manager),
    ):
        with pytest.raises(OSError, match="bind failed"):
            await app.start()

        assert app._lifecycle_state.value == "failed"
        assert app._stopping is True
        assert app.inbound_manager is manager
        assert any("inbound server" in error for error in app._last_shutdown_errors)
        with pytest.raises(RuntimeError, match="failed"):
            await app.start()

        await app.stop()

    assert app._lifecycle_state.value == "stopped"
    assert app.inbound_manager is None
    assert manager.stop.await_count == 2
    session.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_concurrent_and_repeated_start_calls_create_one_runtime(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def wait_inits() -> None:
        entered.set()
        await release.wait()

    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock(side_effect=wait_inits)
    app.plugin_manager.schedule_definitions = Mock(return_value=[])
    app.plugin_manager.list_runtime_plugins = Mock(return_value=[])
    session = MagicMock(close=AsyncMock())

    with (
        patch("core.app_lifecycle.aiohttp.ClientSession", return_value=session) as session_cls,
        patch("core.app_ingress.InboundManager.from_config", return_value=None),
    ):
        first = asyncio.create_task(app.start())
        await entered.wait()
        second = asyncio.create_task(app.start())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)

        original_session = app.http_session
        await app.start()

        assert app.http_session is original_session
        session_cls.assert_called_once()
        app.plugin_manager.load_all.assert_called_once()
        app.plugin_manager.wait_inits.assert_awaited_once()
        await app.stop()

    session.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_start_cancellation_finishes_rollback_before_propagating(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    entered = asyncio.Event()
    blocker = asyncio.Event()

    async def wait_inits() -> None:
        entered.set()
        await blocker.wait()

    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock(side_effect=wait_inits)
    app.plugin_manager.schedule_definitions = Mock(return_value=[])
    app.plugin_manager.list_runtime_plugins = Mock(return_value=[])
    first_session = MagicMock(close=AsyncMock())
    second_session = MagicMock(close=AsyncMock())

    with (
        patch(
            "core.app_lifecycle.aiohttp.ClientSession", side_effect=[first_session, second_session]
        ),
        patch("core.app_ingress.InboundManager.from_config", return_value=None),
    ):
        task = asyncio.create_task(app.start())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        first_session.close.assert_awaited_once()
        assert app.http_session is None
        assert app._lifecycle_state.value == "new"
        assert app._stopping is False

        app.plugin_manager.wait_inits = AsyncMock()
        await app.start()
        assert app.http_session is second_session
        await app.stop()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_start_fatal_is_task_safe_and_rolls_back(temp_app_root: Path):
    class StartupFatal(BaseException):
        pass

    app = XiaoQingApp(temp_app_root)
    fatal = StartupFatal("plugin init fatal")
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock(side_effect=fatal)
    app.plugin_manager.list_runtime_plugins = Mock(return_value=[])
    session = MagicMock(close=AsyncMock())

    with (
        patch("core.app_lifecycle.aiohttp.ClientSession", return_value=session),
        patch("core.app_ingress.InboundManager.from_config", return_value=None),
    ):
        task = asyncio.create_task(app.start())
        with pytest.raises(ApplicationLifecycleFatalError) as exc_info:
            await task

    assert exc_info.value.original is fatal
    session.close.assert_awaited_once()
    assert app.http_session is None
    assert app._lifecycle_state.value == "new"
    assert app._stopping is False


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("runner_name", ["owned", "background"])
async def test_lifecycle_wrapper_immediate_cancel_does_not_leak_inner_coroutine(
    runner_name: str,
):
    import gc
    import warnings

    from core.app_lifecycle import _run_background_operation, _run_owned_operation

    factory_called = False

    async def inner() -> None:
        return None

    def factory():
        nonlocal factory_called
        factory_called = True
        return inner()

    runner = _run_owned_operation if runner_name == "owned" else _run_background_operation
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        task = asyncio.create_task(runner(factory))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gc.collect()

    assert factory_called is False
    assert not any("was never awaited" in str(item.message) for item in caught)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_runtime_config_ws_fatal_is_task_safe(temp_app_root: Path):
    from core.config import ConfigSnapshot

    class WsStopFatal(BaseException):
        pass

    app = XiaoQingApp(temp_app_root)
    fatal = WsStopFatal("ws stop fatal")
    app.http_session = MagicMock()
    app.ws_client = MagicMock(stop=AsyncMock(side_effect=fatal))
    snapshot = ConfigSnapshot(
        config={"enable_ws_client": False, "onebot_ws_uri": ""},
        secrets={},
    )

    task = asyncio.create_task(app._apply_runtime_config(snapshot))
    with pytest.raises(ApplicationLifecycleFatalError) as exc_info:
        await task

    assert exc_info.value.original is fatal
    assert app.ws_client is not None
    app.scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_start_after_terminal_stop_is_rejected_and_stop_is_idempotent(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])
    app.plugin_manager.list_runtime_plugins = Mock(return_value=[])
    session = MagicMock(close=AsyncMock())

    with (
        patch("core.app_lifecycle.aiohttp.ClientSession", return_value=session),
        patch("core.app_ingress.InboundManager.from_config", return_value=None),
    ):
        await app.start()
        await asyncio.gather(app.stop(), app.stop())

        assert app._lifecycle_state.value == "stopped"
        with pytest.raises(RuntimeError, match="stopped"):
            await app.start()

    session.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    "failure_phase",
    [
        "client_session",
        "http_sender",
        "load_all",
        "wait_inits",
        "scheduler",
        "reschedule",
        "ws_constructor",
        "inbound_factory",
        "inbound_start",
    ],
)
async def test_start_failure_at_each_phase_rolls_back_and_allows_retry(
    temp_app_root: Path,
    failure_phase: str,
):
    config_path = temp_app_root / "config" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if failure_phase == "http_sender":
        config["onebot_http_base"] = "http://127.0.0.1:11001"
    if failure_phase == "ws_constructor":
        config["enable_ws_client"] = True
        config["onebot_ws_uri"] = "ws://127.0.0.1:11000/ws"
    if failure_phase in {"inbound_factory", "inbound_start"}:
        config["enable_inbound_server"] = True
        config["inbound_http_base"] = "http://127.0.0.1:12000"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    app = XiaoQingApp(temp_app_root)
    app.plugin_manager.list_runtime_plugins = Mock(return_value=[])
    app.plugin_manager.schedule_definitions = Mock(return_value=[])
    app.plugin_manager.load_all = Mock(
        side_effect=[RuntimeError("load failed"), None] if failure_phase == "load_all" else None
    )
    app.plugin_manager.wait_inits = AsyncMock(
        side_effect=[RuntimeError("init failed"), None] if failure_phase == "wait_inits" else None
    )

    if failure_phase == "scheduler":
        original_ensure_started = app.scheduler.ensure_started
        ensure_calls = 0

        def fail_scheduler_once() -> None:
            nonlocal ensure_calls
            ensure_calls += 1
            if ensure_calls == 1:
                raise RuntimeError("scheduler failed")
            original_ensure_started()

        app.scheduler.ensure_started = Mock(side_effect=fail_scheduler_once)

    if failure_phase == "reschedule":
        original_reschedule = app._reschedule
        reschedule_calls = 0

        def fail_reschedule_once(name: str) -> None:
            nonlocal reschedule_calls
            reschedule_calls += 1
            if reschedule_calls == 1:
                raise RuntimeError("reschedule failed")
            original_reschedule(name)

        app._reschedule = Mock(side_effect=fail_reschedule_once)

    first_session = MagicMock(close=AsyncMock())
    second_session = MagicMock(close=AsyncMock())
    session_side_effect: list[Any] = (
        [RuntimeError("session failed"), second_session]
        if failure_phase == "client_session"
        else [first_session, second_session]
    )
    healthy_sender = MagicMock()
    sender_side_effect: list[Any] = (
        [RuntimeError("sender failed"), healthy_sender]
        if failure_phase == "http_sender"
        else [healthy_sender]
    )
    healthy_ws = MagicMock()
    healthy_ws.set_on_connect = Mock()
    healthy_ws.connect_and_listen = AsyncMock()
    healthy_ws.stop = AsyncMock()
    ws_side_effect: list[Any] = (
        [RuntimeError("ws failed"), healthy_ws]
        if failure_phase == "ws_constructor"
        else [healthy_ws]
    )
    failed_inbound = MagicMock()
    failed_inbound.start = AsyncMock(side_effect=RuntimeError("inbound start failed"))
    failed_inbound.stop = AsyncMock()
    inbound_side_effect: list[Any]
    if failure_phase == "inbound_factory":
        inbound_side_effect = [RuntimeError("inbound factory failed"), None]
    elif failure_phase == "inbound_start":
        inbound_side_effect = [failed_inbound, None]
    else:
        inbound_side_effect = [None, None]

    with (
        patch("core.app_lifecycle.aiohttp.ClientSession", side_effect=session_side_effect),
        patch("core.app_lifecycle.OneBotHttpSender", side_effect=sender_side_effect),
        patch("core.app_ingress.OneBotWsClient", side_effect=ws_side_effect),
        patch("core.app_ingress.InboundManager.from_config", side_effect=inbound_side_effect),
    ):
        with pytest.raises(RuntimeError, match="failed"):
            await app.start()

        assert app._lifecycle_state.value == "new"
        assert app._stopping is False
        assert app.http_session is None
        assert app.http_sender is None
        assert app.ws_client is None
        assert app.inbound_manager is None
        assert app._session_cleanup_task is None
        assert app._config_watch_task is None
        assert app._plugin_watch_task is None
        assert app._ws_client_task is None
        assert app.scheduler.scheduler is None
        if failure_phase != "client_session":
            first_session.close.assert_awaited_once()

        await app.start()
        assert app._lifecycle_state.value == "running"
        assert app.http_session is second_session
        await app.stop()

    second_session.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_start_creates_shared_http_session_with_default_timeout(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])

    mock_session = MagicMock()
    mock_session.close = AsyncMock()
    captured: dict[str, Any] = {}

    def _fake_client_session(*args, **kwargs):
        captured.update(kwargs)
        return mock_session

    with patch("core.app_lifecycle.aiohttp.ClientSession", side_effect=_fake_client_session):
        await app.start()
        await app.stop()

    timeout = captured.get("timeout")
    assert timeout is not None
    assert timeout.total == 30.0
    assert timeout.connect == 10.0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_app_start_tracks_and_stops_background_tasks(temp_app_root: Path):
    """Test app start/stop manages WS and watch background tasks."""
    config_file = temp_app_root / "config" / "config.json"
    with open(config_file, encoding="utf-8") as f:
        config = json.load(f)
    config["enable_ws_client"] = True
    config["onebot_ws_uri"] = "ws://localhost:6700/ws"
    config["enable_plugin_watcher"] = True
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(config, f)

    app = XiaoQingApp(temp_app_root)
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])

    task_cancelled: dict[str, bool] = {"config": False, "plugin": False, "ws": False}

    async def _block_until_cancelled(marker: str):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            task_cancelled[marker] = True
            raise

    async def config_watch(interval: float = 2.0):
        await _block_until_cancelled("config")

    async def plugin_watch():
        await _block_until_cancelled("plugin")

    app.config_manager.watch = config_watch
    app.plugin_manager.watch = plugin_watch

    mock_ws_client = MagicMock()
    mock_ws_client.set_on_connect = Mock()
    mock_ws_client.stop = AsyncMock()

    async def ws_connect_and_listen(handler):
        await _block_until_cancelled("ws")

    mock_ws_client.connect_and_listen = AsyncMock(side_effect=ws_connect_and_listen)

    with patch("core.app_ingress.OneBotWsClient", return_value=mock_ws_client):
        await app.start()
        await asyncio.sleep(0)

        assert getattr(app, "_config_watch_task", None) is not None
        assert getattr(app, "_plugin_watch_task", None) is not None
        assert getattr(app, "_ws_client_task", None) is not None

        await app.stop()

    assert task_cancelled["config"] is True
    assert task_cancelled["plugin"] is True
    assert task_cancelled["ws"] is True
    mock_ws_client.stop.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_apply_config_toggles_plugin_watch_task(temp_app_root: Path):
    """Test _apply_config can enable/disable plugin watcher at runtime."""
    from core.config import ConfigSnapshot

    app = XiaoQingApp(temp_app_root)
    app._lifecycle_state = type(app._lifecycle_state).RUNNING
    task_cancelled = {"plugin": False}

    async def plugin_watch():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            task_cancelled["plugin"] = True
            raise

    app.plugin_manager.watch = plugin_watch
    app._config_watch_task = MagicMock()
    app._config_watch_task.done.return_value = False

    app._apply_config(
        ConfigSnapshot(
            config={**app.config, "enable_plugin_watcher": True},
            secrets=app.secrets,
            revision=1,
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert app._plugin_watch_task is not None

    app._apply_config(
        ConfigSnapshot(
            config={**app.config, "enable_plugin_watcher": False},
            secrets=app.secrets,
            revision=2,
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert task_cancelled["plugin"] is True
    assert app._plugin_watch_task is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_start_binds_inbound_status_providers(temp_app_root: Path):
    """Test inbound manager gets status providers before startup."""
    app = XiaoQingApp(temp_app_root)
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])

    mock_manager = MagicMock()
    mock_manager.start = AsyncMock()
    mock_manager.stop = AsyncMock()

    with patch("core.app_ingress.InboundManager.from_config", return_value=mock_manager):
        await app.start()
        await app.stop()

    mock_manager.set_status_providers.assert_called_once()
    mock_manager.start.assert_awaited_once()
    mock_manager.stop.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_app_start_with_http_configured(temp_app_root: Path):
    """Test app start with HTTP sender configured"""
    import json

    # Update config with HTTP base
    config_file = temp_app_root / "config" / "config.json"
    with open(config_file) as f:
        config = json.load(f)
    config["onebot_http_base"] = "http://localhost:5700"
    with open(config_file, "w") as f:
        json.dump(config, f)

    app = XiaoQingApp(temp_app_root)

    # Mock plugin manager
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])

    await app.start()

    # Verify HTTP sender is created
    assert app.http_sender is not None

    await app.stop()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_app_start_with_ws_disabled(temp_app_root: Path):
    """Test app start with WebSocket client disabled"""
    import json

    # Update config to disable WS
    config_file = temp_app_root / "config" / "config.json"
    with open(config_file) as f:
        config = json.load(f)
    config["enable_ws_client"] = False
    with open(config_file, "w") as f:
        json.dump(config, f)

    app = XiaoQingApp(temp_app_root)

    # Mock plugin manager
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])

    await app.start()

    # WS client should not be created
    assert app.ws_client is None

    await app.stop()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_app_stop(temp_app_root: Path):
    """Test app stop cleans up resources"""
    app = XiaoQingApp(temp_app_root)

    # Mock plugin manager
    app.plugin_manager.list_runtime_plugins = Mock(return_value=[])
    app.plugin_manager.unload_plugin = AsyncMock()

    # Start the app first
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])
    await app.start()

    # Now stop it
    await app.stop()

    # Verify cleanup
    assert app.http_session is None or app.http_session.closed
