"""源码快照、导入来源和路径安全。"""

from __future__ import annotations

import errno

import tests.helpers.plugin_manager_test_support as _fixture_support
from core.plugin_watcher import _ManifestRejection
from tests.helpers.plugin_manager_test_support import (
    LoadedPlugin,
    Mock,
    ModuleType,
    Path,
    PluginDefinition,
    PluginExecutionGate,
    PluginLoadError,
    PluginPathError,
    _build_definition,
    _build_manager,
    _runtime_symlink_or_skip,
    _write_runtime_manifest,
    asyncio,
    os,
    pytest,
    time,
)

_isolate_process_global_plugin_import_state = (
    _fixture_support._isolate_process_global_plugin_import_state
)


def test_prepare_module_load_compiles_lazy_helpers_before_any_plugin_code_runs(
    tmp_path: Path,
) -> None:
    manager    = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    _write_runtime_manifest(plugin_dir, "demo")
    (plugin_dir / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (plugin_dir / "lazy_helper.py").write_text("def broken(:\n", encoding="utf-8")
    definition = manager._load_definition(plugin_dir)
    assert definition is not None

    with pytest.raises(PluginLoadError, match="source could not be compiled"):
        manager._prepare_module_load(plugin_dir, definition, None)


def test_capture_plugin_snapshot_tracks_submodule_changes(tmp_path: Path):
    manager    = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    helper = plugin_dir / "helper.py"
    helper.write_text("value = 1\n", encoding="utf-8")
    definition = _build_definition()
    (plugin_dir / "plugin.json").write_text(
        '{"name":"demo","version":"1.0.0","entry":"main.py","commands":[],"schedule":[],"concurrency":"parallel","enabled":true}',
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text("from .helper import value\n", encoding="utf-8")

    before = manager._capture_plugin_snapshot(plugin_dir, definition)
    helper.write_text("value = 2\n", encoding="utf-8")
    os.utime(helper, None)
    after = manager._capture_plugin_snapshot(plugin_dir, definition)

    assert after != before


def test_capture_plugin_snapshot_ignores_unchanged_content_metadata(tmp_path: Path):
    manager    = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    definition = _build_definition()
    main       = plugin_dir / "main.py"
    helper     = plugin_dir / "helper.py"
    (plugin_dir / "plugin.json").write_text(
        '{"name":"demo","version":"1.0.0","entry":"main.py","commands":[],"schedule":[],"concurrency":"parallel","enabled":true}',
        encoding="utf-8",
    )
    main.write_text("VALUE = 1\n", encoding="utf-8")
    helper.write_text("HELPER = 1\n", encoding="utf-8")

    base = time.time_ns()
    os.utime(main, ns=(base + 1_000, base + 1_000))
    os.utime(helper, ns=(base + 3_000, base + 3_000))
    before = manager._capture_plugin_snapshot(plugin_dir, definition)

    os.utime(main, ns=(base + 2_000, base + 2_000))
    os.utime(helper, ns=(base + 2_000, base + 2_000))
    after = manager._capture_plugin_snapshot(plugin_dir, definition)

    assert after == before


def test_capture_plugin_snapshot_detects_same_size_content_with_preserved_timestamp(
    tmp_path: Path,
):
    manager    = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    definition = _build_definition()
    main       = plugin_dir / "main.py"
    (plugin_dir / "plugin.json").write_text(
        '{"name":"demo","version":"1.0.0","entry":"main.py",'
        '"commands":[],"schedule":[],"concurrency":"parallel","enabled":true}',
        encoding="utf-8",
    )
    main.write_text("VALUE = 1\n", encoding="utf-8")
    original_stat = main.stat()
    before        = manager._capture_plugin_snapshot(plugin_dir, definition)

    main.write_text("VALUE = 2\n", encoding="utf-8")
    os.utime(main, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    after = manager._capture_plugin_snapshot(plugin_dir, definition)

    assert main.stat().st_size == original_stat.st_size
    assert main.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert after != before


def test_iter_watch_files_treats_data_named_source_tree_as_ordinary_source(tmp_path: Path):
    manager    = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    data_dir = plugin_dir / "data"
    data_dir.mkdir()
    definition = _build_definition()
    (plugin_dir / "plugin.json").write_text(
        '{"name":"demo","version":"1.0.0","entry":"main.py","commands":[],"schedule":[],"concurrency":"parallel","enabled":true}',
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    source_file = data_dir / "helper.py"
    source_file.write_text("VALUE = 1\n", encoding="utf-8")

    files = manager._iter_watch_files(plugin_dir, definition)

    assert source_file in files


def test_build_context_ensures_data_dir_once(tmp_path: Path):
    manager     = _build_manager(tmp_path)
    plugin_dir  = manager.plugins_dir / "demo"
    legacy_data = plugin_dir / "data"
    legacy_data.mkdir(parents=True)
    (legacy_data / "state.json").write_text('{"source":"legacy"}', encoding="utf-8")

    manager.build_context("demo")
    first_record = manager._data_directories["demo"]
    manager.build_context("demo")

    assert manager._data_directories["demo"] is first_record
    assert first_record.path == tmp_path / "data" / "demo"
    assert first_record.path.is_dir()
    assert (first_record.path / "state.json").read_text(encoding="utf-8") == ('{"source":"legacy"}')
    assert not legacy_data.exists()
    archive = tmp_path / "data" / ".legacy-plugin-data" / "demo"
    assert (archive / "state.json").read_text(encoding="utf-8") == '{"source":"legacy"}'


def test_existing_external_plugin_data_is_authoritative_over_legacy(tmp_path: Path):
    manager     = _build_manager(tmp_path)
    plugin_dir  = manager.plugins_dir / "demo"
    legacy_data = plugin_dir / "data"
    legacy_data.mkdir(parents=True)
    (legacy_data / "state.json").write_text("legacy", encoding="utf-8")
    external_data = tmp_path / "data" / "demo"
    external_data.mkdir(parents=True)
    (external_data / "state.json").write_text("current", encoding="utf-8")

    manager.build_context("demo")

    assert manager._data_directories["demo"].path == external_data
    assert (external_data / "state.json").read_text(encoding="utf-8") == "current"
    assert not legacy_data.exists()
    archive = tmp_path / "data" / ".legacy-plugin-data" / "demo"
    assert (archive / "state.json").read_text(encoding="utf-8") == "legacy"


def test_cross_device_legacy_archive_uses_private_atomic_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager     = _build_manager(tmp_path)
    legacy_data = manager.plugins_dir / "demo" / "data"
    legacy_data.mkdir(parents=True)
    (legacy_data / "state.json").write_text("legacy", encoding="utf-8")
    original_rename = Path.rename

    def cross_device_for_legacy(source: Path, target: Path) -> Path:
        if source == legacy_data:
            raise OSError(errno.EXDEV, "cross-device migration probe")
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", cross_device_for_legacy)

    manager.build_context("demo")

    active_data = manager._data_directories["demo"].path
    assert (active_data / "state.json").read_text(encoding="utf-8") == "legacy"
    assert not legacy_data.exists()
    archive = tmp_path / "data" / ".legacy-plugin-data" / "demo"
    assert (archive / "state.json").read_text(encoding="utf-8") == "legacy"
    assert list(archive.parent.glob(".demo.archiving-*")) == []


def test_existing_legacy_archive_is_never_overwritten(
    tmp_path: Path,
) -> None:
    manager     = _build_manager(tmp_path)
    legacy_data = manager.plugins_dir / "demo" / "data"
    legacy_data.mkdir(parents=True)
    (legacy_data / "state.json").write_text("unretired", encoding="utf-8")
    external_data = tmp_path / "data" / "demo"
    external_data.mkdir(parents=True)
    (external_data / "state.json").write_text("current", encoding="utf-8")
    archive = tmp_path / "data" / ".legacy-plugin-data" / "demo"
    archive.mkdir(parents=True)
    (archive / "state.json").write_text("previous", encoding="utf-8")

    with pytest.raises(PluginPathError, match="archive already exists"):
        manager.build_context("demo")

    assert (legacy_data / "state.json").read_text(encoding="utf-8") == "unretired"
    assert (external_data / "state.json").read_text(encoding="utf-8") == "current"
    assert (archive / "state.json").read_text(encoding="utf-8") == "previous"


def test_failed_legacy_data_migration_leaves_no_partial_external_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager     = _build_manager(tmp_path)
    legacy_data = manager.plugins_dir / "demo" / "data"
    legacy_data.mkdir(parents=True)
    (legacy_data / "state.json").write_text("legacy", encoding="utf-8")

    def fail_after_partial_copy(_source: Path, destination: Path, **_kwargs) -> None:
        (destination / "partial.txt").write_text("partial", encoding="utf-8")
        raise OSError("copy interrupted")

    monkeypatch.setattr("core.plugin_data.shutil.copytree", fail_after_partial_copy)

    with pytest.raises(PluginPathError, match="cannot copy legacy plugin data"):
        manager.build_context("demo")

    assert not (tmp_path / "data" / "demo").exists()
    assert list((tmp_path / "data").glob(".demo.migrating-*")) == []
    assert (legacy_data / "state.json").read_text(encoding="utf-8") == "legacy"


@pytest.mark.asyncio
async def test_initial_load_retires_legacy_before_source_fingerprint(tmp_path: Path) -> None:
    manager    = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    _write_runtime_manifest(plugin_dir, "demo")
    legacy_data = plugin_dir / "data"
    legacy_data.mkdir()
    (legacy_data / "runtime.py").write_text("def broken(:\n", encoding="utf-8")

    try:
        manager.load_plugin(plugin_dir)

        assert "demo" in manager.list_runtime_plugins()
        assert not legacy_data.exists()
        assert (tmp_path / "data" / "demo" / "runtime.py").is_file()
    finally:
        await manager.unload_plugin("demo")


@pytest.mark.asyncio
async def test_watcher_retires_legacy_before_source_fingerprint(tmp_path: Path) -> None:
    manager    = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    _write_runtime_manifest(plugin_dir, "demo")
    legacy_data = plugin_dir / "data"
    legacy_data.mkdir()
    (legacy_data / "runtime.py").write_text("def broken(:\n", encoding="utf-8")

    try:
        await manager._reconcile_plugin_path(plugin_dir)

        assert "demo" in manager.list_runtime_plugins()
        assert not legacy_data.exists()
        assert (tmp_path / "data" / "demo" / "runtime.py").is_file()
    finally:
        await manager.unload_plugin("demo")


@pytest.mark.asyncio
async def test_unload_plugin_clears_pending_plugin_state(tmp_path: Path):
    manager        = _build_manager(tmp_path)
    definition     = _build_definition()
    module         = ModuleType("plugins.demo.main")
    shutdown_calls = 0

    async def shutdown(context=None) -> None:
        nonlocal shutdown_calls
        shutdown_calls += 1

    module.shutdown = shutdown
    task            = asyncio.create_task(asyncio.Event().wait())
    manager._init_tasks.append(task)
    manager._init_task_plugins[task] = "demo"
    manager._pending_plugins[task]   = (definition, module, 0.0)
    manager._plugin_states["demo"]   = {"value": 1}
    manager._execution_gates["demo"] = PluginExecutionGate("parallel", plugin_name="demo")

    await manager.unload_plugin("demo")

    assert shutdown_calls == 1
    assert task.cancelled()
    assert not manager._init_tasks
    assert not manager._init_task_plugins
    assert not manager._pending_plugins
    assert "demo" not in manager._plugin_states
    assert "demo" not in manager._execution_gates


@pytest.mark.asyncio
async def test_runtime_loads_nested_entry_and_binds_its_exact_origin(tmp_path: Path) -> None:
    manager    = _build_manager(tmp_path)
    name       = "nested_entry_runtime"
    plugin_dir = manager.plugins_dir / name
    nested     = plugin_dir / "nested"
    nested.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (nested / "__init__.py").write_text("", encoding="utf-8")
    (nested / "helper.py").write_text("VALUE = 41\n", encoding="utf-8")
    entry = nested / "entry.py"
    entry.write_text("from .helper import VALUE\nRESULT = VALUE + 1\n", encoding="utf-8")
    _write_runtime_manifest(plugin_dir, name, "nested/entry.py")

    try:
        manager.load_plugin(plugin_dir)

        loaded = manager._plugins[name]
        assert loaded.module.RESULT == 42
        assert loaded.authorized_entry == entry.resolve()
        assert Path(loaded.module.__file__).resolve() == entry.resolve()
        assert loaded.module.__spec__.origin is not None
        assert Path(loaded.module.__spec__.origin).resolve() == entry.resolve()
    finally:
        await manager.unload_plugin(name)


@pytest.mark.asyncio
async def test_runtime_entry_loader_ignores_mtime_size_matched_stale_bytecode(
    tmp_path: Path,
) -> None:
    import py_compile

    manager    = _build_manager(tmp_path)
    name       = "stale_bytecode_runtime"
    plugin_dir = manager.plugins_dir / name
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    entry = plugin_dir / "main.py"
    entry.write_text("VALUE = 'first'\n", encoding="utf-8")
    _write_runtime_manifest(plugin_dir, name)
    original = entry.stat()
    py_compile.compile(str(entry), doraise=True)
    entry.write_text("VALUE = 'other'\n", encoding="utf-8")
    os.utime(entry, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert entry.stat().st_size == original.st_size
    assert entry.stat().st_mtime_ns == original.st_mtime_ns

    try:
        manager.load_plugin(plugin_dir)

        assert manager._plugins[name].module.VALUE == "other"
    finally:
        await manager.unload_plugin(name)


@pytest.mark.asyncio
async def test_live_plugin_lazy_imports_remain_source_only(tmp_path: Path) -> None:
    import py_compile

    manager    = _build_manager(tmp_path)
    name       = "lazy_source_only_runtime"
    plugin_dir = manager.plugins_dir / name
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "main.py").write_text(
        "def load_helper():\n    from . import helper\n    return helper.VALUE\n",
        encoding="utf-8",
    )
    helper = plugin_dir / "helper.py"
    helper.write_text("VALUE = 'first'\n", encoding="utf-8")
    _write_runtime_manifest(plugin_dir, name)
    original = helper.stat()
    py_compile.compile(str(helper), doraise=True)
    helper.write_text("VALUE = 'other'\n", encoding="utf-8")
    os.utime(helper, ns=(original.st_atime_ns, original.st_mtime_ns))

    try:
        manager.load_plugin(plugin_dir)

        loaded = manager._plugins[name]
        assert loaded.module.load_helper() == "other"
    finally:
        await manager.unload_plugin(name)
        assert name not in manager._source_finders


@pytest.mark.asyncio
async def test_same_named_managers_never_reuse_or_purge_foreign_cached_modules(
    tmp_path: Path,
) -> None:
    import sys

    name        = "manager_origin_collision"
    first_root  = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = _build_manager(first_root)
    first._purge_plugin_modules(name)
    first_plugin = first.plugins_dir / name
    first_plugin.mkdir()
    (first_plugin / "__init__.py").write_text("", encoding="utf-8")
    (first_plugin / "main.py").write_text("VALUE = 'first'\n", encoding="utf-8")
    _write_runtime_manifest(first_plugin, name)
    first.load_plugin(first_plugin)
    first_module = first._plugins[name].module

    second        = _build_manager(second_root)
    second_plugin = second.plugins_dir / name
    second_plugin.mkdir()
    (second_plugin / "__init__.py").write_text("", encoding="utf-8")
    (second_plugin / "main.py").write_text("VALUE = 'second'\n", encoding="utf-8")
    _write_runtime_manifest(second_plugin, name)

    try:
        second.load_plugin(second_plugin)

        assert name not in second._plugins
        assert first._plugins[name].module is first_module
        assert sys.modules[f"plugins.{name}.main"] is first_module
        assert first_module.VALUE == "first"

        await second.unload_plugin(name)
        assert sys.modules[f"plugins.{name}.main"] is first_module
        assert first._plugins[name].module is first_module
    finally:
        await first.unload_plugin(name)
        await second.unload_plugin(name)


def test_runtime_rejects_entry_symlink_without_executing_its_target(tmp_path: Path) -> None:
    import sys

    manager    = _build_manager(tmp_path)
    name       = "linked_entry_runtime"
    plugin_dir = manager.plugins_dir / name
    plugin_dir.mkdir()
    tracker                       = ModuleType("xiaoqing_linked_entry_tracker")
    tracker.executions            = 0
    sys.modules[tracker.__name__] = tracker
    outside                       = tmp_path / "outside.py"
    outside.write_text(
        "import xiaoqing_linked_entry_tracker as tracker\ntracker.executions += 1\n",
        encoding="utf-8",
    )
    _runtime_symlink_or_skip(outside, plugin_dir / "main.py")
    _write_runtime_manifest(plugin_dir, name)

    try:
        manager.load_plugin(plugin_dir)

        assert tracker.executions == 0
        assert name not in manager._plugins
        assert f"plugins.{name}.main" not in sys.modules
    finally:
        manager._purge_plugin_modules(name)
        sys.modules.pop(tracker.__name__, None)


def test_runtime_rejects_linked_manifest_authorization(tmp_path: Path) -> None:
    manager    = _build_manager(tmp_path)
    name       = "linked_manifest_runtime"
    plugin_dir = manager.plugins_dir / name
    plugin_dir.mkdir()
    (plugin_dir / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    outside_manifest = tmp_path / "outside-plugin.json"
    outside_manifest.write_text(
        f'{{"name":"{name}","entry":"main.py","enabled":true}}',
        encoding="utf-8",
    )
    _runtime_symlink_or_skip(outside_manifest, plugin_dir / "plugin.json")

    rejection = manager._load_definition(plugin_dir)
    assert isinstance(rejection, _ManifestRejection)
    assert rejection.bucket == "invalid"
    manager.load_plugin(plugin_dir)
    assert name not in manager._plugins


def test_cached_entry_origin_mismatch_is_rejected_before_any_import(tmp_path: Path) -> None:
    import importlib.machinery
    import sys

    manager    = _build_manager(tmp_path)
    name       = "cached_origin_mismatch"
    plugin_dir = manager.plugins_dir / name
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "main.py").write_text("VALUE = 'authorized'\n", encoding="utf-8")
    _write_runtime_manifest(plugin_dir, name)
    outside = tmp_path / "foreign.py"
    outside.write_text("VALUE = 'foreign'\n", encoding="utf-8")
    cached          = ModuleType(f"plugins.{name}.main")
    cached.__file__ = str(outside)
    cached.__spec__ = importlib.machinery.ModuleSpec(
        cached.__name__,
        loader = None,
        origin = str(outside),
    )
    sys.modules[cached.__name__] = cached

    try:
        with pytest.raises(PluginLoadError, match="Failed to load plugin"):
            manager._load_module(plugin_dir, _build_definition(name))

        assert sys.modules[cached.__name__] is cached
        assert not hasattr(cached, "VALUE")
    finally:
        sys.modules.pop(cached.__name__, None)
        manager._purge_plugin_modules(name)


@pytest.mark.asyncio
async def test_post_import_origin_drift_is_restart_only_quarantined(tmp_path: Path) -> None:
    import sys

    manager    = _build_manager(tmp_path)
    name       = "origin_drift_runtime"
    plugin_dir = manager.plugins_dir / name
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 'outside'\n", encoding="utf-8")
    tracker                       = ModuleType("xiaoqing_origin_drift_tracker")
    tracker.executions            = 0
    sys.modules[tracker.__name__] = tracker
    (plugin_dir / "main.py").write_text(
        "import xiaoqing_origin_drift_tracker as tracker\n"
        "tracker.executions += 1\n"
        f"__file__ = {str(outside)!r}\n",
        encoding="utf-8",
    )
    _write_runtime_manifest(plugin_dir, name)

    try:
        manager.load_plugin(plugin_dir)
        await manager.wait_inits()

        assert tracker.executions == 1
        assert name in manager._quarantined_plugins
        assert name in manager._restart_required_plugins
        assert manager._execution_gates[name].closed is True
    finally:
        manager._restart_required_plugins.discard(name)
        manager._quarantined_plugins.discard(name)
        await manager.unload_plugin(name)
        manager._purge_plugin_modules(name)
        sys.modules.pop(tracker.__name__, None)


def test_fingerprint_explicitly_includes_entry_when_walk_snapshot_omits_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager    = _build_manager(tmp_path)
    name       = "explicit_entry_fingerprint"
    plugin_dir = manager.plugins_dir / name
    plugin_dir.mkdir()
    entry = plugin_dir / "main.py"
    entry.write_text("VALUE = 1\n", encoding="utf-8")
    _write_runtime_manifest(plugin_dir, name)
    definition = _build_definition(name)

    monkeypatch.setattr(
        "core.plugin_manager.os.walk",
        lambda *_args, **_kwargs: [(str(plugin_dir.resolve()), [], ["plugin.json"])],
    )

    files = manager._iter_watch_files(plugin_dir, definition)
    assert entry.resolve() in files
    assert (plugin_dir / "plugin.json").resolve() in files


@pytest.mark.asyncio
async def test_entry_replaced_after_fingerprint_is_neither_executed_nor_published(
    tmp_path: Path,
) -> None:
    import sys

    manager    = _build_manager(tmp_path)
    name       = "entry_replace_transaction"
    plugin_dir = manager.plugins_dir / name
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    entry = plugin_dir / "main.py"
    entry.write_text("VALUE = 'old'\n", encoding="utf-8")
    _write_runtime_manifest(plugin_dir, name)
    tracker                       = ModuleType("xiaoqing_entry_replace_tracker")
    tracker.executions            = 0
    sys.modules[tracker.__name__] = tracker
    original_fingerprint          = manager._authorize_plugin_snapshot
    fingerprint_calls             = 0

    def fingerprint_then_replace(
        current_dir: Path,
        definition: PluginDefinition,
    ) -> int:
        nonlocal fingerprint_calls
        result = original_fingerprint(current_dir, definition)
        fingerprint_calls += 1
        if fingerprint_calls == 1:
            entry.write_text(
                "import xiaoqing_entry_replace_tracker as tracker\n"
                "tracker.executions += 1\n"
                "VALUE = 'new'\n",
                encoding="utf-8",
            )
        return result

    manager._authorize_plugin_snapshot = Mock(side_effect=fingerprint_then_replace)
    try:
        manager.load_plugin(plugin_dir)
        await manager.wait_inits()

        assert tracker.executions == 0
        assert fingerprint_calls == 1
        assert name not in manager._plugins
        assert name not in manager._quarantined_plugins
        assert f"plugins.{name}.main" not in sys.modules
    finally:
        manager._authorize_plugin_snapshot = original_fingerprint
        await manager.unload_plugin(name)
        manager._purge_plugin_modules(name)
        sys.modules.pop(tracker.__name__, None)


@pytest.mark.asyncio
async def test_watcher_isolates_unsafe_linked_entry_and_loads_safe_sibling(
    tmp_path: Path,
) -> None:
    import sys

    manager     = _build_manager(tmp_path)
    unsafe_name = "a_unsafe_linked_entry"
    safe_name   = "b_safe_entry"
    unsafe      = manager.plugins_dir / unsafe_name
    safe        = manager.plugins_dir / safe_name
    unsafe.mkdir()
    safe.mkdir()
    outside                       = tmp_path / "outside.py"
    tracker                       = ModuleType("xiaoqing_watcher_link_tracker")
    tracker.executions            = 0
    sys.modules[tracker.__name__] = tracker
    outside.write_text(
        "import xiaoqing_watcher_link_tracker as tracker\ntracker.executions += 1\n",
        encoding="utf-8",
    )
    _runtime_symlink_or_skip(outside, unsafe / "main.py")
    _write_runtime_manifest(unsafe, unsafe_name)
    (safe / "main.py").write_text("VALUE = 'safe'\n", encoding="utf-8")
    _write_runtime_manifest(safe, safe_name)

    try:
        await manager.reconcile_plugins()

        assert tracker.executions == 0
        assert unsafe_name not in manager._plugins
        assert manager._plugins[safe_name].module.VALUE == "safe"
    finally:
        await manager.unload_plugin(unsafe_name)
        await manager.unload_plugin(safe_name)
        manager._purge_plugin_modules(unsafe_name)
        manager._purge_plugin_modules(safe_name)
        sys.modules.pop(tracker.__name__, None)


@pytest.mark.asyncio
async def test_wait_inits_and_unload_share_one_pending_finalizer(tmp_path: Path) -> None:
    manager    = _build_manager(tmp_path)
    definition = _build_definition()
    module     = ModuleType("plugins.demo.main")
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    init_started   = asyncio.Event()
    shutdown_calls = 0

    async def init_work() -> None:
        init_started.set()
        await asyncio.Event().wait()

    async def shutdown(context=None) -> None:
        nonlocal shutdown_calls
        shutdown_calls += 1

    module.shutdown = shutdown
    init_task       = asyncio.create_task(gate.run(init_work))
    manager._init_tasks.append(init_task)
    manager._init_task_plugins[init_task] = "demo"
    manager._pending_plugins[init_task]   = (definition, module, 0.0)
    manager._plugin_states["demo"]        = {"resource": object()}
    manager._execution_gates["demo"]      = gate

    waiter = asyncio.create_task(manager.wait_inits())
    await asyncio.wait_for(init_started.wait(), timeout=1)
    unloader = asyncio.create_task(manager.unload_plugin("demo"))
    await asyncio.wait_for(asyncio.gather(waiter, unloader), timeout=1)

    assert shutdown_calls == 1
    assert init_task.cancelled()
    assert gate.closed is True
    assert not manager._init_tasks
    assert not manager._init_task_plugins
    assert not manager._pending_plugins
    assert not manager._pending_finalizers
    assert "demo" not in manager._plugins
    assert "demo" not in manager._plugin_states
    assert "demo" not in manager._execution_gates


@pytest.mark.asyncio
async def test_wait_inits_cancellation_still_rolls_back_every_pending_generation(
    tmp_path: Path,
) -> None:
    manager    = _build_manager(tmp_path)
    definition = _build_definition()
    module     = ModuleType("plugins.demo.main")
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    init_started   = asyncio.Event()
    shutdown_calls = 0

    async def initialize() -> None:
        init_started.set()
        await asyncio.Event().wait()

    async def shutdown(context=None) -> None:
        nonlocal shutdown_calls
        shutdown_calls += 1

    module.shutdown = shutdown
    init_task       = asyncio.create_task(gate.run(initialize))
    manager._init_tasks.append(init_task)
    manager._init_task_plugins[init_task] = "demo"
    manager._pending_plugins[init_task]   = (definition, module, 0.0)
    manager._plugin_states["demo"]        = {"resource": object()}
    manager._execution_gates["demo"]      = gate

    waiter = asyncio.create_task(manager.wait_inits())
    await asyncio.wait_for(init_started.wait(), timeout=1)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert shutdown_calls == 1
    assert init_task.cancelled()
    assert not manager._init_tasks
    assert not manager._init_task_plugins
    assert not manager._pending_plugins
    assert not manager._pending_finalizers
    assert "demo" not in manager._plugins
    assert "demo" not in manager._plugin_states
    assert "demo" not in manager._execution_gates


@pytest.mark.asyncio
async def test_pending_finalizer_wait_survives_repeated_cancellation(tmp_path: Path) -> None:
    manager           = _build_manager(tmp_path)
    finalizer_started = asyncio.Event()
    release_finalizer = asyncio.Event()

    async def finalize() -> bool:
        finalizer_started.set()
        await release_finalizer.wait()
        return True

    finalizer = asyncio.create_task(finalize())
    waiter    = asyncio.create_task(manager._await_pending_finalizer(finalizer))
    await asyncio.wait_for(finalizer_started.wait(), timeout=1)

    waiter.cancel("first")
    await asyncio.sleep(0)
    assert waiter.done() is False
    waiter.cancel("second")
    await asyncio.sleep(0)
    assert waiter.done() is False

    release_finalizer.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await waiter

    assert caught.value.args == ("first",)
    assert finalizer.done() is True
    assert finalizer.result() is True


@pytest.mark.asyncio
async def test_wait_inits_cancellation_while_finalizing_claims_later_results(
    tmp_path: Path,
) -> None:
    manager                = _build_manager(tmp_path)
    first_shutdown_started = asyncio.Event()
    release_first_shutdown = asyncio.Event()
    shutdown_calls         = {"first": 0, "second": 0}

    async def successful_init() -> None:
        return None

    records = []
    for name in ("first", "second"):
        definition = _build_definition(name)
        module     = ModuleType(f"plugins.{name}.main")

        async def shutdown(context=None, *, plugin_name=name) -> None:
            shutdown_calls[plugin_name] += 1
            if plugin_name == "first":
                first_shutdown_started.set()
                await release_first_shutdown.wait()

        module.shutdown = shutdown
        gate = PluginExecutionGate("parallel", plugin_name=name)
        init_task = asyncio.create_task(successful_init())
        manager._init_tasks.append(init_task)
        manager._init_task_plugins[init_task] = name
        manager._pending_plugins[init_task]   = (definition, module, 0.0)
        manager._plugin_states[name]          = {"resource": object()}
        manager._execution_gates[name]        = gate
        records.append((name, init_task))

    manager._register_loaded_plugin = Mock(side_effect=RuntimeError("publish failed"))
    waiter = asyncio.create_task(manager.wait_inits())
    await asyncio.wait_for(first_shutdown_started.wait(), timeout=1)
    waiter.cancel()
    release_first_shutdown.set()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert shutdown_calls == {"first": 1, "second": 1}
    assert all(task.done() for _name, task in records)
    assert not manager._init_task_plugins
    assert not manager._pending_plugins
    assert not manager._pending_finalizers
    assert not manager._plugins
    assert not manager._plugin_states
    assert not manager._execution_gates


@pytest.mark.asyncio
async def test_unload_cancels_running_plugin_gate_before_shutdown(tmp_path: Path):
    manager         = _build_manager(tmp_path)
    definition      = _build_definition()
    gate            = PluginExecutionGate("sequential")
    entered         = asyncio.Event()
    shutdown_called = asyncio.Event()
    module          = ModuleType("demo.main")

    async def shutdown():
        shutdown_called.set()

    module.shutdown          = shutdown
    manager._plugins["demo"] = LoadedPlugin(
        definition     = definition,
        module         = module,
        mtime          = 0.0,
        execution_gate = gate,
    )
    manager._execution_gates["demo"] = gate

    async def slow_handler() -> None:
        entered.set()
        await asyncio.Event().wait()

    running = asyncio.create_task(gate.run(slow_handler))
    await entered.wait()

    await manager.unload_plugin("demo")

    assert running.cancelled()
    assert shutdown_called.is_set()
    assert "demo" not in manager._plugins
    assert "demo" not in manager._execution_gates
    assert not manager._pending_plugins
