"""卸载、隔离和并发终止。"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import tests.helpers.plugin_manager_test_support as _fixture_support
from tests.helpers.assertions import logged_events
from tests.helpers.plugin_manager_test_support import (
    DEMO_COMMAND,
    HOOKED_PLUGIN_SOURCE,
    BlockedSyncWork,
    PluginExecutionClosed,
    PluginExecutionTimeout,
    build_manager,
    call_plugin_callback,
    load_plugin,
    write_plugin,
)
from tests.helpers.plugin_probe import PROBE

_isolate_process_global_plugin_import_state = (
    _fixture_support._isolate_process_global_plugin_import_state
)


@pytest.mark.asyncio
async def test_shutdown_timeout_log_includes_budget_and_deadline(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def hang(_context) -> None:
        await asyncio.Event().wait()

    PROBE.slots["on_shutdown"] = hang
    manager                    = build_manager(tmp_path)
    await load_plugin(manager)

    with caplog.at_level("WARNING", logger="core.plugin_manager"):
        await manager.unload_plugin("demo", drain_timeout_seconds=0.05)

    (timed_out,) = logged_events(caplog, "plugin.shutdown.timeout")
    assert timed_out.fields["plugin"] == "demo"
    assert timed_out.fields["timeout_s"] > 0
    assert timed_out.fields["remaining_s"] is not None
    assert "demo" in manager._quarantined_plugins


@pytest.mark.asyncio
async def test_shutdown_context_uses_registered_state_when_caller_omits_state(
    tmp_path: Path,
) -> None:
    shutdown_states: list[dict] = []

    async def on_init(context) -> None:
        context.state["from_init"] = True

    async def on_shutdown(context) -> None:
        shutdown_states.append(dict(context.state))
        context.state["shutdown_seen"] = True

    PROBE.slots["on_init"]     = on_init
    PROBE.slots["on_shutdown"] = on_shutdown
    manager                    = build_manager(tmp_path)
    manager.context_factory = lambda *args, **kwargs: SimpleNamespace(state=args[3])
    await load_plugin(manager)
    registered_state = manager._plugin_states["demo"]

    await manager.unload_plugin("demo")

    assert shutdown_states == [{"from_init": True}]
    assert registered_state == {"from_init": True, "shutdown_seen": True}
    assert "demo" not in manager.list_runtime_plugins()


@pytest.mark.asyncio
async def test_shutdown_elapsed_deadline_is_logged_before_callback_is_started(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = build_manager(tmp_path)
    await load_plugin(manager)

    with caplog.at_level("WARNING", logger="core.plugin_manager"):
        await manager.unload_plugin("demo", drain_timeout_seconds=0)

    assert PROBE.log("shutdown") == []
    (skipped,) = logged_events(caplog, "plugin.shutdown.skipped")
    assert skipped.fields["plugin"] == "demo"
    assert skipped.fields["timeout_s"] == 0
    assert skipped.fields["remaining_s"] == 0
    assert "demo" in manager._quarantined_plugins


@pytest.mark.asyncio
async def test_unload_cancellation_retains_closed_quarantined_generation(
    tmp_path: Path,
) -> None:
    shutdown_started = asyncio.Event()
    release_shutdown = asyncio.Event()

    async def on_shutdown(_context) -> None:
        shutdown_started.set()
        await release_shutdown.wait()

    PROBE.slots["on_shutdown"] = on_shutdown
    manager                    = build_manager(tmp_path)
    plugin                     = await load_plugin(manager)
    gate                       = plugin.execution_gate
    state                      = manager._plugin_states["demo"]

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
    shutdown_side_effect_done = asyncio.Event()
    release_shutdown          = asyncio.Event()

    async def on_shutdown(_context) -> None:
        shutdown_side_effect_done.set()
        await release_shutdown.wait()

    PROBE.slots["on_shutdown"] = on_shutdown
    manager                    = build_manager(tmp_path)
    plugin                     = await load_plugin(manager)

    unload_task = asyncio.create_task(manager.unload_plugin("demo"))
    await asyncio.wait_for(shutdown_side_effect_done.wait(), timeout=1)
    unload_task.cancel()
    release_shutdown.set()
    with pytest.raises(asyncio.CancelledError):
        await unload_task

    assert len(PROBE.log("shutdown")) == 1
    assert plugin.shutdown_completed is True
    assert "demo" in manager._quarantined_plugins

    await manager.unload_plugin("demo")

    assert len(PROBE.log("shutdown")) == 1
    assert "demo" not in manager.list_runtime_plugins()


@pytest.mark.asyncio
async def test_unload_cancellation_during_admission_drain_quarantines_generation(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)
    manager.configure_execution({"drain_timeout_seconds": 0.05})
    plugin = await load_plugin(manager, commands=[DEMO_COMMAND])
    gate     = plugin.execution_gate
    state    = manager._plugin_states["demo"]
    resolved = manager.router.resolve("demo")
    assert resolved is not None
    stale_spec = resolved[0]
    unfinished = BlockedSyncWork()
    await gate.run(unfinished.start)

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
    assert manager.router.resolve("demo") is None
    with pytest.raises(PluginExecutionClosed):
        await stale_spec.execution_gate.run(lambda: asyncio.sleep(0))
    assert PROBE.log("shutdown") == []

    await unfinished.release_and_wait()
    manager._quarantined_plugins.discard("demo")
    await manager.unload_plugin("demo")


@pytest.mark.asyncio
async def test_unload_closes_admission_before_a_blocking_state_lock_wait(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)
    plugin = await load_plugin(manager, commands=[DEMO_COMMAND])
    gate     = plugin.execution_gate
    state    = manager._plugin_states["demo"]
    resolved = manager.router.resolve("demo")
    assert resolved is not None
    stale_spec = resolved[0]

    # 只能占住执行门的状态锁：卸载要停在“门已关、还拿不到状态锁”这个确切位置上。
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
    assert manager.router.resolve("demo") is None
    with pytest.raises(PluginExecutionClosed):
        await stale_spec.execution_gate.run(lambda: asyncio.sleep(0))
    assert PROBE.log("shutdown") == []

    manager._quarantined_plugins.discard("demo")
    await manager.unload_plugin("demo")


@pytest.mark.asyncio
async def test_gate_close_failure_propagates_after_fail_closed_quarantine(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)
    plugin = await load_plugin(manager, commands=[DEMO_COMMAND])
    gate     = plugin.execution_gate
    state    = manager._plugin_states["demo"]
    resolved = manager.router.resolve("demo")
    assert resolved is not None
    stale_spec     = resolved[0]
    original_close = gate.close

    async def fail_close(*, timeout_seconds=None):
        raise RuntimeError("drain implementation failed")

    # 真实的排空走不到“排空本身抛错”：只能在执行门关闭处注入这个失败。
    gate.close = fail_close

    with pytest.raises(RuntimeError, match="drain implementation failed"):
        await manager.unload_plugin("demo")

    assert gate.closed is True
    assert manager._plugins["demo"] is plugin
    assert manager._plugin_states["demo"] is state
    assert manager._execution_gates["demo"] is gate
    assert "demo" in manager._quarantined_plugins
    assert manager.router.resolve("demo") is None
    with pytest.raises(PluginExecutionClosed):
        await stale_spec.execution_gate.run(lambda: asyncio.sleep(0))
    assert PROBE.log("shutdown") == []

    gate.close = original_close
    await manager.unload_plugin("demo")


def fail_purge_once(manager) -> None:  # type: ignore[no-untyped-def]
    """让下一次模块清理抛错，之后照常调用原函数。"""

    real_purge = manager._purge_plugin_modules
    failures   = [RuntimeError("purge failed")]

    def purge(name: str) -> None:
        if failures:
            raise failures.pop()
        real_purge(name)

    # 真实输入走不到清理本身失败：只能在清理处注入失败（只失败一次，重试走真实清理）。
    manager._purge_plugin_modules = purge


@pytest.mark.asyncio
async def test_module_purge_failure_retains_exact_closed_generation_for_retry(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)
    plugin = await load_plugin(manager, commands=[DEMO_COMMAND])
    gate  = plugin.execution_gate
    state = manager._plugin_states["demo"]
    fail_purge_once(manager)

    with pytest.raises(RuntimeError, match="purge failed"):
        await manager.unload_plugin("demo")

    assert manager._plugins["demo"] is plugin
    assert manager._plugin_states["demo"] is state
    assert manager._execution_gates["demo"] is gate
    assert gate.closed is True
    assert "demo" in manager._quarantined_plugins
    assert manager.router.resolve("demo") is None
    assert len(PROBE.log("shutdown")) == 1

    await manager.unload_plugin("demo")

    assert len(PROBE.log("shutdown")) == 1
    assert "demo" not in manager.list_runtime_plugins()
    assert "plugins.demo.main" not in sys.modules


@pytest.mark.asyncio
async def test_reload_purge_failure_keeps_old_plugin_gate_and_state_together(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)
    plugin = await load_plugin(manager, commands=[DEMO_COMMAND])
    gate  = plugin.execution_gate
    state = manager._plugin_states["demo"]
    fail_purge_once(manager)

    with pytest.raises(RuntimeError, match="purge failed"):
        await manager.reload_plugin("demo")

    assert manager._plugins["demo"] is plugin
    assert manager._plugin_states["demo"] is state
    assert manager._execution_gates["demo"] is gate
    assert gate.closed is True
    assert "demo" in manager._quarantined_plugins
    assert manager.router.resolve("demo") is None
    assert len(PROBE.log("shutdown")) == 1
    # 旧代没清干净就不装候选代：插件源码只被导入过一次。
    assert len(PROBE.log("import")) == 1

    await manager.unload_plugin("demo")
    assert len(PROBE.log("shutdown")) == 1
    assert "demo" not in manager.list_runtime_plugins()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["unload", "reload"])
async def test_lifecycle_cancellation_during_shutdown_drain_quarantines_generation(
    tmp_path: Path,
    operation: str,
) -> None:
    manager = build_manager(tmp_path)
    manager.configure_execution({"drain_timeout_seconds": 0.05})
    unfinished           = BlockedSyncWork()
    second_close_started = asyncio.Event()

    async def on_shutdown(_context) -> None:
        # 关闭回调里启动的同步工作还没做完：“关闭回调排空”这一步等不空。
        await unfinished.start()

    PROBE.slots["on_shutdown"] = on_shutdown
    plugin = await load_plugin(manager, commands=[DEMO_COMMAND])
    gate     = plugin.execution_gate
    state    = manager._plugin_states["demo"]
    resolved = manager.router.resolve("demo")
    assert resolved is not None
    stale_spec     = resolved[0]
    close_calls    = 0
    original_close = gate.close

    async def tracked_close(*, timeout_seconds=None):
        nonlocal close_calls
        close_calls += 1
        if close_calls == 2:
            second_close_started.set()
        return await original_close(timeout_seconds=timeout_seconds)

    # 在第二次关闭（关闭回调之后的排空）开始时设屏障，随后照常调用原函数。
    gate.close = tracked_close

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
    assert manager.router.resolve("demo") is None
    with pytest.raises(PluginExecutionClosed):
        await stale_spec.execution_gate.run(lambda: asyncio.sleep(0))
    assert len(PROBE.log("shutdown")) == 1
    # 重载在旧代停下之前没有导入候选代。
    assert len(PROBE.log("import")) == 1

    await unfinished.release_and_wait()
    manager._quarantined_plugins.discard("demo")
    await manager.unload_plugin("demo")
    assert len(PROBE.log("shutdown")) == 1


@pytest.mark.asyncio
async def test_unload_reload_and_reconcile_share_lifecycle_lock(tmp_path: Path) -> None:
    active  = 0
    maximum = 0

    async def exclusive_hook(_context) -> None:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.02)
        finally:
            active -= 1

    PROBE.slots["on_init"]     = exclusive_hook
    PROBE.slots["on_shutdown"] = exclusive_hook
    manager                    = build_manager(tmp_path)
    for name in ("first", "second", "third"):
        await load_plugin(manager, name)
    write_plugin(manager, "second", HOOKED_PLUGIN_SOURCE + "\nVERSION = 2\n")

    await asyncio.gather(
        manager.unload_plugin("first"),
        manager.reload_plugin("second"),
        manager.reconcile_plugins(),
    )

    # 三个入口都真的跑过 init / shutdown，而且从没有两个同时在跑。
    assert len(PROBE.log("shutdown")) >= 2
    assert maximum == 1


@pytest.mark.asyncio
async def test_concurrent_reload_and_unload_shutdown_each_generation_once(
    tmp_path: Path,
) -> None:
    old_shutdown_started = asyncio.Event()
    release_old_shutdown = asyncio.Event()

    async def on_shutdown(_context) -> None:
        old_shutdown_started.set()
        await release_old_shutdown.wait()

    PROBE.slots["on_shutdown"] = on_shutdown
    manager                    = build_manager(tmp_path)
    await load_plugin(manager)
    write_plugin(
        manager,
        "demo",
        """
        from tests.helpers.plugin_probe import PROBE

        async def shutdown(context=None):
            PROBE.log("candidate_shutdown").append(1)
        """,
    )

    reload_task = asyncio.create_task(manager.reload_plugin("demo"))
    await asyncio.wait_for(old_shutdown_started.wait(), timeout=1)
    unload_task = asyncio.create_task(manager.unload_plugin("demo"))
    await asyncio.sleep(0)
    assert unload_task.done() is False

    release_old_shutdown.set()
    await asyncio.wait_for(asyncio.gather(reload_task, unload_task), timeout=1)

    assert len(PROBE.log("shutdown")) == 1
    assert len(PROBE.log("candidate_shutdown")) == 1
    assert "demo" not in manager._plugins
    assert "demo" not in manager._execution_gates
    assert "demo" not in manager._quarantined_plugins


async def run_timed_out_sync_callback(plugin) -> tuple[threading.Event, threading.Event]:  # type: ignore[no-untyped-def]
    """让执行门里一个同步回调超时：它的线程仍在跑，这一代的执行门排不空。"""

    started  = threading.Event()
    release  = threading.Event()
    finished = threading.Event()

    def blocking_handler() -> None:
        started.set()
        release.wait(timeout=2)
        finished.set()

    with pytest.raises(PluginExecutionTimeout):
        await plugin.execution_gate.run(lambda: call_plugin_callback(blocking_handler))
    assert started.is_set()
    return release, finished


@pytest.mark.asyncio
async def test_unload_quarantines_until_timed_out_sync_callback_really_finishes(
    tmp_path: Path,
):
    manager = build_manager(tmp_path)
    # 给 Windows 线程池留出实际启动时间，同时仍验证超时隔离语义。
    manager.configure_execution({"timeout_seconds": 0.1, "drain_timeout_seconds": 0.1})
    plugin = await load_plugin(manager, concurrency="sequential")
    gate  = plugin.execution_gate
    state = manager._plugin_states["demo"]
    release, finished = await run_timed_out_sync_callback(plugin)

    await manager.unload_plugin("demo")

    assert manager._plugins["demo"] is plugin
    assert manager._plugin_states["demo"] is state
    assert manager._execution_gates["demo"] is gate
    assert "demo" in manager._quarantined_plugins
    assert PROBE.log("shutdown") == []
    assert "plugins.demo.main" in sys.modules

    release.set()
    assert await asyncio.to_thread(finished.wait, 1)
    await asyncio.sleep(0)
    await manager.unload_plugin("demo")

    assert len(PROBE.log("shutdown")) == 1
    assert "demo" not in manager._plugins
    assert "demo" not in manager._plugin_states
    assert "demo" not in manager._execution_gates
    assert "demo" not in manager._quarantined_plugins
    assert "plugins.demo.main" not in sys.modules


@pytest.mark.asyncio
async def test_reload_never_installs_candidate_beside_timed_out_sync_callback(
    tmp_path: Path,
):
    manager = build_manager(tmp_path)
    manager.configure_execution({"timeout_seconds": 0.1, "drain_timeout_seconds": 0.1})
    old_plugin = await load_plugin(manager, concurrency="sequential")
    gate = old_plugin.execution_gate
    release, finished = await run_timed_out_sync_callback(old_plugin)

    await manager.reload_plugin("demo")

    assert manager._plugins["demo"] is old_plugin
    assert manager._execution_gates["demo"] is gate
    assert gate.closed is True
    assert "demo" in manager._quarantined_plugins
    # 候选代没有被导入，旧代的关闭回调也没有被调用。
    assert len(PROBE.log("import")) == 1
    assert PROBE.log("shutdown") == []

    release.set()
    assert await asyncio.to_thread(finished.wait, 1)
    await asyncio.sleep(0)
    assert (await gate.close()).drained is True
