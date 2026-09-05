"""插件代际重新加载、隔离与运行态协调。"""

from __future__ import annotations

import tests.helpers.plugin_manager_test_support as _fixture_support
from core.plugin_watcher import _ManifestRejection
from tests.helpers.plugin_manager_test_support import (
    AsyncMock,
    LoadedPlugin,
    Mock,
    ModuleType,
    Path,
    PluginExecutionClosed,
    PluginExecutionGate,
    _AsyncConcurrencyProbe,
    _build_definition,
    _build_manager,
    asyncio,
    pytest,
    textwrap,
)

_isolate_process_global_plugin_import_state = (
    _fixture_support._isolate_process_global_plugin_import_state
)


@pytest.mark.asyncio
async def test_reload_plugin_keeps_old_instance_when_fingerprint_fails(tmp_path: Path):
    manager    = _build_manager(tmp_path)
    definition = _build_definition()
    old_gate = PluginExecutionGate("parallel", plugin_name="demo")
    old_plugin = LoadedPlugin(
        definition     = definition,
        module         = ModuleType("demo.main"),
        mtime          = 0.0,
        execution_gate = old_gate,
    )
    manager._plugins["demo"]         = old_plugin
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
    manager                     = _build_manager(tmp_path)
    old_definition              = _build_definition()
    changed_definition          = _build_definition()
    changed_definition.commands = [
        {
            "name": "demo",
            "triggers": ["demo"],
            "help": "demo",
            "admin_only": True,
        }
    ]
    module          = ModuleType("plugins.demo.main")
    module.shutdown = AsyncMock()
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    plugin = LoadedPlugin(
        definition     = old_definition,
        module         = module,
        mtime          = 0.0,
        execution_gate = gate,
    )
    manager._plugins["demo"]         = plugin
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
    manager             = _build_manager(tmp_path)
    definition          = _build_definition()
    definition.commands = [
        {"name": "demo", "triggers": ["demo"], "help": "demo"},
    ]
    module         = ModuleType("plugins.demo.main")
    shutdown_calls = 0

    async def handle(command, args, event, context):
        return []

    async def shutdown(context=None) -> None:
        nonlocal shutdown_calls
        shutdown_calls += 1

    module.handle   = handle
    module.shutdown = shutdown
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    plugin = LoadedPlugin(
        definition     = definition,
        module         = module,
        mtime          = 0.0,
        execution_gate = gate,
    )
    manager._plugins["demo"]         = plugin
    manager._execution_gates["demo"] = gate
    manager.router.replace_plugin(
        definition.name,
        manager._build_command_specs(definition, module, gate),
    )
    if manifest_state == "invalid":
        manager._load_definition = Mock(return_value=None)
    else:
        disabled         = _build_definition()
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
    manager    = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text("{}", encoding="utf-8")
    definition = _build_definition()
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    plugin = LoadedPlugin(
        definition     = definition,
        module         = ModuleType("plugins.demo.main"),
        mtime          = 0.0,
        execution_gate = gate,
    )
    manager._plugins["demo"]         = plugin
    manager._execution_gates["demo"] = gate
    manager._load_definition = Mock(return_value=_ManifestRejection("dependency"))
    manager._unload_plugin_once = AsyncMock()

    await manager._reconcile_plugin_path(plugin_dir)

    manager._unload_plugin_once.assert_not_awaited()
    assert manager._plugins["demo"] is plugin


@pytest.mark.asyncio
async def test_reload_plugin_quarantines_old_instance_when_shutdown_fails(tmp_path: Path):
    manager    = _build_manager(tmp_path)
    definition = _build_definition()
    old_gate = PluginExecutionGate("parallel", plugin_name="demo")
    old_plugin = LoadedPlugin(
        definition     = definition,
        module         = ModuleType("demo.main"),
        mtime          = 0.0,
        execution_gate = old_gate,
    )
    manager._plugins["demo"]         = old_plugin
    manager._execution_gates["demo"] = old_gate
    old_state                        = {"resource": object()}
    manager._plugin_states["demo"]   = old_state
    manager._load_definition = Mock(return_value=definition)
    manager._shutdown_plugin_instance = AsyncMock(return_value=False)
    manager._authorize_plugin_snapshot = Mock(return_value=1.0)
    manager._purge_plugin_modules = Mock()
    manager.router.clear_plugin   = Mock()

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
    manager          = _build_manager(tmp_path)
    definition       = _build_definition()
    shutdown_started = asyncio.Event()
    release_shutdown = asyncio.Event()
    old_module       = ModuleType("demo.main")

    async def shutdown():
        shutdown_started.set()
        await release_shutdown.wait()

    old_module.shutdown = shutdown
    old_gate = PluginExecutionGate("parallel", plugin_name="demo")
    old_plugin = LoadedPlugin(
        definition     = definition,
        module         = old_module,
        mtime          = 0.0,
        execution_gate = old_gate,
    )
    manager._plugins["demo"]         = old_plugin
    manager._execution_gates["demo"] = old_gate
    manager._load_definition = Mock(return_value=definition)
    manager._authorize_plugin_snapshot = Mock(return_value=1.0)
    manager._definition_is_current = Mock(return_value=True)

    canonical_module = ModuleType("plugins.demo.main")

    async def load_canonical(_plugin_dir, transaction):
        return LoadedPlugin(
            definition     = definition,
            module         = canonical_module,
            mtime          = transaction.mtime,
            execution_gate = transaction.gate,
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
@pytest.mark.parametrize(
    "manifest_failure",
    ["disabled", "deleted", "invalid", "invalid-with-backup", "missing-entry"],
)
async def test_reconcile_unloads_fail_closed_manifest_and_recovers_once(
    tmp_path: Path,
    manifest_failure: str,
) -> None:
    manager    = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    manifest_path    = plugin_dir / "plugin.json"
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
    original        = manager._plugins["demo"]
    original_gate   = original.execution_gate
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
    resolved    = manager.router.resolve("demo")
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
    manifest_path    = plugin_dir / "plugin.json"
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
    gate        = quarantined.execution_gate
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
    manager    = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    definition = _build_definition()
    module     = ModuleType("plugins.demo.main")
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    init_task = asyncio.create_task(asyncio.sleep(3600))
    manager._init_tasks.append(init_task)
    manager._init_task_plugins[init_task] = "demo"
    manager._pending_plugins[init_task]   = (definition, module, 0.0)
    manager._execution_gates["demo"]      = gate
    manager._plugin_states["demo"]        = {"pending": True}
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
    probe   = _AsyncConcurrencyProbe()
    manager._reconcile_plugins_once = AsyncMock(side_effect=probe.run)

    await asyncio.gather(manager.reconcile_plugins(), manager.reconcile_plugins())

    assert probe.calls == 2
    assert probe.maximum_active == 1


@pytest.mark.asyncio
async def test_reload_and_reconcile_share_one_lifecycle_lock(tmp_path: Path) -> None:
    manager = _build_manager(tmp_path)
    probe   = _AsyncConcurrencyProbe()
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
        definition             = _build_definition(name)
        manager._plugins[name] = LoadedPlugin(
            definition = definition,
            module     = ModuleType(f"plugins.{name}.main"),
            mtime      = 0.0,
        )

    async def reload_once(name: str) -> None:
        if name == "blocked":
            manager._quarantined_plugins.add(name)

    manager._reload_plugin_once = AsyncMock(side_effect=reload_once)
    manager._reconcile_plugins_once = AsyncMock()
    before_reload                   = AsyncMock()

    completed = await manager.reload_all_plugins(before_reload=before_reload)

    assert completed is False
    before_reload.assert_awaited_once_with("blocked")
    manager._reload_plugin_once.assert_awaited_once_with("blocked")
    manager._reconcile_plugins_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_reload_all_retires_invalid_plugin_before_later_quarantine(
    tmp_path: Path,
) -> None:
    manager     = _build_manager(tmp_path)
    definitions = {name: _build_definition(name) for name in ("a_invalid", "b_blocked")}
    for name, definition in definitions.items():
        gate = PluginExecutionGate("parallel", plugin_name=name)
        manager._plugins[name] = LoadedPlugin(
            definition     = definition,
            module         = ModuleType(f"plugins.{name}.main"),
            mtime          = 0.0,
            execution_gate = gate,
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
    manager    = _build_manager(tmp_path)
    definition = _build_definition()
    plugin     = LoadedPlugin(
        definition = definition,
        module     = ModuleType("plugins.demo.main"),
        mtime      = 0.0,
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
    manager    = _build_manager(tmp_path)
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
    old_gate   = old_plugin.execution_gate
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
    manager    = _build_manager(tmp_path)
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
    old_definition  = _build_definition()
    module          = ModuleType("plugins.demo.main")
    module.shutdown = AsyncMock()
    gate = PluginExecutionGate("parallel", plugin_name="demo")
    manager._plugins["demo"] = LoadedPlugin(
        definition     = old_definition,
        module         = module,
        mtime          = 0.0,
        execution_gate = gate,
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
    manager    = _build_manager(tmp_path)
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
    old_definition           = _build_definition()
    old_mtime                = 123
    manager._plugins["demo"] = LoadedPlugin(
        definition = old_definition,
        module     = ModuleType("plugins.demo.main"),
        mtime      = old_mtime,
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
    manager          = _build_manager(tmp_path)
    definition       = _build_definition()
    shutdown_started = asyncio.Event()
    release_shutdown = asyncio.Event()
    old_module       = ModuleType("plugins.demo.main")

    async def old_shutdown():
        shutdown_started.set()
        await release_shutdown.wait()

    old_module.shutdown = old_shutdown
    old_gate = PluginExecutionGate("parallel", plugin_name="demo")
    old_plugin = LoadedPlugin(
        definition     = definition,
        module         = old_module,
        mtime          = 0.0,
        execution_gate = old_gate,
    )
    manager._plugins["demo"]         = old_plugin
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
