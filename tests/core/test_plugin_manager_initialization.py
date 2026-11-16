"""初始化、发布和失败收敛。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import tests.helpers.plugin_manager_test_support as _fixture_support
from core.models import _CAPABILITY_CONTRACTS
from tests.helpers.plugin_manager_test_support import (
    ConcurrentFuture,
    ModuleType,
    PluginExecutionGate,
    PluginLifecycleFatalError,
    PluginPathError,
    build_manager,
    load_plugin,
    load_plugin_dir,
    textwrap,
    write_plugin,
)
from tests.helpers.plugin_probe import PROBE

_isolate_process_global_plugin_import_state = (
    _fixture_support._isolate_process_global_plugin_import_state
)


@pytest.mark.asyncio
async def test_load_rejects_same_file_under_noncanonical_alias(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import sys

    manager                     = build_manager(tmp_path)
    plugin_dir                  = write_plugin(manager, "alias_demo")
    alias                       = ModuleType("alias_demo.main")
    alias.__file__              = str(plugin_dir / "main.py")
    sys.modules[alias.__name__] = alias
    try:
        await load_plugin_dir(manager, plugin_dir)

        assert "Non-canonical plugin module aliases" in caplog.text
        assert manager.get("alias_demo") is None
        assert PROBE.log("import") == []
    finally:
        sys.modules.pop(alias.__name__, None)
        manager._purge_plugin_modules("alias_demo")


def test_alias_scan_stats_only_lexical_plugin_candidates_and_bounds_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    manager    = build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "alias_demo"
    plugin_dir.mkdir()
    aliases: dict[str, ModuleType] = {}
    for index in range(3):
        source = plugin_dir / f"alias_{index}.py"
        source.write_text(f"VALUE = {index}\n", encoding="utf-8")
        module                   = ModuleType(f"alias_probe_{index}")
        module.__file__          = str(source)
        aliases[module.__name__] = module
    foreign_paths = {str(tmp_path / "foreign" / f"module_{index}.py") for index in range(20)}
    foreign_modules: dict[str, ModuleType] = {}
    for index, path in enumerate(sorted(foreign_paths)):
        module                           = ModuleType(f"foreign_probe_{index}")
        module.__file__                  = path
        foreign_modules[module.__name__] = module

    original_stat                 = Path.stat
    foreign_stat_calls: list[str] = []

    def tracked_stat(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(path) in foreign_paths:
            foreign_stat_calls.append(str(path))
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", tracked_stat)
    monkeypatch.setattr("core.plugin_generation._MAX_MODULE_ORIGIN_CACHE_ENTRIES", 2)
    sys.modules.update(aliases)
    sys.modules.update(foreign_modules)
    try:
        found = manager._plugin_module_aliases(plugin_dir, "alias_demo")
    finally:
        for module_name in (*aliases, *foreign_modules):
            sys.modules.pop(module_name, None)

    assert set(aliases).issubset(found)
    assert foreign_stat_calls == []
    assert len(manager._module_origin_cache) == 2


def test_purge_plugin_modules_preserves_unowned_alias_even_when_path_matches(tmp_path: Path):
    import sys

    manager    = build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "purge_demo"
    plugin_dir.mkdir()
    entry = plugin_dir / "main.py"
    entry.write_text("", encoding="utf-8")
    alias                       = ModuleType("purge_demo.main")
    alias.__file__              = str(entry)
    sys.modules[alias.__name__] = alias
    try:
        manager._purge_plugin_modules("purge_demo")
        assert sys.modules[alias.__name__] is alias
    finally:
        sys.modules.pop(alias.__name__, None)


def test_purge_uses_exact_raw_parent_bindings_without_module_hooks(tmp_path: Path) -> None:
    import sys

    manager = build_manager(tmp_path)

    class HookedPackage(ModuleType):
        hook_calls = 0

        def __getattribute__(self, name: str):  # type: ignore[no-untyped-def]
            if name == "__dict__":
                type(self).hook_calls += 1
            return super().__getattribute__(name)

        def __setattr__(self, name: str, value: object) -> None:
            type(self).hook_calls += 1
            super().__setattr__(name, value)

        def __delattr__(self, name: str) -> None:
            type(self).hook_calls += 1
            super().__delattr__(name)

    package                = HookedPackage("plugins.atomic_probe")
    module_a               = ModuleType("plugins.atomic_probe.a")
    module_b               = ModuleType("plugins.atomic_probe.b")
    sentinel               = object()
    package_namespace      = ModuleType.__getattribute__(package, "__dict__")
    package_namespace["a"] = sentinel
    package_namespace["b"] = module_b
    plugins_package        = sys.modules["plugins"]
    ModuleType.__getattribute__(plugins_package, "__dict__")["atomic_probe"] = package
    sys.modules[package.__name__]                   = package
    sys.modules[module_a.__name__]                  = module_a
    sys.modules[module_b.__name__]                  = module_b
    manager._private_plugin_modules["atomic_probe"] = {
        package.__name__ : package,
        module_a.__name__: module_a,
        module_b.__name__: module_b,
    }

    try:
        with pytest.raises(PluginPathError, match="parent binding is foreign"):
            manager._purge_plugin_modules("atomic_probe")

        assert sys.modules[package.__name__] is package
        assert sys.modules[module_a.__name__] is module_a
        assert sys.modules[module_b.__name__] is module_b
        assert plugins_package.atomic_probe is package
        assert package.a is sentinel
        assert package.b is module_b
        assert HookedPackage.hook_calls == 0

        package_namespace["a"] = module_a
        manager._purge_plugin_modules("atomic_probe")

        assert package.__name__ not in sys.modules
        assert module_a.__name__ not in sys.modules
        assert module_b.__name__ not in sys.modules
        assert "atomic_probe" not in ModuleType.__getattribute__(
            plugins_package,
            "__dict__",
        )
        assert HookedPackage.hook_calls == 0
    finally:
        manager._purge_plugin_modules("atomic_probe")


@pytest.mark.asyncio
async def test_undrained_partial_import_cannot_downgrade_restart_only_ledger(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)
    manager.configure_execution({"drain_timeout_seconds": 0.01})
    unfinished: ConcurrentFuture[None] = ConcurrentFuture()

    class GatesWithUnfinishedWork(dict[str, PluginExecutionGate]):
        """候选代的执行门一登记，就挂上一件做不完的同步工作：回滚时排不空。"""

        def __setitem__(self, name: str, gate: PluginExecutionGate) -> None:
            super().__setitem__(name, gate)
            if name == "demo" and not gate.closed:
                gate._sync_futures.add(unfinished)

    # 真实输入造不出“导入到一半、而候选代的执行门里还有没做完的同步工作”：
    # 只能在候选代执行门登记处挂上这件工作。
    manager._execution_gates = GatesWithUnfinishedWork(manager._execution_gates)
    write_plugin(
        manager,
        "demo",
        """
        from tests.helpers.plugin_probe import PROBE

        async def shutdown(context=None):
            PROBE.log("shutdown").append(1)

        raise RuntimeError("partial import")
        """,
    )

    await load_plugin_dir(manager, manager.plugins_dir / "demo")

    gate = manager._execution_gates["demo"]
    assert "demo" in manager._restart_required_plugins
    assert "demo" in manager._quarantined_plugins
    assert manager._plugins["demo"].module.__name__ == "plugins.demo.main"
    assert gate.closed is True

    unfinished.set_result(None)
    await asyncio.sleep(0)
    await manager.unload_plugin("demo")
    assert "demo" in manager.list_runtime_plugins()
    assert PROBE.log("shutdown") == []

    manager._restart_required_plugins.discard("demo")
    manager._quarantined_plugins.discard("demo")
    await manager.unload_plugin("demo")
    assert len(PROBE.log("shutdown")) == 1


@pytest.mark.asyncio
async def test_plugin_is_published_after_async_init_completes(tmp_path: Path):
    manager    = build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "plugin.json").write_text(
        textwrap.dedent(
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
        ).strip(),
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(
        textwrap.dedent(
            """
            import asyncio

            READY = False

            async def init(context=None):
                global READY
                await asyncio.sleep(0)
                READY = True

            async def handle(command, args, event, context):
                return [{"type": "text", "data": {"text": "ok"}}]
            """
        ).strip(),
        encoding="utf-8",
    )

    await load_plugin_dir(manager, plugin_dir)

    assert manager._plugins["demo"].module.READY is True
    resolved = manager.router.resolve("demo")
    assert resolved is not None
    assert resolved[0].execution_gate is manager._plugins["demo"].execution_gate


@pytest.mark.asyncio
async def test_init_context_carries_the_declarations_of_the_generation_being_loaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """init 拿到的能力来自正在加载的这一代：首次加载时不为空，热重载时不是上一代的。"""

    # 能力按插件名放行：让这个测试插件可以声明一项。
    monkeypatch.setitem(_CAPABILITY_CONTRACTS, "config_subscription", frozenset({"declares"}))
    manager                          = build_manager(tmp_path)
    issued: list[tuple[object, ...]] = []
    manager.context_factory          = lambda *args: issued.append(args) or object()
    plugin_dir                       = manager.plugins_dir / "declares"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "main.py").write_text("async def init(context):\n    pass\n", encoding="utf-8")

    def write_manifest(capabilities: str) -> None:
        (plugin_dir / "plugin.json").write_text(
            '{"name":"declares","version":"1.0.0","entry":"main.py","commands":[],'
            f'"schedule":[],"concurrency":"parallel","enabled":true,"capabilities":{capabilities}}}',
            encoding="utf-8",
        )

    write_manifest('["config_subscription"]')
    await load_plugin_dir(manager, plugin_dir)

    assert [args[8] for args in issued] == [frozenset({"config_subscription"})]

    write_manifest("[]")
    await manager.reload_plugin("declares")

    assert [args[8] for args in issued] == [frozenset({"config_subscription"}), frozenset()]


@pytest.mark.asyncio
async def test_async_init_with_kwargs_does_not_receive_positional_context(tmp_path: Path):
    manager    = build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "kwargs_init"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "plugin.json").write_text(
        '{"name":"kwargs_init","version":"1.0.0","entry":"main.py",'
        '"commands":[],"schedule":[],"concurrency":"parallel","enabled":true}',
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(
        "INIT_KWARGS = None\n"
        "async def init(**kwargs):\n"
        "    global INIT_KWARGS\n"
        "    INIT_KWARGS = kwargs\n",
        encoding="utf-8",
    )

    await load_plugin_dir(manager, plugin_dir)

    assert manager._plugins["kwargs_init"].module.INIT_KWARGS == {}


@pytest.mark.asyncio
async def test_async_init_system_exit_rolls_back_before_rethrow(tmp_path: Path) -> None:
    import sys

    manager    = build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "fatal_init"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "plugin.json").write_text(
        '{"name":"fatal_init","version":"1.0.0","entry":"main.py",'
        '"commands":[],"schedule":[],"concurrency":"parallel","enabled":true}',
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(
        "import xiaoqing_fatal_init_tracker as tracker\n"
        "async def init(context=None):\n"
        "    raise SystemExit('fatal init')\n"
        "async def shutdown(context=None):\n"
        "    tracker.shutdowns += 1\n",
        encoding="utf-8",
    )
    tracker                       = ModuleType("xiaoqing_fatal_init_tracker")
    tracker.shutdowns             = 0
    sys.modules[tracker.__name__] = tracker

    try:
        with pytest.raises(PluginLifecycleFatalError) as raised:
            await manager.load_all()
        assert isinstance(raised.value.original, SystemExit)
        assert str(raised.value.original) == "fatal init"

        assert tracker.shutdowns == 1
        assert "fatal_init" not in manager.list_runtime_plugins()
    finally:
        manager._purge_plugin_modules("fatal_init")
        sys.modules.pop(tracker.__name__, None)


@pytest.mark.asyncio
async def test_top_level_system_exit_is_deferred_until_rollback_finishes(
    tmp_path: Path,
) -> None:
    import sys

    manager    = build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "fatal_import"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "plugin.json").write_text(
        '{"name":"fatal_import","version":"1.0.0","entry":"main.py",'
        '"commands":[],"schedule":[],"concurrency":"parallel","enabled":true}',
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(
        "import xiaoqing_fatal_import_tracker as tracker\n"
        "async def shutdown(context=None):\n"
        "    tracker.shutdowns += 1\n"
        "raise SystemExit('fatal import')\n",
        encoding="utf-8",
    )
    tracker                       = ModuleType("xiaoqing_fatal_import_tracker")
    tracker.shutdowns             = 0
    sys.modules[tracker.__name__] = tracker

    try:
        with pytest.raises(PluginLifecycleFatalError) as raised:
            await manager.load_all()
        assert isinstance(raised.value.original, SystemExit)
        assert str(raised.value.original) == "fatal import"
        gate = manager._execution_gates["fatal_import"]
        assert gate.closed is True

        assert tracker.shutdowns == 1
        assert "fatal_import" in manager._restart_required_plugins
        assert "fatal_import" in manager._quarantined_plugins
        assert manager._execution_gates["fatal_import"] is gate
        assert gate.closed is True
    finally:
        manager._purge_plugin_modules("fatal_import")
        manager._restart_required_plugins.discard("fatal_import")
        manager._quarantined_plugins.discard("fatal_import")
        manager._plugins.pop("fatal_import", None)
        manager._plugin_states.pop("fatal_import", None)
        manager._execution_gates.pop("fatal_import", None)
        sys.modules.pop(tracker.__name__, None)


@pytest.mark.asyncio
async def test_shutdown_system_exit_quarantines_before_rethrow(tmp_path: Path) -> None:
    async def fatal_shutdown(_context) -> None:
        raise SystemExit("fatal shutdown")

    PROBE.slots["on_shutdown"] = fatal_shutdown
    manager                    = build_manager(tmp_path)
    plugin                     = await load_plugin(manager)
    gate                       = plugin.execution_gate
    state                      = manager._plugin_states["demo"]

    with pytest.raises(PluginLifecycleFatalError) as raised:
        await manager.unload_plugin("demo")
    assert isinstance(raised.value.original, SystemExit)
    assert str(raised.value.original) == "fatal shutdown"

    assert manager._plugins["demo"] is plugin
    assert manager._plugin_states["demo"] is state
    assert manager._execution_gates["demo"] is gate
    assert gate.closed is True
    assert plugin.shutdown_attempted is True
    assert plugin.shutdown_completed is False
    assert "demo" in manager._quarantined_plugins
