"""文件监听和运行态协调。"""

from __future__ import annotations

import tests.helpers.plugin_manager_test_support as _fixture_support
from core.plugin_watcher import _ManifestRejection
from tests.helpers.plugin_manager_test_support import (
    AsyncMock,
    LoadedPlugin,
    Mock,
    ModuleType,
    Path,
    PluginDefinition,
    PluginExecutionClosed,
    PluginExecutionGate,
    PluginLifecycleFatalError,
    _AsyncConcurrencyProbe,
    _build_definition,
    _build_manager,
    _FatalLifecycleError,
    asyncio,
    os,
    pytest,
    textwrap,
)

_isolate_process_global_plugin_import_state = (
    _fixture_support._isolate_process_global_plugin_import_state
)


@pytest.mark.asyncio
async def test_reload_plugin_keeps_old_instance_when_fingerprint_fails(tmp_path: Path):
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    old_gate = PluginExecutionGate("parallel", plugin_name="demo")
    old_plugin = LoadedPlugin(
        definition=definition,
        module=ModuleType("demo.main"),
        mtime=0.0,
        execution_gate=old_gate,
    )
    manager._plugins["demo"] = old_plugin
    manager._execution_gates["demo"] = old_gate
    manager._load_definition = Mock(return_value=definition)
    manager._authorize_plugin_snapshot = Mock(side_effect=OSError("stat failed"))
    manager._shutdown_plugin_instance = AsyncMock()

    await manager.reload_plugin("demo")

    assert manager._plugins["demo"] is old_plugin
    assert old_gate.closed is False
    assert await old_gate.run(AsyncMock(return_value="still live")) == "still live"
    manager._shutdown_plugin_instance.assert_not_awaited()


@pytest.mark.asyncio
async def test_reload_retires_old_policy_when_replacement_fingerprint_fails(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    old_definition = _build_definition()
    changed_definition = _build_definition()
    changed_definition.commands = [
        {
            "name": "demo",
            "triggers": ["demo"],
            "help": "demo",
            "admin_only": True,
        }
    ]
    module = ModuleType("plugins.demo.main")
    module.shutdown = AsyncMock()
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    plugin = LoadedPlugin(
        definition=old_definition,
        module=module,
        mtime=0.0,
        execution_gate=gate,
    )
    manager._plugins["demo"] = plugin
    manager._execution_gates["demo"] = gate
    manager._load_definition = Mock(return_value=changed_definition)
    manager._authorize_plugin_snapshot = Mock(side_effect=OSError("stat denied"))

    await manager.reload_plugin("demo")

    assert gate.closed is True
    module.shutdown.assert_awaited_once()
    assert "demo" not in manager.list_runtime_plugins()
    assert manager.router.resolve("demo") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest_state", ["invalid", "disabled"])
async def test_direct_reload_retires_invalid_or_disabled_manifest_fail_closed(
    tmp_path: Path,
    manifest_state: str,
) -> None:
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    definition.commands = [
        {"name": "demo", "triggers": ["demo"], "help": "demo"},
    ]
    module = ModuleType("plugins.demo.main")
    shutdown_calls = 0

    async def handle(command, args, event, context):
        return []

    async def shutdown(context=None) -> None:
        nonlocal shutdown_calls
        shutdown_calls += 1

    module.handle = handle
    module.shutdown = shutdown
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    plugin = LoadedPlugin(
        definition=definition,
        module=module,
        mtime=0.0,
        execution_gate=gate,
    )
    manager._plugins["demo"] = plugin
    manager._execution_gates["demo"] = gate
    manager.router.replace_plugin(
        definition.name,
        manager._build_command_specs(definition, module, gate),
    )
    if manifest_state == "invalid":
        manager._load_definition = Mock(return_value=None)
    else:
        disabled = _build_definition()
        disabled.enabled = False
        manager._load_definition = Mock(return_value=disabled)

    await manager.reload_plugin("demo")

    assert shutdown_calls == 1
    assert gate.closed is True
    assert manager.router.resolve("demo") is None
    assert "demo" not in manager._plugins
    assert "demo" not in manager._execution_gates


@pytest.mark.asyncio
async def test_reconcile_keeps_live_generation_when_required_dependency_is_transiently_missing(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text("{}", encoding="utf-8")
    definition = _build_definition()
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    plugin = LoadedPlugin(
        definition=definition,
        module=ModuleType("plugins.demo.main"),
        mtime=0.0,
        execution_gate=gate,
    )
    manager._plugins["demo"] = plugin
    manager._execution_gates["demo"] = gate
    manager._load_definition = Mock(return_value=_ManifestRejection("dependency"))
    manager._unload_plugin_once = AsyncMock()

    await manager._reconcile_plugin_path(plugin_dir)

    manager._unload_plugin_once.assert_not_awaited()
    assert manager._plugins["demo"] is plugin


@pytest.mark.asyncio
async def test_reload_plugin_quarantines_old_instance_when_shutdown_fails(tmp_path: Path):
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    old_gate = PluginExecutionGate("parallel", plugin_name="demo")
    old_plugin = LoadedPlugin(
        definition=definition,
        module=ModuleType("demo.main"),
        mtime=0.0,
        execution_gate=old_gate,
    )
    manager._plugins["demo"] = old_plugin
    manager._execution_gates["demo"] = old_gate
    old_state = {"resource": object()}
    manager._plugin_states["demo"] = old_state
    manager._load_definition = Mock(return_value=definition)
    manager._shutdown_plugin_instance = AsyncMock(return_value=False)
    manager._authorize_plugin_snapshot = Mock(return_value=1.0)
    manager._purge_plugin_modules = Mock()
    manager.router.clear_plugin = Mock()

    await manager.reload_plugin("demo")

    assert old_gate.closed is True
    assert manager._plugins["demo"] is old_plugin
    assert manager._execution_gates["demo"] is old_gate
    assert manager._plugin_states["demo"] is old_state
    assert "demo" in manager._quarantined_plugins
    manager._shutdown_plugin_instance.assert_awaited_once_with("demo", old_plugin)
    manager._purge_plugin_modules.assert_not_called()
    manager.router.clear_plugin.assert_called_once_with("demo")


@pytest.mark.asyncio
async def test_reload_plugin_closes_old_gate_before_shutdown(tmp_path: Path):
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    shutdown_started = asyncio.Event()
    release_shutdown = asyncio.Event()
    old_module = ModuleType("demo.main")

    async def shutdown():
        shutdown_started.set()
        await release_shutdown.wait()

    old_module.shutdown = shutdown
    old_gate = PluginExecutionGate("parallel", plugin_name="demo")
    old_plugin = LoadedPlugin(
        definition=definition,
        module=old_module,
        mtime=0.0,
        execution_gate=old_gate,
    )
    manager._plugins["demo"] = old_plugin
    manager._execution_gates["demo"] = old_gate
    manager._load_definition = Mock(return_value=definition)
    manager._authorize_plugin_snapshot = Mock(return_value=1.0)
    manager._definition_is_current = Mock(return_value=True)

    canonical_module = ModuleType("plugins.demo.main")

    async def load_canonical(_plugin_dir, transaction):
        return LoadedPlugin(
            definition=definition,
            module=canonical_module,
            mtime=transaction.mtime,
            execution_gate=transaction.gate,
        )

    manager._load_canonical_candidate = AsyncMock(side_effect=load_canonical)

    reload_task = asyncio.create_task(manager.reload_plugin("demo"))
    await asyncio.wait_for(shutdown_started.wait(), timeout=1)

    assert old_gate.closed is True
    operation = AsyncMock()
    with pytest.raises(PluginExecutionClosed, match="unloading"):
        await old_gate.run(operation)
    operation.assert_not_awaited()

    release_shutdown.set()
    await asyncio.wait_for(reload_task, timeout=1)
    assert manager._plugins["demo"].module is canonical_module


@pytest.mark.asyncio
async def test_watch_does_not_auto_reload_quarantined_plugin(tmp_path: Path):
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    old_gate = PluginExecutionGate("parallel", plugin_name="demo")
    await old_gate.close()
    manager._plugins["demo"] = LoadedPlugin(
        definition=definition,
        module=ModuleType("plugins.demo.main"),
        mtime=0.0,
        execution_gate=old_gate,
    )
    manager._execution_gates["demo"] = old_gate
    manager._quarantined_plugins.add("demo")
    manager._load_definition = Mock(return_value=definition)
    manager._capture_plugin_snapshot_async = AsyncMock(return_value=1.0)
    manager._load_new_plugin_from_watch = AsyncMock()
    manager.reload_plugin = AsyncMock()
    manager.update_poll_interval(0.01)
    reconciled = asyncio.Event()
    original_clear = manager.router.clear_plugin

    def clear_plugin(name: str) -> None:
        original_clear(name)
        reconciled.set()

    manager.router.clear_plugin = Mock(side_effect=clear_plugin)
    watcher = asyncio.create_task(manager.watch())
    await asyncio.wait_for(reconciled.wait(), timeout=1)
    watcher.cancel()

    with pytest.raises(asyncio.CancelledError):
        await watcher

    manager._load_new_plugin_from_watch.assert_not_awaited()
    manager.reload_plugin.assert_not_awaited()
    assert manager._watch_waiters == {}


@pytest.mark.asyncio
async def test_watch_retries_after_ordinary_reconcile_failure(tmp_path: Path) -> None:
    manager = _build_manager(tmp_path)
    manager.update_poll_interval(0.01)
    second_poll = asyncio.Event()
    calls = 0

    async def reconcile() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first poll failed after quarantine")
        second_poll.set()

    manager.reconcile_plugins = AsyncMock(side_effect=reconcile)
    watcher = asyncio.create_task(manager.watch())
    try:
        await asyncio.wait_for(second_poll.wait(), timeout=1)
        assert watcher.done() is False
        assert manager.reconcile_plugins.await_count >= 2
    finally:
        watcher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watcher


def test_plugin_watch_poll_interval_rejects_non_finite_and_non_positive_values(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    manager.update_poll_interval(0.25)

    for invalid in (0, -1, float("nan"), float("inf"), float("-inf"), True):
        manager.update_poll_interval(invalid)
        assert manager._poll_interval == 0.25

    manager.update_poll_interval(0.001)
    assert manager._poll_interval == 0.01


@pytest.mark.asyncio
async def test_watch_interval_decrease_interrupts_old_long_wait(tmp_path: Path) -> None:
    manager = _build_manager(tmp_path)
    manager.update_poll_interval(60)
    reconciled = asyncio.Event()

    async def reconcile() -> None:
        reconciled.set()

    manager.reconcile_plugins = AsyncMock(side_effect=reconcile)
    watcher = asyncio.create_task(manager.watch())
    for _ in range(20):
        if manager._watch_waiters:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("watcher did not register its managed interval waiter")

    manager.update_poll_interval(0.01)
    await asyncio.wait_for(reconciled.wait(), timeout=1)

    watcher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watcher
    assert manager._watch_waiters == {}


@pytest.mark.asyncio
async def test_watch_interval_increase_does_not_trigger_immediate_reconcile(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    manager.update_poll_interval(0.01)
    first_reconcile = asyncio.Event()

    async def reconcile() -> None:
        first_reconcile.set()

    manager.reconcile_plugins = AsyncMock(side_effect=reconcile)
    watcher = asyncio.create_task(manager.watch())
    await asyncio.wait_for(first_reconcile.wait(), timeout=1)
    manager.reconcile_plugins.reset_mock()

    manager.update_poll_interval(0.2)
    await asyncio.sleep(0.05)

    manager.reconcile_plugins.assert_not_awaited()
    watcher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watcher
    assert manager._watch_waiters == {}


@pytest.mark.asyncio
async def test_watch_rate_limits_repeated_round_failures_and_recovers(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = _build_manager(tmp_path)
    manager.update_poll_interval(0.01)
    recovered = asyncio.Event()
    calls = 0

    async def reconcile() -> None:
        nonlocal calls
        calls += 1
        if calls <= 5:
            raise OSError("transient root scan failure")
        recovered.set()

    manager.reconcile_plugins = AsyncMock(side_effect=reconcile)
    caplog.set_level("WARNING", logger="core.plugin_manager")
    watcher = asyncio.create_task(manager.watch())
    try:
        await asyncio.wait_for(recovered.wait(), timeout=1)
        assert watcher.done() is False
        failures = [
            record
            for record in caplog.records
            if "reconciliation failed; retrying next interval" in record.getMessage()
        ]
        assert len(failures) == 1
    finally:
        watcher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watcher


@pytest.mark.asyncio
async def test_watch_propagates_fatal_lifecycle_carrier(tmp_path: Path) -> None:
    manager = _build_manager(tmp_path)
    manager.update_poll_interval(0.01)
    fatal = PluginLifecycleFatalError("demo", _FatalLifecycleError("fatal watch"))
    manager.reconcile_plugins = AsyncMock(side_effect=fatal)

    with pytest.raises(PluginLifecycleFatalError) as raised:
        await asyncio.wait_for(manager.watch(), timeout=1)

    assert raised.value is fatal


@pytest.mark.asyncio
async def test_reconcile_path_stat_failure_preserves_runtime_and_processes_sibling(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    inaccessible = manager.plugins_dir / "a_inaccessible"
    healthy = manager.plugins_dir / "b_healthy"
    inaccessible.mkdir()
    healthy.mkdir()
    (healthy / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    old_definition = _build_definition("a_inaccessible")
    old_plugin = LoadedPlugin(
        definition=old_definition,
        module=ModuleType("plugins.a_inaccessible.main"),
        mtime=0,
    )
    manager._plugins[old_definition.name] = old_plugin
    healthy_definition = _build_definition("b_healthy")
    original_is_plugin_dir = manager._is_plugin_dir

    def classify(path: Path) -> bool:
        if path == inaccessible:
            raise PermissionError("stat denied")
        return original_is_plugin_dir(path)

    manager._is_plugin_dir = Mock(side_effect=classify)
    manager._load_definition = Mock(return_value=healthy_definition)
    manager._capture_plugin_snapshot_async = AsyncMock(return_value=1)
    manager._load_new_plugin_from_watch = AsyncMock()

    await manager.reconcile_plugins()

    assert manager._plugins[old_definition.name] is old_plugin
    manager._load_new_plugin_from_watch.assert_awaited_once_with(
        healthy,
        healthy_definition,
        1,
    )


@pytest.mark.asyncio
async def test_reconcile_incomplete_root_iteration_preserves_unknown_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _build_manager(tmp_path)
    unseen = manager.plugins_dir / "a_unseen"
    healthy = manager.plugins_dir / "b_healthy"
    unseen.mkdir()
    healthy.mkdir()
    (healthy / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    old_definition = _build_definition("a_unseen")
    old_plugin = LoadedPlugin(
        definition=old_definition,
        module=ModuleType("plugins.a_unseen.main"),
        mtime=0,
    )
    manager._plugins[old_definition.name] = old_plugin
    healthy_definition = _build_definition("b_healthy")
    original_iterdir = Path.iterdir

    def flaky_iterdir(path: Path):
        if path != manager.plugins_dir:
            return original_iterdir(path)

        def incomplete():
            yield healthy
            raise OSError("plugin root changed during iteration")

        return incomplete()

    monkeypatch.setattr(Path, "iterdir", flaky_iterdir)
    manager._load_definition = Mock(return_value=healthy_definition)
    manager._capture_plugin_snapshot_async = AsyncMock(return_value=1)
    manager._load_new_plugin_from_watch = AsyncMock()

    await manager.reconcile_plugins()

    assert manager._plugins[old_definition.name] is old_plugin
    manager._load_new_plugin_from_watch.assert_awaited_once_with(
        healthy,
        healthy_definition,
        1,
    )


@pytest.mark.asyncio
async def test_reconcile_deleted_plugin_failure_does_not_block_siblings_and_retries(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    for name in ("a_broken", "b_deleted"):
        manager._plugins[name] = LoadedPlugin(
            definition=_build_definition(name),
            module=ModuleType(f"plugins.{name}.main"),
            mtime=0,
        )
    healthy = manager.plugins_dir / "c_healthy"
    healthy.mkdir()
    (healthy / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    healthy_definition = _build_definition("c_healthy")
    broken_attempts = 0

    async def unload(name: str) -> None:
        nonlocal broken_attempts
        if name == "a_broken":
            broken_attempts += 1
            if broken_attempts == 1:
                raise RuntimeError("shutdown failed")
        manager._plugins.pop(name, None)

    manager._unload_plugin_once = AsyncMock(side_effect=unload)
    manager._load_definition = Mock(return_value=healthy_definition)
    manager._capture_plugin_snapshot_async = AsyncMock(return_value=1)
    manager._load_new_plugin_from_watch = AsyncMock()

    await manager.reconcile_plugins()

    assert "a_broken" in manager._plugins
    assert "b_deleted" not in manager._plugins
    manager._load_new_plugin_from_watch.assert_awaited_once_with(
        healthy,
        healthy_definition,
        1,
    )

    await manager.reconcile_plugins()

    assert "a_broken" not in manager._plugins
    assert manager._unload_plugin_once.await_args_list[:3] == [
        (("a_broken",), {}),
        (("b_deleted",), {}),
        (("a_broken",), {}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest_error", [FileNotFoundError, PermissionError])
async def test_reconcile_manifest_read_failure_does_not_block_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest_error: type[OSError],
) -> None:
    manager = _build_manager(tmp_path)
    inaccessible = manager.plugins_dir / "a_inaccessible"
    healthy = manager.plugins_dir / "b_healthy"
    for plugin_dir in (inaccessible, healthy):
        plugin_dir.mkdir()
        (plugin_dir / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
        (plugin_dir / "plugin.json").write_text(
            f'{{"name":"{plugin_dir.name}","version":"1.0.0","entry":"main.py",'
            '"commands":[],"schedule":[],"concurrency":"parallel","enabled":true}',
            encoding="utf-8",
        )
    original_open = Path.open

    def flaky_open(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == inaccessible / "plugin.json" and mode == "rb":
            raise manifest_error("manifest read failed")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", flaky_open)
    manager._load_new_plugin_from_watch = AsyncMock()

    await manager.reconcile_plugins()

    manager._load_new_plugin_from_watch.assert_awaited_once()
    load_args = manager._load_new_plugin_from_watch.await_args.args
    assert load_args[0] == healthy
    assert load_args[1].name == healthy.name


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest_state", ["missing", "invalid"])
async def test_watcher_manifest_errors_are_rate_limited_but_manual_loads_are_not(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    manifest_state: str,
) -> None:
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "broken"
    plugin_dir.mkdir()
    if manifest_state == "invalid":
        (plugin_dir / "plugin.json").write_text("{invalid", encoding="utf-8")
    expected = "Missing plugin.json" if manifest_state == "missing" else "Invalid plugin.json"
    caplog.set_level("WARNING", logger="core.plugin_manager")

    for _ in range(5):
        await manager.reconcile_plugins()

    watcher_messages = [record for record in caplog.records if expected in record.getMessage()]
    assert len(watcher_messages) == 1

    caplog.clear()
    manager._load_definition(plugin_dir)
    manager._load_definition(plugin_dir)
    manual_messages = [record for record in caplog.records if expected in record.getMessage()]
    assert len(manual_messages) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["rglob", "open"])
async def test_reconcile_fingerprint_race_isolated_to_one_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    manager = _build_manager(tmp_path)
    definitions: dict[str, PluginDefinition] = {}
    for name in ("a_racy", "b_changed"):
        plugin_dir = manager.plugins_dir / name
        plugin_dir.mkdir()
        (plugin_dir / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
        (plugin_dir / "plugin.json").write_text("{}", encoding="utf-8")
        definition = _build_definition(name)
        definitions[name] = definition
        manager._plugins[name] = LoadedPlugin(
            definition=definition,
            module=ModuleType(f"plugins.{name}.main"),
            mtime=0,
        )

    manager._load_definition = Mock(side_effect=lambda path: definitions[path.name])
    manager._reload_plugin_once = AsyncMock()
    racy_dir = manager.plugins_dir / "a_racy"
    if failure_stage == "rglob":
        original_iter = manager._iter_watch_files

        def flaky_iter(plugin_dir: Path, definition: PluginDefinition) -> list[Path]:
            if plugin_dir == racy_dir:
                raise OSError("directory replaced during rglob")
            return original_iter(plugin_dir, definition)

        manager._iter_watch_files = Mock(side_effect=flaky_iter)
    else:
        original_open = Path.open
        failed_path = racy_dir / "main.py"

        def flaky_open(path: Path, *args, **kwargs):
            if path == failed_path:
                raise FileNotFoundError("file renamed before open")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", flaky_open)

    await manager.reconcile_plugins()

    assert manager._plugins["a_racy"].mtime == 0
    manager._reload_plugin_once.assert_awaited_once()
    reload_call = manager._reload_plugin_once.await_args
    assert reload_call.args == ("b_changed",)
    assert reload_call.kwargs["authorization"].mtime != 0


@pytest.mark.asyncio
async def test_reconcile_transient_fingerprint_failure_recovers_next_round(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    definition = _build_definition()
    old_plugin = LoadedPlugin(
        definition=definition,
        module=ModuleType("plugins.demo.main"),
        mtime=1,
    )
    manager._plugins[definition.name] = old_plugin
    manager._load_definition = Mock(return_value=definition)
    manager._capture_plugin_snapshot_async = AsyncMock(
        side_effect=[OSError("file replaced during read"), 2]
    )
    manager._reload_plugin_once = AsyncMock()

    await manager.reconcile_plugins()
    assert manager._plugins[definition.name] is old_plugin
    manager._reload_plugin_once.assert_not_awaited()

    await manager.reconcile_plugins()
    manager._reload_plugin_once.assert_awaited_once()
    reload_call = manager._reload_plugin_once.await_args
    assert reload_call.args == (definition.name,)
    assert reload_call.kwargs["authorization"].mtime == 2


@pytest.mark.asyncio
async def test_reload_authorization_snapshot_runs_off_event_loop(tmp_path: Path) -> None:
    import threading
    import time

    manager = _build_manager(tmp_path)
    definition = _build_definition()
    old_plugin = LoadedPlugin(
        definition=definition,
        module=ModuleType("plugins.demo.main"),
        mtime=0,
    )
    manager._load_definition = Mock(return_value=definition)
    started = threading.Event()
    release = threading.Event()
    blocked_at: list[float] = []

    def blocking_fingerprint(_plugin_dir: Path, _definition: PluginDefinition) -> int:
        blocked_at.append(time.perf_counter())
        started.set()
        release.wait(timeout=1)
        return 1

    manager._authorize_plugin_snapshot = Mock(side_effect=blocking_fingerprint)
    safety_release = threading.Timer(0.5, release.set)
    safety_release.start()
    task = asyncio.create_task(manager._prepare_reload_authorization("demo", old_plugin))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        assert time.perf_counter() - blocked_at[0] < 0.3
        release.set()
        authorization = await asyncio.wait_for(task, timeout=1)
    finally:
        safety_release.cancel()
        release.set()

    assert authorization is not None
    assert authorization.mtime == 1


def test_capture_plugin_snapshot_rejects_atomic_replace_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    definition = _build_definition()
    main = plugin_dir / "main.py"
    main.write_text("VALUE = 1\n", encoding="utf-8")
    (plugin_dir / "plugin.json").write_text("{}", encoding="utf-8")
    original_open = Path.open
    original_stat = Path.stat
    opened = False

    def replacing_open(path: Path, *args, **kwargs):
        nonlocal opened
        handle = original_open(path, *args, **kwargs)
        if path == main:
            opened = True
        return handle

    def replacement_stat(path: Path, *args, **kwargs):
        metadata = original_stat(path, *args, **kwargs)
        if path != main or not opened:
            return metadata
        # Windows does not permit replacing this test handle while it is open.
        # Return the metadata identity that the path would expose after an
        # editor/deployer atomically swapped in a same-size replacement.
        return type(
            "ReplacementMetadata",
            (),
            {
                "st_dev": metadata.st_dev,
                "st_ino": metadata.st_ino + 1,
                "st_size": metadata.st_size,
                "st_mtime_ns": metadata.st_mtime_ns,
            },
        )()

    monkeypatch.setattr(Path, "open", replacing_open)
    monkeypatch.setattr(Path, "stat", replacement_stat)

    with pytest.raises(OSError, match="changed while fingerprinting"):
        manager._capture_plugin_snapshot(plugin_dir, definition)

    assert opened is True


@pytest.mark.asyncio
async def test_watch_snapshot_reuses_recent_stable_metadata_then_hashes_change(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    definition = _build_definition()
    source = plugin_dir / "main.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (plugin_dir / "plugin.json").write_text("{}", encoding="utf-8")
    first = manager._capture_plugin_snapshot(plugin_dir, definition)
    full_capture = Mock(wraps=manager._capture_plugin_snapshot)
    manager._capture_plugin_snapshot = full_capture

    stable = await manager._capture_plugin_snapshot_async(
        plugin_dir,
        definition,
        previous=first,
    )

    assert stable is first
    full_capture.assert_not_called()

    source.write_text("VALUE = 200\n", encoding="utf-8")
    changed = await manager._capture_plugin_snapshot_async(
        plugin_dir,
        definition,
        previous=first,
    )

    full_capture.assert_called_once_with(plugin_dir, definition)
    assert changed != first


@pytest.mark.asyncio
async def test_reconcile_rejects_cross_file_mixed_fingerprint_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _build_manager(tmp_path)
    monkeypatch.setattr(
        "core.plugin_watcher._PLUGIN_FINGERPRINT_AUDIT_INTERVAL_SECONDS",
        0.0,
    )
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    definition = _build_definition()
    definition.entry = "a.py"
    first = plugin_dir / "a.py"
    second = plugin_dir / "b.py"
    replacement = plugin_dir / "replacement.tmp"
    manifest = plugin_dir / "plugin.json"
    first.write_text("VALUE = 1\n", encoding="utf-8")
    second.write_text("VALUE = 1\n", encoding="utf-8")
    replacement.write_text("VALUE = 2\n", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    old_plugin = LoadedPlugin(
        definition=definition,
        module=ModuleType("plugins.demo.main"),
        mtime=manager._capture_plugin_snapshot(plugin_dir, definition),
    )
    manager._plugins[definition.name] = old_plugin
    manager._load_definition = Mock(return_value=definition)
    manager._reload_plugin_once = AsyncMock()
    original_open = Path.open
    replaced = False

    def replace_first_before_reading_second(path: Path, *args, **kwargs):
        nonlocal replaced
        handle = original_open(path, *args, **kwargs)
        if path == second and not replaced:
            replaced = True
            os.replace(replacement, first)
        return handle

    monkeypatch.setattr(Path, "open", replace_first_before_reading_second)

    await manager.reconcile_plugins()

    assert replaced is True
    assert manager._plugins[definition.name] is old_plugin
    manager._reload_plugin_once.assert_not_awaited()


def test_iter_watch_files_traverses_data_named_source_directory(tmp_path: Path) -> None:
    data_ancestor = tmp_path / "data"
    data_ancestor.mkdir()
    manager = _build_manager(data_ancestor)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    definition = _build_definition()
    main = plugin_dir / "main.py"
    helper = plugin_dir / "helper.py"
    runtime_data = plugin_dir / "data"
    runtime_data.mkdir()
    state = runtime_data / "state.json"
    nested_source = runtime_data / "helper.py"
    main.write_text("VALUE = 1\n", encoding="utf-8")
    helper.write_text("HELPER = 1\n", encoding="utf-8")
    state.write_text("{}", encoding="utf-8")
    nested_source.write_text("NESTED = 1\n", encoding="utf-8")
    (plugin_dir / "plugin.json").write_text(
        '{"name":"demo","entry":"main.py","enabled":true}',
        encoding="utf-8",
    )

    files = manager._iter_watch_files(plugin_dir, definition)

    assert main in files
    assert helper in files
    assert nested_source in files
    assert state not in files


def test_fingerprint_prunes_bytecode_before_descent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    definition = _build_definition()
    main = plugin_dir / "main.py"
    manifest = plugin_dir / "plugin.json"
    cache_dir = plugin_dir / "__pycache__"
    cache_dir.mkdir()
    main.write_text("VALUE = 1\n", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    (cache_dir / "generated.py").write_text("VALUE = 2\n", encoding="utf-8")
    original_scandir = os.scandir
    original_stat = Path.stat
    scanned: list[Path] = []

    def tracked_scandir(path):
        scanned.append(Path(path))
        return original_scandir(path)

    def guarded_stat(path: Path, *args, **kwargs):
        if cache_dir == path or cache_dir in path.parents:
            raise AssertionError("bytecode cache must be pruned before stat")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", tracked_scandir)
    monkeypatch.setattr(Path, "stat", guarded_stat)

    fingerprint = manager._capture_plugin_snapshot(plugin_dir, definition)

    assert isinstance(fingerprint, int)
    assert cache_dir not in scanned


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manifest_failure",
    ["disabled", "deleted", "invalid", "invalid-with-backup", "missing-entry"],
)
async def test_reconcile_unloads_fail_closed_manifest_and_recovers_once(
    tmp_path: Path,
    manifest_failure: str,
) -> None:
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    manifest_path = plugin_dir / "plugin.json"
    enabled_manifest = textwrap.dedent(
        """
        {
          "name": "demo",
          "version": "1.0.0",
          "entry": "main.py",
          "commands": [{"name": "demo", "triggers": ["demo"], "help": "demo"}],
          "schedule": [],
          "concurrency": "parallel",
          "enabled": true
        }
        """
    ).strip()
    manifest_path.write_text(enabled_manifest, encoding="utf-8")
    (plugin_dir / "main.py").write_text(
        textwrap.dedent(
            """
            SHUTDOWN_CALLS = 0

            async def shutdown(context=None):
                global SHUTDOWN_CALLS
                SHUTDOWN_CALLS += 1

            async def handle(command, args, event, context):
                return [{"type": "text", "data": {"text": "ok"}}]
            """
        ).strip(),
        encoding="utf-8",
    )

    manager.load_plugin(plugin_dir)
    original = manager._plugins["demo"]
    original_gate = original.execution_gate
    original_unload = manager._unload_plugin_once
    manager._unload_plugin_once = AsyncMock(wraps=original_unload)
    assert manager.router.resolve("demo") is not None

    if manifest_failure == "disabled":
        manifest_path.write_text(
            enabled_manifest.replace('"enabled": true', '"enabled": false'),
            encoding="utf-8",
        )
    elif manifest_failure == "deleted":
        manifest_path.unlink()
    elif manifest_failure == "invalid":
        manifest_path.write_text("{invalid", encoding="utf-8")
    elif manifest_failure == "invalid-with-backup":
        manifest_path.with_name("plugin.json.bak").write_text(
            enabled_manifest,
            encoding="utf-8",
        )
        manifest_path.write_text("{invalid", encoding="utf-8")
    else:
        manifest_path.write_text(
            enabled_manifest.replace('"entry": "main.py"', '"entry": "missing.py"'),
            encoding="utf-8",
        )

    await manager.reconcile_plugins()

    manager._unload_plugin_once.assert_awaited_once_with("demo")
    assert "demo" not in manager._plugins
    assert "demo" not in manager._execution_gates
    assert manager.router.resolve("demo") is None
    assert original_gate is not None and original_gate.closed is True
    assert original.module.SHUTDOWN_CALLS == 1

    manifest_path.write_text(enabled_manifest, encoding="utf-8")
    await manager.reconcile_plugins()

    replacement = manager._plugins["demo"]
    resolved = manager.router.resolve("demo")
    assert replacement is not original
    assert replacement.execution_gate is not original_gate
    assert resolved is not None
    assert resolved[0].execution_gate is replacement.execution_gate
    assert len([spec for spec in manager.router._commands if spec.plugin == "demo"]) == 1

    await manager.reconcile_plugins()
    assert manager._plugins["demo"] is replacement
    assert len([spec for spec in manager.router._commands if spec.plugin == "demo"]) == 1

    await manager.unload_plugin("demo")


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest_failure", ["disabled", "invalid"])
async def test_reconcile_never_auto_retires_or_replaces_quarantined_plugin(
    tmp_path: Path,
    manifest_failure: str,
) -> None:
    manager = _build_manager(tmp_path)
    # The two parametrized cases intentionally create restart/quarantine
    # state under the same canonical import name.  Start each isolated case
    # without inheriting the previous case's process-global module handles.
    manager._purge_plugin_modules("demo")
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    manifest_path = plugin_dir / "plugin.json"
    enabled_manifest = (
        '{"name":"demo","version":"1.0.0","entry":"main.py",'
        '"commands":[{"name":"demo","triggers":["demo"],"help":"demo"}],'
        '"schedule":[],"concurrency":"parallel","enabled":true}'
    )
    manifest_path.write_text(enabled_manifest, encoding="utf-8")
    (plugin_dir / "main.py").write_text(
        "async def handle(command, args, event, context):\n    return []\n",
        encoding="utf-8",
    )
    manager.load_plugin(plugin_dir)
    quarantined = manager._plugins["demo"]
    gate = quarantined.execution_gate
    assert gate is not None
    quarantined.module.shutdown = AsyncMock(side_effect=RuntimeError("resource still live"))

    if manifest_failure == "disabled":
        manifest_path.write_text(
            enabled_manifest.replace('"enabled":true', '"enabled":false'),
            encoding="utf-8",
        )
    else:
        manifest_path.write_text("{invalid", encoding="utf-8")
    await manager.reconcile_plugins()
    manifest_path.write_text(enabled_manifest, encoding="utf-8")
    await manager.reconcile_plugins()

    assert manager._plugins["demo"] is quarantined
    assert manager._execution_gates["demo"] is gate
    assert "demo" in manager._quarantined_plugins
    assert gate.closed is True
    assert manager.router.resolve("demo") is None
    quarantined.module.shutdown.assert_awaited_once()

    quarantined.module.shutdown = AsyncMock()
    manager._quarantined_plugins.discard("demo")
    await manager.unload_plugin("demo")
    manager._purge_plugin_modules("demo")
    manager._release_plugin_namespace("demo")


@pytest.mark.asyncio
async def test_reconcile_cancels_pending_init_when_plugin_directory_disappears(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    definition = _build_definition()
    module = ModuleType("plugins.demo.main")
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    init_task = asyncio.create_task(asyncio.sleep(3600))
    manager._init_tasks.append(init_task)
    manager._init_task_plugins[init_task] = "demo"
    manager._pending_plugins[init_task] = (definition, module, 0.0)
    manager._execution_gates["demo"] = gate
    manager._plugin_states["demo"] = {"pending": True}
    plugin_dir.rmdir()

    await manager.reconcile_plugins()

    assert init_task.cancelled()
    assert not manager._init_tasks
    assert not manager._init_task_plugins
    assert not manager._pending_plugins
    assert "demo" not in manager._execution_gates
    assert "demo" not in manager._plugin_states
    assert "demo" not in manager._plugins


@pytest.mark.asyncio
async def test_reconcile_calls_are_serialized(tmp_path: Path) -> None:
    manager = _build_manager(tmp_path)
    probe = _AsyncConcurrencyProbe()
    manager._reconcile_plugins_once = AsyncMock(side_effect=probe.run)

    await asyncio.gather(manager.reconcile_plugins(), manager.reconcile_plugins())

    assert probe.calls == 2
    assert probe.maximum_active == 1


@pytest.mark.asyncio
async def test_reload_and_reconcile_share_one_lifecycle_lock(tmp_path: Path) -> None:
    manager = _build_manager(tmp_path)
    probe = _AsyncConcurrencyProbe()
    manager._reload_plugin_once = AsyncMock(side_effect=probe.run)
    manager._reconcile_plugins_once = AsyncMock(side_effect=probe.run)

    await asyncio.gather(manager.reload_plugin("demo"), manager.reconcile_plugins())

    assert probe.maximum_active == 1
    manager._reload_plugin_once.assert_awaited_once_with("demo")
    manager._reconcile_plugins_once.assert_awaited_once()


@pytest.mark.asyncio
async def test_reload_all_stops_before_later_plugins_or_reconcile_on_quarantine(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    for name in ("blocked", "later"):
        definition = _build_definition(name)
        manager._plugins[name] = LoadedPlugin(
            definition=definition,
            module=ModuleType(f"plugins.{name}.main"),
            mtime=0.0,
        )

    async def reload_once(name: str) -> None:
        if name == "blocked":
            manager._quarantined_plugins.add(name)

    manager._reload_plugin_once = AsyncMock(side_effect=reload_once)
    manager._reconcile_plugins_once = AsyncMock()
    before_reload = AsyncMock()

    completed = await manager.reload_all_plugins(before_reload=before_reload)

    assert completed is False
    before_reload.assert_awaited_once_with("blocked")
    manager._reload_plugin_once.assert_awaited_once_with("blocked")
    manager._reconcile_plugins_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_reload_all_retires_invalid_plugin_before_later_quarantine(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    definitions = {name: _build_definition(name) for name in ("a_invalid", "b_blocked")}
    for name, definition in definitions.items():
        gate = PluginExecutionGate("parallel", plugin_name=name)
        manager._plugins[name] = LoadedPlugin(
            definition=definition,
            module=ModuleType(f"plugins.{name}.main"),
            mtime=0.0,
            execution_gate=gate,
        )
        manager._execution_gates[name] = gate

    manager._load_definition = Mock(
        side_effect=lambda path: None if path.name == "a_invalid" else definitions["b_blocked"]
    )
    manager._authorize_plugin_snapshot = Mock(return_value=1.0)

    async def shutdown(name, _plugin) -> bool:
        return name == "a_invalid"

    manager._shutdown_plugin_instance = AsyncMock(side_effect=shutdown)
    manager._reconcile_plugins_once = AsyncMock()

    completed = await manager.reload_all_plugins()

    assert completed is False
    assert "a_invalid" not in manager._plugins
    assert "a_invalid" not in manager._execution_gates
    assert manager._plugins["b_blocked"].module.__name__ == "plugins.b_blocked.main"
    assert manager._execution_gates["b_blocked"].closed is True
    assert "b_blocked" in manager._quarantined_plugins
    manager._reconcile_plugins_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_reload_refuses_quarantined_generation(tmp_path: Path) -> None:
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    plugin = LoadedPlugin(
        definition=definition,
        module=ModuleType("plugins.demo.main"),
        mtime=0.0,
    )
    manager._plugins["demo"] = plugin
    manager._quarantined_plugins.add("demo")
    manager._load_definition = Mock()

    await manager.reload_plugin("demo")

    assert manager._plugins["demo"] is plugin
    manager._load_definition.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_quarantines_old_entry_when_new_entry_cannot_import(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    manifest_path = plugin_dir / "plugin.json"
    manifest_path.write_text(
        '{"name":"demo","version":"1.0.0","entry":"main.py",'
        '"commands":[{"name":"demo","triggers":["demo"],"help":"demo"}],'
        '"schedule":[],"concurrency":"parallel","enabled":true}',
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(
        "async def handle(command, args, event, context):\n    return []\n",
        encoding="utf-8",
    )
    manager.load_plugin(plugin_dir)
    old_plugin = manager._plugins["demo"]
    old_gate = old_plugin.execution_gate
    (plugin_dir / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace("main.py", "broken.py"),
        encoding="utf-8",
    )

    await manager.reconcile_plugins()

    quarantined = manager._plugins["demo"]
    assert quarantined is old_plugin
    assert quarantined.definition.entry == "main.py"
    assert quarantined.execution_gate is manager._execution_gates["demo"]
    assert quarantined.execution_gate is not None
    assert quarantined.execution_gate.closed is True
    assert old_gate is not None and old_gate.closed is True
    assert "demo" in manager._quarantined_plugins
    assert manager.router.resolve("demo") is None

    await manager.unload_plugin("demo")
    assert "demo" not in manager.list_runtime_plugins()
    assert "demo" not in manager._restart_required_plugins


@pytest.mark.asyncio
async def test_reconcile_retires_changed_policy_when_fingerprint_fails(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (plugin_dir / "plugin.json").write_text(
        '{"name":"demo","version":"1.0.0","entry":"main.py",'
        '"commands":[{"name":"demo","triggers":["demo"],"help":"demo",'
        '"admin_only":true}],"schedule":[],"concurrency":"parallel",'
        '"enabled":true}',
        encoding="utf-8",
    )
    old_definition = _build_definition()
    module = ModuleType("plugins.demo.main")
    module.shutdown = AsyncMock()
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    manager._plugins["demo"] = LoadedPlugin(
        definition=old_definition,
        module=module,
        mtime=0.0,
        execution_gate=gate,
    )
    manager._execution_gates["demo"] = gate
    manager._capture_plugin_snapshot_async = AsyncMock(side_effect=OSError("stat denied"))

    await manager.reconcile_plugins()

    assert gate.closed is True
    module.shutdown.assert_awaited_once()
    assert "demo" not in manager.list_runtime_plugins()


@pytest.mark.asyncio
async def test_reconcile_detects_definition_change_even_when_fingerprint_matches(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (plugin_dir / "plugin.json").write_text(
        '{"name":"demo","version":"1.0.0","entry":"main.py",'
        '"commands":[{"name":"demo","triggers":["demo"],"help":"demo",'
        '"admin_only":true}],"schedule":[],"concurrency":"parallel",'
        '"enabled":true}',
        encoding="utf-8",
    )
    old_definition = _build_definition()
    old_mtime = 123
    manager._plugins["demo"] = LoadedPlugin(
        definition=old_definition,
        module=ModuleType("plugins.demo.main"),
        mtime=old_mtime,
    )
    manager._capture_plugin_snapshot_async = AsyncMock(return_value=old_mtime)
    manager._reload_plugin_once = AsyncMock()

    await manager.reconcile_plugins()

    manager._reload_plugin_once.assert_awaited_once()
    reload_call = manager._reload_plugin_once.await_args
    assert reload_call.args == ("demo",)
    assert reload_call.kwargs["authorization"].definition_changed is True


@pytest.mark.asyncio
async def test_reload_cancellation_during_shutdown_quarantines_old(tmp_path: Path):
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    shutdown_started = asyncio.Event()
    release_shutdown = asyncio.Event()
    old_module = ModuleType("plugins.demo.main")

    async def old_shutdown():
        shutdown_started.set()
        await release_shutdown.wait()

    old_module.shutdown = old_shutdown
    old_gate = PluginExecutionGate("parallel", plugin_name="demo")
    old_plugin = LoadedPlugin(
        definition=definition,
        module=old_module,
        mtime=0.0,
        execution_gate=old_gate,
    )
    manager._plugins["demo"] = old_plugin
    manager._execution_gates["demo"] = old_gate
    manager._load_definition = Mock(return_value=definition)
    manager._authorize_plugin_snapshot = Mock(return_value=1.0)
    manager._definition_is_current = Mock(return_value=True)

    reload_task = asyncio.create_task(manager.reload_plugin("demo"))
    await asyncio.wait_for(shutdown_started.wait(), timeout=1)
    reload_task.cancel()
    await asyncio.sleep(0)
    assert reload_task.done() is False
    release_shutdown.set()
    with pytest.raises(asyncio.CancelledError):
        await reload_task

    assert old_gate.closed is True
    assert manager._plugins["demo"] is old_plugin
    assert "demo" in manager._quarantined_plugins
