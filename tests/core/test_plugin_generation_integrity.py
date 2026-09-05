# 验证插件导入代不可变；每例完整恢复进程级模块和导入器状态。
"""Regression tests for immutable plugin import generations.

These tests deliberately exercise process-global import state.  Every case uses
unique module names and the autouse fixture restores the canonical namespace,
finder list, and package search path even when an assertion fails.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import py_compile
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import ModuleType

import pytest

import core.plugin_manager as plugin_manager_module
from core.models import PluginManifest
from core.plugin_manager import PluginManager, PluginPathError
from core.router import CommandRouter

_PLUGIN_PREFIX  = "integrity_"
_TRACKER_PREFIX = "xiaoqing_integrity_"


def _drop_test_modules() -> None:
    """Remove only modules owned by this file, including parent attributes."""

    names = [
        name
        for name in sys.modules
        if name.startswith(f"plugins.{_PLUGIN_PREFIX}") or name.startswith(_TRACKER_PREFIX)
    ]
    for name in sorted(names, key=lambda value: value.count("."), reverse=True):
        module = sys.modules.get(name)
        parent_name, separator, child_name = name.rpartition(".")
        parent = sys.modules.get(parent_name) if separator else None
        if isinstance(parent, ModuleType) and vars(parent).get(child_name) is module:
            delattr(parent, child_name)
        sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def _restore_process_import_state():
    """Keep managers created here from leaking process-global import state."""

    plugins_package  = importlib.import_module("plugins")
    meta_path        = list(sys.meta_path)
    sys_path         = list(sys.path)
    plugins_path     = list(plugins_package.__path__)
    namespace_owners = dict(plugin_manager_module._PLUGIN_NAMESPACE_OWNERS)
    path_leases      = {
        key: type(lease)(
            container = lease.container,
            owners    = lease.owners,
            inserted  = lease.inserted,
        )
        for key, lease in plugin_manager_module._PROCESS_IMPORT_PATH_LEASES.items()
    }

    yield

    _drop_test_modules()
    sys.meta_path[:]            = meta_path
    sys.path[:]                 = sys_path
    plugins_package.__path__[:] = plugins_path
    plugin_manager_module._PLUGIN_NAMESPACE_OWNERS.clear()
    plugin_manager_module._PLUGIN_NAMESPACE_OWNERS.update(namespace_owners)
    plugin_manager_module._PROCESS_IMPORT_PATH_LEASES.clear()
    plugin_manager_module._PROCESS_IMPORT_PATH_LEASES.update(path_leases)


def _build_manager(root: Path) -> PluginManager:
    plugins_dir = root / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "__init__.py").write_text("", encoding="utf-8")
    return PluginManager(
        plugins_dir     = plugins_dir,
        router          = CommandRouter(),
        context_factory = lambda *args, **kwargs: object(),
    )


def test_plugin_manager_degrades_to_restart_only_when_import_barrier_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_calls: list[PluginManager] = []

    def record_setup(manager: PluginManager) -> None:
        setup_calls.append(manager)

    monkeypatch.setattr(PluginManager, "_setup_sys_path", record_setup)

    unavailable = plugin_manager_module._ModuleImportBarrierCapability(
        False,
        "unsupported interpreter",
    )

    monkeypatch.setattr(
        plugin_manager_module._ModuleImportBarrierCoordinator,
        "capability",
        lambda _self: unavailable,
    )

    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "__init__.py").write_text("", encoding="utf-8")
    manager = PluginManager(
        plugins_dir     = plugins_dir,
        router          = CommandRouter(),
        context_factory = lambda *args, **kwargs: object(),
    )

    assert setup_calls == [manager]
    assert manager.hot_reload_supported is False
    assert manager.hot_reload_unavailable_reason == "unsupported interpreter"

    with pytest.raises(PluginPathError, match="restart the process"):
        asyncio.run(manager.reload_all_plugins())


def test_import_barrier_probe_checks_behavior_and_leaves_no_synthetic_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = plugin_manager_module._ModuleImportBarrierCoordinator()

    class NonBlockingFakeModuleLock:
        def acquire(self) -> None:
            return None

        def release(self) -> None:
            return None

    monkeypatch.setattr(
        plugin_manager_module._ModuleImportBarrierCoordinator,
        "_lock_getter",
        staticmethod(lambda: lambda _name: NonBlockingFakeModuleLock()),
    )

    capability = coordinator.capability()

    assert capability.available is False
    assert "do not honor" in str(capability.reason)
    assert not any(
        name.startswith("__xiaoqing_plugin_import_barrier_probe_") for name in sys.modules
    )


def test_import_barrier_probe_treats_private_api_failure_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = plugin_manager_module._ModuleImportBarrierCoordinator()

    def unavailable_lock_getter() -> object:
        raise RuntimeError("private API layout changed")

    monkeypatch.setattr(
        plugin_manager_module._ModuleImportBarrierCoordinator,
        "_lock_getter",
        staticmethod(unavailable_lock_getter),
    )

    capability = coordinator.capability()

    assert capability.available is False
    assert capability.reason == "module-lock capability is unavailable: RuntimeError"


def test_import_barrier_coordinator_keeps_one_worker_across_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator             = plugin_manager_module._ModuleImportBarrierCoordinator()
    coordinator._capability = plugin_manager_module._ModuleImportBarrierCapability(True)
    held_lock_entered       = threading.Event()
    release_held_lock       = threading.Event()

    class FakeModuleLock:
        def __init__(self, name: str) -> None:
            self.name = name

        def acquire(self) -> None:
            if self.name == "held":
                held_lock_entered.set()
                release_held_lock.wait()

        def release(self) -> None:
            return None

    def fake_getter(name: str) -> FakeModuleLock:
        return FakeModuleLock(name)

    monkeypatch.setattr(
        plugin_manager_module._ModuleImportBarrierCoordinator,
        "_lock_getter",
        staticmethod(lambda: fake_getter),
    )

    with pytest.raises(PluginPathError, match="timed out waiting for plugin import module locks"):
        coordinator.cross(("held",), timeout=0.05)
    assert held_lock_entered.is_set()
    worker = coordinator._worker
    assert worker is not None and worker.is_alive()

    with pytest.raises(PluginPathError, match="timed out waiting for the plugin import barrier"):
        coordinator.cross(("queued",), timeout=0.05)
    assert coordinator._worker is worker

    release_held_lock.set()
    deadline = time.monotonic() + 1
    while coordinator._busy and time.monotonic() < deadline:
        time.sleep(0.01)
    coordinator.cross(("after-timeout",), timeout=0.5)
    assert coordinator._worker is worker


def _write_manifest(plugin_dir: Path, name: str, *, entry: str = "main.py") -> None:
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "entry": entry,
                "commands": [],
                "schedule": [],
                "concurrency": "parallel",
                "enabled": True,
            }
        ),
        encoding="utf-8",
    )


def _write_plugin(
    manager: PluginManager,
    name: str,
    main_source: str,
) -> Path:
    plugin_dir = manager.plugins_dir / name
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "main.py").write_text(main_source, encoding="utf-8")
    _write_manifest(plugin_dir, name)
    return plugin_dir


async def _force_cleanup(manager: PluginManager, name: str) -> None:
    """Terminate a test generation, including intentional restart quarantine."""

    manager._restart_required_plugins.discard(name)
    manager._quarantined_plugins.discard(name)
    try:
        try:
            await manager.unload_plugin(name)
        except PluginPathError:
            pass
    finally:
        canonical = f"plugins.{name}"
        owned     = manager._owned_plugin_modules.get(name, {})
        if manager._owns_plugin_namespace(name):
            for module_name in sorted(
                [
                    module_name
                    for module_name in sys.modules
                    if module_name == canonical or module_name.startswith(f"{canonical}.")
                ],
                key     = lambda value: value.count("."),
                reverse = True,
            ):
                module = sys.modules.get(module_name)
                if owned.get(module_name) is module:
                    continue
                parent_name, _, child_name = module_name.rpartition(".")
                parent = sys.modules.get(parent_name)
                if isinstance(parent, ModuleType):
                    namespace = ModuleType.__getattribute__(parent, "__dict__")
                    if namespace.get(child_name) is module:
                        namespace.pop(child_name, None)
                sys.modules.pop(module_name, None)
        manager._purge_plugin_modules(name)
        manager._release_plugin_namespace(name)
        manager._plugins.pop(name, None)
        manager._plugin_states.pop(name, None)
        manager._execution_gates.pop(name, None)
        manager._quarantined_plugins.discard(name)
        manager._restart_required_plugins.discard(name)


def _create_directory_link(target: Path, link: Path) -> None:
    """Create an unprivileged Windows junction or a directory symlink."""

    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
            check          = False,
            capture_output = True,
            text           = True,
            encoding       = "utf-8",
            errors         = "replace",
        )
        if completed.returncode == 0:
            return
    try:
        os.symlink(target, link, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory links are unavailable: {exc}")


def _remove_directory_link(link: Path) -> None:
    if not os.path.lexists(link):
        return
    if os.name == "nt":
        os.rmdir(link)
    else:
        link.unlink()


@pytest.mark.asyncio
async def test_orphan_lazy_helper_bytecode_is_not_importable(tmp_path: Path) -> None:
    manager    = _build_manager(tmp_path)
    name       = "integrity_orphan_pyc"
    plugin_dir = _write_plugin(
        manager,
        name,
        "import importlib\n"
        "def load_helper():\n"
        "    return importlib.import_module(f'{__package__}.helper')\n",
    )
    helper = plugin_dir / "helper.py"
    helper.write_text("VALUE = 'bytecode-only'\n", encoding="utf-8")
    cached = Path(py_compile.compile(str(helper), doraise=True))
    helper.unlink()
    assert cached.is_file()

    try:
        manager.load_plugin(plugin_dir)

        loaded = manager.get(name)
        assert loaded is not None
        with pytest.raises(ModuleNotFoundError, match="authorized plugin snapshot"):
            loaded.module.load_helper()
        assert f"plugins.{name}.helper" not in sys.modules
    finally:
        await _force_cleanup(manager, name)


@pytest.mark.asyncio
async def test_source_added_after_publication_is_not_lazy_importable(tmp_path: Path) -> None:
    manager    = _build_manager(tmp_path)
    name       = "integrity_added_source"
    plugin_dir = _write_plugin(
        manager,
        name,
        "import importlib\n"
        "def load_added():\n"
        "    return importlib.import_module(f'{__package__}.added')\n",
    )

    try:
        manager.load_plugin(plugin_dir)
        loaded = manager.get(name)
        assert loaded is not None

        (plugin_dir / "added.py").write_text("VALUE = 'new generation'\n", encoding="utf-8")

        with pytest.raises(ModuleNotFoundError, match="authorized plugin snapshot"):
            loaded.module.load_added()
        assert f"plugins.{name}.added" not in sys.modules
    finally:
        await _force_cleanup(manager, name)


@pytest.mark.asyncio
async def test_source_finder_resume_requires_a_matching_publication_pause(
    tmp_path: Path,
) -> None:
    manager    = _build_manager(tmp_path)
    name       = "integrity_publication_resume"
    plugin_dir = _write_plugin(manager, name, "VALUE = 'published'\n")

    try:
        manager.load_plugin(plugin_dir)
        finder = manager._source_finders[name]

        with pytest.raises(PluginPathError, match="cannot resume plugin generation"):
            finder.resume_after_publication()

        finder.pause_for_publication()
        finder.resume_after_publication()
        assert finder._publication_paused is False
    finally:
        await _force_cleanup(manager, name)


@pytest.mark.asyncio
async def test_async_init_cache_removal_cannot_create_two_live_generations(
    tmp_path: Path,
) -> None:
    name            = "integrity_manager_collision"
    canonical_entry = f"plugins.{name}.main"
    first           = _build_manager(tmp_path / "first")
    second          = _build_manager(tmp_path / "second")
    first_plugin    = _write_plugin(
        first,
        name,
        "import sys\n"
        "import xiaoqing_integrity_collision_tracker as tracker\n"
        "async def init():\n"
        "    sys.modules.pop(__name__, None)\n"
        "    sys.modules[__name__] = tracker.foreign\n"
        "    tracker.started.set()\n"
        "    await tracker.release.wait()\n",
    )
    second_plugin   = _write_plugin(second, name, "VALUE = 'second'\n")
    external_source = tmp_path / "foreign_generation.py"
    external_source.write_text("VALUE = 'foreign'\n", encoding="utf-8")
    foreign                       = ModuleType(canonical_entry)
    foreign.__file__              = str(external_source)
    tracker                       = ModuleType("xiaoqing_integrity_collision_tracker")
    tracker.foreign               = foreign
    tracker.started               = asyncio.Event()
    tracker.release               = asyncio.Event()
    sys.modules[tracker.__name__] = tracker

    try:
        first.load_plugin(first_plugin)
        await asyncio.wait_for(tracker.started.wait(), timeout=1)
        assert sys.modules[canonical_entry] is foreign

        second.load_plugin(second_plugin)

        assert second.get(name) is None
        assert name not in second.list_runtime_plugins()

        tracker.release.set()
        await asyncio.wait_for(first.wait_inits(), timeout=1)

        assert name in first._quarantined_plugins
        assert first._execution_gates[name].closed is True
        assert first.get(name) is None or name in first._quarantined_plugins
        assert sys.modules[canonical_entry] is foreign

        await first.unload_plugin(name)
        await second.unload_plugin(name)
        assert sys.modules[canonical_entry] is foreign
    finally:
        tracker.release.set()
        await _force_cleanup(second, name)
        await _force_cleanup(first, name)
        sys.modules.pop(canonical_entry, None)
        sys.modules.pop(tracker.__name__, None)


@pytest.mark.asyncio
async def test_unload_ignores_external_module_after_plugin_path_becomes_link(
    tmp_path: Path,
) -> None:
    manager      = _build_manager(tmp_path)
    name         = "integrity_renamed_root"
    plugin_dir   = _write_plugin(manager, name, "VALUE = 'loaded'\n")
    moved_dir    = manager.plugins_dir / f"{name}_moved"
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    external_source = external_dir / "already_imported.py"
    external_source.write_text("VALUE = 'external'\n", encoding="utf-8")
    external                       = ModuleType("xiaoqing_integrity_external_module")
    external.__file__              = str(external_source)
    sys.modules[external.__name__] = external

    try:
        manager.load_plugin(plugin_dir)
        assert manager.get(name) is not None

        plugin_dir.rename(moved_dir)
        _create_directory_link(external_dir, plugin_dir)

        await manager.unload_plugin(name)

        assert sys.modules[external.__name__] is external
        assert manager.get(name) is None
    finally:
        await _force_cleanup(manager, name)
        _remove_directory_link(plugin_dir)
        sys.modules.pop(external.__name__, None)


@pytest.mark.asyncio
async def test_case_variant_lazy_import_cannot_alias_one_windows_source(
    tmp_path: Path,
) -> None:
    manager    = _build_manager(tmp_path)
    name       = "integrity_case_variant"
    plugin_dir = _write_plugin(
        manager,
        name,
        "import importlib\n"
        "def load(spelling):\n"
        "    return importlib.import_module(f'{__package__}.{spelling}')\n",
    )
    (plugin_dir / "helper.py").write_text("VALUE = 42\n", encoding="utf-8")

    try:
        manager.load_plugin(plugin_dir)
        loaded = manager.get(name)
        assert loaded is not None

        helper = loaded.module.load("helper")
        assert helper.VALUE == 42
        with pytest.raises(ModuleNotFoundError, match="authorized plugin snapshot"):
            loaded.module.load("Helper")

        assert sys.modules[f"plugins.{name}.helper"] is helper
        assert f"plugins.{name}.Helper" not in sys.modules
    finally:
        await _force_cleanup(manager, name)


@pytest.mark.parametrize(
    "entry",
    [
        "data/main.py",
        "__pycache__/main.py",
        "\uff4dain.py",
    ],
)
def test_manifest_model_rejects_reserved_or_nfkc_unstable_entries(entry: str) -> None:
    with pytest.raises(ValueError):
        PluginManifest(name="integrity_invalid_entry", entry=entry)


@pytest.mark.asyncio
async def test_nested_data_package_is_fingerprinted_and_importable(tmp_path: Path) -> None:
    manager     = _build_manager(tmp_path)
    name        = "integrity_nested_data_package"
    plugin_dir  = manager.plugins_dir / name
    package     = plugin_dir / "pkg"
    nested_data = package / "data"
    nested_data.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    initializer = nested_data / "__init__.py"
    initializer.write_text("TOKEN = 'first'\n", encoding="utf-8")
    (nested_data / "main.py").write_text("from . import TOKEN\nVALUE = TOKEN\n", encoding="utf-8")
    _write_manifest(plugin_dir, name, entry="pkg/data/main.py")
    definition = manager._load_definition(plugin_dir)
    assert definition is not None

    first = manager._capture_plugin_snapshot(plugin_dir, definition)
    initializer.write_text("TOKEN = 'other'\n", encoding="utf-8")
    second = manager._capture_plugin_snapshot(plugin_dir, definition)
    assert first != second

    try:
        manager.load_plugin(plugin_dir)
        loaded = manager.get(name)
        assert loaded is not None
        assert loaded.module.VALUE == "other"
    finally:
        await _force_cleanup(manager, name)


@pytest.mark.asyncio
async def test_source_change_after_fingerprint_is_rejected_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager      = _build_manager(tmp_path)
    name         = "integrity_source_aba"
    tracker_name = "xiaoqing_integrity_aba_tracker"
    safe_source  = f"import {tracker_name} as tracker\ntracker.safe += 1\nVALUE = 'SAFE'\n"
    evil_source  = f"import {tracker_name} as tracker\ntracker.evil += 1\nVALUE = 'EVIL'\n"
    safe_bytes   = safe_source.encode()
    evil_bytes   = evil_source.encode()
    assert len(safe_bytes) == len(evil_bytes)
    plugin_dir = _write_plugin(manager, name, safe_source)
    entry      = plugin_dir / "main.py"
    entry.write_bytes(safe_bytes)
    tracker                     = ModuleType(tracker_name)
    tracker.safe                = 0
    tracker.evil                = 0
    sys.modules[tracker_name]   = tracker
    original_authorize_snapshot = manager._authorize_plugin_snapshot
    calls                       = 0

    def fingerprint_then_change_source(current_dir: Path, definition):
        nonlocal calls
        calls += 1
        fingerprint = original_authorize_snapshot(current_dir, definition)
        if calls == 1:
            assert fingerprint.sources["main.py"] == safe_bytes
            entry.write_bytes(evil_bytes)
        return fingerprint

    monkeypatch.setattr(manager, "_authorize_plugin_snapshot", fingerprint_then_change_source)
    try:
        manager.load_plugin(plugin_dir)

        loaded = manager.get(name)
        assert loaded is None
        assert tracker.safe == 0
        assert tracker.evil == 0
        assert calls == 1
        assert entry.read_bytes() == evil_bytes
    finally:
        await _force_cleanup(manager, name)
        sys.modules.pop(tracker_name, None)


@pytest.mark.asyncio
async def test_lazy_namespace_packages_are_owned_and_fully_unloaded(tmp_path: Path) -> None:
    manager    = _build_manager(tmp_path)
    name       = "integrity_namespace_unload"
    plugin_dir = _write_plugin(
        manager,
        name,
        "import importlib\n"
        "def load_helper():\n"
        "    return importlib.import_module(f'{__package__}.ns.helper')\n",
    )
    namespace_dir = plugin_dir / "ns"
    namespace_dir.mkdir()
    (namespace_dir / "helper.py").write_text("VALUE = 42\n", encoding="utf-8")
    namespace_name = f"plugins.{name}.ns"
    helper_name    = f"{namespace_name}.helper"

    manager.load_plugin(plugin_dir)
    loaded = manager.get(name)
    assert loaded is not None
    helper = loaded.module.load_helper()
    assert helper.VALUE == 42
    assert manager._owned_plugin_modules[name][namespace_name] is sys.modules[namespace_name]
    assert manager._owned_plugin_modules[name][helper_name] is helper

    await manager.unload_plugin(name)

    assert namespace_name not in sys.modules
    assert helper_name not in sys.modules
    assert name not in manager._source_finders
    assert name not in manager._owned_plugin_modules


@pytest.mark.asyncio
async def test_entry_can_join_thread_that_lazy_imports_same_generation(tmp_path: Path) -> None:
    manager    = _build_manager(tmp_path)
    name       = "integrity_import_lock_order"
    plugin_dir = _write_plugin(
        manager,
        name,
        "import importlib\n"
        "import threading\n"
        "result = []\n"
        "def worker():\n"
        "    result.append(importlib.import_module(f'{__package__}.helper').VALUE)\n"
        "thread = threading.Thread(target=worker)\n"
        "thread.start()\n"
        "thread.join(2)\n"
        "if thread.is_alive():\n"
        "    raise RuntimeError('plugin import lock deadlocked')\n"
        "VALUE = result[0]\n",
    )
    (plugin_dir / "helper.py").write_text("VALUE = 42\n", encoding="utf-8")

    try:
        manager.load_plugin(plugin_dir)
        loaded = manager.get(name)
        assert loaded is not None
        assert loaded.module.VALUE == 42
    finally:
        await _force_cleanup(manager, name)


@pytest.mark.asyncio
async def test_spec_created_before_unload_is_revoked_before_source_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager      = _build_manager(tmp_path)
    name         = "integrity_stale_spec"
    tracker_name = "xiaoqing_integrity_stale_spec_tracker"
    plugin_dir   = _write_plugin(
        manager,
        name,
        "import importlib\n"
        "def load_helper():\n"
        "    return importlib.import_module(f'{__package__}.helper')\n",
    )
    (plugin_dir / "helper.py").write_text(
        f"import {tracker_name} as tracker\ntracker.executions += 1\nVALUE = 1\n",
        encoding="utf-8",
    )
    tracker                   = ModuleType(tracker_name)
    tracker.executions        = 0
    sys.modules[tracker_name] = tracker
    helper_name               = f"plugins.{name}.helper"

    manager.load_plugin(plugin_dir)
    loaded = manager.get(name)
    assert loaded is not None
    finder                             = manager._source_finders[name]
    original_find_spec                 = finder.find_spec
    original_deactivate                = finder.deactivate_and_wait
    spec_ready                         = threading.Event()
    release_spec                       = threading.Event()
    import_errors: list[BaseException] = []

    def delayed_find_spec(fullname, path=None, target=None):  # type: ignore[no-untyped-def]
        spec = original_find_spec(fullname, path, target)
        if fullname == helper_name:
            spec_ready.set()
            if not release_spec.wait(2):
                raise RuntimeError("test did not release stale spec")
        return spec

    def deactivate_then_release(*, timeout=5.0):  # type: ignore[no-untyped-def]
        module_names = original_deactivate(timeout=timeout)
        release_spec.set()
        return module_names

    monkeypatch.setattr(finder, "find_spec", delayed_find_spec)
    monkeypatch.setattr(finder, "deactivate_and_wait", deactivate_then_release)

    def import_helper() -> None:
        try:
            loaded.module.load_helper()
        except BaseException as exc:
            import_errors.append(exc)

    import_thread = threading.Thread(target=import_helper)
    import_thread.start()
    assert spec_ready.wait(2)
    try:
        await manager.unload_plugin(name)
        import_thread.join(2)

        assert not import_thread.is_alive()
        assert tracker.executions == 0
        assert len(import_errors) == 1
        assert isinstance(import_errors[0], ImportError)
        assert helper_name not in sys.modules
        assert name not in manager._source_finders
        assert name not in manager._owned_plugin_modules
        assert name not in plugin_manager_module._PLUGIN_NAMESPACE_OWNERS
    finally:
        release_spec.set()
        import_thread.join(2)
        await _force_cleanup(manager, name)
        sys.modules.pop(tracker_name, None)


@pytest.mark.asyncio
async def test_unload_crosses_importlib_post_exec_parent_binding_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager      = _build_manager(tmp_path)
    name         = "integrity_import_tail"
    tracker_name = "xiaoqing_integrity_import_tail_tracker"
    plugin_dir   = _write_plugin(
        manager,
        name,
        "import importlib\n"
        "def load_helper():\n"
        "    return importlib.import_module(f'{__package__}.helper')\n",
    )
    (plugin_dir / "helper.py").write_text(
        f"import {tracker_name} as tracker\ntracker.executions += 1\nVALUE = 1\n",
        encoding="utf-8",
    )
    tracker                   = ModuleType(tracker_name)
    tracker.executions        = 0
    sys.modules[tracker_name] = tracker
    helper_name               = f"plugins.{name}.helper"
    manager.load_plugin(plugin_dir)
    loaded = manager.get(name)
    assert loaded is not None
    finder                             = manager._source_finders[name]
    original_deactivate                = finder.deactivate_and_wait
    original_exec                      = plugin_manager_module._SourceOnlyPluginLoader.exec_module
    exec_returned                      = threading.Event()
    release_exec                       = threading.Event()
    import_results: list[ModuleType]   = []
    import_errors: list[BaseException] = []

    def delayed_exec(loader, module):  # type: ignore[no-untyped-def]
        original_exec(loader, module)
        if loader.name == helper_name:
            exec_returned.set()
            if not release_exec.wait(2):
                raise RuntimeError("test did not release import tail")

    def deactivate_then_release(*, timeout=5.0):  # type: ignore[no-untyped-def]
        module_names = original_deactivate(timeout=timeout)
        release_exec.set()
        return module_names

    monkeypatch.setattr(
        plugin_manager_module._SourceOnlyPluginLoader,
        "exec_module",
        delayed_exec,
    )
    monkeypatch.setattr(finder, "deactivate_and_wait", deactivate_then_release)

    def import_helper() -> None:
        try:
            import_results.append(loaded.module.load_helper())
        except BaseException as exc:
            import_errors.append(exc)

    import_thread = threading.Thread(target=import_helper)
    import_thread.start()
    assert exec_returned.wait(2)
    try:
        await manager.unload_plugin(name)
        import_thread.join(2)

        assert not import_thread.is_alive()
        assert not import_errors
        assert len(import_results) == 1
        assert tracker.executions == 1
        assert helper_name not in sys.modules
        assert f"plugins.{name}" not in sys.modules
        assert name not in manager._owned_plugin_modules
    finally:
        release_exec.set()
        import_thread.join(2)
        await _force_cleanup(manager, name)
        sys.modules.pop(tracker_name, None)


@pytest.mark.asyncio
async def test_purge_cannot_delete_foreign_replacement_after_drain_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager    = _build_manager(tmp_path)
    name       = "integrity_purge_swap"
    plugin_dir = _write_plugin(manager, name, "VALUE = 'owned'\n")
    manager.load_plugin(plugin_dir)
    loaded = manager.get(name)
    assert loaded is not None
    package_name                      = f"plugins.{name}"
    entry_name                        = f"{package_name}.main"
    package                           = sys.modules[package_name]
    owned_entry                       = loaded.module
    foreign_entry                     = ModuleType(entry_name)
    original_barrier                  = manager._wait_for_module_import_barriers
    barrier_crossed                   = threading.Event()
    resume_purge                      = threading.Event()
    purge_errors: list[BaseException] = []

    def paused_barrier(module_names, *, timeout=5.0):  # type: ignore[no-untyped-def]
        original_barrier(module_names, timeout=timeout)
        barrier_crossed.set()
        if not resume_purge.wait(2):
            raise RuntimeError("test did not resume purge")

    monkeypatch.setattr(manager, "_wait_for_module_import_barriers", paused_barrier)

    def purge() -> None:
        try:
            manager._purge_plugin_modules(name)
        except BaseException as exc:
            purge_errors.append(exc)

    purge_thread = threading.Thread(target=purge)
    purge_thread.start()
    assert barrier_crossed.wait(2)
    sys.modules[entry_name] = foreign_entry
    ModuleType.__getattribute__(package, "__dict__")["main"] = foreign_entry
    resume_purge.set()
    purge_thread.join(2)

    try:
        assert not purge_thread.is_alive()
        assert len(purge_errors) == 1
        assert isinstance(purge_errors[0], PluginPathError)
        assert sys.modules[entry_name] is foreign_entry
        assert ModuleType.__getattribute__(package, "__dict__")["main"] is foreign_entry
        assert manager._owned_plugin_modules[name][entry_name] is owned_entry
        assert manager._owns_plugin_namespace(name)
    finally:
        resume_purge.set()
        sys.modules[entry_name] = owned_entry
        ModuleType.__getattribute__(package, "__dict__")["main"] = owned_entry
        await _force_cleanup(manager, name)


@pytest.mark.asyncio
async def test_reload_restore_collision_preserves_foreign_objects_and_quarantines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager                   = _build_manager(tmp_path)
    name                      = "integrity_restore_collision"
    tracker_name              = "xiaoqing_integrity_restore_collision_tracker"
    tracker                   = ModuleType(tracker_name)
    tracker.old_inits         = 0
    tracker.candidate_inits   = 0
    sys.modules[tracker_name] = tracker
    plugin_dir                = _write_plugin(
        manager,
        name,
        f"import {tracker_name} as tracker\n"
        "async def init(context=None):\n"
        "    tracker.old_inits += 1\n"
        "async def shutdown(context=None):\n"
        "    return None\n",
    )
    manager.load_plugin(plugin_dir)
    await manager.wait_inits()
    old_plugin = manager.get(name)
    assert old_plugin is not None
    assert tracker.old_inits == 1

    (plugin_dir / "main.py").write_text(
        f"import {tracker_name} as tracker\n"
        "async def init(context=None):\n"
        "    tracker.candidate_inits += 1\n"
        "    raise RuntimeError('candidate init failed')\n"
        "async def shutdown(context=None):\n"
        "    return None\n",
        encoding="utf-8",
    )
    package_name             = f"plugins.{name}"
    entry_name               = f"{package_name}.main"
    foreign_package          = ModuleType(package_name)
    foreign_package.__path__ = []  # type: ignore[attr-defined]
    foreign_entry            = ModuleType(entry_name)
    ModuleType.__getattribute__(foreign_package, "__dict__")["main"] = foreign_entry
    plugins_package  = sys.modules["plugins"]
    original_restore = manager._restore_generation_modules

    def collide_then_restore(plugin_name, modules, **kwargs):  # type: ignore[no-untyped-def]
        sys.modules[package_name] = foreign_package
        sys.modules[entry_name]   = foreign_entry
        ModuleType.__getattribute__(plugins_package, "__dict__")[name] = foreign_package
        return original_restore(plugin_name, modules, **kwargs)

    monkeypatch.setattr(manager, "_restore_generation_modules", collide_then_restore)
    try:
        await manager.reload_plugin(name)

        assert tracker.candidate_inits == 1
        assert tracker.old_inits == 1
        assert manager.get(name) is old_plugin
        assert name in manager._quarantined_plugins
        assert name in manager._restart_required_plugins
        assert manager._execution_gates[name].closed is True
        assert sys.modules[package_name] is foreign_package
        assert sys.modules[entry_name] is foreign_entry
        assert ModuleType.__getattribute__(plugins_package, "__dict__")[name] is foreign_package
        assert foreign_package not in manager._owned_plugin_modules.get(name, {}).values()
        assert foreign_entry not in manager._owned_plugin_modules.get(name, {}).values()
        assert manager._owns_plugin_namespace(name)
    finally:
        await _force_cleanup(manager, name)
        sys.modules.pop(tracker_name, None)


def test_restore_blocks_absent_fast_path_import_until_old_objects_are_committed(
    tmp_path: Path,
) -> None:
    manager    = _build_manager(tmp_path)
    name       = "integrity_restore_race"
    plugin_dir = _write_plugin(
        manager,
        name,
        "VALUE = 'candidate source must not execute during exact restore'\n",
    )
    definition = manager._load_definition(plugin_dir)
    assert definition is not None
    fingerprint = manager._capture_plugin_snapshot(plugin_dir, definition)
    manager._claim_plugin_namespace(name)

    package_name         = f"plugins.{name}"
    entry_name           = f"{package_name}.main"
    old_package          = ModuleType(package_name)
    old_package.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
    old_entry            = ModuleType(entry_name)
    ModuleType.__getattribute__(old_package, "__dict__")["main"] = old_entry
    modules                             = {package_name: old_package, entry_name: old_entry}
    modules_inserted                    = threading.Event()
    release_commit                      = threading.Event()
    import_done                         = threading.Event()
    import_results: list[ModuleType]    = []
    import_errors: list[BaseException]  = []
    restore_errors: list[BaseException] = []

    class BlockingOwnedLedger(dict[str, dict[str, ModuleType]]):
        def __setitem__(self, key: str, value: dict[str, ModuleType]) -> None:
            if key == name and value:
                # _restore_generation_modules writes this ledger only after it
                # has populated sys.modules and parent attributes, but while it
                # still owns every CPython module lock and marks specs as
                # initializing.  Freeze exactly inside that commit window.
                modules_inserted.set()
                if not release_commit.wait(2):
                    raise RuntimeError("test did not release restore commit")
            super().__setitem__(key, value)

    manager._owned_plugin_modules = BlockingOwnedLedger(manager._owned_plugin_modules)

    def import_entry() -> None:
        try:
            import_results.append(importlib.import_module(entry_name))
        except BaseException as exc:
            import_errors.append(exc)
        finally:
            import_done.set()

    def restore() -> None:
        try:
            manager._restore_generation_modules(
                name,
                modules,
                plugin_root = plugin_dir,
                sources     = fingerprint.sources,
            )
        except BaseException as exc:
            restore_errors.append(exc)

    restore_thread = threading.Thread(target=restore)
    restore_thread.start()
    assert modules_inserted.wait(2)
    import_thread = threading.Thread(target=import_entry)
    import_thread.start()

    try:
        assert not import_done.wait(0.1)
        release_commit.set()
        restore_thread.join(3)
        import_thread.join(3)
        assert not restore_thread.is_alive()
        assert not import_thread.is_alive()
        assert not restore_errors
        assert not import_errors
        assert import_results == [old_entry]
        assert sys.modules[package_name] is old_package
        assert sys.modules[entry_name] is old_entry
        assert manager._source_finders[name]._active is True
    finally:
        release_commit.set()
        restore_thread.join(3)
        import_thread.join(3)
        manager._purge_plugin_modules(name)
        manager._release_plugin_namespace(name)
