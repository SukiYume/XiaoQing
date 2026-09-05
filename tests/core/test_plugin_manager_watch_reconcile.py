"""插件文件监听、快照指纹与增量协调。"""

from __future__ import annotations

import tests.helpers.plugin_manager_test_support as _fixture_support
from tests.helpers.plugin_manager_test_support import (
    AsyncMock,
    LoadedPlugin,
    Mock,
    ModuleType,
    Path,
    PluginDefinition,
    PluginExecutionGate,
    PluginLifecycleFatalError,
    _build_definition,
    _build_manager,
    _FatalLifecycleError,
    asyncio,
    os,
    pytest,
)

_isolate_process_global_plugin_import_state = (
    _fixture_support._isolate_process_global_plugin_import_state
)


@pytest.mark.asyncio
async def test_watch_does_not_auto_reload_quarantined_plugin(tmp_path: Path):
    manager    = _build_manager(tmp_path)
    definition = _build_definition()
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    old_gate = PluginExecutionGate("parallel", plugin_name="demo")
    await old_gate.close()
    manager._plugins["demo"] = LoadedPlugin(
        definition     = definition,
        module         = ModuleType("plugins.demo.main"),
        mtime          = 0.0,
        execution_gate = old_gate,
    )
    manager._execution_gates["demo"] = old_gate
    manager._quarantined_plugins.add("demo")
    manager._load_definition = Mock(return_value=definition)
    manager._capture_plugin_snapshot_async = AsyncMock(return_value=1.0)
    manager._load_new_plugin_from_watch = AsyncMock()
    manager.reload_plugin               = AsyncMock()
    manager.update_poll_interval(0.01)
    reconciled     = asyncio.Event()
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
    calls       = 0

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
    calls     = 0

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
    manager      = _build_manager(tmp_path)
    inaccessible = manager.plugins_dir / "a_inaccessible"
    healthy      = manager.plugins_dir / "b_healthy"
    inaccessible.mkdir()
    healthy.mkdir()
    (healthy / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    old_definition = _build_definition("a_inaccessible")
    old_plugin     = LoadedPlugin(
        definition = old_definition,
        module     = ModuleType("plugins.a_inaccessible.main"),
        mtime      = 0,
    )
    manager._plugins[old_definition.name] = old_plugin
    healthy_definition                    = _build_definition("b_healthy")
    original_is_plugin_dir                = manager._is_plugin_dir

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
    unseen  = manager.plugins_dir / "a_unseen"
    healthy = manager.plugins_dir / "b_healthy"
    unseen.mkdir()
    healthy.mkdir()
    (healthy / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    old_definition = _build_definition("a_unseen")
    old_plugin     = LoadedPlugin(
        definition = old_definition,
        module     = ModuleType("plugins.a_unseen.main"),
        mtime      = 0,
    )
    manager._plugins[old_definition.name] = old_plugin
    healthy_definition                    = _build_definition("b_healthy")
    original_iterdir                      = Path.iterdir

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
            definition = _build_definition(name),
            module     = ModuleType(f"plugins.{name}.main"),
            mtime      = 0,
        )
    healthy = manager.plugins_dir / "c_healthy"
    healthy.mkdir()
    (healthy / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    healthy_definition = _build_definition("c_healthy")
    broken_attempts    = 0

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
    manager      = _build_manager(tmp_path)
    inaccessible = manager.plugins_dir / "a_inaccessible"
    healthy      = manager.plugins_dir / "b_healthy"
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
    manager    = _build_manager(tmp_path)
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
    manager                                  = _build_manager(tmp_path)
    definitions: dict[str, PluginDefinition] = {}
    for name in ("a_racy", "b_changed"):
        plugin_dir = manager.plugins_dir / name
        plugin_dir.mkdir()
        (plugin_dir / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
        (plugin_dir / "plugin.json").write_text("{}", encoding="utf-8")
        definition             = _build_definition(name)
        definitions[name]      = definition
        manager._plugins[name] = LoadedPlugin(
            definition = definition,
            module     = ModuleType(f"plugins.{name}.main"),
            mtime      = 0,
        )

    manager._load_definition = Mock(side_effect=lambda path: definitions[path.name])
    manager._reload_plugin_once = AsyncMock()
    racy_dir                    = manager.plugins_dir / "a_racy"
    if failure_stage == "rglob":
        original_iter = manager._iter_watch_files

        def flaky_iter(plugin_dir: Path, definition: PluginDefinition) -> list[Path]:
            if plugin_dir == racy_dir:
                raise OSError("directory replaced during rglob")
            return original_iter(plugin_dir, definition)

        manager._iter_watch_files = Mock(side_effect=flaky_iter)
    else:
        original_open = Path.open
        failed_path   = racy_dir / "main.py"

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
    manager    = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    definition = _build_definition()
    old_plugin = LoadedPlugin(
        definition = definition,
        module     = ModuleType("plugins.demo.main"),
        mtime      = 1,
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

    manager    = _build_manager(tmp_path)
    definition = _build_definition()
    old_plugin = LoadedPlugin(
        definition = definition,
        module     = ModuleType("plugins.demo.main"),
        mtime      = 0,
    )
    manager._load_definition = Mock(return_value=definition)
    started                 = threading.Event()
    release                 = threading.Event()
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
    manager    = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    definition = _build_definition()
    main       = plugin_dir / "main.py"
    main.write_text("VALUE = 1\n", encoding="utf-8")
    (plugin_dir / "plugin.json").write_text("{}", encoding="utf-8")
    original_open = Path.open
    original_stat = Path.stat
    opened        = False

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
    manager    = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    definition = _build_definition()
    source     = plugin_dir / "main.py"
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
    definition       = _build_definition()
    definition.entry = "a.py"
    first            = plugin_dir / "a.py"
    second           = plugin_dir / "b.py"
    replacement      = plugin_dir / "replacement.tmp"
    manifest         = plugin_dir / "plugin.json"
    first.write_text("VALUE = 1\n", encoding="utf-8")
    second.write_text("VALUE = 1\n", encoding="utf-8")
    replacement.write_text("VALUE = 2\n", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    old_plugin = LoadedPlugin(
        definition = definition,
        module     = ModuleType("plugins.demo.main"),
        mtime      = manager._capture_plugin_snapshot(plugin_dir, definition),
    )
    manager._plugins[definition.name] = old_plugin
    manager._load_definition = Mock(return_value=definition)
    manager._reload_plugin_once = AsyncMock()
    original_open               = Path.open
    replaced                    = False

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
    manager    = _build_manager(data_ancestor)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    definition   = _build_definition()
    main         = plugin_dir / "main.py"
    helper       = plugin_dir / "helper.py"
    runtime_data = plugin_dir / "data"
    runtime_data.mkdir()
    state         = runtime_data / "state.json"
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
    manager    = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    definition = _build_definition()
    main       = plugin_dir / "main.py"
    manifest   = plugin_dir / "plugin.json"
    cache_dir  = plugin_dir / "__pycache__"
    cache_dir.mkdir()
    main.write_text("VALUE = 1\n", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    (cache_dir / "generated.py").write_text("VALUE = 2\n", encoding="utf-8")
    original_scandir    = os.scandir
    original_stat       = Path.stat
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
