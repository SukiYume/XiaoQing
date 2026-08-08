"""候选代重载和回滚。"""

from __future__ import annotations

import importlib

import tests.helpers.plugin_manager_test_support as _fixture_support
from tests.helpers.plugin_manager_test_support import (
    _PROCESS_IMPORT_PATH_LEASES,
    AsyncMock,
    CommandRouter,
    ConcurrentFuture,
    LoadedPlugin,
    Mock,
    ModuleType,
    Path,
    PluginExecutionGate,
    PluginExecutionPolicy,
    PluginLoadError,
    PluginManager,
    PluginPathError,
    _build_definition,
    _build_manager,
    _register_test_command,
    _write_runtime_manifest,
    asyncio,
    call_plugin_callback,
    os,
    pytest,
    sys,
    textwrap,
    threading,
)

_isolate_process_global_plugin_import_state = (
    _fixture_support._isolate_process_global_plugin_import_state
)


@pytest.mark.asyncio
async def test_reload_cancellation_promptly_cancels_candidate_init(tmp_path: Path) -> None:
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    _write_runtime_manifest(plugin_dir, "demo")
    old_module = ModuleType("plugins.demo.main")
    old_module.shutdown = AsyncMock()
    candidate_module = ModuleType("plugins.demo.main")
    candidate_module.shutdown = AsyncMock()
    old_gate = PluginExecutionGate("parallel", plugin_name="demo")
    old_plugin = LoadedPlugin(
        definition=definition,
        module=old_module,
        mtime=0.0,
        execution_gate=old_gate,
    )
    manager._plugins["demo"] = old_plugin
    manager._plugin_states["demo"] = {"old": True}
    manager._execution_gates["demo"] = old_gate
    manager._load_definition = Mock(return_value=definition)
    manager._authorize_plugin_snapshot = Mock(
        return_value=manager._capture_plugin_snapshot(plugin_dir, definition)
    )
    manager._definition_is_current = Mock(return_value=True)
    init_started = asyncio.Event()
    init_cancelled = asyncio.Event()

    async def initialize() -> None:
        init_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            init_cancelled.set()
            raise

    def load_module(_plugin_dir, _definition, *, transaction, prepared):
        assert prepared[1] == plugin_dir / "main.py"
        transaction.import_attempted = True
        transaction.import_completed = True
        transaction.module = candidate_module
        init_task = asyncio.create_task(manager._capture_lifecycle(initialize()))
        transaction.init_task = init_task
        return candidate_module, init_task

    manager._load_module = Mock(side_effect=load_module)

    reload_task = asyncio.create_task(manager.reload_plugin("demo"))
    await asyncio.wait_for(init_started.wait(), timeout=1)
    reload_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(reload_task, timeout=1)

    assert init_cancelled.is_set()
    candidate_module.shutdown.assert_awaited_once()
    assert manager._plugins["demo"] is old_plugin
    assert manager._execution_gates["demo"] is old_plugin.execution_gate
    assert manager._execution_gates["demo"].closed is False


@pytest.mark.asyncio
async def test_reload_quarantines_candidate_when_candidate_shutdown_fails(tmp_path: Path):
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    old_gate = PluginExecutionGate("parallel", plugin_name="demo")
    old_plugin = LoadedPlugin(
        definition=definition,
        module=ModuleType("plugins.demo.main"),
        mtime=0.0,
        execution_gate=old_gate,
    )
    manager._plugins["demo"] = old_plugin
    manager._execution_gates["demo"] = old_gate
    manager._load_definition = Mock(return_value=definition)
    manager._shutdown_plugin_instance = AsyncMock(side_effect=[True, False])
    manager._authorize_plugin_snapshot = Mock(return_value=1.0)
    candidate_module = ModuleType("plugins.demo.main")

    async def fail_candidate(_plugin_dir, transaction) -> LoadedPlugin:
        transaction.module = candidate_module
        __import__("sys").modules["plugins.demo.main"] = candidate_module
        raise RuntimeError("candidate init failed")

    manager._load_canonical_candidate = AsyncMock(side_effect=fail_candidate)

    try:
        await manager.reload_plugin("demo")

        candidate = manager._plugins["demo"]
        assert old_gate.closed is True
        assert candidate.module is candidate_module
        assert candidate.execution_gate is manager._execution_gates["demo"]
        assert candidate.execution_gate is not None and candidate.execution_gate.closed is True
        assert "demo" in manager._quarantined_plugins
        assert manager._shutdown_plugin_instance.await_count == 2
    finally:
        __import__("sys").modules.pop("plugins.demo.main", None)


@pytest.mark.asyncio
async def test_candidate_with_undrained_init_work_is_not_replaced_by_old_generation(
    tmp_path: Path,
) -> None:
    import sys

    manager = _build_manager(tmp_path)
    definition = _build_definition()
    old_plugin = LoadedPlugin(
        definition=definition,
        module=ModuleType("plugins.demo.main"),
        mtime=0.0,
        execution_gate=PluginExecutionGate("parallel", plugin_name="demo"),
    )
    manager._plugins["demo"] = old_plugin
    manager._execution_gates["demo"] = old_plugin.execution_gate
    manager._load_definition = Mock(return_value=definition)
    manager._authorize_plugin_snapshot = Mock(return_value=1.0)
    manager._definition_is_current = Mock(return_value=True)
    candidate_module = ModuleType("plugins.demo.main")
    candidate_shutdown_calls = 0
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_init_work() -> None:
        started.set()
        release.wait(timeout=2)
        finished.set()

    async def candidate_shutdown(context=None) -> None:
        nonlocal candidate_shutdown_calls
        candidate_shutdown_calls += 1

    candidate_module.shutdown = candidate_shutdown

    async def fail_candidate(_plugin_dir, transaction):
        gate = transaction.gate
        transaction.module = candidate_module
        gate.set_policy(
            PluginExecutionPolicy(
                timeout_seconds=0.01,
                drain_timeout_seconds=0.01,
            )
        )
        sys.modules["plugins.demo.main"] = candidate_module
        await gate.run(lambda: call_plugin_callback(blocking_init_work))
        raise AssertionError("timed out candidate unexpectedly returned")

    manager._load_canonical_candidate = AsyncMock(side_effect=fail_candidate)

    try:
        await manager.reload_plugin("demo")

        candidate = manager._plugins["demo"]
        assert started.is_set()
        assert candidate.module is candidate_module
        assert candidate.execution_gate is manager._execution_gates["demo"]
        assert candidate.execution_gate is not None and candidate.execution_gate.closed is True
        assert candidate_shutdown_calls == 0
        assert "demo" in manager._quarantined_plugins
        assert manager._plugins["demo"] is not old_plugin
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        await asyncio.sleep(0)
        manager._quarantined_plugins.discard("demo")
        await manager.unload_plugin("demo")
        sys.modules.pop("plugins.demo.main", None)

    assert candidate_shutdown_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_stage", ["admission", "shutdown_drain"])
async def test_candidate_rollback_close_cancellation_keeps_exact_candidate_generation(
    tmp_path: Path,
    cancel_stage: str,
) -> None:
    import sys

    manager = _build_manager(tmp_path)
    definition = _build_definition()
    old_module = ModuleType("plugins.demo.main")
    old_module.shutdown = AsyncMock()
    old_gate = PluginExecutionGate("parallel", plugin_name="demo")
    old_plugin = LoadedPlugin(
        definition=definition,
        module=old_module,
        mtime=0.0,
        execution_gate=old_gate,
    )
    old_state = {"old": True}
    manager._plugins["demo"] = old_plugin
    manager._plugin_states["demo"] = old_state
    manager._execution_gates["demo"] = old_gate
    manager._load_definition = Mock(return_value=definition)
    manager._authorize_plugin_snapshot = Mock(return_value=1.0)
    manager._definition_is_current = Mock(return_value=True)

    candidate_module = ModuleType("plugins.demo.main")
    candidate_module.shutdown = AsyncMock()
    unfinished_sync_work: ConcurrentFuture[None] = ConcurrentFuture()
    target_close_started = asyncio.Event()
    candidate_gate_holder: list[PluginExecutionGate] = []

    async def candidate_shutdown(context=None) -> None:
        if cancel_stage == "shutdown_drain":
            candidate_gate_holder[0]._sync_futures.add(unfinished_sync_work)

    candidate_module.shutdown = AsyncMock(side_effect=candidate_shutdown)

    async def fail_candidate(_plugin_dir, transaction):
        gate = transaction.gate
        transaction.module = candidate_module
        candidate_gate_holder.append(gate)
        gate.set_policy(PluginExecutionPolicy(drain_timeout_seconds=0.05))
        original_close = gate.close
        close_calls = 0

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
        sys.modules["plugins.demo.main"] = candidate_module
        raise RuntimeError("candidate init failed")

    manager._load_canonical_candidate = AsyncMock(side_effect=fail_candidate)
    reload_task = asyncio.create_task(manager.reload_plugin("demo"))
    try:
        await asyncio.wait_for(target_close_started.wait(), timeout=1)
        candidate_gate = candidate_gate_holder[0]
        candidate_state = manager._plugin_states["demo"]
        reload_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reload_task

        candidate = manager._plugins["demo"]
        assert candidate is not old_plugin
        assert candidate.module is candidate_module
        assert candidate.execution_gate is candidate_gate
        assert manager._plugin_states["demo"] is candidate_state
        assert manager._execution_gates["demo"] is candidate_gate
        assert candidate_gate.closed is True
        assert "demo" in manager._quarantined_plugins
        assert manager.router.resolve("/demo") is None
        old_module.shutdown.assert_awaited_once()
        if cancel_stage == "admission":
            candidate_module.shutdown.assert_not_awaited()
        else:
            candidate_module.shutdown.assert_awaited_once()

        unfinished_sync_work.set_result(None)
        manager._quarantined_plugins.discard("demo")
        await manager.unload_plugin("demo")
        candidate_module.shutdown.assert_awaited_once()
    finally:
        if not unfinished_sync_work.done():
            unfinished_sync_work.set_result(None)
        if not reload_task.done():
            reload_task.cancel()
            await asyncio.gather(reload_task, return_exceptions=True)
        sys.modules.pop("plugins.demo.main", None)


@pytest.mark.asyncio
async def test_candidate_without_module_close_cancellation_removes_retired_old_registry(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    old_module = ModuleType("plugins.demo.main")
    old_module.shutdown = AsyncMock()
    old_gate = PluginExecutionGate("parallel", plugin_name="demo")
    old_plugin = LoadedPlugin(
        definition=definition,
        module=old_module,
        mtime=0.0,
        execution_gate=old_gate,
    )
    old_state = {"old": True}
    _register_test_command(manager, old_gate)
    manager._plugins["demo"] = old_plugin
    manager._plugin_states["demo"] = old_state
    manager._execution_gates["demo"] = old_gate
    manager._load_definition = Mock(return_value=definition)
    manager._authorize_plugin_snapshot = Mock(return_value=1.0)

    unfinished_sync_work: ConcurrentFuture[None] = ConcurrentFuture()
    candidate_close_started = asyncio.Event()
    candidate_gate_holder: list[PluginExecutionGate] = []

    async def fail_without_module(_plugin_dir, transaction):
        gate = transaction.gate
        candidate_gate_holder.append(gate)
        gate.set_policy(PluginExecutionPolicy(drain_timeout_seconds=0.05))
        gate._sync_futures.add(unfinished_sync_work)
        original_close = gate.close

        async def tracked_close(*, timeout_seconds=None):
            candidate_close_started.set()
            return await original_close(timeout_seconds=timeout_seconds)

        gate.close = tracked_close
        raise RuntimeError("candidate failed before module import")

    manager._load_canonical_candidate = AsyncMock(side_effect=fail_without_module)
    reload_task = asyncio.create_task(manager.reload_plugin("demo"))
    await asyncio.wait_for(candidate_close_started.wait(), timeout=1)
    candidate_gate = candidate_gate_holder[0]
    candidate_state = manager._plugin_states["demo"]
    reload_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reload_task

    assert "demo" not in manager._plugins
    assert manager._plugin_states["demo"] is candidate_state
    assert manager._execution_gates["demo"] is candidate_gate
    assert candidate_gate.closed is True
    assert "demo" in manager._quarantined_plugins
    assert manager.router.resolve("/demo") is None
    old_module.shutdown.assert_awaited_once()

    unfinished_sync_work.set_result(None)
    manager._quarantined_plugins.discard("demo")
    await manager.unload_plugin("demo")
    assert "demo" not in manager.list_runtime_plugins()


@pytest.mark.asyncio
async def test_canonical_publication_failure_rolls_back_candidate_and_restores_old(
    tmp_path: Path,
) -> None:
    import sys

    manager = _build_manager(tmp_path)
    definition = _build_definition()
    old_module = ModuleType("plugins.demo.main")
    candidate_module = ModuleType("plugins.demo.main")
    shutdown_calls = {"old": 0, "candidate": 0}

    async def old_shutdown(context=None) -> None:
        shutdown_calls["old"] += 1

    async def candidate_shutdown(context=None) -> None:
        shutdown_calls["candidate"] += 1

    old_module.shutdown = old_shutdown
    candidate_module.shutdown = candidate_shutdown
    old_gate = PluginExecutionGate("parallel", plugin_name="demo")
    old_plugin = LoadedPlugin(
        definition=definition,
        module=old_module,
        mtime=0.0,
        execution_gate=old_gate,
    )
    old_state = {"resource": object()}
    manager._plugins["demo"] = old_plugin
    manager._plugin_states["demo"] = old_state
    manager._execution_gates["demo"] = old_gate
    manager._load_definition = Mock(return_value=definition)
    manager._authorize_plugin_snapshot = Mock(return_value=1.0)
    manager._definition_is_current = Mock(return_value=True)
    candidate_holder: list[LoadedPlugin] = []

    async def load_candidate(_plugin_dir, transaction):
        candidate = LoadedPlugin(
            definition=definition,
            module=candidate_module,
            mtime=transaction.mtime,
            execution_gate=transaction.gate,
        )
        candidate_holder.append(candidate)
        return candidate

    manager._load_canonical_candidate = AsyncMock(side_effect=load_candidate)
    register = manager._register_loaded_plugin
    failed = False

    def fail_candidate_publish(definition, module, mtime, **kwargs):
        nonlocal failed
        if module is candidate_module and not failed:
            failed = True
            raise RuntimeError("candidate publish failed")
        return register(definition, module, mtime, **kwargs)

    manager._register_loaded_plugin = Mock(side_effect=fail_candidate_publish)
    sys.modules["plugins.demo.main"] = old_module

    try:
        await manager.reload_plugin("demo")

        assert shutdown_calls == {"old": 1, "candidate": 1}
        assert manager._plugins["demo"] is old_plugin
        assert manager._plugin_states["demo"] is old_state
        assert manager._execution_gates["demo"] is old_plugin.execution_gate
        assert manager._execution_gates["demo"] is not old_gate
        assert manager._execution_gates["demo"].closed is False
        assert candidate_holder[0].execution_gate is not None
        assert candidate_holder[0].execution_gate.closed is True
        assert "demo" not in manager._quarantined_plugins
        assert sys.modules["plugins.demo.main"] is old_module
    finally:
        sys.modules.pop("plugins.demo.main", None)
        manager._purge_plugin_modules("demo")
        manager._release_plugin_namespace("demo")


@pytest.mark.asyncio
async def test_restore_cancellation_propagates_after_quarantined_rollback(
    tmp_path: Path,
) -> None:
    import sys

    manager = _build_manager(tmp_path)
    definition = _build_definition()
    old_module = ModuleType("plugins.demo.main")
    shutdown_calls = 0

    async def shutdown(context=None) -> None:
        nonlocal shutdown_calls
        shutdown_calls += 1

    old_module.shutdown = shutdown
    old_gate = PluginExecutionGate("parallel", plugin_name="demo")
    old_plugin = LoadedPlugin(
        definition=definition,
        module=old_module,
        mtime=0.0,
        execution_gate=old_gate,
    )
    manager._plugins["demo"] = old_plugin
    manager._plugin_states["demo"] = {"resource": object()}
    manager._execution_gates["demo"] = old_gate
    manager._load_definition = Mock(return_value=definition)
    manager._authorize_plugin_snapshot = Mock(return_value=1.0)
    manager._definition_is_current = Mock(return_value=True)
    manager._load_canonical_candidate = AsyncMock(side_effect=RuntimeError("candidate failed"))
    restore_started = asyncio.Event()

    async def block_restore(*_args) -> None:
        restore_started.set()
        await asyncio.Event().wait()

    manager._initialize_plugin_instance = AsyncMock(side_effect=block_restore)
    sys.modules["plugins.demo.main"] = old_module

    reload_task = asyncio.create_task(manager.reload_plugin("demo"))
    try:
        await asyncio.wait_for(restore_started.wait(), timeout=1)
        reload_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reload_task

        restored = manager._plugins["demo"]
        assert restored.module is old_module
        assert restored.execution_gate is manager._execution_gates["demo"]
        assert restored.execution_gate is not None and restored.execution_gate.closed is True
        assert "demo" in manager._quarantined_plugins
        assert shutdown_calls == 2
    finally:
        if not reload_task.done():
            reload_task.cancel()
            await asyncio.gather(reload_task, return_exceptions=True)
        manager._quarantined_plugins.discard("demo")
        await manager.unload_plugin("demo")
        sys.modules.pop("plugins.demo.main", None)


@pytest.mark.asyncio
async def test_reload_restore_revalidates_authorization_after_blocked_old_init(
    tmp_path: Path,
) -> None:
    import sys

    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    manifest = (
        '{"name":"demo","version":"1.0.0","entry":"main.py",'
        '"commands":[],"schedule":[],"concurrency":"parallel","enabled":true}'
    )
    manifest_path = plugin_dir / "plugin.json"
    manifest_path.write_text(manifest, encoding="utf-8")
    definition = manager._load_definition(plugin_dir)
    assert definition is not None
    old_module = ModuleType("plugins.demo.main")
    old_module.shutdown = AsyncMock()
    old_gate = PluginExecutionGate("parallel", plugin_name="demo")
    old_plugin = LoadedPlugin(
        definition=definition,
        module=old_module,
        # This synthetic fixture deliberately has no complete package graph;
        # real source-backed generations are covered by the canonical reload
        # tests below.
        mtime=0.0,
        execution_gate=old_gate,
    )
    manager._plugins["demo"] = old_plugin
    manager._plugin_states["demo"] = {"old": True}
    manager._execution_gates["demo"] = old_gate
    manager._load_canonical_candidate = AsyncMock(side_effect=RuntimeError("candidate failed"))
    restore_started = asyncio.Event()
    release_restore = asyncio.Event()

    async def blocked_restore(*_args) -> None:
        restore_started.set()
        await release_restore.wait()

    manager._initialize_plugin_instance = AsyncMock(side_effect=blocked_restore)
    manager._register_loaded_plugin = Mock(wraps=manager._register_loaded_plugin)
    sys.modules["plugins.demo.main"] = old_module
    reload_task = asyncio.create_task(manager.reload_plugin("demo"))
    try:
        await asyncio.wait_for(restore_started.wait(), timeout=1)
        manifest_path.write_text(
            manifest.replace('"enabled":true', '"enabled":false'),
            encoding="utf-8",
        )
        release_restore.set()
        await asyncio.wait_for(reload_task, timeout=1)

        recovery_gate = manager._execution_gates["demo"]
        assert manager._plugins["demo"] is old_plugin
        assert recovery_gate is old_plugin.execution_gate
        assert recovery_gate is not old_gate
        assert recovery_gate.closed is True
        assert "demo" in manager._quarantined_plugins
        assert manager.router.resolve("demo") is None
        manager._register_loaded_plugin.assert_not_called()
        assert old_module.shutdown.await_count == 2
    finally:
        release_restore.set()
        if not reload_task.done():
            reload_task.cancel()
            await asyncio.gather(reload_task, return_exceptions=True)
        if "demo" in manager.list_runtime_plugins():
            await manager.unload_plugin("demo")
        sys.modules.pop("plugins.demo.main", None)


@pytest.mark.asyncio
async def test_reload_same_entry_policy_change_never_restores_old_generation(
    tmp_path: Path,
) -> None:
    import sys

    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    manifest_path = plugin_dir / "plugin.json"
    old_manifest = (
        '{"name":"demo","version":"1.0.0","entry":"main.py",'
        '"commands":[{"name":"demo","triggers":["demo"],"help":"demo",'
        '"admin_only":false}],"schedule":[],"concurrency":"parallel",'
        '"enabled":true}'
    )
    manifest_path.write_text(old_manifest, encoding="utf-8")
    old_definition = manager._load_definition(plugin_dir)
    assert old_definition is not None
    old_module = ModuleType("plugins.demo.main")
    old_module.shutdown = AsyncMock()
    old_gate = PluginExecutionGate("parallel", plugin_name="demo")
    old_plugin = LoadedPlugin(
        definition=old_definition,
        module=old_module,
        mtime=manager._capture_plugin_snapshot(plugin_dir, old_definition),
        execution_gate=old_gate,
    )
    manager._plugins["demo"] = old_plugin
    manager._plugin_states["demo"] = {"old": True}
    manager._execution_gates["demo"] = old_gate
    manager._load_canonical_candidate = AsyncMock(side_effect=RuntimeError("candidate failed"))
    manager._initialize_plugin_instance = AsyncMock()
    manager._register_loaded_plugin = Mock(wraps=manager._register_loaded_plugin)
    sys.modules["plugins.demo.main"] = old_module
    manifest_path.write_text(
        old_manifest.replace('"admin_only":false', '"admin_only":true'),
        encoding="utf-8",
    )

    try:
        await manager.reload_plugin("demo")

        assert manager._plugins["demo"] is old_plugin
        assert manager._execution_gates["demo"] is old_gate
        assert old_gate.closed is True
        assert "demo" in manager._quarantined_plugins
        assert manager.router.resolve("demo") is None
        manager._initialize_plugin_instance.assert_not_awaited()
        manager._register_loaded_plugin.assert_not_called()
        old_module.shutdown.assert_awaited_once()
    finally:
        if "demo" in manager.list_runtime_plugins():
            await manager.unload_plugin("demo")
        sys.modules.pop("plugins.demo.main", None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "canonical_failure",
    [
        PluginLoadError("demo", "canonical import failed"),
        RuntimeError("canonical init failed"),
    ],
    ids=["import", "init"],
)
async def test_reload_restores_old_plugin_after_canonical_failure(
    tmp_path: Path,
    canonical_failure: Exception,
):
    import sys

    manager = _build_manager(tmp_path)
    definition = _build_definition()
    definition.commands = [{"name": "demo", "triggers": ["demo"], "help": "demo"}]
    old_state = {"resource": object()}
    old_module = ModuleType("plugins.demo.main")

    async def shutdown():
        old_state.clear()

    async def handle(command, args, event, context):
        return [{"type": "text", "data": {"text": "old"}}]

    old_module.shutdown = shutdown
    old_module.handle = handle
    old_gate = PluginExecutionGate("parallel", plugin_name="demo")
    old_plugin = LoadedPlugin(
        definition=definition,
        module=old_module,
        mtime=0.0,
        execution_gate=old_gate,
    )
    manager._plugins["demo"] = old_plugin
    manager._plugin_states["demo"] = old_state
    manager._execution_gates["demo"] = old_gate
    manager.router.replace_plugin(
        definition.name,
        manager._build_command_specs(definition, old_module),
    )
    manager._load_definition = Mock(return_value=definition)
    manager._authorize_plugin_snapshot = Mock(return_value=1.0)
    manager._definition_is_current = Mock(return_value=True)
    manager._load_canonical_candidate = AsyncMock(side_effect=canonical_failure)
    sentinel = old_state["resource"]
    sys.modules["plugins.demo.main"] = old_module

    try:
        await manager.reload_plugin("demo")

        recovery_gate = manager._execution_gates["demo"]
        assert manager._plugins["demo"] is old_plugin
        assert manager._plugin_states["demo"] is old_state
        assert old_state == {"resource": sentinel}
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
    finally:
        sys.modules.pop("plugins.demo.main", None)


@pytest.mark.asyncio
async def test_reload_real_canonical_init_failure_restores_old_module(tmp_path: Path):
    import importlib
    import sys

    manager = _build_manager(tmp_path)
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

    manager.load_plugin(plugin_dir)
    await manager.wait_inits()
    old_plugin = manager._plugins["demo"]
    old_module = old_plugin.module
    old_package = sys.modules["plugins.demo"]
    old_gate = old_plugin.execution_gate
    assert old_gate is not None
    old_state = {"sentinel": object()}
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
        assert not manager._init_tasks
        assert not manager._init_task_plugins
    finally:
        await manager.unload_plugin("demo")


@pytest.mark.asyncio
async def test_manager_close_preserves_other_plugin_root_leases(tmp_path: Path) -> None:
    plugins_package = importlib.import_module("plugins")
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = _build_manager(first_root)
    second = _build_manager(second_root)
    first_project = os.path.abspath(first.plugins_dir.parent)
    second_project = os.path.abspath(second.plugins_dir.parent)
    first_package = os.path.abspath(first.plugins_dir)
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
    shared_root = tmp_path / "shared"
    shared_root.mkdir()
    first = _build_manager(shared_root)
    second = PluginManager(
        plugins_dir=first.plugins_dir,
        router=CommandRouter(),
        context_factory=lambda *_args, **_kwargs: Mock(),
    )
    project_path = os.path.abspath(shared_root)
    package_path = os.path.abspath(first.plugins_dir)

    await first.close(timeout_seconds=0.1)
    assert project_path in sys.path
    assert package_path in plugins_package.__path__  # type: ignore[attr-defined]

    await second.close(timeout_seconds=0.1)
    assert project_path not in sys.path
    assert package_path not in plugins_package.__path__  # type: ignore[attr-defined]

    external_root = tmp_path / "external"
    external_plugins = external_root / "plugins"
    external_plugins.mkdir(parents=True)
    (external_plugins / "__init__.py").write_text("", encoding="utf-8")
    external_project_path = os.path.abspath(external_root)
    external_package_path = os.path.abspath(external_plugins)
    sys.path.insert(0, external_project_path)
    plugins_package.__path__.insert(0, external_package_path)  # type: ignore[attr-defined]
    manager = PluginManager(
        plugins_dir=external_plugins,
        router=CommandRouter(),
        context_factory=lambda *_args, **_kwargs: Mock(),
    )

    await manager.close(timeout_seconds=0.1)

    assert sys.path.count(external_project_path) == 1
    assert list(plugins_package.__path__).count(external_package_path) == 1  # type: ignore[attr-defined]


def test_import_path_setup_failure_rolls_back_every_acquired_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins_package = importlib.import_module("plugins")
    immutable_package_path = tuple(plugins_package.__path__)  # type: ignore[attr-defined]
    monkeypatch.setattr(plugins_package, "__path__", immutable_package_path)
    root = tmp_path / "rollback"
    plugins_dir = root / "plugins"
    plugins_dir.mkdir(parents=True)
    project_path = os.path.abspath(root)
    leases_before = set(_PROCESS_IMPORT_PATH_LEASES)

    with pytest.raises(PluginPathError, match="no mutable package path"):
        PluginManager(
            plugins_dir=plugins_dir,
            router=CommandRouter(),
            context_factory=lambda *_args, **_kwargs: Mock(),
        )

    assert project_path not in sys.path
    assert set(_PROCESS_IMPORT_PATH_LEASES) == leases_before


@pytest.mark.asyncio
async def test_close_cleans_owned_entry_after_package_path_object_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins_package = importlib.import_module("plugins")
    manager = _build_manager(tmp_path)
    owned_container = plugins_package.__path__  # type: ignore[attr-defined]
    package_path = os.path.abspath(manager.plugins_dir)
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
    tracker_name = "xiaoqing_recovery_drain_tracker"
    tracker = ModuleType(tracker_name)
    tracker.resist = False
    tracker.started = asyncio.Event()
    tracker.release = asyncio.Event()
    tracker.finished = asyncio.Event()
    tracker.old_inits = 0
    tracker.old_shutdowns = 0
    tracker.candidate_inits = 0
    tracker.cancellations = 0
    sys.modules[tracker_name] = tracker

    manager = _build_manager(tmp_path)
    manager._execution_policy = PluginExecutionPolicy.from_mapping(
        {
            "timeout_seconds": 0.02,
            "drain_timeout_seconds": 0.02,
        },
        fallback=manager._execution_policy,
    )
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
    manager.load_plugin(plugin_dir)
    await manager.wait_inits()
    old_plugin = manager._plugins["demo"]
    old_module = old_plugin.module
    old_gate = old_plugin.execution_gate
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
) -> None:
    import importlib
    import sys

    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "main.py").write_text(
        textwrap.dedent(
            """
            import sys
            import xiaoqing_candidate_tracker as tracker

            async def init(context=None):
                tracker.resource_open = True
                sys.modules.pop(__name__, None)
                raise RuntimeError("candidate removed its canonical module")

            async def shutdown(context=None):
                tracker.shutdown_calls += 1
                tracker.resource_open = False
            """
        ).strip(),
        encoding="utf-8",
    )
    _write_runtime_manifest(plugin_dir, "demo")
    definition = _build_definition()
    old_module = ModuleType("plugins.demo.main")
    old_module.init = AsyncMock()
    old_module.shutdown = AsyncMock()
    old_gate = PluginExecutionGate("parallel", plugin_name="demo")
    old_plugin = LoadedPlugin(
        definition=definition,
        module=old_module,
        mtime=0.0,
        execution_gate=old_gate,
    )
    old_state = {"old": True}
    tracker = ModuleType("xiaoqing_candidate_tracker")
    tracker.resource_open = False
    tracker.shutdown_calls = 0
    manager._plugins["demo"] = old_plugin
    manager._plugin_states["demo"] = old_state
    manager._execution_gates["demo"] = old_gate
    manager._load_definition = Mock(return_value=definition)
    manager._authorize_plugin_snapshot = Mock(return_value=1.0)
    sys.modules["plugins.demo.main"] = old_module
    sys.modules[tracker.__name__] = tracker
    importlib.invalidate_caches()

    try:
        await manager.reload_plugin("demo")

        assert tracker.shutdown_calls == 1
        assert tracker.resource_open is False
        assert manager._plugins["demo"] is old_plugin
        assert manager._plugin_states["demo"] is old_state
        assert manager._execution_gates["demo"].closed is False
        old_module.init.assert_awaited_once()
        old_module.shutdown.assert_awaited_once()
        assert "demo" not in manager._quarantined_plugins
    finally:
        sys.modules.pop(tracker.__name__, None)
        manager._purge_plugin_modules("demo")
