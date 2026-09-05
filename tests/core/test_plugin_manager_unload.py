"""卸载、隔离和并发终止。"""

from __future__ import annotations

import time

import tests.helpers.plugin_manager_test_support as _fixture_support
from tests.helpers.plugin_manager_test_support import (
    AsyncMock,
    ConcurrentFuture,
    LoadedPlugin,
    Mock,
    ModuleType,
    Path,
    PluginExecutionClosed,
    PluginExecutionGate,
    PluginExecutionPolicy,
    PluginExecutionTimeout,
    _AsyncConcurrencyProbe,
    _build_definition,
    _build_manager,
    _register_test_command,
    asyncio,
    call_plugin_callback,
    pytest,
    threading,
)

_isolate_process_global_plugin_import_state = (
    _fixture_support._isolate_process_global_plugin_import_state
)


@pytest.mark.asyncio
async def test_shutdown_timeout_log_includes_budget_and_deadline(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = _build_manager(tmp_path)
    module  = ModuleType("plugins.demo.main")

    async def shutdown() -> None:
        await asyncio.Event().wait()

    module.shutdown = shutdown
    plugin = LoadedPlugin(definition=_build_definition(), module=module, mtime=0.0)

    with caplog.at_level("WARNING", logger="core.plugin_manager"):
        result = await manager._shutdown_plugin_instance(
            "demo",
            plugin,
            shutdown_deadline=time.monotonic() + 0.05,
        )

    assert result is False
    assert "Plugin demo shutdown timed out" in caplog.text
    assert "shutdown_timeout=" in caplog.text
    assert "deadline_remaining=" in caplog.text


@pytest.mark.asyncio
async def test_shutdown_context_uses_registered_state_when_caller_omits_state(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    module  = ModuleType("plugins.demo.main")

    async def shutdown(context) -> None:
        context.state["shutdown_seen"] = True

    module.shutdown = shutdown

    class Context:
        def __init__(self, state) -> None:
            self.state = state

    manager.context_factory = lambda *args, **kwargs: Context(args[3])
    plugin = LoadedPlugin(definition=_build_definition(), module=module, mtime=0.0)

    assert await manager._shutdown_plugin_instance("demo", plugin) is True
    assert manager._plugin_states["demo"] == {"shutdown_seen": True}


@pytest.mark.asyncio
async def test_shutdown_elapsed_deadline_is_logged_before_callback_is_started(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager         = _build_manager(tmp_path)
    module          = ModuleType("plugins.demo.main")
    module.shutdown = AsyncMock()
    plugin = LoadedPlugin(definition=_build_definition(), module=module, mtime=0.0)

    with caplog.at_level("WARNING", logger="core.plugin_manager"):
        result = await manager._shutdown_plugin_instance(
            "demo",
            plugin,
            shutdown_deadline=0.0,
        )

    assert result is False
    module.shutdown.assert_not_awaited()
    assert "Plugin demo shutdown skipped: deadline already elapsed" in caplog.text
    assert "shutdown_timeout=0.000s" in caplog.text
    assert "deadline_remaining=0.000s" in caplog.text


@pytest.mark.asyncio
async def test_unload_cancellation_retains_closed_quarantined_generation(
    tmp_path: Path,
) -> None:
    manager          = _build_manager(tmp_path)
    definition       = _build_definition()
    module           = ModuleType("plugins.demo.main")
    shutdown_started = asyncio.Event()
    release_shutdown = asyncio.Event()

    async def shutdown(context=None) -> None:
        shutdown_started.set()
        await release_shutdown.wait()

    module.shutdown = shutdown
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    plugin = LoadedPlugin(
        definition     = definition,
        module         = module,
        mtime          = 0.0,
        execution_gate = gate,
    )
    state                            = {"resource": object()}
    manager._plugins["demo"]         = plugin
    manager._plugin_states["demo"]   = state
    manager._execution_gates["demo"] = gate

    unload_task = asyncio.create_task(manager.unload_plugin("demo"))
    await asyncio.wait_for(shutdown_started.wait(), timeout=1)
    unload_task.cancel()
    await asyncio.sleep(0)
    assert unload_task.done() is False
    release_shutdown.set()
    with pytest.raises(asyncio.CancelledError):
        await unload_task

    assert manager._plugins["demo"] is plugin
    assert manager._plugin_states["demo"] is state
    assert manager._execution_gates["demo"] is gate
    assert gate.closed is True
    assert "demo" in manager._quarantined_plugins

    await manager.unload_plugin("demo")


@pytest.mark.asyncio
async def test_shutdown_side_effect_is_not_invoked_twice_after_unload_cancellation(
    tmp_path: Path,
) -> None:
    manager                   = _build_manager(tmp_path)
    definition                = _build_definition()
    module                    = ModuleType("plugins.demo.main")
    shutdown_side_effect_done = asyncio.Event()
    release_shutdown          = asyncio.Event()
    shutdown_calls            = 0

    async def shutdown(context=None) -> None:
        nonlocal shutdown_calls
        shutdown_calls += 1
        shutdown_side_effect_done.set()
        await release_shutdown.wait()

    module.shutdown = shutdown
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    plugin = LoadedPlugin(
        definition     = definition,
        module         = module,
        mtime          = 0.0,
        execution_gate = gate,
    )
    state                            = {"resource": object()}
    manager._plugins["demo"]         = plugin
    manager._plugin_states["demo"]   = state
    manager._execution_gates["demo"] = gate

    unload_task = asyncio.create_task(manager.unload_plugin("demo"))
    await asyncio.wait_for(shutdown_side_effect_done.wait(), timeout=1)
    unload_task.cancel()
    release_shutdown.set()
    with pytest.raises(asyncio.CancelledError):
        await unload_task

    assert shutdown_calls == 1
    assert plugin.shutdown_completed is True
    assert "demo" in manager._quarantined_plugins

    await manager.unload_plugin("demo")

    assert shutdown_calls == 1
    assert "demo" not in manager.list_runtime_plugins()


@pytest.mark.asyncio
async def test_unload_cancellation_during_admission_drain_quarantines_generation(
    tmp_path: Path,
) -> None:
    manager         = _build_manager(tmp_path)
    definition      = _build_definition()
    module          = ModuleType("plugins.demo.main")
    module.shutdown = AsyncMock()
    gate            = PluginExecutionGate(
        "parallel",
        plugin_name="demo",
        policy=PluginExecutionPolicy(drain_timeout_seconds=0.05),
    )
    plugin = LoadedPlugin(
        definition     = definition,
        module         = module,
        mtime          = 0.0,
        execution_gate = gate,
    )
    state                                        = {"resource": object()}
    unfinished_sync_work: ConcurrentFuture[None] = ConcurrentFuture()
    gate._sync_futures.add(unfinished_sync_work)

    stale_spec                       = _register_test_command(manager, gate)
    manager._plugins["demo"]         = plugin
    manager._plugin_states["demo"]   = state
    manager._execution_gates["demo"] = gate

    unload_task = asyncio.create_task(manager.unload_plugin("demo"))
    while not gate.closed:
        await asyncio.sleep(0)
    unload_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await unload_task

    assert manager._plugins["demo"] is plugin
    assert manager._plugin_states["demo"] is state
    assert manager._execution_gates["demo"] is gate
    assert "demo" in manager._quarantined_plugins
    assert manager.router.resolve("/demo") is None
    with pytest.raises(PluginExecutionClosed):
        await stale_spec.execution_gate.run(lambda: asyncio.sleep(0))
    module.shutdown.assert_not_awaited()

    unfinished_sync_work.set_result(None)
    manager._quarantined_plugins.discard("demo")
    await manager.unload_plugin("demo")


@pytest.mark.asyncio
async def test_unload_closes_admission_before_a_blocking_state_lock_wait(
    tmp_path: Path,
) -> None:
    manager         = _build_manager(tmp_path)
    definition      = _build_definition()
    module          = ModuleType("plugins.demo.main")
    module.shutdown = AsyncMock()
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    plugin = LoadedPlugin(
        definition     = definition,
        module         = module,
        mtime          = 0.0,
        execution_gate = gate,
    )
    state                            = {"resource": object()}
    stale_spec                       = _register_test_command(manager, gate)
    manager._plugins["demo"]         = plugin
    manager._plugin_states["demo"]   = state
    manager._execution_gates["demo"] = gate

    state_lock = gate._state_lock.get()
    await state_lock.acquire()
    unload_task = asyncio.create_task(manager.unload_plugin("demo"))
    await asyncio.sleep(0)

    assert gate.closed is True
    unload_task.cancel()
    await asyncio.sleep(0)
    assert unload_task.done() is False
    unload_task.cancel()
    await asyncio.sleep(0)
    assert unload_task.done() is False
    state_lock.release()
    with pytest.raises(asyncio.CancelledError):
        await unload_task

    assert manager._plugins["demo"] is plugin
    assert manager._plugin_states["demo"] is state
    assert manager._execution_gates["demo"] is gate
    assert "demo" in manager._quarantined_plugins
    assert manager.router.resolve("/demo") is None
    with pytest.raises(PluginExecutionClosed):
        await stale_spec.execution_gate.run(lambda: asyncio.sleep(0))
    module.shutdown.assert_not_awaited()

    manager._quarantined_plugins.discard("demo")
    await manager.unload_plugin("demo")


@pytest.mark.asyncio
async def test_gate_close_failure_propagates_after_fail_closed_quarantine(
    tmp_path: Path,
) -> None:
    manager         = _build_manager(tmp_path)
    definition      = _build_definition()
    module          = ModuleType("plugins.demo.main")
    module.shutdown = AsyncMock()
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    original_close = gate.close

    async def fail_close(*, timeout_seconds=None):
        raise RuntimeError("drain implementation failed")

    gate.close = fail_close
    plugin     = LoadedPlugin(
        definition     = definition,
        module         = module,
        mtime          = 0.0,
        execution_gate = gate,
    )
    state                            = {"resource": object()}
    stale_spec                       = _register_test_command(manager, gate)
    manager._plugins["demo"]         = plugin
    manager._plugin_states["demo"]   = state
    manager._execution_gates["demo"] = gate

    with pytest.raises(RuntimeError, match="drain implementation failed"):
        await manager.unload_plugin("demo")

    assert gate.closed is True
    assert manager._plugins["demo"] is plugin
    assert manager._plugin_states["demo"] is state
    assert manager._execution_gates["demo"] is gate
    assert "demo" in manager._quarantined_plugins
    assert manager.router.resolve("/demo") is None
    with pytest.raises(PluginExecutionClosed):
        await stale_spec.execution_gate.run(lambda: asyncio.sleep(0))
    module.shutdown.assert_not_awaited()

    gate.close = original_close
    await manager.unload_plugin("demo")


@pytest.mark.asyncio
async def test_module_purge_failure_retains_exact_closed_generation_for_retry(
    tmp_path: Path,
) -> None:
    manager         = _build_manager(tmp_path)
    definition      = _build_definition()
    module          = ModuleType("plugins.demo.main")
    module.shutdown = AsyncMock()
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    plugin = LoadedPlugin(
        definition     = definition,
        module         = module,
        mtime          = 0.0,
        execution_gate = gate,
    )
    state                            = {"resource": object()}
    manager._plugins["demo"]         = plugin
    manager._plugin_states["demo"]   = state
    manager._execution_gates["demo"] = gate
    manager._purge_plugin_modules = Mock(side_effect=RuntimeError("purge failed"))

    with pytest.raises(RuntimeError, match="purge failed"):
        await manager.unload_plugin("demo")

    assert manager._plugins["demo"] is plugin
    assert manager._plugin_states["demo"] is state
    assert manager._execution_gates["demo"] is gate
    assert gate.closed is True
    assert "demo" in manager._quarantined_plugins
    assert manager.router.resolve("demo") is None
    module.shutdown.assert_awaited_once()

    manager._purge_plugin_modules = Mock()
    await manager.unload_plugin("demo")

    module.shutdown.assert_awaited_once()
    assert "demo" not in manager.list_runtime_plugins()


@pytest.mark.asyncio
async def test_reload_purge_failure_keeps_old_plugin_gate_and_state_together(
    tmp_path: Path,
) -> None:
    manager         = _build_manager(tmp_path)
    definition      = _build_definition()
    module          = ModuleType("plugins.demo.main")
    module.shutdown = AsyncMock()
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    plugin = LoadedPlugin(
        definition     = definition,
        module         = module,
        mtime          = 0.0,
        execution_gate = gate,
    )
    state                            = {"resource": object()}
    manager._plugins["demo"]         = plugin
    manager._plugin_states["demo"]   = state
    manager._execution_gates["demo"] = gate
    manager._load_definition = Mock(return_value=definition)
    manager._authorize_plugin_snapshot = Mock(return_value=1.0)
    manager._purge_plugin_modules = Mock(side_effect=RuntimeError("purge failed"))
    manager._load_canonical_candidate = AsyncMock()

    with pytest.raises(RuntimeError, match="purge failed"):
        await manager.reload_plugin("demo")

    assert manager._plugins["demo"] is plugin
    assert manager._plugin_states["demo"] is state
    assert manager._execution_gates["demo"] is gate
    assert gate.closed is True
    assert "demo" in manager._quarantined_plugins
    assert manager.router.resolve("demo") is None
    module.shutdown.assert_awaited_once()
    manager._load_canonical_candidate.assert_not_awaited()

    manager._purge_plugin_modules = Mock()
    await manager.unload_plugin("demo")
    module.shutdown.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["unload", "reload"])
async def test_lifecycle_cancellation_during_shutdown_drain_quarantines_generation(
    tmp_path: Path,
    operation: str,
) -> None:
    manager    = _build_manager(tmp_path)
    definition = _build_definition()
    gate       = PluginExecutionGate(
        "parallel",
        plugin_name="demo",
        policy=PluginExecutionPolicy(drain_timeout_seconds=0.05),
    )
    unfinished_sync_work: ConcurrentFuture[None] = ConcurrentFuture()
    second_close_started                         = asyncio.Event()
    close_calls                                  = 0
    original_close                               = gate.close

    async def tracked_close(*, timeout_seconds=None):
        nonlocal close_calls
        close_calls += 1
        if close_calls == 2:
            second_close_started.set()
        return await original_close(timeout_seconds=timeout_seconds)

    gate.close = tracked_close
    module     = ModuleType("plugins.demo.main")

    async def shutdown(context=None) -> None:
        gate._sync_futures.add(unfinished_sync_work)

    module.shutdown = AsyncMock(side_effect=shutdown)
    plugin = LoadedPlugin(
        definition     = definition,
        module         = module,
        mtime          = 0.0,
        execution_gate = gate,
    )
    state                            = {"resource": object()}
    stale_spec                       = _register_test_command(manager, gate)
    manager._plugins["demo"]         = plugin
    manager._plugin_states["demo"]   = state
    manager._execution_gates["demo"] = gate
    if operation == "reload":
        manager._load_definition = Mock(return_value=definition)
        manager._authorize_plugin_snapshot = Mock(return_value=1.0)
        manager._load_canonical_candidate = AsyncMock()

    lifecycle_task = asyncio.create_task(getattr(manager, f"{operation}_plugin")("demo"))
    await asyncio.wait_for(second_close_started.wait(), timeout=1)
    lifecycle_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await lifecycle_task

    assert manager._plugins["demo"] is plugin
    assert manager._plugin_states["demo"] is state
    assert manager._execution_gates["demo"] is gate
    assert plugin.shutdown_completed is True
    assert "demo" in manager._quarantined_plugins
    assert manager.router.resolve("/demo") is None
    with pytest.raises(PluginExecutionClosed):
        await stale_spec.execution_gate.run(lambda: asyncio.sleep(0))
    module.shutdown.assert_awaited_once()
    if operation == "reload":
        manager._load_canonical_candidate.assert_not_awaited()

    unfinished_sync_work.set_result(None)
    manager._quarantined_plugins.discard("demo")
    await manager.unload_plugin("demo")
    module.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_unload_reload_and_reconcile_share_lifecycle_lock(tmp_path: Path) -> None:
    manager = _build_manager(tmp_path)
    probe   = _AsyncConcurrencyProbe()
    manager._unload_plugin_once = AsyncMock(side_effect=probe.run)
    manager._reload_plugin_once = AsyncMock(side_effect=probe.run)
    manager._reconcile_plugins_once = AsyncMock(side_effect=probe.run)

    await asyncio.gather(
        manager.unload_plugin("demo"),
        manager.reload_plugin("demo"),
        manager.reconcile_plugins(),
    )

    assert probe.maximum_active == 1


@pytest.mark.asyncio
async def test_concurrent_reload_and_unload_shutdown_each_generation_once(
    tmp_path: Path,
) -> None:
    manager              = _build_manager(tmp_path)
    definition           = _build_definition()
    old_module           = ModuleType("plugins.demo.main")
    candidate_module     = ModuleType("plugins.demo.main")
    old_shutdown_started = asyncio.Event()
    release_old_shutdown = asyncio.Event()
    shutdown_calls       = {"old": 0, "candidate": 0}

    async def old_shutdown(context=None) -> None:
        shutdown_calls["old"] += 1
        old_shutdown_started.set()
        await release_old_shutdown.wait()

    async def candidate_shutdown(context=None) -> None:
        shutdown_calls["candidate"] += 1

    old_module.shutdown       = old_shutdown
    candidate_module.shutdown = candidate_shutdown
    old_gate = PluginExecutionGate("parallel", plugin_name="demo")
    manager._plugins["demo"] = LoadedPlugin(
        definition     = definition,
        module         = old_module,
        mtime          = 0.0,
        execution_gate = old_gate,
    )
    manager._execution_gates["demo"] = old_gate
    manager._load_definition = Mock(return_value=definition)
    manager._authorize_plugin_snapshot = Mock(return_value=1.0)

    async def load_candidate(_plugin_dir, transaction):
        return LoadedPlugin(
            definition     = definition,
            module         = candidate_module,
            mtime          = transaction.mtime,
            execution_gate = transaction.gate,
        )

    manager._load_canonical_candidate = AsyncMock(side_effect=load_candidate)

    reload_task = asyncio.create_task(manager.reload_plugin("demo"))
    await asyncio.wait_for(old_shutdown_started.wait(), timeout=1)
    unload_task = asyncio.create_task(manager.unload_plugin("demo"))
    await asyncio.sleep(0)
    assert unload_task.done() is False

    release_old_shutdown.set()
    await asyncio.wait_for(asyncio.gather(reload_task, unload_task), timeout=1)

    assert shutdown_calls == {"old": 1, "candidate": 1}
    assert "demo" not in manager._plugins
    assert "demo" not in manager._execution_gates
    assert "demo" not in manager._quarantined_plugins


@pytest.mark.asyncio
async def test_unload_quarantines_until_timed_out_sync_callback_really_finishes(
    tmp_path: Path,
):
    manager    = _build_manager(tmp_path)
    definition = _build_definition()
    gate       = PluginExecutionGate(
        "sequential",
        plugin_name = "demo",
        policy      = PluginExecutionPolicy(
            timeout_seconds       = 0.1,
            drain_timeout_seconds = 0.1,
        ),
    )
    started         = threading.Event()
    release         = threading.Event()
    finished        = threading.Event()
    shutdown_called = asyncio.Event()
    module          = ModuleType("plugins.demo.main")

    def blocking_handler() -> None:
        started.set()
        release.wait(timeout=2)
        finished.set()

    async def shutdown() -> None:
        shutdown_called.set()

    module.shutdown = shutdown
    plugin          = LoadedPlugin(
        definition     = definition,
        module         = module,
        mtime          = 0.0,
        execution_gate = gate,
    )
    state                            = {"owned": object()}
    manager._plugins["demo"]         = plugin
    manager._plugin_states["demo"]   = state
    manager._execution_gates["demo"] = gate
    manager._purge_plugin_modules    = Mock()

    with pytest.raises(PluginExecutionTimeout):
        await gate.run(lambda: call_plugin_callback(blocking_handler))
    assert started.is_set()

    await manager.unload_plugin("demo")

    assert manager._plugins["demo"] is plugin
    assert manager._plugin_states["demo"] is state
    assert manager._execution_gates["demo"] is gate
    assert "demo" in manager._quarantined_plugins
    assert shutdown_called.is_set() is False
    manager._purge_plugin_modules.assert_not_called()

    release.set()
    assert await asyncio.to_thread(finished.wait, 1)
    await asyncio.sleep(0)
    await manager.unload_plugin("demo")

    assert shutdown_called.is_set()
    assert "demo" not in manager._plugins
    assert "demo" not in manager._plugin_states
    assert "demo" not in manager._execution_gates
    assert "demo" not in manager._quarantined_plugins
    manager._purge_plugin_modules.assert_called_once_with("demo")


@pytest.mark.asyncio
async def test_reload_never_installs_candidate_beside_timed_out_sync_callback(
    tmp_path: Path,
):
    manager    = _build_manager(tmp_path)
    definition = _build_definition()
    gate       = PluginExecutionGate(
        "sequential",
        plugin_name = "demo",
        policy      = PluginExecutionPolicy(
            # 给 Windows 线程池留出实际启动时间，同时仍验证超时隔离语义。
            timeout_seconds       = 0.1,
            drain_timeout_seconds = 0.1,
        ),
    )
    started  = threading.Event()
    release  = threading.Event()
    finished = threading.Event()

    def blocking_handler() -> None:
        started.set()
        release.wait(timeout=2)
        finished.set()

    old_plugin = LoadedPlugin(
        definition     = definition,
        module         = ModuleType("plugins.demo.main"),
        mtime          = 0.0,
        execution_gate = gate,
    )
    manager._plugins["demo"]         = old_plugin
    manager._execution_gates["demo"] = gate
    manager._plugin_states["demo"]   = {"old": True}
    manager._load_definition = Mock(return_value=definition)
    manager._shutdown_plugin_instance = AsyncMock(return_value=True)
    manager._authorize_plugin_snapshot = Mock(return_value=1.0)
    manager._load_canonical_candidate = AsyncMock()

    with pytest.raises(PluginExecutionTimeout):
        await gate.run(lambda: call_plugin_callback(blocking_handler))
    assert started.is_set()

    await manager.reload_plugin("demo")

    assert manager._plugins["demo"] is old_plugin
    assert manager._execution_gates["demo"] is gate
    assert gate.closed is True
    assert "demo" in manager._quarantined_plugins
    manager._load_canonical_candidate.assert_not_awaited()
    manager._shutdown_plugin_instance.assert_not_awaited()

    release.set()
    assert await asyncio.to_thread(finished.wait, 1)
    await asyncio.sleep(0)
    assert (await gate.close()).drained is True
