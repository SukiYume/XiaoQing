"""源码快照、导入来源和路径安全。"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

import pytest

import tests.helpers.plugin_manager_test_support as _fixture_support
from core.plugin_watcher import _ManifestRejection
from tests.helpers.plugin_manager_test_support import (
    LoadedPlugin,
    ModuleType,
    PluginDefinition,
    PluginExecutionGate,
    PluginLoadError,
    _build_definition,
    _build_manager,
    _runtime_symlink_or_skip,
    _write_runtime_manifest,
    load_plugin_dir,
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
    manager = _build_manager(tmp_path)
    (manager.plugins_dir / "demo").mkdir()

    manager.build_context("demo")
    first_record = manager._data_directories["demo"]
    manager.build_context("demo")

    assert manager._data_directories["demo"] is first_record
    assert first_record.path == tmp_path / "data" / "demo"
    assert first_record.path.is_dir()


def test_data_directory_inside_the_plugin_tree_is_left_alone(tmp_path: Path):
    """插件目录下名叫 data 的目录只是普通源码目录：不搬、不删，数据目录仍在 data_root 下。"""

    manager = _build_manager(tmp_path)
    in_tree = manager.plugins_dir / "demo" / "data"
    in_tree.mkdir(parents=True)
    (in_tree / "state.json").write_text("in-tree", encoding="utf-8")

    manager.build_context("demo")

    assert (in_tree / "state.json").read_text(encoding="utf-8") == "in-tree"
    assert list(manager._data_directories["demo"].path.iterdir()) == []


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
        await load_plugin_dir(manager, plugin_dir)

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
        await load_plugin_dir(manager, plugin_dir)

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
        await load_plugin_dir(manager, plugin_dir)

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
    await load_plugin_dir(first, first_plugin)
    first_module = first._plugins[name].module

    second        = _build_manager(second_root)
    second_plugin = second.plugins_dir / name
    second_plugin.mkdir()
    (second_plugin / "__init__.py").write_text("", encoding="utf-8")
    (second_plugin / "main.py").write_text("VALUE = 'second'\n", encoding="utf-8")
    _write_runtime_manifest(second_plugin, name)

    try:
        await load_plugin_dir(second, second_plugin)

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


@pytest.mark.asyncio
async def test_runtime_rejects_entry_symlink_without_executing_its_target(tmp_path: Path) -> None:
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
        await load_plugin_dir(manager, plugin_dir)

        assert tracker.executions == 0
        assert name not in manager._plugins
        assert f"plugins.{name}.main" not in sys.modules
    finally:
        manager._purge_plugin_modules(name)
        sys.modules.pop(tracker.__name__, None)


@pytest.mark.asyncio
async def test_runtime_rejects_linked_manifest_authorization(tmp_path: Path) -> None:
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
    await load_plugin_dir(manager, plugin_dir)
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
        await load_plugin_dir(manager, plugin_dir)

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

    with os.scandir(plugin_dir) as entries:
        snapshot = [item for item in entries if item.name != "main.py"]
    scanner = Mock(return_value=nullcontext(snapshot))
    monkeypatch.setattr("core.plugin_watcher.os.scandir", scanner)

    files = manager._iter_watch_files(plugin_dir, definition)
    scanner.assert_called_once()
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
        await load_plugin_dir(manager, plugin_dir)

        assert tracker.executions == 0
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
