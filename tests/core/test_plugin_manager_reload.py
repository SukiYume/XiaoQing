"""候选代重载和回滚。"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import tests.helpers.plugin_manager_test_support as _fixture_support
from core.plugin_manager_support import _PROCESS_IMPORT_PATH_LEASES
from tests.helpers.plugin_manager_test_support import (
    CommandRouter,
    ConcurrentFuture,
    LoadedPlugin,
    ModuleType,
    PluginExecutionGate,
    PluginExecutionPolicy,
    PluginLoadError,
    PluginManager,
    PluginPathError,
    build_manager,
    load_plugin_dir,
    textwrap,
)

_isolate_process_global_plugin_import_state = (
    _fixture_support._isolate_process_global_plugin_import_state
)


_TRACKER_NAME = "xiaoqing_reload_tracker"

_DEMO_MANIFEST = (
    '{"name":"demo","version":"1.0.0","entry":"main.py",'
    '"commands":[{"name":"demo","triggers":["demo"],"help":"demo"}],'
    '"schedule":[],"concurrency":"parallel","enabled":true}'
)

# 旧一代：init / shutdown 各记一次，handle 回答 "old"。
_OLD_SOURCE = """
import xiaoqing_reload_tracker as tracker

async def init(context=None):
    tracker.old_inits += 1

async def shutdown(context=None):
    tracker.old_shutdowns += 1

async def handle(command, args, event, context):
    return [{"type": "text", "data": {"text": "old"}}]
"""

# 候选代：init 直接失败；shutdown 记次数。
_FAILING_CANDIDATE_SOURCE = """
import sys
import xiaoqing_reload_tracker as tracker

async def init(context=None):
    tracker.candidate_inits += 1
    tracker.candidate_module = sys.modules[__name__]
    raise RuntimeError("candidate init failed")

async def shutdown(context=None):
    tracker.candidate_shutdowns += 1
"""


@pytest.fixture
def tracker():
    """插件源码通过这个模块向测试回报 init / shutdown 次数。"""

    module                     = ModuleType(_TRACKER_NAME)
    module.old_inits           = 0
    module.old_shutdowns       = 0
    module.candidate_inits     = 0
    module.candidate_shutdowns = 0
    sys.modules[_TRACKER_NAME] = module
    yield module
    sys.modules.pop(_TRACKER_NAME, None)


def _write_demo(
    manager: PluginManager,
    source: str,
    *,
    manifest: str = _DEMO_MANIFEST,
    name: str     = "demo",
) -> Path:
    plugin_dir = manager.plugins_dir / name
    plugin_dir.mkdir(exist_ok=True)
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "main.py").write_text(textwrap.dedent(source).strip() + "\n", encoding="utf-8")
    (plugin_dir / "plugin.json").write_text(manifest, encoding="utf-8")
    return plugin_dir


async def _load_old_demo(manager: PluginManager) -> tuple[Path, LoadedPlugin]:
    plugin_dir = _write_demo(manager, _OLD_SOURCE)
    await load_plugin_dir(manager, plugin_dir)
    return plugin_dir, manager._plugins["demo"]


async def _cleanup_demo(manager: PluginManager, name: str = "demo") -> None:
    manager._restart_required_plugins.discard(name)
    manager._quarantined_plugins.discard(name)
    if name in manager.list_runtime_plugins():
        await manager.unload_plugin(name)


@pytest.mark.asyncio
async def test_reload_cancellation_promptly_cancels_candidate_init(
    tmp_path: Path,
    tracker: ModuleType,
) -> None:
    manager = build_manager(tmp_path)
    _, old_plugin = await _load_old_demo(manager)
    tracker.init_started   = asyncio.Event()
    tracker.init_cancelled = asyncio.Event()
    _write_demo(
        manager,
        """
        import asyncio
        import xiaoqing_reload_tracker as tracker

        async def init(context=None):
            tracker.init_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                tracker.init_cancelled.set()
                raise

        async def shutdown(context=None):
            tracker.candidate_shutdowns += 1
        """,
    )

    reload_task = asyncio.create_task(manager.reload_plugin("demo"))
    try:
        await asyncio.wait_for(tracker.init_started.wait(), timeout=1)
        reload_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(reload_task, timeout=1)

        assert tracker.init_cancelled.is_set()
        assert tracker.candidate_shutdowns == 1
        assert manager._plugins["demo"] is old_plugin
        assert manager._execution_gates["demo"] is old_plugin.execution_gate
        assert manager._execution_gates["demo"].closed is False
    finally:
        await _cleanup_demo(manager)


@pytest.mark.asyncio
async def test_reload_quarantines_candidate_when_candidate_shutdown_fails(
    tmp_path: Path,
    tracker: ModuleType,
) -> None:
    manager = build_manager(tmp_path)
    _, old_plugin = await _load_old_demo(manager)
    old_gate = old_plugin.execution_gate
    _write_demo(
        manager,
        """
        import sys
        import xiaoqing_reload_tracker as tracker

        async def init(context=None):
            tracker.candidate_module = sys.modules[__name__]
            raise RuntimeError("candidate init failed")

        async def shutdown(context=None):
            tracker.candidate_shutdowns += 1
            raise RuntimeError("candidate shutdown failed")
        """,
    )

    try:
        await manager.reload_plugin("demo")

        candidate = manager._plugins["demo"]
        assert old_gate is not None and old_gate.closed is True
        assert candidate is not old_plugin
        assert candidate.module is tracker.candidate_module
        assert candidate.execution_gate is manager._execution_gates["demo"]
        assert candidate.execution_gate is not None and candidate.execution_gate.closed is True
        assert "demo" in manager._quarantined_plugins
        assert tracker.old_shutdowns == 1
        assert tracker.candidate_shutdowns == 1
    finally:
        await _cleanup_demo(manager)


@pytest.mark.asyncio
async def test_candidate_with_undrained_init_work_is_not_replaced_by_old_generation(
    tmp_path: Path,
    tracker: ModuleType,
) -> None:
    manager = build_manager(tmp_path)
    _, old_plugin = await _load_old_demo(manager)
    manager.configure_execution({"timeout_seconds": 0.01, "drain_timeout_seconds": 0.01})
    tracker.started  = threading.Event()
    tracker.release  = threading.Event()
    tracker.finished = threading.Event()
    _write_demo(
        manager,
        """
        import sys
        import xiaoqing_reload_tracker as tracker
        from core.plugin_base import run_sync

        def blocking_init_work():
            tracker.started.set()
            tracker.release.wait(timeout=2)
            tracker.finished.set()

        async def init(context=None):
            tracker.candidate_module = sys.modules[__name__]
            await run_sync(blocking_init_work)

        async def shutdown(context=None):
            tracker.candidate_shutdowns += 1
        """,
    )

    try:
        await manager.reload_plugin("demo")

        candidate = manager._plugins["demo"]
        assert tracker.started.is_set()
        assert candidate is not old_plugin
        assert candidate.module is tracker.candidate_module
        assert candidate.execution_gate is manager._execution_gates["demo"]
        assert candidate.execution_gate is not None and candidate.execution_gate.closed is True
        assert tracker.candidate_shutdowns == 0
        assert "demo" in manager._quarantined_plugins
    finally:
        tracker.release.set()
        assert await asyncio.to_thread(tracker.finished.wait, 1)
        await asyncio.sleep(0)
        await _cleanup_demo(manager)

    assert tracker.candidate_shutdowns == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_stage", ["admission", "shutdown_drain"])
async def test_candidate_rollback_close_cancellation_keeps_exact_candidate_generation(
    tmp_path: Path,
    tracker: ModuleType,
    cancel_stage: str,
) -> None:
    manager = build_manager(tmp_path)
    _, old_plugin = await _load_old_demo(manager)
    unfinished_sync_work: ConcurrentFuture[None]     = ConcurrentFuture()
    target_close_started                             = asyncio.Event()
    candidate_gate_holder: list[PluginExecutionGate] = []

    def on_candidate_init() -> None:
        # 候选代刚建好、init 失败之前：在它的执行门上设屏障，并让它带着未结束的同步任务。
        gate = manager._execution_gates["demo"]
        candidate_gate_holder.append(gate)
        gate.set_policy(PluginExecutionPolicy(drain_timeout_seconds=0.05))
        original_close = gate.close
        close_calls    = 0

        async def tracked_close(*, timeout_seconds=None):
            nonlocal close_calls
            close_calls += 1
            expected_call = 1 if cancel_stage == "admission" else 2
            if close_calls == expected_call:
                target_close_started.set()
            return await original_close(timeout_seconds=timeout_seconds)

        gate.close = tracked_close
        if cancel_stage == "admission":
            gate._sync_futures.add(unfinished_sync_work)

    def on_candidate_shutdown() -> None:
        if cancel_stage == "shutdown_drain":
            candidate_gate_holder[0]._sync_futures.add(unfinished_sync_work)

    tracker.on_candidate_init     = on_candidate_init
    tracker.on_candidate_shutdown = on_candidate_shutdown
    _write_demo(
        manager,
        """
        import sys
        import xiaoqing_reload_tracker as tracker

        async def init(context=None):
            tracker.candidate_module = sys.modules[__name__]
            tracker.on_candidate_init()
            raise RuntimeError("candidate init failed")

        async def shutdown(context=None):
            tracker.candidate_shutdowns += 1
            tracker.on_candidate_shutdown()
        """,
    )
    old_state   = manager._plugin_states["demo"]
    reload_task = asyncio.create_task(manager.reload_plugin("demo"))
    try:
        await asyncio.wait_for(target_close_started.wait(), timeout=1)
        candidate_gate  = candidate_gate_holder[0]
        candidate_state = manager._plugin_states["demo"]
        reload_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reload_task

        candidate = manager._plugins["demo"]
        assert candidate is not old_plugin
        assert candidate.module is tracker.candidate_module
        assert candidate.execution_gate is candidate_gate
        assert candidate_state is not old_state
        assert manager._plugin_states["demo"] is candidate_state
        assert manager._execution_gates["demo"] is candidate_gate
        assert candidate_gate.closed is True
        assert "demo" in manager._quarantined_plugins
        assert manager.router.resolve("demo") is None
        assert tracker.old_shutdowns == 1
        assert tracker.candidate_shutdowns == (0 if cancel_stage == "admission" else 1)

        unfinished_sync_work.set_result(None)
        manager._quarantined_plugins.discard("demo")
        await manager.unload_plugin("demo")
        assert tracker.candidate_shutdowns == 1
    finally:
        if not unfinished_sync_work.done():
            unfinished_sync_work.set_result(None)
        if not reload_task.done():
            reload_task.cancel()
            await asyncio.gather(reload_task, return_exceptions=True)
        await _cleanup_demo(manager)


@pytest.mark.asyncio
async def test_candidate_without_module_close_cancellation_removes_retired_old_registry(
    tmp_path: Path,
    tracker: ModuleType,
) -> None:
    manager = build_manager(tmp_path)
    _, _ = await _load_old_demo(manager)
    unfinished_sync_work: ConcurrentFuture[None]     = ConcurrentFuture()
    candidate_close_started                          = asyncio.Event()
    candidate_gate_holder: list[PluginExecutionGate] = []

    class ObservedGates(dict[str, PluginExecutionGate]):
        """候选代的执行门一登记就设屏障：这时候选代还没有任何模块。"""

        def __setitem__(self, name: str, gate: PluginExecutionGate) -> None:
            super().__setitem__(name, gate)
            if candidate_gate_holder or name != "demo" or gate.closed:
                return
            if gate is old_gate_holder[0]:
                return
            candidate_gate_holder.append(gate)
            gate.set_policy(PluginExecutionPolicy(drain_timeout_seconds=0.05))
            gate._sync_futures.add(unfinished_sync_work)
            original_close = gate.close

            async def tracked_close(*, timeout_seconds=None):
                candidate_close_started.set()
                return await original_close(timeout_seconds=timeout_seconds)

            gate.close = tracked_close

    old_gate_holder          = [manager._execution_gates["demo"]]
    manager._execution_gates = ObservedGates(manager._execution_gates)
    # 语法错误：候选代在得到模块对象之前就导入失败。
    _write_demo(manager, "def broken(:\n")

    reload_task = asyncio.create_task(manager.reload_plugin("demo"))
    try:
        await asyncio.wait_for(candidate_close_started.wait(), timeout=1)
        candidate_gate  = candidate_gate_holder[0]
        candidate_state = manager._plugin_states["demo"]
        reload_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reload_task

        assert "demo" not in manager._plugins
        assert manager._plugin_states["demo"] is candidate_state
        assert manager._execution_gates["demo"] is candidate_gate
        assert candidate_gate.closed is True
        assert "demo" in manager._quarantined_plugins
        assert manager.router.resolve("demo") is None
        assert tracker.old_shutdowns == 1

        unfinished_sync_work.set_result(None)
        manager._quarantined_plugins.discard("demo")
        manager._restart_required_plugins.discard("demo")
        await manager.unload_plugin("demo")
        assert "demo" not in manager.list_runtime_plugins()
    finally:
        if not unfinished_sync_work.done():
            unfinished_sync_work.set_result(None)
        if not reload_task.done():
            reload_task.cancel()
            await asyncio.gather(reload_task, return_exceptions=True)
        await _cleanup_demo(manager)


@pytest.mark.asyncio
async def test_canonical_publication_failure_rolls_back_candidate_and_restores_old(
    tmp_path: Path,
    tracker: ModuleType,
) -> None:
    manager = build_manager(tmp_path)
    _, old_plugin = await _load_old_demo(manager)
    old_gate        = old_plugin.execution_gate
    old_state       = manager._plugin_states["demo"]
    old_module      = sys.modules["plugins.demo.main"]
    tracker.manager = manager
    _write_demo(
        manager,
        """
        import xiaoqing_reload_tracker as tracker

        IS_CANDIDATE = True

        async def init(context=None):
            tracker.candidate_inits += 1
            tracker.candidate_gate = tracker.manager._execution_gates["demo"]

        async def shutdown(context=None):
            tracker.candidate_shutdowns += 1

        async def handle(command, args, event, context):
            return []
        """,
    )
    register = manager._register_loaded_plugin

    def fail_candidate_publish(definition, module, mtime, **kwargs):
        if getattr(module, "IS_CANDIDATE", False):
            raise RuntimeError("candidate publish failed")
        return register(definition, module, mtime, **kwargs)

    # 候选代 init 成功、清单授权仍有效时发布失败：真实输入走不到这个位置，只能在发布处注入失败。
    manager._register_loaded_plugin = fail_candidate_publish  # type: ignore[method-assign]

    try:
        await manager.reload_plugin("demo")

        assert tracker.old_shutdowns == 1
        assert tracker.candidate_inits == 1
        assert tracker.candidate_shutdowns == 1
        assert manager._plugins["demo"] is old_plugin
        assert manager._plugin_states["demo"] is old_state
        assert manager._execution_gates["demo"] is old_plugin.execution_gate
        assert manager._execution_gates["demo"] is not old_gate
        assert manager._execution_gates["demo"].closed is False
        assert tracker.candidate_gate.closed is True
        assert "demo" not in manager._quarantined_plugins
        assert sys.modules["plugins.demo.main"] is old_module
        assert manager.router.resolve("demo") is not None
    finally:
        await _cleanup_demo(manager)


@pytest.mark.asyncio
async def test_restore_cancellation_propagates_after_quarantined_rollback(
    tmp_path: Path,
    tracker: ModuleType,
) -> None:
    manager    = build_manager(tmp_path)
    plugin_dir = _write_demo(
        manager,
        """
        import asyncio
        import xiaoqing_reload_tracker as tracker

        async def init(context=None):
            tracker.old_inits += 1
            if tracker.old_inits >= 2:
                tracker.restore_started.set()
                await asyncio.Event().wait()

        async def shutdown(context=None):
            tracker.old_shutdowns += 1
        """,
    )
    tracker.restore_started = asyncio.Event()
    await load_plugin_dir(manager, plugin_dir)
    old_module = sys.modules["plugins.demo.main"]
    _write_demo(manager, _FAILING_CANDIDATE_SOURCE)

    reload_task = asyncio.create_task(manager.reload_plugin("demo"))
    try:
        await asyncio.wait_for(tracker.restore_started.wait(), timeout=1)
        reload_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reload_task

        restored = manager._plugins["demo"]
        assert restored.module is old_module
        assert restored.execution_gate is manager._execution_gates["demo"]
        assert restored.execution_gate is not None and restored.execution_gate.closed is True
        assert "demo" in manager._quarantined_plugins
        assert tracker.old_shutdowns == 2
    finally:
        if not reload_task.done():
            reload_task.cancel()
            await asyncio.gather(reload_task, return_exceptions=True)
        await _cleanup_demo(manager)


@pytest.mark.asyncio
async def test_reload_restore_revalidates_authorization_after_blocked_old_init(
    tmp_path: Path,
    tracker: ModuleType,
) -> None:
    manager    = build_manager(tmp_path)
    plugin_dir = _write_demo(
        manager,
        """
        import asyncio
        import xiaoqing_reload_tracker as tracker

        async def init(context=None):
            tracker.old_inits += 1
            if tracker.old_inits >= 2:
                tracker.restore_started.set()
                await tracker.release_restore.wait()

        async def shutdown(context=None):
            tracker.old_shutdowns += 1

        async def handle(command, args, event, context):
            return []
        """,
    )
    tracker.restore_started = asyncio.Event()
    tracker.release_restore = asyncio.Event()
    await load_plugin_dir(manager, plugin_dir)
    old_plugin = manager._plugins["demo"]
    old_gate   = old_plugin.execution_gate
    assert manager.router.resolve("demo") is not None
    _write_demo(manager, _FAILING_CANDIDATE_SOURCE)
    manifest_path = plugin_dir / "plugin.json"

    reload_task = asyncio.create_task(manager.reload_plugin("demo"))
    try:
        await asyncio.wait_for(tracker.restore_started.wait(), timeout=1)
        manifest_path.write_text(
            _DEMO_MANIFEST.replace('"enabled":true', '"enabled":false'),
            encoding="utf-8",
        )
        tracker.release_restore.set()
        await asyncio.wait_for(reload_task, timeout=1)

        recovery_gate = manager._execution_gates["demo"]
        assert manager._plugins["demo"] is old_plugin
        assert recovery_gate is old_plugin.execution_gate
        assert recovery_gate is not old_gate
        assert recovery_gate.closed is True
        assert "demo" in manager._quarantined_plugins
        assert manager.router.resolve("demo") is None
        assert tracker.old_shutdowns == 2
    finally:
        tracker.release_restore.set()
        if not reload_task.done():
            reload_task.cancel()
            await asyncio.gather(reload_task, return_exceptions=True)
        await _cleanup_demo(manager)


@pytest.mark.asyncio
async def test_reload_same_entry_policy_change_never_restores_old_generation(
    tmp_path: Path,
    tracker: ModuleType,
) -> None:
    manager = build_manager(tmp_path)
    _, old_plugin = await _load_old_demo(manager)
    old_gate = old_plugin.execution_gate
    assert manager.router.resolve("demo") is not None
    _write_demo(
        manager,
        _FAILING_CANDIDATE_SOURCE,
        manifest=_DEMO_MANIFEST.replace('"help":"demo"', '"help":"demo","admin_only":true'),
    )

    try:
        await manager.reload_plugin("demo")

        assert manager._plugins["demo"] is old_plugin
        assert manager._execution_gates["demo"] is old_gate
        assert old_gate is not None and old_gate.closed is True
        assert "demo" in manager._quarantined_plugins
        assert manager.router.resolve("demo") is None
        assert tracker.old_inits == 1
        assert tracker.old_shutdowns == 1
    finally:
        await _cleanup_demo(manager)


async def _assert_old_generation_restored(
    manager: PluginManager,
    old_plugin: LoadedPlugin,
    old_gate: PluginExecutionGate,
    old_state: dict,
    old_module: ModuleType,
) -> None:
    recovery_gate = manager._execution_gates["demo"]
    assert manager._plugins["demo"] is old_plugin
    assert manager._plugin_states["demo"] is old_state
    assert old_state == {"resource": old_module.SENTINEL}
    assert recovery_gate is old_plugin.execution_gate
    assert recovery_gate is not old_gate
    assert recovery_gate.closed is False
    assert "demo" not in manager._quarantined_plugins
    assert sys.modules["plugins.demo.main"] is old_module
    resolved = manager.router.resolve("demo")
    assert resolved is not None
    assert resolved[0].execution_gate is recovery_gate
    operation = AsyncMock(return_value="available")
    assert await recovery_gate.run(operation) == "available"
    operation.assert_awaited_once()


_STATEFUL_OLD_SOURCE = """
SENTINEL = object()

async def init(context):
    context.state["resource"] = SENTINEL

async def shutdown(context):
    context.state.clear()

async def handle(command, args, event, context):
    return [{"type": "text", "data": {"text": "old"}}]
"""


@pytest.mark.asyncio
async def test_reload_restores_old_plugin_after_candidate_init_failure(tmp_path: Path) -> None:
    manager = build_manager(tmp_path)
    manager.context_factory = lambda _name, _dir, _data, state, *_rest: SimpleNamespace(state=state)
    plugin_dir = _write_demo(manager, _STATEFUL_OLD_SOURCE)
    await load_plugin_dir(manager, plugin_dir)
    old_plugin = manager._plugins["demo"]
    old_gate   = old_plugin.execution_gate
    old_state  = manager._plugin_states["demo"]
    old_module = sys.modules["plugins.demo.main"]
    assert old_gate is not None
    assert old_state == {"resource": old_module.SENTINEL}
    _write_demo(
        manager,
        """
        async def init(context):
            raise RuntimeError("canonical init failed")
        """,
    )

    try:
        await manager.reload_plugin("demo")

        await _assert_old_generation_restored(manager, old_plugin, old_gate, old_state, old_module)
    finally:
        await _cleanup_demo(manager)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old_init", "expected_inits"),
    [
        pytest.param("", 0, id="no-init"),
        pytest.param(
            "async def init():\n    tracker.old_inits += 1\n", 2, id="init-without-context"
        ),
    ],
)
async def test_reload_restores_an_old_generation_that_has_no_init_or_takes_no_context(
    tmp_path: Path, tracker: ModuleType, old_init: str, expected_inits: int
) -> None:
    manager    = build_manager(tmp_path)
    plugin_dir = _write_demo(
        manager,
        "import xiaoqing_reload_tracker as tracker\n"
        + old_init
        + "\nasync def handle(command, args, event, context):\n"
        + '    return [{"type": "text", "data": {"text": "old"}}]\n',
    )
    await load_plugin_dir(manager, plugin_dir)
    old_plugin = manager._plugins["demo"]
    old_gate   = old_plugin.execution_gate
    _write_demo(manager, _FAILING_CANDIDATE_SOURCE)

    try:
        await manager.reload_plugin("demo")

        recovery_gate = manager._execution_gates["demo"]
        assert manager._plugins["demo"] is old_plugin
        assert recovery_gate is old_plugin.execution_gate
        assert recovery_gate is not old_gate
        assert recovery_gate.closed is False
        assert "demo" not in manager._quarantined_plugins
        resolved = manager.router.resolve("demo")
        assert resolved is not None and resolved[0].execution_gate is recovery_gate
        # 旧一代的 init 在加载时跑一次、恢复时再跑一次；没有 init 的旧一代恢复时也不出错。
        assert tracker.old_inits == expected_inits
    finally:
        await _cleanup_demo(manager)


@pytest.mark.asyncio
async def test_reload_restores_old_plugin_after_candidate_import_failure(tmp_path: Path) -> None:
    manager = build_manager(tmp_path)
    manager.context_factory = lambda _name, _dir, _data, state, *_rest: SimpleNamespace(state=state)
    plugin_dir = _write_demo(manager, _STATEFUL_OLD_SOURCE)
    await load_plugin_dir(manager, plugin_dir)
    old_plugin = manager._plugins["demo"]
    old_gate   = old_plugin.execution_gate
    old_state  = manager._plugin_states["demo"]
    old_module = sys.modules["plugins.demo.main"]
    assert old_gate is not None

    async def fail_before_import(_plugin_dir, _transaction) -> LoadedPlugin:
        raise PluginLoadError("demo", "canonical import failed")

    # 导入失败发生在候选代开始执行任何源码之前：真实输入走不到，只能在这个位置注入失败。
    manager._load_canonical_candidate = fail_before_import  # type: ignore[method-assign]

    try:
        await manager.reload_plugin("demo")

        await _assert_old_generation_restored(manager, old_plugin, old_gate, old_state, old_module)
    finally:
        await _cleanup_demo(manager)


@pytest.mark.asyncio
async def test_reload_real_canonical_init_failure_restores_old_module(tmp_path: Path):
    import importlib
    import sys

    manager    = build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    entry = plugin_dir / "main.py"
    entry.write_text(
        textwrap.dedent(
            """
            INITIALIZATIONS = 0

            async def init(context=None):
                global INITIALIZATIONS
                INITIALIZATIONS += 1

            async def shutdown(context=None):
                return None

            async def handle(command, args, event, context):
                return [{"type": "text", "data": {"text": "old"}}]
            """
        ).strip(),
        encoding="utf-8",
    )
    (plugin_dir / "plugin.json").write_text(
        '{"name":"demo","version":"1.0.0","entry":"main.py",'
        '"commands":[{"name":"demo","triggers":["demo"],"help":"demo"}],'
        '"schedule":[],"concurrency":"parallel","enabled":true}',
        encoding="utf-8",
    )

    await load_plugin_dir(manager, plugin_dir)
    old_plugin  = manager._plugins["demo"]
    old_module  = old_plugin.module
    old_package = sys.modules["plugins.demo"]
    old_gate    = old_plugin.execution_gate
    assert old_gate is not None
    old_state                      = {"sentinel": object()}
    manager._plugin_states["demo"] = old_state

    entry.write_text(
        textwrap.dedent(
            """
            CANDIDATE_MARKER = "this replacement is intentionally longer than the old file"

            async def init(context=None):
                raise RuntimeError("canonical candidate init failed")

            async def shutdown(context=None):
                return None

            async def handle(command, args, event, context):
                return [{"type": "text", "data": {"text": "new"}}]
            """
        ).strip(),
        encoding="utf-8",
    )
    importlib.invalidate_caches()

    try:
        await manager.reload_plugin("demo")

        assert manager._plugins["demo"] is old_plugin
        assert manager._plugin_states["demo"] is old_state
        assert manager._execution_gates["demo"].closed is False
        assert sys.modules["plugins.demo.main"] is old_module
        assert sys.modules["plugins"].demo is old_package
        assert old_package.main is old_module
        assert importlib.import_module("plugins.demo.main") is old_module
        assert old_module.INITIALIZATIONS == 2
        resolved = manager.router.resolve("demo")
        assert resolved is not None
        assert resolved[0].handler is old_module.handle
        assert resolved[0].execution_gate is manager._execution_gates["demo"]
    finally:
        await manager.unload_plugin("demo")


@pytest.mark.asyncio
async def test_manager_close_preserves_other_plugin_root_leases(tmp_path: Path) -> None:
    plugins_package = importlib.import_module("plugins")
    first_root      = tmp_path / "first"
    second_root     = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first          = build_manager(first_root)
    second         = build_manager(second_root)
    first_project  = os.path.abspath(first.plugins_dir.parent)
    second_project = os.path.abspath(second.plugins_dir.parent)
    first_package  = os.path.abspath(first.plugins_dir)
    second_package = os.path.abspath(second.plugins_dir)

    assert first_project in sys.path
    assert second_project in sys.path
    assert first_package in plugins_package.__path__  # type: ignore[attr-defined]
    assert second_package in plugins_package.__path__  # type: ignore[attr-defined]

    await first.close(timeout_seconds=0.1)

    assert first_project not in sys.path
    assert first_package not in plugins_package.__path__  # type: ignore[attr-defined]
    assert second_project in sys.path
    assert second_package in plugins_package.__path__  # type: ignore[attr-defined]

    await second.close(timeout_seconds=0.1)

    assert second_project not in sys.path
    assert second_package not in plugins_package.__path__  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_same_root_and_preexisting_import_paths_are_not_removed_early(tmp_path: Path) -> None:
    plugins_package = importlib.import_module("plugins")
    shared_root     = tmp_path / "shared"
    shared_root.mkdir()
    first  = build_manager(shared_root)
    second = PluginManager(
        plugins_dir     = first.plugins_dir,
        router          = CommandRouter(),
        context_factory = lambda *_args, **_kwargs: Mock(),
    )
    project_path = os.path.abspath(shared_root)
    package_path = os.path.abspath(first.plugins_dir)

    await first.close(timeout_seconds=0.1)
    assert project_path in sys.path
    assert package_path in plugins_package.__path__  # type: ignore[attr-defined]

    await second.close(timeout_seconds=0.1)
    assert project_path not in sys.path
    assert package_path not in plugins_package.__path__  # type: ignore[attr-defined]

    external_root    = tmp_path / "external"
    external_plugins = external_root / "plugins"
    external_plugins.mkdir(parents=True)
    (external_plugins / "__init__.py").write_text("", encoding="utf-8")
    external_project_path = os.path.abspath(external_root)
    external_package_path = os.path.abspath(external_plugins)
    sys.path.insert(0, external_project_path)
    plugins_package.__path__.insert(0, external_package_path)  # type: ignore[attr-defined]
    manager = PluginManager(
        plugins_dir     = external_plugins,
        router          = CommandRouter(),
        context_factory = lambda *_args, **_kwargs: Mock(),
    )

    await manager.close(timeout_seconds=0.1)

    assert sys.path.count(external_project_path) == 1
    assert list(plugins_package.__path__).count(external_package_path) == 1  # type: ignore[attr-defined]


def test_import_path_setup_failure_rolls_back_every_acquired_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins_package        = importlib.import_module("plugins")
    immutable_package_path = tuple(plugins_package.__path__)  # type: ignore[attr-defined]
    monkeypatch.setattr(plugins_package, "__path__", immutable_package_path)
    root        = tmp_path / "rollback"
    plugins_dir = root / "plugins"
    plugins_dir.mkdir(parents=True)
    project_path  = os.path.abspath(root)
    leases_before = set(_PROCESS_IMPORT_PATH_LEASES)

    with pytest.raises(PluginPathError, match="no mutable package path"):
        PluginManager(
            plugins_dir     = plugins_dir,
            router          = CommandRouter(),
            context_factory = lambda *_args, **_kwargs: Mock(),
        )

    assert project_path not in sys.path
    assert set(_PROCESS_IMPORT_PATH_LEASES) == leases_before


@pytest.mark.asyncio
async def test_close_cleans_owned_entry_after_package_path_object_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins_package = importlib.import_module("plugins")
    manager         = build_manager(tmp_path)
    owned_container = plugins_package.__path__  # type: ignore[attr-defined]
    package_path    = os.path.abspath(manager.plugins_dir)
    assert package_path in owned_container
    replacement = [value for value in owned_container if value != package_path]
    monkeypatch.setattr(plugins_package, "__path__", replacement)

    await manager.close(timeout_seconds=0.1)

    assert package_path not in owned_container
    assert plugins_package.__path__ is replacement  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_recovery_init_that_resists_cancellation_keeps_exact_generation_owned(
    tmp_path: Path,
) -> None:
    tracker_name              = "xiaoqing_recovery_drain_tracker"
    tracker                   = ModuleType(tracker_name)
    tracker.resist            = False
    tracker.started           = asyncio.Event()
    tracker.release           = asyncio.Event()
    tracker.finished          = asyncio.Event()
    tracker.old_inits         = 0
    tracker.old_shutdowns     = 0
    tracker.candidate_inits   = 0
    tracker.cancellations     = 0
    sys.modules[tracker_name] = tracker

    manager = build_manager(tmp_path)
    manager.configure_execution({"timeout_seconds": 0.02, "drain_timeout_seconds": 0.02})
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    entry = plugin_dir / "main.py"
    entry.write_text(
        textwrap.dedent(
            f"""
            import asyncio
            import {tracker_name} as tracker

            async def init(context=None):
                tracker.old_inits += 1
                if not tracker.resist:
                    return
                tracker.started.set()
                while not tracker.release.is_set():
                    try:
                        await tracker.release.wait()
                    except asyncio.CancelledError:
                        tracker.cancellations += 1
                tracker.finished.set()

            async def shutdown(context=None):
                tracker.old_shutdowns += 1
            """
        ).strip(),
        encoding="utf-8",
    )
    (plugin_dir / "plugin.json").write_text(
        '{"name":"demo","version":"1.0.0","entry":"main.py",'
        '"commands":[],"schedule":[],"concurrency":"parallel","enabled":true}',
        encoding="utf-8",
    )
    await load_plugin_dir(manager, plugin_dir)
    old_plugin     = manager._plugins["demo"]
    old_module     = old_plugin.module
    old_gate       = old_plugin.execution_gate
    tracker.resist = True
    entry.write_text(
        textwrap.dedent(
            f"""
            import {tracker_name} as tracker

            async def init(context=None):
                tracker.candidate_inits += 1
                raise RuntimeError("candidate init failed")

            async def shutdown(context=None):
                return None
            """
        ).strip(),
        encoding="utf-8",
    )

    try:
        await manager.reload_plugin("demo")

        recovery_gate = manager._execution_gates["demo"]
        assert tracker.started.is_set()
        assert tracker.candidate_inits == 1
        assert tracker.old_inits == 2
        assert tracker.old_shutdowns == 1
        assert tracker.cancellations >= 1
        assert manager._plugins["demo"] is old_plugin
        assert old_plugin.execution_gate is recovery_gate
        assert recovery_gate is not old_gate
        assert recovery_gate.closed is True
        assert recovery_gate.drained is False
        assert "demo" in manager._quarantined_plugins
        assert "demo" in manager._restart_required_plugins
        assert sys.modules["plugins.demo.main"] is old_module
        assert old_module in manager._owned_plugin_modules["demo"].values()

        tracker.release.set()
        await asyncio.wait_for(tracker.finished.wait(), timeout=1)
        for _ in range(100):
            if recovery_gate.drained:
                break
            await asyncio.sleep(0.01)
        assert recovery_gate.drained is True

        manager._restart_required_plugins.discard("demo")
        manager._quarantined_plugins.discard("demo")
        await manager.unload_plugin("demo")
        assert "demo" not in manager.list_runtime_plugins()
    finally:
        tracker.release.set()
        if "demo" in manager.list_runtime_plugins():
            manager._restart_required_plugins.discard("demo")
            manager._quarantined_plugins.discard("demo")
            await manager.unload_plugin("demo")
        sys.modules.pop(tracker_name, None)


@pytest.mark.asyncio
async def test_reload_rolls_back_candidate_that_removes_its_sys_modules_entry(
    tmp_path: Path,
    tracker: ModuleType,
) -> None:
    manager = build_manager(tmp_path)
    _, old_plugin = await _load_old_demo(manager)
    old_gate              = old_plugin.execution_gate
    old_state             = manager._plugin_states["demo"]
    tracker.resource_open = False
    _write_demo(
        manager,
        """
        import sys
        import xiaoqing_reload_tracker as tracker

        async def init(context=None):
            tracker.resource_open = True
            sys.modules.pop(__name__, None)
            raise RuntimeError("candidate removed its canonical module")

        async def shutdown(context=None):
            tracker.candidate_shutdowns += 1
            tracker.resource_open = False
        """,
    )
    importlib.invalidate_caches()

    try:
        await manager.reload_plugin("demo")

        assert tracker.candidate_shutdowns == 1
        assert tracker.resource_open is False
        assert manager._plugins["demo"] is old_plugin
        assert manager._plugin_states["demo"] is old_state
        assert manager._execution_gates["demo"] is not old_gate
        assert manager._execution_gates["demo"].closed is False
        assert tracker.old_inits == 2
        assert tracker.old_shutdowns == 1
        assert "demo" not in manager._quarantined_plugins
    finally:
        await _cleanup_demo(manager)
