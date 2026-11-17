"""插件文件监听、快照指纹与增量协调。"""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

import tests.helpers.plugin_manager_test_support as _fixture_support
from tests.helpers.assertions import logged_events
from tests.helpers.plugin_manager_test_support import (
    HOOKED_PLUGIN_SOURCE,
    FatalLifecycleError,
    PluginDefinition,
    PluginLifecycleFatalError,
    build_manager,
    load_plugin,
    load_plugin_dir,
    write_plugin,
    write_runtime_manifest,
)
from tests.helpers.plugin_probe import PROBE

_isolate_process_global_plugin_import_state = (
    _fixture_support._isolate_process_global_plugin_import_state
)

# 关闭回调失败的一代：卸载它会被隔离。
_FAILING_SHUTDOWN_SOURCE = """
from tests.helpers.plugin_probe import PROBE

PROBE.log("import").append(1)

async def shutdown(context=None):
    PROBE.log("shutdown").append(1)
    raise RuntimeError("resource still live")
"""

# 每个版本的源码：导入时按名字记一笔，``VERSION`` 区分新旧。
_VERSIONED_SOURCE = """
from tests.helpers.plugin_probe import PROBE

VERSION = {version}
PROBE.log("{name}").append(VERSION)
"""


def versioned_source(name: str, version: int) -> str:
    return _VERSIONED_SOURCE.format(name=name, version=version)


def definition_of(manager, name: str = "demo") -> PluginDefinition:  # type: ignore[no-untyped-def]
    """磁盘上插件当前的清单经真实的清单读取得到的定义。"""

    definition = manager._load_definition(manager.plugins_dir / name)
    assert isinstance(definition, PluginDefinition)
    return definition


async def wait_until(condition, *, timeout: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition was not reached in time"
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_watch_does_not_auto_reload_quarantined_plugin(tmp_path: Path):
    manager     = build_manager(tmp_path)
    quarantined = await load_plugin(manager, "demo", _FAILING_SHUTDOWN_SOURCE)
    await manager.unload_plugin("demo")
    assert "demo" in manager._quarantined_plugins
    write_plugin(manager, "demo", versioned_source("demo", 2))
    # 磁盘上另有一个新插件：它被装入，就说明监听确实跑过了一轮。
    write_plugin(manager, "fresh", versioned_source("fresh", 1))
    manager.update_poll_interval(0.01)

    watcher = asyncio.create_task(manager.watch())
    try:
        await wait_until(lambda: manager.get("fresh") is not None)
    finally:
        watcher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watcher

    assert manager._plugins["demo"] is quarantined
    assert PROBE.log("demo") == []
    assert len(PROBE.log("import")) == 1
    assert manager._watch_waiters == {}


def test_plugin_watch_poll_interval_rejects_non_finite_and_non_positive_values(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)
    manager.update_poll_interval(0.25)

    for invalid in (0, -1, float("nan"), float("inf"), float("-inf"), True):
        manager.update_poll_interval(invalid)
        assert manager._poll_interval == 0.25

    manager.update_poll_interval(0.001)
    assert manager._poll_interval == 0.01


def observe_reconcile_rounds(manager, *, failures: list[BaseException] | None = None) -> list[int]:  # type: ignore[no-untyped-def]
    """记录监听每一轮协调的发生，随后照常调用原函数；``failures`` 里的异常依次在前几轮抛出。"""

    rounds: list[int] = []
    real_reconcile    = manager.reconcile_plugins

    async def reconcile() -> None:
        rounds.append(len(rounds) + 1)
        if failures:
            raise failures.pop(0)
        await real_reconcile()

    # 监听循环只认 ``reconcile_plugins``：在这一处记录每一轮，并在需要时注入失败。
    manager.reconcile_plugins = reconcile
    return rounds


@pytest.mark.asyncio
async def test_watch_interval_decrease_interrupts_old_long_wait(tmp_path: Path) -> None:
    manager = build_manager(tmp_path)
    manager.update_poll_interval(60)
    rounds  = observe_reconcile_rounds(manager)
    watcher = asyncio.create_task(manager.watch())
    for _ in range(20):
        if manager._watch_waiters:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("watcher did not register its managed interval waiter")

    manager.update_poll_interval(0.01)
    await wait_until(lambda: rounds)

    watcher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watcher
    assert manager._watch_waiters == {}


@pytest.mark.asyncio
async def test_watch_interval_increase_does_not_trigger_immediate_reconcile(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)
    manager.update_poll_interval(0.01)
    rounds  = observe_reconcile_rounds(manager)
    watcher = asyncio.create_task(manager.watch())
    await wait_until(lambda: rounds)
    rounds_before = len(rounds)

    manager.update_poll_interval(0.2)
    await asyncio.sleep(0.05)

    assert len(rounds) == rounds_before
    watcher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watcher
    assert manager._watch_waiters == {}


@pytest.mark.asyncio
async def test_watch_rate_limits_repeated_round_failures_and_recovers(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = build_manager(tmp_path)
    manager.update_poll_interval(0.01)
    rounds = observe_reconcile_rounds(
        manager,
        failures=[OSError("transient root scan failure") for _ in range(5)],
    )
    caplog.set_level("WARNING", logger="core.plugin_manager")
    watcher = asyncio.create_task(manager.watch())
    try:
        await wait_until(lambda: len(rounds) > 5)
        assert watcher.done() is False
        assert len(logged_events(caplog, "plugin.watch.round_failed")) == 1
    finally:
        watcher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watcher


@pytest.mark.asyncio
async def test_watch_propagates_fatal_lifecycle_carrier(tmp_path: Path) -> None:
    manager = build_manager(tmp_path)
    manager.update_poll_interval(0.01)
    fatal = PluginLifecycleFatalError("demo", FatalLifecycleError("fatal watch"))
    observe_reconcile_rounds(manager, failures=[fatal])

    with pytest.raises(PluginLifecycleFatalError) as raised:
        await asyncio.wait_for(manager.watch(), timeout=1)

    assert raised.value is fatal


@pytest.mark.asyncio
async def test_reconcile_path_stat_failure_preserves_runtime_and_processes_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager      = build_manager(tmp_path)
    old_plugin   = await load_plugin(manager, "a_inaccessible")
    inaccessible = manager.plugins_dir / "a_inaccessible"
    write_plugin(manager, "b_healthy", versioned_source("healthy", 1))
    original_lstat = Path.lstat

    def flaky_lstat(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if path == inaccessible:
            raise PermissionError("stat denied")
        return original_lstat(path, *args, **kwargs)

    # 操作系统边界：这个目录的元数据读取被拒绝，监听既不能当它不存在，也不能放弃同级插件。
    monkeypatch.setattr(Path, "lstat", flaky_lstat)

    await manager.reconcile_plugins()

    assert manager._plugins["a_inaccessible"] is old_plugin
    assert old_plugin.execution_gate.closed is False
    assert PROBE.log("shutdown") == []
    healthy = manager.get("b_healthy")
    assert healthy is not None
    assert healthy.module.VERSION == 1


@pytest.mark.asyncio
async def test_reconcile_incomplete_root_iteration_preserves_unknown_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager          = build_manager(tmp_path)
    old_plugin       = await load_plugin(manager, "a_unseen")
    healthy          = write_plugin(manager, "b_healthy", versioned_source("healthy", 1))
    original_iterdir = Path.iterdir

    def flaky_iterdir(path: Path):
        if path != manager.plugins_dir:
            return original_iterdir(path)

        def incomplete():
            yield healthy
            raise OSError("plugin root changed during iteration")

        return incomplete()

    # 操作系统边界：插件根目录遍历到一半失败，只见到了同级插件。
    monkeypatch.setattr(Path, "iterdir", flaky_iterdir)

    await manager.reconcile_plugins()

    assert manager._plugins["a_unseen"] is old_plugin
    assert old_plugin.execution_gate.closed is False
    assert PROBE.log("shutdown") == []
    loaded_healthy = manager.get("b_healthy")
    assert loaded_healthy is not None
    assert loaded_healthy.module.VERSION == 1


@pytest.mark.asyncio
async def test_reconcile_deleted_plugin_failure_does_not_block_siblings_and_stays_quarantined(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = build_manager(tmp_path)
    broken  = await load_plugin(manager, "a_broken")
    await load_plugin(manager, "b_deleted")
    write_plugin(manager, "c_healthy", versioned_source("healthy", 1))
    shutil.rmtree(manager.plugins_dir / "a_broken")
    shutil.rmtree(manager.plugins_dir / "b_deleted")
    real_purge = manager._purge_plugin_modules

    def purge(name: str) -> None:
        if name == "a_broken":
            raise RuntimeError("purge failed")
        real_purge(name)

    # 真实输入走不到清理本身失败：只能在清理处对这一个插件注入失败，其余照常。
    manager._purge_plugin_modules = purge

    await manager.reconcile_plugins()

    # 失败的那个被隔离，不拖累同级：别的被删插件照常退役，新插件照常装入。
    assert manager._plugins["a_broken"] is broken
    assert "a_broken" in manager._quarantined_plugins
    assert "b_deleted" not in manager.list_runtime_plugins()
    healthy = manager.get("c_healthy")
    assert healthy is not None
    assert healthy.module.VERSION == 1

    caplog.clear()
    await manager.reconcile_plugins()

    # 被隔离的一代不会被自动重试退役，等运维处理。
    assert manager._plugins["a_broken"] is broken
    (kept,) = logged_events(caplog, "plugin.watch.quarantined_deleted")
    assert kept.fields["plugin"] == "a_broken"


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest_error", [FileNotFoundError, PermissionError])
async def test_reconcile_manifest_read_failure_does_not_block_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest_error: type[OSError],
) -> None:
    manager      = build_manager(tmp_path)
    inaccessible = manager.plugins_dir / "a_inaccessible"
    healthy      = manager.plugins_dir / "b_healthy"
    for plugin_dir in (inaccessible, healthy):
        plugin_dir.mkdir()
        (plugin_dir / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
        write_runtime_manifest(plugin_dir, plugin_dir.name)
    original_open = Path.open

    def flaky_open(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == inaccessible / "plugin.json" and mode == "rb":
            raise manifest_error("manifest read failed")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", flaky_open)

    await manager.reconcile_plugins()

    assert manager.get("a_inaccessible") is None
    loaded_healthy = manager.get("b_healthy")
    assert loaded_healthy is not None
    assert loaded_healthy.module.VALUE == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest_state", ["missing", "invalid"])
async def test_watcher_manifest_errors_are_rate_limited_but_manual_loads_are_not(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    manifest_state: str,
) -> None:
    manager    = build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "broken"
    plugin_dir.mkdir()
    if manifest_state == "invalid":
        (plugin_dir / "plugin.json").write_text("{invalid", encoding="utf-8")
    expected = (
        "plugin.manifest.missing" if manifest_state == "missing" else "plugin.manifest.parse_failed"
    )
    caplog.set_level("WARNING", logger="core.plugin_manager")

    for _ in range(5):
        await manager.reconcile_plugins()

    assert len(logged_events(caplog, expected)) == 1

    caplog.clear()
    manager._load_definition(plugin_dir)
    manager._load_definition(plugin_dir)
    assert len(logged_events(caplog, expected)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["scandir", "open"])
async def test_reconcile_fingerprint_race_isolated_to_one_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    manager = build_manager(tmp_path)
    racy    = await load_plugin(manager, "a_racy", versioned_source("a_racy", 1))
    changed = await load_plugin(manager, "b_changed", versioned_source("b_changed", 1))
    # 两个插件的源码都改了：监听都要重新取指纹，只有 a_racy 的读取撞上了竞态。
    racy_dir = write_plugin(manager, "a_racy", versioned_source("a_racy", 2))
    write_plugin(manager, "b_changed", versioned_source("b_changed", 2))
    if failure_stage == "scandir":
        original_scandir = os.scandir

        def flaky_scandir(path):  # type: ignore[no-untyped-def]
            if Path(path) == racy_dir:
                raise OSError("directory replaced during scan")
            return original_scandir(path)

        monkeypatch.setattr(os, "scandir", flaky_scandir)
    else:
        original_open = Path.open
        failed_path   = racy_dir / "main.py"

        def flaky_open(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
            if path == failed_path:
                raise FileNotFoundError("file renamed before open")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", flaky_open)

    await manager.reconcile_plugins()

    # 指纹失败的插件保持原样、等下一轮；同级插件照常换到新一代。
    assert manager._plugins["a_racy"] is racy
    assert PROBE.log("a_racy") == [1]
    assert manager._plugins["b_changed"] is not changed
    assert manager._plugins["b_changed"].module.VERSION == 2


@pytest.mark.asyncio
async def test_reconcile_transient_fingerprint_failure_recovers_next_round(
    tmp_path: Path,
) -> None:
    manager    = build_manager(tmp_path)
    old_plugin = await load_plugin(manager, "demo", versioned_source("demo", 1))
    write_plugin(manager, "demo", versioned_source("demo", 2))
    real_capture = manager._capture_plugin_snapshot_async
    failures     = [OSError("file replaced during read")]

    async def capture(plugin_dir, definition, **kwargs):  # type: ignore[no-untyped-def]
        if failures:
            raise failures.pop()
        return await real_capture(plugin_dir, definition, **kwargs)

    # 文件在读取中途被换掉的竞态无法稳定地用真实输入造出来：只能在取指纹处注入失败（只一次）。
    manager._capture_plugin_snapshot_async = capture

    await manager.reconcile_plugins()
    assert manager._plugins["demo"] is old_plugin
    assert PROBE.log("demo") == [1]

    await manager.reconcile_plugins()
    assert manager._plugins["demo"] is not old_plugin
    assert manager._plugins["demo"].module.VERSION == 2
    assert PROBE.log("demo") == [1, 2]


@pytest.mark.asyncio
async def test_reload_authorization_snapshot_runs_off_event_loop(tmp_path: Path) -> None:
    manager = build_manager(tmp_path)
    old     = await load_plugin(manager, "demo", versioned_source("demo", 1))
    write_plugin(manager, "demo", versioned_source("demo", 2))
    real_snapshot           = manager._authorize_plugin_snapshot
    started                 = threading.Event()
    release                 = threading.Event()
    blocked_at: list[float] = []

    def blocking_fingerprint(plugin_dir: Path, definition: PluginDefinition):
        # 屏障：第一次取指纹停在这里，放行后照常调用原函数。
        if not blocked_at:
            blocked_at.append(time.perf_counter())
            started.set()
            release.wait(timeout=1)
        return real_snapshot(plugin_dir, definition)

    manager._authorize_plugin_snapshot = blocking_fingerprint
    safety_release                     = threading.Timer(0.5, release.set)
    safety_release.start()
    task = asyncio.create_task(manager.reload_plugin("demo"))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        # 取指纹被拦住期间事件循环仍然在转。
        assert time.perf_counter() - blocked_at[0] < 0.3
        release.set()
        await asyncio.wait_for(task, timeout=2)
    finally:
        safety_release.cancel()
        release.set()

    assert manager._plugins["demo"] is not old
    assert manager._plugins["demo"].module.VERSION == 2


def test_capture_plugin_snapshot_rejects_atomic_replace_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = build_manager(tmp_path)
    plugin_dir = write_plugin(manager, source="VALUE = 1\n")
    definition    = definition_of(manager)
    main          = plugin_dir / "main.py"
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
                "st_dev"     : metadata.st_dev,
                "st_ino"     : metadata.st_ino + 1,
                "st_size"    : metadata.st_size,
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
    manager = build_manager(tmp_path)
    plugin_dir = write_plugin(manager, source="VALUE = 1\n")
    definition = definition_of(manager)
    first      = manager._capture_plugin_snapshot(plugin_dir, definition)

    stable = await manager._capture_plugin_snapshot_async(
        plugin_dir,
        definition,
        previous=first,
    )

    # 元数据没变、上一次的指纹还新鲜：直接复用那一个对象，不重新散列。
    assert stable is first

    (plugin_dir / "main.py").write_text("VALUE = 200\n", encoding="utf-8")
    changed = await manager._capture_plugin_snapshot_async(
        plugin_dir,
        definition,
        previous=first,
    )

    assert changed is not first
    assert changed != first
    assert changed.sources["main.py"].decode().strip() == "VALUE = 200"


@pytest.mark.asyncio
async def test_reconcile_rejects_cross_file_mixed_fingerprint_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = build_manager(tmp_path)
    monkeypatch.setattr(
        "core.plugin_watcher._PLUGIN_FINGERPRINT_AUDIT_INTERVAL_SECONDS",
        0.0,
    )
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    first       = plugin_dir / "a.py"
    second      = plugin_dir / "b.py"
    replacement = plugin_dir / "replacement.tmp"
    first.write_text(HOOKED_PLUGIN_SOURCE, encoding="utf-8")
    second.write_text("VALUE = 1\n", encoding="utf-8")
    replacement.write_text(HOOKED_PLUGIN_SOURCE + "\nVALUE = 2\n", encoding="utf-8")
    write_runtime_manifest(plugin_dir, "demo", "a.py")
    await load_plugin_dir(manager, plugin_dir)
    old_plugin = manager.get("demo")
    assert old_plugin is not None
    original_open = Path.open
    replaced      = False

    def replace_first_before_reading_second(path: Path, *args, **kwargs):
        nonlocal replaced
        handle = original_open(path, *args, **kwargs)
        if path == second and not replaced:
            replaced = True
            os.replace(replacement, first)
        return handle

    # 操作系统边界：读第二个文件时，第一个文件被原子换成了新内容。
    monkeypatch.setattr(Path, "open", replace_first_before_reading_second)

    await manager.reconcile_plugins()

    assert replaced is True
    assert manager._plugins["demo"] is old_plugin
    assert len(PROBE.log("import")) == 1


def test_iter_watch_files_traverses_data_named_source_directory(tmp_path: Path) -> None:
    data_ancestor = tmp_path / "data"
    data_ancestor.mkdir()
    manager = build_manager(data_ancestor)
    plugin_dir = write_plugin(manager, source="VALUE = 1\n")
    definition   = definition_of(manager)
    main         = plugin_dir / "main.py"
    helper       = plugin_dir / "helper.py"
    runtime_data = plugin_dir / "data"
    runtime_data.mkdir()
    state         = runtime_data / "state.json"
    nested_source = runtime_data / "helper.py"
    helper.write_text("HELPER = 1\n", encoding="utf-8")
    state.write_text("{}", encoding="utf-8")
    nested_source.write_text("NESTED = 1\n", encoding="utf-8")

    files = manager._iter_watch_files(plugin_dir, definition)

    assert main in files
    assert helper in files
    assert nested_source in files
    assert state not in files


def test_fingerprint_prunes_bytecode_before_descent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = build_manager(tmp_path)
    plugin_dir = write_plugin(manager, source="VALUE = 1\n")
    definition = definition_of(manager)
    cache_dir  = plugin_dir / "__pycache__"
    cache_dir.mkdir()
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
