"""停机、watcher 和资源回收。"""

from __future__ import annotations

import tests.helpers.app_test_support as _fixture_support
from tests.helpers.app_test_support import (
    Any,
    AsyncMock,
    MagicMock,
    Mock,
    Path,
    SimpleNamespace,
    XiaoQingApp,
    asyncio,
    cancellation_resistant_callback,
    cancellation_then_release_callback,
    os,
    patch,
    pytest,
    resist_cancellation_until_released,
    sys,
    time,
)

mock_dependencies = _fixture_support.mock_dependencies
temp_app_root = _fixture_support.temp_app_root


@pytest.mark.asyncio
@pytest.mark.integration
async def test_app_stop_unloads_plugins(temp_app_root: Path):
    """Test app stop unloads all plugins"""
    app = XiaoQingApp(temp_app_root)

    runtime_plugins = ["test_plugin"]

    async def unload_plugin(name: str) -> None:
        runtime_plugins.remove(name)

    app.plugin_manager.list_runtime_plugins = Mock(side_effect=lambda: list(runtime_plugins))
    app.plugin_manager.unload_plugin = AsyncMock(side_effect=unload_plugin)

    # Start
    app.plugin_manager.load_all = Mock()
    app.plugin_manager.wait_inits = AsyncMock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])
    await app.start()

    # Stop
    await app.stop()

    # Verify unload was called
    app.plugin_manager.unload_plugin.assert_called_once_with("test_plugin")


@pytest.mark.asyncio
async def test_app_shutdown_closes_plugin_sync_broker_before_http(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    calls: list[str] = []
    app.plugin_manager.list_runtime_plugins = Mock(return_value=[])
    project_path = os.path.abspath(temp_app_root)
    package_path = os.path.abspath(temp_app_root / "plugins")
    plugins_package = sys.modules["plugins"]
    assert project_path in sys.path
    assert package_path in plugins_package.__path__  # type: ignore[attr-defined]

    async def close_broker(*, timeout_seconds: float):
        assert timeout_seconds > 0
        calls.append("broker")
        return SimpleNamespace(drained=True)

    async def close_http() -> None:
        calls.append("http")

    app.plugin_manager.close_execution_broker = AsyncMock(side_effect=close_broker)
    app.http_session = MagicMock(close=AsyncMock(side_effect=close_http))

    await app.stop()

    assert calls == ["broker", "http"]
    assert app._lifecycle_state.value == "stopped"
    assert project_path not in sys.path
    assert package_path not in plugins_package.__path__  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_app_retains_http_until_sync_broker_drain_retry_succeeds(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    app.plugin_manager.list_runtime_plugins = Mock(return_value=[])
    app.plugin_manager.close_execution_broker = AsyncMock(
        side_effect=[
            SimpleNamespace(drained=False, pending_callbacks=1),
            SimpleNamespace(drained=True, pending_callbacks=0),
        ]
    )
    session = MagicMock(close=AsyncMock())
    app.http_session = session

    await app.stop()

    assert app._lifecycle_state.value == "failed"
    assert app.http_session is session
    session.close.assert_not_awaited()
    assert any("plugin sync broker" in error for error in app._last_shutdown_errors)

    await app.stop()

    assert app._lifecycle_state.value == "stopped"
    assert app.http_session is None
    session.close.assert_awaited_once()
    assert app.plugin_manager.close_execution_broker.await_count == 2


@pytest.mark.asyncio
async def test_plugin_batch_shutdown_uses_one_absolute_budget(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)

    class SlowPluginManager:
        execution_drain_timeout_seconds = 0.04

        def __init__(self) -> None:
            self.runtime = ["first", "second"]
            self.unload_calls: list[tuple[str, float | None]] = []
            self.broker_close_calls = 0

        def list_runtime_plugins(self) -> list[str]:
            return list(self.runtime)

        async def unload_plugin(
            self,
            name: str,
            *,
            drain_timeout_seconds: float | None = None,
        ) -> None:
            self.unload_calls.append((name, drain_timeout_seconds))
            await asyncio.sleep(10)

        async def close_execution_broker(self, *, timeout_seconds: float):
            self.broker_close_calls += 1
            return SimpleNamespace(drained=True)

    manager = SlowPluginManager()
    app.plugin_manager = manager  # type: ignore[assignment]
    errors: list[str] = []
    started = time.monotonic()

    completed = await asyncio.wait_for(
        app._unload_plugins_for_shutdown(errors),
        timeout=0.2,
    )

    elapsed = time.monotonic() - started
    assert completed is False
    assert elapsed < 0.15
    assert len(manager.unload_calls) == 1
    assert manager.unload_calls[0][0] == "first"
    assert 0 < (manager.unload_calls[0][1] or 0) <= 0.040001
    assert manager.broker_close_calls == 0
    assert any("plugin first" in error for error in errors)
    assert any("budget exhausted" in error for error in errors)

    app.scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_stop_continues_after_cleanup_failures(temp_app_root: Path):
    """Independent cleanup continues while dependent resources wait for retries."""
    app = XiaoQingApp(temp_app_root)
    calls: list[str] = []
    inbound_attempts = 0
    ws_attempts = 0
    http_attempts = 0
    scheduler_attempts = 0
    broken_plugin_attempts = 0
    runtime_plugins = ["broken", "healthy"]

    async def fail_inbound_once() -> None:
        nonlocal inbound_attempts
        inbound_attempts += 1
        calls.append("inbound")
        if inbound_attempts == 1:
            raise RuntimeError("inbound stop failed")

    async def fail_ws_once() -> None:
        nonlocal ws_attempts
        ws_attempts += 1
        calls.append("ws")
        if ws_attempts == 1:
            raise RuntimeError("ws stop failed")

    async def fail_http_once() -> None:
        nonlocal http_attempts
        http_attempts += 1
        calls.append("http")
        if http_attempts == 1:
            raise RuntimeError("http close failed")

    async def fail_background_task() -> None:
        raise RuntimeError("watcher failed")

    async def unload_plugin(name: str) -> None:
        nonlocal broken_plugin_attempts
        calls.append(f"plugin:{name}")
        if name == "broken":
            broken_plugin_attempts += 1
            if broken_plugin_attempts == 1:
                raise RuntimeError("plugin shutdown failed")
        runtime_plugins.remove(name)

    original_scheduler_shutdown = app.scheduler.shutdown_async

    async def fail_scheduler(**kwargs: Any) -> None:
        nonlocal scheduler_attempts
        scheduler_attempts += 1
        calls.append("scheduler")
        if scheduler_attempts == 1:
            raise RuntimeError("scheduler stop failed")
        await original_scheduler_shutdown(**kwargs)

    failed_inbound = MagicMock(stop=AsyncMock(side_effect=fail_inbound_once))
    app.inbound_manager = failed_inbound
    failed_ws = MagicMock(stop=AsyncMock(side_effect=fail_ws_once))
    failed_http = MagicMock(close=AsyncMock(side_effect=fail_http_once))
    app.ws_client = failed_ws
    app.http_session = failed_http
    app.scheduler.shutdown_async = AsyncMock(side_effect=fail_scheduler)
    app.plugin_manager.list_runtime_plugins = Mock(side_effect=lambda: list(runtime_plugins))
    app.plugin_manager.unload_plugin = AsyncMock(side_effect=unload_plugin)
    app._config_watch_task = asyncio.create_task(fail_background_task())
    await asyncio.sleep(0)

    await asyncio.wait_for(app.stop(), timeout=1)

    assert calls == [
        "inbound",
        "ws",
    ]
    assert app.inbound_manager is failed_inbound
    assert app.ws_client is failed_ws
    assert app.http_session is failed_http
    assert app._config_watch_task is None
    assert any("inbound server" in error for error in app._last_shutdown_errors)
    assert any("_config_watch_task" in error for error in app._last_shutdown_errors)
    assert any("cleanup deferred" in error for error in app._last_shutdown_errors)
    assert app._lifecycle_state.value == "failed"

    await asyncio.wait_for(app.stop(), timeout=1)

    assert calls == [
        "inbound",
        "ws",
        "inbound",
        "ws",
        "scheduler",
    ]
    assert app.inbound_manager is None
    assert app.ws_client is None
    assert app.http_session is failed_http
    assert app.scheduler.scheduler is not None
    assert any("scheduler" in error for error in app._last_shutdown_errors)
    assert any("cleanup deferred" in error for error in app._last_shutdown_errors)
    assert app._lifecycle_state.value == "failed"

    await asyncio.wait_for(app.stop(), timeout=1)

    assert calls == [
        "inbound",
        "ws",
        "inbound",
        "ws",
        "scheduler",
        "scheduler",
        "plugin:broken",
        "plugin:healthy",
    ]
    assert app.http_session is failed_http
    assert app.scheduler.scheduler is None
    assert any("plugin broken" in error for error in app._last_shutdown_errors)
    assert any("HTTP cleanup deferred" in error for error in app._last_shutdown_errors)
    assert app._lifecycle_state.value == "failed"

    await asyncio.wait_for(app.stop(), timeout=1)

    assert calls == [
        "inbound",
        "ws",
        "inbound",
        "ws",
        "scheduler",
        "scheduler",
        "plugin:broken",
        "plugin:healthy",
        "scheduler",
        "plugin:broken",
        "http",
    ]
    assert app.http_session is failed_http
    assert any("HTTP session" in error for error in app._last_shutdown_errors)
    assert app._lifecycle_state.value == "failed"

    await asyncio.wait_for(app.stop(), timeout=1)

    assert calls == [
        "inbound",
        "ws",
        "inbound",
        "ws",
        "scheduler",
        "scheduler",
        "plugin:broken",
        "plugin:healthy",
        "scheduler",
        "plugin:broken",
        "http",
        "scheduler",
        "http",
    ]
    assert app.inbound_manager is None
    assert app.ws_client is None
    assert app.http_session is None
    assert app.scheduler.scheduler is None
    assert app._lifecycle_state.value == "stopped"
    assert app._last_shutdown_errors == ()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_retains_plugins_and_http_until_scheduler_jobs_stop(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    app.scheduler._shutdown_timeout_seconds = 0.01
    scheduler = app.scheduler.scheduler
    assert scheduler is not None
    executor = next(iter(scheduler._executors.values()))
    started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release = asyncio.Event()
    runtime_plugins = ["scheduled_plugin"]

    resistant_job = cancellation_then_release_callback(started, cancellation_seen, release)

    async def unload_plugin(name: str) -> None:
        runtime_plugins.remove(name)

    scheduler.add_job(resistant_job, "date")
    await asyncio.wait_for(started.wait(), timeout=1)
    job_task = next(
        future for future in executor._pending_futures if isinstance(future, asyncio.Task)
    )
    session = MagicMock(close=AsyncMock())
    app.http_session = session
    app.plugin_manager.list_runtime_plugins = Mock(side_effect=lambda: list(runtime_plugins))
    app.plugin_manager.unload_plugin = AsyncMock(side_effect=unload_plugin)

    try:
        await app.stop()

        assert app._lifecycle_state.value == "failed"
        assert cancellation_seen.is_set()
        assert job_task.done() is False
        assert app.scheduler.scheduler is scheduler
        assert runtime_plugins == ["scheduled_plugin"]
        app.plugin_manager.unload_plugin.assert_not_awaited()
        session.close.assert_not_awaited()
        assert app.http_session is session

        release.set()
        await job_task
        await app.stop()

        assert app._lifecycle_state.value == "stopped"
        assert app.scheduler.scheduler is None
        assert runtime_plugins == []
        app.plugin_manager.unload_plugin.assert_awaited_once_with("scheduled_plugin")
        session.close.assert_awaited_once()
        assert app.http_session is None
    finally:
        release.set()
        if not job_task.done():
            job_task.cancel()
        await asyncio.gather(job_task, return_exceptions=True)
        if app._lifecycle_state.value != "stopped":
            app.scheduler._shutdown_timeout_seconds = 0.5
            await app.stop()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_rapid_config_generations_remain_owned_and_shutdown_is_retryable(
    temp_app_root: Path,
):
    from core.config import ConfigSnapshot

    app = XiaoQingApp(temp_app_root)
    app._background_task_stop_timeout_seconds = 0.01
    app.http_session = MagicMock(close=AsyncMock())
    inbound = MagicMock(stop=AsyncMock())
    app.inbound_manager = inbound
    first_entered = asyncio.Event()
    first_cancelled = asyncio.Event()
    first_release = asyncio.Event()
    second_entered = asyncio.Event()
    second_release = asyncio.Event()
    call_count = 0

    async def apply_runtime(
        _snapshot: ConfigSnapshot,
        *,
        owner: Any = None,
    ) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            async with app._inbound_reconcile_lock.get():
                first_entered.set()
                while not first_release.is_set():
                    try:
                        await first_release.wait()
                    except asyncio.CancelledError:
                        first_cancelled.set()
            return
        second_entered.set()
        await second_release.wait()

    app._apply_runtime_config = AsyncMock(side_effect=apply_runtime)
    first_snapshot = ConfigSnapshot(config=app.config, secrets=app.secrets, revision=1)
    second_snapshot = ConfigSnapshot(config=app.config, secrets=app.secrets, revision=2)

    app._apply_config(first_snapshot)
    first = app._config_apply_task
    assert first is not None
    await first_entered.wait()

    app._apply_config(second_snapshot)
    second = app._config_apply_task
    assert second is not None and second is not first
    await first_cancelled.wait()
    await second_entered.wait()
    assert first in app._config_apply_tasks
    assert second in app._config_apply_tasks

    await asyncio.wait_for(app.stop(), timeout=0.5)

    assert app._lifecycle_state.value == "failed"
    assert first in app._config_apply_tasks
    assert app.inbound_manager is inbound
    inbound.stop.assert_not_awaited()
    assert app.http_session is not None
    app.http_session.close.assert_not_awaited()
    assert app.scheduler.scheduler is not None

    first_release.set()
    second_release.set()
    await asyncio.gather(first, second, return_exceptions=True)
    await asyncio.sleep(0)
    await asyncio.wait_for(app.stop(), timeout=0.5)

    inbound.stop.assert_awaited_once()
    assert app.inbound_manager is None
    assert app._config_apply_tasks == set()
    assert app.http_session is None
    assert app._lifecycle_state.value == "stopped"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_plugin_watcher_disable_reenable_waits_for_old_generation(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    app._lifecycle_state = type(app._lifecycle_state).RUNNING
    config_watch_release = asyncio.Event()
    app._config_watch_task = asyncio.create_task(config_watch_release.wait())
    first_entered = asyncio.Event()
    first_cancelled = asyncio.Event()
    first_release = asyncio.Event()
    second_entered = asyncio.Event()
    second_release = asyncio.Event()
    watch_calls = 0

    async def watch() -> None:
        nonlocal watch_calls
        watch_calls += 1
        if watch_calls == 1:
            first_entered.set()
            while not first_release.is_set():
                try:
                    await first_release.wait()
                except asyncio.CancelledError:
                    first_cancelled.set()
            return
        second_entered.set()
        await second_release.wait()

    app.plugin_manager.watch = AsyncMock(side_effect=watch)
    app._configure_plugin_watch({"enable_plugin_watcher": True})
    first = app._plugin_watch_task
    assert first is not None
    await first_entered.wait()

    app._configure_plugin_watch({"enable_plugin_watcher": False})
    await first_cancelled.wait()
    app._configure_plugin_watch({"enable_plugin_watcher": True})
    await asyncio.sleep(0)

    assert app._plugin_watch_task is first
    assert first in app._plugin_watch_tasks
    assert watch_calls == 1

    first_release.set()
    await asyncio.wait_for(second_entered.wait(), timeout=1)
    assert watch_calls == 2
    assert app._plugin_watch_task is not first

    app._stopping = True
    await app._cancel_plugin_watch_tasks()
    config_watch_release.set()
    await app._config_watch_task
    app._config_watch_task = None
    second_release.set()
    app.scheduler.shutdown()


def test_plugin_watcher_poll_interval_falls_back_for_invalid_values(
    temp_app_root: Path,
) -> None:
    app = XiaoQingApp(temp_app_root)

    for invalid in (0, -1, float("nan"), float("inf"), float("-inf"), True, "bad"):
        assert app._plugin_watch_poll_interval({"plugin_poll_interval": invalid}) == 3600.0

    assert app._plugin_watch_poll_interval({"plugin_poll_interval": 0.25}) == 0.25
    app.scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("first_outcome", ["return", "ordinary", "fatal"])
async def test_plugin_watcher_supervisor_restarts_unexpected_exit_with_backoff(
    temp_app_root: Path,
    first_outcome: str,
) -> None:
    class FatalWatchError(BaseException):
        pass

    app = XiaoQingApp(temp_app_root)
    app._lifecycle_state = type(app._lifecycle_state).RUNNING
    # Windows SelectorEventLoop 的时钟分辨率可能高于 10 ms；使用明显高于
    # 一个时钟刻度的延迟，避免事件循环把短定时器当作已经到期。
    app._plugin_watch_restart_base_delay_seconds = 0.05
    app._plugin_watch_restart_max_delay_seconds = 0.1
    app._plugin_watch_stable_reset_seconds = 60.0
    config_watch_release = asyncio.Event()
    app._config_watch_task = asyncio.create_task(config_watch_release.wait())
    second_entered = asyncio.Event()
    second_release = asyncio.Event()
    starts: list[float] = []

    async def watch() -> None:
        starts.append(asyncio.get_running_loop().time())
        if len(starts) == 1:
            if first_outcome == "ordinary":
                raise RuntimeError("watch failed")
            if first_outcome == "fatal":
                raise FatalWatchError("fatal watch failure")
            return
        second_entered.set()
        await second_release.wait()

    app.plugin_manager.watch = AsyncMock(side_effect=watch)
    app._configure_plugin_watch({"enable_plugin_watcher": True})

    await asyncio.wait_for(second_entered.wait(), timeout=1)

    assert len(starts) == 2
    assert starts[1] - starts[0] >= 0.04
    assert app._plugin_watch_task is not None
    assert app._plugin_watch_task.done() is False

    app._stopping = True
    await app._cancel_plugin_watch_tasks()
    config_watch_release.set()
    await app._config_watch_task
    app._config_watch_task = None
    second_release.set()
    app.scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_plugin_watcher_supervisor_backoff_increases_after_repeated_failures(
    temp_app_root: Path,
) -> None:
    app = XiaoQingApp(temp_app_root)
    app._lifecycle_state = type(app._lifecycle_state).RUNNING
    app._plugin_watch_restart_base_delay_seconds = 0.05
    app._plugin_watch_restart_max_delay_seconds = 0.2
    app._plugin_watch_stable_reset_seconds = 60.0
    config_watch_release = asyncio.Event()
    app._config_watch_task = asyncio.create_task(config_watch_release.wait())
    fourth_entered = asyncio.Event()
    fourth_release = asyncio.Event()
    starts: list[float] = []

    async def watch() -> None:
        starts.append(asyncio.get_running_loop().time())
        if len(starts) < 4:
            raise RuntimeError(f"watch failure {len(starts)}")
        fourth_entered.set()
        await fourth_release.wait()

    app.plugin_manager.watch = AsyncMock(side_effect=watch)
    app._configure_plugin_watch({"enable_plugin_watcher": True})

    await asyncio.wait_for(fourth_entered.wait(), timeout=1)

    assert len(starts) == 4
    gaps = [later - earlier for earlier, later in zip(starts[:-1], starts[1:], strict=True)]
    assert gaps[0] >= 0.04
    assert gaps[1] >= 0.09
    assert gaps[2] >= 0.18

    app._stopping = True
    await app._cancel_plugin_watch_tasks()
    config_watch_release.set()
    await app._config_watch_task
    app._config_watch_task = None
    fourth_release.set()
    app.scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_plugin_watcher_stable_generation_resets_restart_backoff(
    temp_app_root: Path,
) -> None:
    app = XiaoQingApp(temp_app_root)
    app._lifecycle_state = type(app._lifecycle_state).RUNNING
    app._plugin_watch_restart_base_delay_seconds = 0.01
    app._plugin_watch_restart_max_delay_seconds = 0.08
    app._plugin_watch_stable_reset_seconds = 0.02
    app._plugin_watch_restart_failures = 3
    second_entered = asyncio.Event()
    second_release = asyncio.Event()
    starts: list[float] = []

    async def watch() -> None:
        starts.append(asyncio.get_running_loop().time())
        if len(starts) == 1:
            await asyncio.sleep(0.025)
            raise RuntimeError("failure after stable generation")
        second_entered.set()
        await second_release.wait()

    app.plugin_manager.watch = AsyncMock(side_effect=watch)
    app._configure_plugin_watch({"enable_plugin_watcher": True})
    await asyncio.wait_for(second_entered.wait(), timeout=1)

    restart_gap = starts[1] - starts[0] - 0.025
    assert 0.005 <= restart_gap < 0.05
    assert app._plugin_watch_restart_failures == 1

    app._stopping = True
    await app._cancel_plugin_watch_tasks()
    second_release.set()
    app.scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_plugin_watcher_disable_enable_replaces_cancelled_restart_once(
    temp_app_root: Path,
) -> None:
    app = XiaoQingApp(temp_app_root)
    app._lifecycle_state = type(app._lifecycle_state).RUNNING
    app._plugin_watch_restart_base_delay_seconds = 0.2
    app._plugin_watch_restart_max_delay_seconds = 0.2
    config_watch_release = asyncio.Event()
    app._config_watch_task = asyncio.create_task(config_watch_release.wait())
    second_entered = asyncio.Event()
    second_release = asyncio.Event()
    calls = 0

    async def watch() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first generation failed")
        second_entered.set()
        await second_release.wait()

    app.plugin_manager.watch = AsyncMock(side_effect=watch)
    app._configure_plugin_watch({"enable_plugin_watcher": True})
    for _ in range(20):
        restart_task = app._plugin_watch_restart_task
        if restart_task is not None and not restart_task.done():
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("watch supervisor did not schedule a restart")

    app._configure_plugin_watch({"enable_plugin_watcher": False})
    app._configure_plugin_watch({"enable_plugin_watcher": True})
    await asyncio.wait_for(second_entered.wait(), timeout=1)
    await asyncio.sleep(0.25)

    assert calls == 2
    assert app._plugin_watch_task is not None
    assert app._plugin_watch_task.done() is False

    app._stopping = True
    await app._cancel_plugin_watch_tasks()
    config_watch_release.set()
    await app._config_watch_task
    app._config_watch_task = None
    second_release.set()
    app.scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_plugin_watcher_stop_cancels_pending_supervised_restart(
    temp_app_root: Path,
) -> None:
    app = XiaoQingApp(temp_app_root)
    app._lifecycle_state = type(app._lifecycle_state).RUNNING
    app._plugin_watch_restart_base_delay_seconds = 0.2
    app._plugin_watch_restart_max_delay_seconds = 0.2
    config_watch_release = asyncio.Event()
    app._config_watch_task = asyncio.create_task(config_watch_release.wait())
    first_entered = asyncio.Event()
    calls = 0

    async def watch() -> None:
        nonlocal calls
        calls += 1
        first_entered.set()
        raise RuntimeError("immediate watcher failure")

    app.plugin_manager.watch = AsyncMock(side_effect=watch)
    app._configure_plugin_watch({"enable_plugin_watcher": True})
    await first_entered.wait()
    for _ in range(20):
        restart_task = app._plugin_watch_restart_task
        if restart_task is not None and not restart_task.done():
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("watch supervisor did not schedule a restart")

    assert "plugin watcher restart" in app._live_control_plane_tasks()
    await app._cancel_plugin_watch_tasks()
    await asyncio.sleep(0.25)

    assert calls == 1
    assert app._plugin_watch_restart_task is None
    assert app._plugin_watch_tasks == set()

    config_watch_release.set()
    await app._config_watch_task
    app._config_watch_task = None
    app.scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_plugin_watcher_restarts_after_config_watcher_dies_while_app_running(
    temp_app_root: Path,
) -> None:
    app = XiaoQingApp(temp_app_root)
    app._lifecycle_state = type(app._lifecycle_state).RUNNING
    app._plugin_watch_restart_base_delay_seconds = 0.01
    config_watch_release = asyncio.Event()
    app._config_watch_task = asyncio.create_task(config_watch_release.wait())
    fail_watch = asyncio.Event()
    watch_entered = asyncio.Event()
    second_entered = asyncio.Event()
    second_release = asyncio.Event()
    calls = 0

    async def watch() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            watch_entered.set()
            await fail_watch.wait()
            raise RuntimeError("watch failed after config watcher")
        second_entered.set()
        await second_release.wait()

    app.plugin_manager.watch = AsyncMock(side_effect=watch)
    app._configure_plugin_watch({"enable_plugin_watcher": True})
    await watch_entered.wait()
    config_watch_release.set()
    await app._config_watch_task
    fail_watch.set()
    await asyncio.wait_for(second_entered.wait(), timeout=1)

    assert calls == 2
    assert app._plugin_watch_restart_task is None
    assert app._plugin_watch_task is not None
    assert app._plugin_watch_task.done() is False

    app._config_watch_task = None
    app._stopping = True
    await app._cancel_plugin_watch_tasks()
    second_release.set()
    app.scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("inactive_state", ["NEW", "STOPPING", "FAILED", "STOPPED"])
async def test_plugin_watcher_does_not_start_outside_active_lifecycle(
    temp_app_root: Path,
    inactive_state: str,
) -> None:
    app = XiaoQingApp(temp_app_root)
    app._lifecycle_state = getattr(type(app._lifecycle_state), inactive_state)
    app.plugin_manager.watch = AsyncMock()

    app._configure_plugin_watch({"enable_plugin_watcher": True})
    await asyncio.sleep(0)

    assert app._plugin_watch_task is None
    assert app._plugin_watch_restart_task is None
    app.plugin_manager.watch.assert_not_awaited()
    app.scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_plugin_watcher_resistant_generation_is_retained_until_stop_retry(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    app._lifecycle_state = type(app._lifecycle_state).RUNNING
    app._background_task_stop_timeout_seconds = 0.01
    config_watch_release = asyncio.Event()
    app._config_watch_task = asyncio.create_task(config_watch_release.wait())
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    runtime_plugins = ["watch_plugin"]
    session = MagicMock(close=AsyncMock())

    async def unload_plugin(name: str) -> None:
        runtime_plugins.remove(name)

    watch = cancellation_resistant_callback(entered, cancelled, release)

    app.plugin_manager.watch = AsyncMock(side_effect=watch)
    app.plugin_manager.list_runtime_plugins = Mock(side_effect=lambda: list(runtime_plugins))
    app.plugin_manager.unload_plugin = AsyncMock(side_effect=unload_plugin)
    app.http_session = session
    app._configure_plugin_watch({"enable_plugin_watcher": True})
    task = app._plugin_watch_task
    assert task is not None
    await entered.wait()

    await asyncio.wait_for(app.stop(), timeout=0.5)

    assert cancelled.is_set()
    assert app._lifecycle_state.value == "failed"
    assert task in app._plugin_watch_tasks
    assert not task.done()
    assert app.scheduler.scheduler is not None
    assert runtime_plugins == ["watch_plugin"]
    app.plugin_manager.unload_plugin.assert_not_awaited()
    session.close.assert_not_awaited()

    release.set()
    config_watch_release.set()
    await asyncio.wait_for(task, timeout=1)
    await asyncio.sleep(0)
    await asyncio.wait_for(app.stop(), timeout=0.5)

    assert app._plugin_watch_tasks == set()
    assert runtime_plugins == []
    app.plugin_manager.unload_plugin.assert_awaited_once_with("watch_plugin")
    session.close.assert_awaited_once()
    assert app._lifecycle_state.value == "stopped"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_resistant_reload_task_defers_runtime_dependency_cleanup(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    app._background_task_stop_timeout_seconds = 0.01
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    runtime_plugins = ["reload_plugin"]
    session = MagicMock(close=AsyncMock())

    resistant_reload = cancellation_resistant_callback(entered, cancelled, release)

    async def unload_plugin(name: str) -> None:
        runtime_plugins.remove(name)

    reload_task = asyncio.create_task(resistant_reload())
    app._reload_task = reload_task
    app.plugin_manager.list_runtime_plugins = Mock(side_effect=lambda: list(runtime_plugins))
    app.plugin_manager.unload_plugin = AsyncMock(side_effect=unload_plugin)
    app.http_session = session
    await entered.wait()

    try:
        await asyncio.wait_for(app.stop(), timeout=0.5)

        assert cancelled.is_set()
        assert reload_task.done() is False
        assert app._reload_task is reload_task
        assert app.scheduler.scheduler is not None
        assert runtime_plugins == ["reload_plugin"]
        app.plugin_manager.unload_plugin.assert_not_awaited()
        session.close.assert_not_awaited()
        assert app._lifecycle_state.value == "failed"

        release.set()
        await asyncio.wait_for(reload_task, timeout=1)
        await app.stop()

        assert app._reload_task is None
        assert app.scheduler.scheduler is None
        assert runtime_plugins == []
        app.plugin_manager.unload_plugin.assert_awaited_once_with("reload_plugin")
        session.close.assert_awaited_once()
        assert app._lifecycle_state.value == "stopped"
    finally:
        release.set()
        if not reload_task.done():
            reload_task.cancel()
        await asyncio.gather(reload_task, return_exceptions=True)
        if app._lifecycle_state.value != "stopped":
            app._background_task_stop_timeout_seconds = 0.5
            await app.stop()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ws_client_stop_is_shared_across_reconcile_timeouts(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    app._background_task_stop_timeout_seconds = 0.01
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    resistant_stop = cancellation_resistant_callback(entered, cancelled, release)

    old_client = MagicMock(
        ws_uri="ws://old/ws",
        auth_token="old-token",
        _queue_size=10,
        stop=AsyncMock(side_effect=resistant_stop),
    )
    app.ws_client = old_client
    new_client = MagicMock(
        ws_uri="ws://new/ws",
        auth_token="new-token",
        _queue_size=20,
        stop=AsyncMock(),
        set_on_connect=Mock(),
        connect_and_listen=AsyncMock(),
    )

    with patch("core.app_ingress.OneBotWsClient", return_value=new_client) as client_cls:
        first = asyncio.create_task(
            app._reconcile_ws_client(
                enable_ws=True,
                ws_uri="ws://new/ws",
                token="new-token",
                queue_size=20,
            )
        )
        await entered.wait()
        with pytest.raises(RuntimeError, match="stop exceeded"):
            await first
        assert cancelled.is_set()

        with pytest.raises(RuntimeError, match="stop exceeded"):
            await app._reconcile_ws_client(
                enable_ws=True,
                ws_uri="ws://new/ws",
                token="new-token",
                queue_size=20,
            )
        assert old_client.stop.await_count == 1

        release.set()
        stop_task = app._ws_client_stop_task
        assert stop_task is not None
        await asyncio.wait_for(asyncio.shield(stop_task), timeout=1)
        await app._reconcile_ws_client(
            enable_ws=True,
            ws_uri="ws://new/ws",
            token="new-token",
            queue_size=20,
        )

    client_cls.assert_called_once()
    assert old_client.stop.await_count == 1
    assert app.ws_client is new_client
    await app._stop_ws_client()
    await app._cancel_task("_ws_client_task")
    app.scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_ws_stop_starts_close_before_waiting_for_cancel_resistant_attempt(
    temp_app_root: Path,
    monkeypatch,
):
    from core.onebot import OneBotWsClient

    app = XiaoQingApp(temp_app_root)
    app._background_task_stop_timeout_seconds = 0.1
    client = OneBotWsClient("ws://old/ws", "")
    client._shutdown_timeout_seconds = 0.1
    attempt_entered = asyncio.Event()
    attempt_cancelled = asyncio.Event()
    release_attempt = asyncio.Event()

    class ReleasingSocket:
        closed = False
        close_code = None

        async def close(self) -> None:
            await attempt_cancelled.wait()
            release_attempt.set()

    socket = ReleasingSocket()

    async def resistant_attempt(_handler):
        client._ws = socket
        client._connected_auth_generation = client._endpoint_auth.generation
        await resist_cancellation_until_released(
            attempt_entered,
            attempt_cancelled,
            release_attempt,
        )
        return 0.0

    monkeypatch.setattr(client, "_connect_once", resistant_attempt)
    listener = asyncio.create_task(client.connect_and_listen(AsyncMock()))
    await attempt_entered.wait()
    app.ws_client = client
    app._ws_client_task = listener

    await asyncio.wait_for(app._stop_ws_client(), timeout=0.5)

    assert attempt_cancelled.is_set()
    assert release_attempt.is_set()
    assert app.ws_client is None
    assert client._connection_attempt_tasks == set()
    await app._cancel_task("_ws_client_task")
    app.scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_stop_retains_resistant_ws_client_until_retry(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    app._background_task_stop_timeout_seconds = 0.01
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    resistant_stop = cancellation_resistant_callback(entered, cancelled, release)

    client = MagicMock(stop=AsyncMock(side_effect=resistant_stop))
    app.ws_client = client

    first_stop = asyncio.create_task(app.stop())
    await entered.wait()
    await asyncio.wait_for(first_stop, timeout=0.5)

    assert cancelled.is_set()
    assert app.ws_client is client
    assert app._ws_client_stop_task is not None
    assert app._lifecycle_state.value == "failed"

    release.set()
    await asyncio.wait_for(asyncio.shield(app._ws_client_stop_task), timeout=1)
    await asyncio.wait_for(app.stop(), timeout=0.5)

    assert app.ws_client is None
    assert app._ws_client_stop_task is None
    assert app._lifecycle_state.value == "stopped"


@pytest.mark.unit
def test_app_ignores_config_and_schedule_updates_while_stopping(temp_app_root: Path):
    """Configuration callbacks cannot recreate runtime components after stop begins."""
    from core.config import ConfigSnapshot

    app = XiaoQingApp(temp_app_root)
    app._stopping = True
    app.dispatcher.refresh_prefix_cache = Mock()
    app.scheduler.replace_prefix = Mock()
    app.config_manager.reload = Mock()

    snapshot = ConfigSnapshot(config=app.config, secrets=app.secrets)
    app._apply_config(snapshot)
    app.reload_config()
    app._reschedule("startup")

    app.dispatcher.refresh_prefix_cache.assert_not_called()
    app.config_manager.reload.assert_not_called()
    app.scheduler.replace_prefix.assert_not_called()
