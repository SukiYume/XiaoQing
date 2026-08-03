"""初始化、发布和失败收敛。"""

from __future__ import annotations

import tests.helpers.plugin_manager_test_support as _fixture_support
from tests.helpers.plugin_manager_test_support import (
    AsyncMock,
    ConcurrentFuture,
    LoadedPlugin,
    Mock,
    ModuleType,
    Path,
    PluginExecutionGate,
    PluginExecutionPolicy,
    PluginLifecycleFatalError,
    PluginPathError,
    _build_definition,
    _build_manager,
    _FatalLifecycleError,
    _write_runtime_manifest,
    asyncio,
    call_plugin_callback,
    pytest,
    textwrap,
    threading,
)

_isolate_process_global_plugin_import_state = (
    _fixture_support._isolate_process_global_plugin_import_state
)


def test_load_all_is_sorted_and_isolates_one_bad_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _build_manager(tmp_path)
    for name in ("zeta", "broken", "unreadable", "alpha"):
        (manager.plugins_dir / name).mkdir()
    loaded: list[str] = []
    original_classifier = manager._is_plugin_dir

    def classify(path: Path) -> bool:
        if path.name == "unreadable":
            raise OSError("metadata unavailable")
        return original_classifier(path)

    def load(path: Path) -> None:
        loaded.append(path.name)
        if path.name == "broken":
            raise ValueError("plugin-local failure")

    monkeypatch.setattr(manager, "_is_plugin_dir", classify)
    monkeypatch.setattr(manager, "load_plugin", load)

    manager.load_all()

    assert loaded == ["alpha", "broken", "zeta"]


@pytest.mark.asyncio
async def test_load_all_uses_initialization_barrier_inside_event_loop(tmp_path: Path) -> None:
    manager = _build_manager(tmp_path)
    manager._load_all_async = AsyncMock()

    manager.load_all()
    await manager.wait_inits()

    manager._load_all_async.assert_awaited_once()


def test_load_module_rejects_same_file_under_noncanonical_alias(tmp_path: Path):
    import sys

    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "alias_demo"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    entry = plugin_dir / "main.py"
    entry.write_text("VALUE = 1\n", encoding="utf-8")
    _write_runtime_manifest(plugin_dir, "alias_demo")
    alias = ModuleType("alias_demo.main")
    alias.__file__ = str(entry)
    sys.modules[alias.__name__] = alias
    try:
        with pytest.raises(Exception, match="Non-canonical plugin module aliases"):
            manager._load_module(plugin_dir, _build_definition("alias_demo"))
    finally:
        sys.modules.pop(alias.__name__, None)
        manager._purge_plugin_modules("alias_demo")


def test_alias_scan_stats_only_lexical_plugin_candidates_and_bounds_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "alias_demo"
    plugin_dir.mkdir()
    aliases: dict[str, ModuleType] = {}
    for index in range(3):
        source = plugin_dir / f"alias_{index}.py"
        source.write_text(f"VALUE = {index}\n", encoding="utf-8")
        module = ModuleType(f"alias_probe_{index}")
        module.__file__ = str(source)
        aliases[module.__name__] = module
    foreign_paths = {str(tmp_path / "foreign" / f"module_{index}.py") for index in range(20)}
    foreign_modules: dict[str, ModuleType] = {}
    for index, path in enumerate(sorted(foreign_paths)):
        module = ModuleType(f"foreign_probe_{index}")
        module.__file__ = path
        foreign_modules[module.__name__] = module

    original_stat = Path.stat
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

    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "purge_demo"
    plugin_dir.mkdir()
    entry = plugin_dir / "main.py"
    entry.write_text("", encoding="utf-8")
    alias = ModuleType("purge_demo.main")
    alias.__file__ = str(entry)
    sys.modules[alias.__name__] = alias
    try:
        manager._purge_plugin_modules("purge_demo")
        assert sys.modules[alias.__name__] is alias
    finally:
        sys.modules.pop(alias.__name__, None)


def test_purge_uses_exact_raw_parent_bindings_without_module_hooks(tmp_path: Path) -> None:
    import sys

    manager = _build_manager(tmp_path)

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

    package = HookedPackage("plugins.atomic_probe")
    module_a = ModuleType("plugins.atomic_probe.a")
    module_b = ModuleType("plugins.atomic_probe.b")
    sentinel = object()
    package_namespace = ModuleType.__getattribute__(package, "__dict__")
    package_namespace["a"] = sentinel
    package_namespace["b"] = module_b
    plugins_package = sys.modules["plugins"]
    ModuleType.__getattribute__(plugins_package, "__dict__")["atomic_probe"] = package
    sys.modules[package.__name__] = package
    sys.modules[module_a.__name__] = module_a
    sys.modules[module_b.__name__] = module_b
    manager._private_plugin_modules["atomic_probe"] = {
        package.__name__: package,
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
@pytest.mark.parametrize("shutdown_fails", [False, True])
async def test_wait_inits_rolls_back_never_published_plugin_once(
    tmp_path: Path,
    shutdown_fails: bool,
) -> None:
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    module = ModuleType("plugins.demo.main")
    shutdown_calls = 0

    async def shutdown(context=None) -> None:
        nonlocal shutdown_calls
        shutdown_calls += 1
        if shutdown_fails:
            raise RuntimeError("cleanup failed")

    module.shutdown = shutdown
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    state = {"resource": object()}

    async def fail_init() -> None:
        raise RuntimeError("init failed after acquiring resource")

    init_task = asyncio.create_task(fail_init())
    manager._init_tasks.append(init_task)
    manager._init_task_plugins[init_task] = "demo"
    manager._pending_plugins[init_task] = (definition, module, 0.0)
    manager._execution_gates["demo"] = gate
    manager._plugin_states["demo"] = state

    try:
        await manager.wait_inits()

        assert shutdown_calls == 1
        assert gate.closed is True
        assert not manager._init_tasks
        assert not manager._init_task_plugins
        assert not manager._pending_plugins
        assert manager.router.resolve("demo") is None
        if shutdown_fails:
            assert manager._plugins["demo"].module is module
            assert manager._plugin_states["demo"] is state
            assert manager._execution_gates["demo"] is gate
            assert "demo" in manager._quarantined_plugins
            plugin_dir = manager.plugins_dir / "demo"
            plugin_dir.mkdir(exist_ok=True)
            manager._load_module = Mock()
            manager.load_plugin(plugin_dir)
            await manager.reconcile_plugins()
            manager._load_module.assert_not_called()
            assert manager._plugins["demo"].module is module
            assert manager._plugin_states["demo"] is state
            assert manager._execution_gates["demo"] is gate
        else:
            assert "demo" not in manager._plugins
            assert "demo" not in manager._plugin_states
            assert "demo" not in manager._execution_gates
            assert "demo" not in manager._quarantined_plugins
    finally:
        if "demo" in manager._plugins:
            module.shutdown = AsyncMock()
            manager._quarantined_plugins.discard("demo")
            await manager.unload_plugin("demo")
        else:
            await gate.close()
            manager._plugin_states.pop("demo", None)
            manager._execution_gates.pop("demo", None)


@pytest.mark.asyncio
async def test_failed_init_with_undrained_sync_work_remains_quarantined(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    module = ModuleType("plugins.demo.main")
    gate = PluginExecutionGate(
        "parallel",
        plugin_name="demo",
        policy=PluginExecutionPolicy(
            timeout_seconds=0.1,
            drain_timeout_seconds=0.1,
        ),
    )
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    shutdown_calls = 0

    def blocking_init_work() -> None:
        started.set()
        release.wait(timeout=2)
        finished.set()

    async def shutdown(context=None) -> None:
        nonlocal shutdown_calls
        shutdown_calls += 1

    module.shutdown = shutdown

    async def initialize() -> None:
        await gate.run(lambda: call_plugin_callback(blocking_init_work))

    init_task = asyncio.create_task(initialize())
    manager._init_tasks.append(init_task)
    manager._init_task_plugins[init_task] = "demo"
    manager._pending_plugins[init_task] = (definition, module, 0.0)
    state = {"resource": object()}
    manager._plugin_states["demo"] = state
    manager._execution_gates["demo"] = gate

    try:
        await manager.wait_inits()

        assert started.is_set()
        assert shutdown_calls == 0
        assert manager._plugins["demo"].module is module
        assert manager._plugin_states["demo"] is state
        assert manager._execution_gates["demo"] is gate
        assert gate.closed is True
        assert "demo" in manager._quarantined_plugins

        plugin_dir = manager.plugins_dir / "demo"
        plugin_dir.mkdir()
        manager._load_module = Mock()
        manager.load_plugin(plugin_dir)
        await manager.reconcile_plugins()
        manager._load_module.assert_not_called()
        assert manager._plugins["demo"].module is module
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        await asyncio.sleep(0)
        manager._quarantined_plugins.discard("demo")
        await manager.unload_plugin("demo")

    assert shutdown_calls == 1
    assert "demo" not in manager._plugins


@pytest.mark.asyncio
async def test_undrained_partial_import_cannot_downgrade_restart_only_ledger(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    module = ModuleType("plugins.demo.main")
    module.shutdown = AsyncMock()
    gate = PluginExecutionGate(
        "parallel",
        plugin_name="demo",
        policy=PluginExecutionPolicy(drain_timeout_seconds=0.01),
    )
    unfinished: ConcurrentFuture[None] = ConcurrentFuture()
    gate._sync_futures.add(unfinished)
    manager._plugin_states["demo"] = {"resource": object()}
    manager._execution_gates["demo"] = gate

    clean = await manager._rollback_pending_plugin(
        definition,
        module,
        0.0,
        retain_quarantine=True,
    )

    assert clean is False
    assert "demo" in manager._restart_required_plugins
    assert "demo" in manager._quarantined_plugins
    assert manager._plugins["demo"].module is module
    assert gate.closed is True

    unfinished.set_result(None)
    await asyncio.sleep(0)
    await manager.unload_plugin("demo")
    assert "demo" in manager.list_runtime_plugins()
    module.shutdown.assert_not_awaited()

    manager._restart_required_plugins.discard("demo")
    manager._quarantined_plugins.discard("demo")
    await manager.unload_plugin("demo")
    module.shutdown.assert_awaited_once()


def test_load_plugin_never_overwrites_quarantined_generation(tmp_path: Path) -> None:
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        '{"name":"demo","entry":"main.py","enabled":true}',
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    quarantined = LoadedPlugin(
        definition=_build_definition(),
        module=ModuleType("plugins.demo.main"),
        mtime=0.0,
        execution_gate=PluginExecutionGate("parallel", plugin_name="demo"),
    )
    manager._plugins["demo"] = quarantined
    manager._execution_gates["demo"] = quarantined.execution_gate
    manager._quarantined_plugins.add("demo")
    manager._load_module = Mock()

    manager.load_plugin(plugin_dir)

    assert manager._plugins["demo"] is quarantined
    manager._load_module.assert_not_called()


def test_load_plugin_fingerprints_before_importing_or_starting_init(tmp_path: Path) -> None:
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    definition = _build_definition()
    manager._load_definition = Mock(return_value=definition)
    manager._authorize_plugin_snapshot = Mock(side_effect=OSError("stat failed"))
    manager._load_module = Mock(return_value=(ModuleType("plugins.demo.main"), None))

    manager.load_plugin(plugin_dir)

    manager._load_module.assert_not_called()
    assert not manager._init_tasks
    assert not manager._init_task_plugins
    assert not manager._pending_plugins
    assert "demo" not in manager._plugins
    assert "demo" not in manager._execution_gates
    assert "demo" not in manager._plugin_states


def test_sync_load_preserves_original_fatal_over_ordinary_cleanup_failure(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    definition = _build_definition()
    module = ModuleType("plugins.demo.main")
    expected = _FatalLifecycleError("fatal import")
    manager._load_definition = Mock(return_value=definition)
    manager._authorize_plugin_snapshot = Mock(return_value=1.0)

    def fail_load(_plugin_dir, _definition, *, transaction):
        transaction.module = module
        raise expected

    manager._load_module = Mock(side_effect=fail_load)
    manager._rollback_pending_plugin = AsyncMock(side_effect=RuntimeError("cleanup failed"))

    with pytest.raises(_FatalLifecycleError) as raised:
        manager.load_plugin(plugin_dir)

    assert raised.value is expected
    assert isinstance(raised.value.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_load_plugin_rolls_back_no_init_publication_failure(tmp_path: Path) -> None:
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
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
    manager._load_definition = Mock(return_value=definition)
    manager._authorize_plugin_snapshot = Mock(return_value=1.0)
    manager._load_module = Mock(return_value=(module, None))
    replace_plugin = manager.router.replace_plugin
    failed = False

    def fail_first_publish(name, specs):
        nonlocal failed
        previous = replace_plugin(name, specs)
        if specs and not failed:
            failed = True
            raise RuntimeError("router commit failed")
        return previous

    manager.router.replace_plugin = Mock(side_effect=fail_first_publish)

    manager.load_plugin(plugin_dir)
    await manager.wait_inits()

    assert shutdown_calls == 1
    assert manager.router.resolve("demo") is None
    assert "demo" not in manager._plugins
    assert "demo" not in manager._execution_gates
    assert "demo" not in manager._plugin_states
    assert not manager._pending_finalizers


@pytest.mark.asyncio
async def test_wait_inits_rolls_back_when_publication_fails(tmp_path: Path) -> None:
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    module = ModuleType("plugins.demo.main")
    shutdown_calls = 0

    async def shutdown(context=None) -> None:
        nonlocal shutdown_calls
        shutdown_calls += 1

    module.shutdown = shutdown
    gate = PluginExecutionGate("parallel", plugin_name="demo")

    async def successful_init() -> None:
        return None

    init_task = asyncio.create_task(successful_init())
    manager._init_tasks.append(init_task)
    manager._init_task_plugins[init_task] = "demo"
    manager._pending_plugins[init_task] = (definition, module, 0.0)
    manager._execution_gates["demo"] = gate
    manager._plugin_states["demo"] = {"resource": object()}
    manager._register_loaded_plugin = Mock(side_effect=RuntimeError("publish failed"))

    await manager.wait_inits()

    assert shutdown_calls == 1
    assert gate.closed is True
    assert "demo" not in manager._plugins
    assert "demo" not in manager._execution_gates
    assert "demo" not in manager._plugin_states
    assert manager.router.resolve("demo") is None


@pytest.mark.asyncio
async def test_one_publication_failure_does_not_block_other_init_commits(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    bad_definition = _build_definition("bad")
    good_definition = _build_definition("good")
    bad_module = ModuleType("plugins.bad.main")
    good_module = ModuleType("plugins.good.main")
    bad_shutdown_calls = 0

    async def bad_shutdown(context=None) -> None:
        nonlocal bad_shutdown_calls
        bad_shutdown_calls += 1

    async def successful_init() -> None:
        return None

    bad_module.shutdown = bad_shutdown
    original_register = manager._register_loaded_plugin

    def publish(definition, module, mtime) -> None:
        if definition.name == "bad":
            raise RuntimeError("publish failed")
        original_register(definition, module, mtime)

    manager._register_loaded_plugin = Mock(side_effect=publish)
    for definition, module in (
        (bad_definition, bad_module),
        (good_definition, good_module),
    ):
        task = asyncio.create_task(successful_init())
        manager._init_tasks.append(task)
        manager._init_task_plugins[task] = definition.name
        manager._pending_plugins[task] = (definition, module, 0.0)
        manager._execution_gates[definition.name] = PluginExecutionGate(
            "parallel", plugin_name=definition.name
        )
        manager._plugin_states[definition.name] = {}

    await manager.wait_inits()

    assert bad_shutdown_calls == 1
    assert "bad" not in manager._plugins
    assert manager._plugins["good"].module is good_module
    assert not manager._init_task_plugins
    assert not manager._pending_plugins
    assert not manager._pending_finalizers

    await manager.unload_plugin("good")


@pytest.mark.asyncio
async def test_concurrent_wait_inits_share_the_same_inflight_batch(tmp_path: Path) -> None:
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    module = ModuleType("plugins.demo.main")
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    init_started = asyncio.Event()
    release_init = asyncio.Event()

    async def initialize() -> None:
        init_started.set()
        await release_init.wait()

    init_task = asyncio.create_task(initialize())
    manager._init_tasks.append(init_task)
    manager._init_task_plugins[init_task] = "demo"
    manager._pending_plugins[init_task] = (definition, module, 0.0)
    manager._plugin_states["demo"] = {}
    manager._execution_gates["demo"] = gate

    first_waiter = asyncio.create_task(manager.wait_inits())
    await asyncio.wait_for(init_started.wait(), timeout=1)
    second_waiter = asyncio.create_task(manager.wait_inits())
    await asyncio.sleep(0)

    assert second_waiter.done() is False
    assert "demo" not in manager._plugins

    release_init.set()
    await asyncio.gather(first_waiter, second_waiter)

    assert manager._plugins["demo"].module is module
    assert manager._execution_gates["demo"].closed is False
    await manager.unload_plugin("demo")


@pytest.mark.asyncio
async def test_completed_fatal_finalizer_is_retained_until_wait_inits_observes_it(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    module = ModuleType("plugins.demo.main")
    manager._rollback_pending_plugin = AsyncMock(side_effect=_FatalLifecycleError("fatal cleanup"))

    manager._start_pending_rollback((definition, module, 0.0))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert "demo" in manager._pending_finalizers
    with pytest.raises(PluginLifecycleFatalError) as raised:
        await manager.wait_inits()
    assert isinstance(raised.value.original, _FatalLifecycleError)
    assert str(raised.value.original) == "fatal cleanup"
    assert not manager._pending_finalizers


@pytest.mark.asyncio
async def test_wait_inits_waits_all_finalizers_before_raising_fatal(tmp_path: Path) -> None:
    manager = _build_manager(tmp_path)
    release_slow_cleanup = asyncio.Event()

    async def rollback(definition, _module, _mtime, **_kwargs) -> bool:
        if definition.name == "fatal":
            raise _FatalLifecycleError("fatal cleanup")
        await release_slow_cleanup.wait()
        return True

    manager._rollback_pending_plugin = AsyncMock(side_effect=rollback)
    manager._start_pending_rollback(
        (_build_definition("fatal"), ModuleType("plugins.fatal.main"), 0.0)
    )
    manager._start_pending_rollback(
        (_build_definition("slow"), ModuleType("plugins.slow.main"), 0.0)
    )

    waiter = asyncio.create_task(manager.wait_inits())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert waiter.done() is False

    release_slow_cleanup.set()
    with pytest.raises(PluginLifecycleFatalError) as raised:
        await waiter
    assert isinstance(raised.value.original, _FatalLifecycleError)
    assert str(raised.value.original) == "fatal cleanup"
    assert not manager._pending_finalizers


@pytest.mark.asyncio
async def test_cancelling_wait_inits_observer_does_not_cancel_shared_batch(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    module = ModuleType("plugins.demo.main")
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    init_started = asyncio.Event()
    release_init = asyncio.Event()

    async def initialize() -> None:
        init_started.set()
        await release_init.wait()

    init_task = asyncio.create_task(initialize())
    manager._init_tasks.append(init_task)
    manager._init_task_plugins[init_task] = "demo"
    manager._pending_plugins[init_task] = (definition, module, 0.0)
    manager._plugin_states["demo"] = {}
    manager._execution_gates["demo"] = gate

    owner = asyncio.create_task(manager.wait_inits())
    await asyncio.wait_for(init_started.wait(), timeout=1)
    observer = asyncio.create_task(manager.wait_inits())
    await asyncio.sleep(0)
    observer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observer

    assert init_task.cancelled() is False
    assert owner.done() is False
    release_init.set()
    await asyncio.wait_for(owner, timeout=1)

    assert manager._plugins["demo"].module is module
    assert manager._execution_gates["demo"].closed is False
    await manager.unload_plugin("demo")


@pytest.mark.asyncio
async def test_unload_claims_later_init_while_earlier_rollback_is_slow(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)
    bad_definition = _build_definition("bad")
    good_definition = _build_definition("good")
    bad_module = ModuleType("plugins.bad.main")
    good_module = ModuleType("plugins.good.main")
    bad_shutdown_started = asyncio.Event()
    release_bad_shutdown = asyncio.Event()
    shutdown_calls = {"bad": 0, "good": 0}

    async def bad_shutdown(context=None) -> None:
        shutdown_calls["bad"] += 1
        bad_shutdown_started.set()
        await release_bad_shutdown.wait()

    async def good_shutdown(context=None) -> None:
        shutdown_calls["good"] += 1

    bad_module.shutdown = bad_shutdown
    good_module.shutdown = good_shutdown
    original_register = manager._register_loaded_plugin

    def publish(definition, module, mtime) -> None:
        if definition.name == "bad":
            raise RuntimeError("bad publish failed")
        original_register(definition, module, mtime)

    manager._register_loaded_plugin = Mock(side_effect=publish)
    for definition, module in (
        (bad_definition, bad_module),
        (good_definition, good_module),
    ):
        init_task = asyncio.create_task(asyncio.sleep(0))
        manager._init_tasks.append(init_task)
        manager._init_task_plugins[init_task] = definition.name
        manager._pending_plugins[init_task] = (definition, module, 0.0)
        manager._plugin_states[definition.name] = {}
        manager._execution_gates[definition.name] = PluginExecutionGate(
            "parallel",
            plugin_name=definition.name,
        )

    waiter = asyncio.create_task(manager.wait_inits())
    await asyncio.wait_for(bad_shutdown_started.wait(), timeout=1)

    await manager.unload_plugin("good")
    assert "good" not in manager.list_runtime_plugins()

    release_bad_shutdown.set()
    await waiter

    assert shutdown_calls == {"bad": 1, "good": 1}
    assert "bad" not in manager.list_runtime_plugins()
    assert "good" not in manager.list_runtime_plugins()
    assert not manager._init_task_plugins
    assert not manager._pending_plugins
    assert not manager._pending_finalizers


@pytest.mark.asyncio
async def test_wait_inits_logs_timeout_from_async_init(tmp_path: Path, caplog):
    manager = _build_manager(tmp_path)

    async def timeout():
        raise asyncio.TimeoutError()

    task = asyncio.create_task(timeout())
    manager._init_tasks.append(task)
    manager._init_task_plugins[task] = "demo"
    manager.unload_plugin = AsyncMock()

    with caplog.at_level("WARNING"):
        await manager.wait_inits()

    assert "Plugin demo init timed out" in caplog.text


@pytest.mark.asyncio
async def test_load_plugin_registers_after_async_init_completes(tmp_path: Path):
    manager = _build_manager(tmp_path)
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

    manager.load_plugin(plugin_dir)
    assert "demo" not in manager._plugins

    await manager.wait_inits()

    assert "demo" in manager._plugins
    resolved = manager.router.resolve("demo")
    assert resolved is not None
    assert resolved[0].execution_gate is manager._plugins["demo"].execution_gate


@pytest.mark.asyncio
async def test_async_init_with_kwargs_does_not_receive_positional_context(tmp_path: Path):
    manager = _build_manager(tmp_path)
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

    manager.load_plugin(plugin_dir)
    await manager.wait_inits()

    assert manager._plugins["kwargs_init"].module.INIT_KWARGS == {}


@pytest.mark.asyncio
async def test_async_init_system_exit_rolls_back_before_rethrow(tmp_path: Path) -> None:
    import sys

    manager = _build_manager(tmp_path)
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
    tracker = ModuleType("xiaoqing_fatal_init_tracker")
    tracker.shutdowns = 0
    sys.modules[tracker.__name__] = tracker

    try:
        manager.load_plugin(plugin_dir)
        with pytest.raises(PluginLifecycleFatalError) as raised:
            await manager.wait_inits()
        assert isinstance(raised.value.original, SystemExit)
        assert str(raised.value.original) == "fatal init"

        assert tracker.shutdowns == 1
        assert "fatal_init" not in manager.list_runtime_plugins()
        assert not manager._pending_plugins
        assert not manager._pending_finalizers
    finally:
        manager._purge_plugin_modules("fatal_init")
        sys.modules.pop(tracker.__name__, None)


@pytest.mark.asyncio
async def test_late_fatal_after_init_timeout_outranks_timeout_after_cleanup(
    tmp_path: Path,
) -> None:
    import sys

    manager = _build_manager(tmp_path)
    manager.configure_execution(
        {
            "timeout_seconds": 0.01,
            "drain_timeout_seconds": 0.2,
        }
    )
    plugin_dir = manager.plugins_dir / "late_fatal"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "plugin.json").write_text(
        '{"name":"late_fatal","version":"1.0.0","entry":"main.py",'
        '"commands":[],"schedule":[],"concurrency":"parallel","enabled":true}',
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(
        "import asyncio\n"
        "import xiaoqing_late_fatal_tracker as tracker\n"
        "async def init(context=None):\n"
        "    try:\n"
        "        await asyncio.Event().wait()\n"
        "    except asyncio.CancelledError:\n"
        "        raise SystemExit('late fatal')\n"
        "async def shutdown(context=None):\n"
        "    tracker.shutdowns += 1\n",
        encoding="utf-8",
    )
    tracker = ModuleType("xiaoqing_late_fatal_tracker")
    tracker.shutdowns = 0
    sys.modules[tracker.__name__] = tracker

    try:
        manager.load_plugin(plugin_dir)
        with pytest.raises(PluginLifecycleFatalError) as raised:
            await manager.wait_inits()
        assert isinstance(raised.value.original, SystemExit)
        assert str(raised.value.original) == "late fatal"

        assert tracker.shutdowns == 1
        assert "late_fatal" not in manager.list_runtime_plugins()
    finally:
        manager._purge_plugin_modules("late_fatal")
        sys.modules.pop(tracker.__name__, None)


@pytest.mark.asyncio
async def test_top_level_system_exit_is_deferred_until_rollback_finishes(
    tmp_path: Path,
) -> None:
    import sys

    manager = _build_manager(tmp_path)
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
    tracker = ModuleType("xiaoqing_fatal_import_tracker")
    tracker.shutdowns = 0
    sys.modules[tracker.__name__] = tracker

    try:
        manager.load_plugin(plugin_dir)
        gate = manager._execution_gates["fatal_import"]
        assert gate.closed is True

        with pytest.raises(PluginLifecycleFatalError) as raised:
            await manager.wait_inits()
        assert isinstance(raised.value.original, SystemExit)
        assert str(raised.value.original) == "fatal import"

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
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    module = ModuleType("plugins.demo.main")

    async def fatal_shutdown(context=None) -> None:
        raise SystemExit("fatal shutdown")

    module.shutdown = fatal_shutdown
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    plugin = LoadedPlugin(
        definition=definition,
        module=module,
        mtime=0.0,
        execution_gate=gate,
    )
    state = {"resource": object()}
    manager._plugins["demo"] = plugin
    manager._plugin_states["demo"] = state
    manager._execution_gates["demo"] = gate

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


@pytest.mark.asyncio
async def test_wait_inits_cannot_steal_live_unload_deferred_fatal(tmp_path: Path) -> None:
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    shutdown_started = asyncio.Event()
    release_shutdown = asyncio.Event()
    module = ModuleType("plugins.demo.main")

    async def shutdown(context=None) -> None:
        shutdown_started.set()
        await release_shutdown.wait()

    module.shutdown = shutdown
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    expected = SystemExit("owned by demo unload")
    gate._deferred_fatal_errors.append(expected)
    manager._plugins["demo"] = LoadedPlugin(
        definition=definition,
        module=module,
        mtime=0.0,
        execution_gate=gate,
    )
    manager._execution_gates["demo"] = gate

    unload_task = asyncio.create_task(manager.unload_plugin("demo"))
    await asyncio.wait_for(shutdown_started.wait(), timeout=1)

    await manager.wait_inits()
    assert unload_task.done() is False

    release_shutdown.set()
    with pytest.raises(PluginLifecycleFatalError) as raised:
        await unload_task
    assert raised.value.original is expected
    assert "demo" not in manager.list_runtime_plugins()
