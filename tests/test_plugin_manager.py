"""
PluginManager unit tests.
"""

import asyncio
import os
import textwrap
import threading
import time
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, Mock

import pytest

from core.exceptions import PluginLoadError
from core.plugin_execution import (
    PluginExecutionClosed,
    PluginExecutionGate,
    PluginExecutionPolicy,
    PluginExecutionTimeout,
    call_plugin_callback,
)
from core.plugin_manager import (
    LoadedPlugin,
    PluginDefinition,
    PluginManager,
    PluginServiceDefinition,
)
from core.router import CommandRouter


def _build_manager(tmp_path: Path) -> PluginManager:
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "__init__.py").write_text("", encoding="utf-8")
    return PluginManager(
        plugins_dir=plugins_dir,
        router=CommandRouter(),
        context_factory=lambda *args, **kwargs: Mock(),
    )


def _build_definition(name: str = "demo") -> PluginDefinition:
    return PluginDefinition(
        name=name,
        version="1.0.0",
        entry="main.py",
        commands=[],
        schedule=[],
        concurrency="parallel",
        enabled=True,
    )


def test_is_plugin_dir_skips_deprecated_dirs(tmp_path: Path):
    manager = _build_manager(tmp_path)
    deprecated_dir = manager.plugins_dir / "memo_deprecated"
    deprecated_dir.mkdir()

    assert manager._is_plugin_dir(deprecated_dir) is False


def test_plugin_modules_use_only_the_plugins_namespace(tmp_path: Path):
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "canonical_demo"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "main.py").write_text("VALUE = object()\n", encoding="utf-8")
    definition = _build_definition("canonical_demo")

    module, _ = manager._load_module(plugin_dir, definition)

    assert module is not None
    assert module.__name__ == "plugins.canonical_demo.main"
    assert "canonical_demo.main" not in __import__("sys").modules
    manager._purge_plugin_modules("canonical_demo")


def test_load_definition_rejects_unknown_concurrency_mode(tmp_path: Path, caplog):
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        '{"name":"demo","entry":"main.py","concurrency":"shared"}',
        encoding="utf-8",
    )

    assert manager._load_definition(plugin_dir) is None
    assert "Invalid plugin.json" in caplog.text


def _service_definition(
    *,
    owner: str = "voice",
    name: str = "voice.synthesize_text",
    callback: str = "synthesize",
    callers: frozenset[str] = frozenset({"smalltalk"}),
    required_capability: str | None = None,
) -> PluginDefinition:
    definition = _build_definition(owner)
    definition.services = (
        PluginServiceDefinition(
            name=name,
            callback=callback,
            callers=callers,
            required_capability=required_capability,
        ),
    )
    return definition


def test_declared_service_registry_is_immutable_and_caller_scoped(tmp_path: Path):
    manager = _build_manager(tmp_path)
    module = ModuleType("plugins.voice.main")

    async def synthesize(text, context):
        return text, context

    module.synthesize = synthesize
    manager._register_loaded_plugin(_service_definition(), module, 0)

    loaded, service = manager.resolve_service(
        caller_plugin="smalltalk",
        service_name="voice.synthesize_text",
    )
    assert service.callback is synthesize
    assert loaded.services["voice.synthesize_text"] is service
    with pytest.raises(TypeError):
        loaded.services["voice.synthesize_text"] = service  # type: ignore[index]
    with pytest.raises(PermissionError):
        manager.resolve_service(
            caller_plugin="shell",
            service_name="voice.synthesize_text",
        )
    with pytest.raises(RuntimeError):
        manager.resolve_service(
            caller_plugin="smalltalk",
            service_name="voice.shutdown",
        )


def test_declared_service_callback_and_required_capability_fail_closed(tmp_path: Path):
    manager = _build_manager(tmp_path)
    module = ModuleType("plugins.codex.main")
    definition = _service_definition(
        owner="codex",
        name="codex.enqueue_arxiv_summary",
        callback="enqueue",
        callers=frozenset({"arxiv_filter"}),
        required_capability="codex_arxiv_summary",
    )
    with pytest.raises(PluginLoadError, match="Declared service callback"):
        manager._register_loaded_plugin(definition, module, 0)

    async def enqueue(*args):
        return args

    module.enqueue = enqueue
    manager._register_loaded_plugin(definition, module, 0)
    with pytest.raises(PermissionError, match="requires capability"):
        manager.resolve_service(
            caller_plugin="arxiv_filter",
            service_name="codex.enqueue_arxiv_summary",
        )
    loaded, _ = manager.resolve_service(
        caller_plugin="arxiv_filter",
        service_name="codex.enqueue_arxiv_summary",
        granted_capabilities=frozenset({"codex_arxiv_summary"}),
    )
    assert loaded.definition.name == "codex"

    manager._quarantined_plugins.add("codex")
    with pytest.raises(RuntimeError, match="not accepting calls"):
        manager.resolve_service(
            caller_plugin="arxiv_filter",
            service_name="codex.enqueue_arxiv_summary",
            granted_capabilities=frozenset({"codex_arxiv_summary"}),
        )
    manager._quarantined_plugins.discard("codex")

    loaded.execution_gate._closed = True  # lifecycle fail-closed probe
    with pytest.raises(RuntimeError, match="not accepting calls"):
        manager.resolve_service(
            caller_plugin="arxiv_filter",
            service_name="codex.enqueue_arxiv_summary",
            granted_capabilities=frozenset({"codex_arxiv_summary"}),
        )


def test_load_definition_rejects_unknown_manifest_field(tmp_path: Path, caplog):
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        '{"name":"demo","entry":"main.py","unknown_runtime_option":true}',
        encoding="utf-8",
    )

    assert manager._load_definition(plugin_dir) is None
    assert "Invalid plugin.json" in caplog.text


def test_load_definition_rejects_missing_required_python_dependency(tmp_path: Path, caplog):
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        '{"name":"demo","dependencies":[{"name":"missing_xiaoqing_dependency","required":true}]}',
        encoding="utf-8",
    )

    assert manager._load_definition(plugin_dir) is None
    assert "requires Python dependency missing_xiaoqing_dependency" in caplog.text


def test_load_definition_allows_missing_optional_python_dependency(tmp_path: Path, caplog):
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        '{"name":"demo","dependencies":[{"name":"missing_xiaoqing_dependency","required":false}]}',
        encoding="utf-8",
    )

    with caplog.at_level("INFO"):
        definition = manager._load_definition(plugin_dir)

    assert definition is not None
    assert definition.dependencies == ["missing_xiaoqing_dependency"]
    assert "optional Python dependency missing_xiaoqing_dependency is unavailable" in caplog.text


def test_all_active_plugin_manifests_validate_against_the_strict_schema():
    root = Path(__file__).resolve().parents[1]
    manager = PluginManager(
        plugins_dir=root / "plugins",
        router=CommandRouter(),
        context_factory=lambda *args, **kwargs: Mock(),
    )

    definitions = [
        manager._load_definition(path)
        for path in (root / "plugins").iterdir()
        if manager._is_plugin_dir(path)
    ]

    assert all(definition is not None for definition in definitions)


def test_configure_execution_applies_per_plugin_timeout_override(tmp_path: Path):
    manager = _build_manager(tmp_path)
    manager.configure_execution(
        {
            "timeout_seconds": 12,
            "parallel_limit": 2,
            "drain_timeout_seconds": 7,
            "overrides": {"codex": {"timeout_seconds": 0}},
        }
    )

    ordinary = manager._execution_gate_for(_build_definition("ordinary"))
    codex = manager._execution_gate_for(_build_definition("codex"))

    assert ordinary.policy.timeout_seconds == 12
    assert ordinary.policy.parallel_limit == 2
    assert ordinary.policy.drain_timeout_seconds == 7
    assert codex.policy.timeout_seconds is None


def test_trusted_admin_plugin_timeout_is_disabled_by_default(tmp_path: Path):
    manager = _build_manager(tmp_path)
    gate = manager._execution_gate_for(_build_definition("qingssh"))
    assert gate.policy.timeout_seconds is None


@pytest.mark.asyncio
async def test_reload_plugin_validates_shadow_but_installs_canonical_instance(tmp_path: Path):
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    old_module = ModuleType("demo.main")
    new_module = ModuleType("shadow.demo.main")
    canonical_module = ModuleType("plugins.demo.main")
    old_plugin = LoadedPlugin(definition=definition, module=old_module, mtime=0.0)
    manager._plugins["demo"] = old_plugin
    old_state = {"old": object()}
    manager._plugin_states["demo"] = old_state
    manager._load_definition = Mock(return_value=definition)
    manager._load_shadow_module = Mock(return_value=(new_module, "shadow.demo"))
    manager._initialize_shadow_plugin = AsyncMock()
    manager._shutdown_plugin_instance = AsyncMock(return_value=True)
    manager._get_mtime = Mock(return_value=1.0)
    manager._purge_shadow_modules = Mock()

    async def load_canonical(_plugin_dir, _definition, gate):
        assert manager._plugins["demo"] is old_plugin
        return LoadedPlugin(
            definition=definition,
            module=canonical_module,
            mtime=1.0,
            execution_gate=gate,
        )

    manager._load_canonical_candidate = AsyncMock(side_effect=load_canonical)

    await manager.reload_plugin("demo")

    manager._initialize_shadow_plugin.assert_awaited_once()
    assert manager._shutdown_plugin_instance.await_count == 2
    manager._shutdown_plugin_instance.assert_any_await("demo", old_plugin)
    assert manager._plugins["demo"].module is canonical_module
    assert manager._plugins["demo"].module.__name__ == "plugins.demo.main"
    assert manager._plugin_states["demo"] is not old_state
    manager._purge_shadow_modules.assert_called_once_with("shadow.demo")


@pytest.mark.asyncio
async def test_reload_plugin_keeps_old_instance_when_shadow_init_fails(tmp_path: Path):
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
    manager._load_shadow_module = Mock(return_value=(ModuleType("shadow.demo.main"), "shadow.demo"))
    manager._initialize_shadow_plugin = AsyncMock(side_effect=RuntimeError("init failed"))
    manager._purge_shadow_modules = Mock()

    await manager.reload_plugin("demo")

    assert manager._plugins["demo"] is old_plugin
    assert old_gate.closed is False
    assert await old_gate.run(AsyncMock(return_value="still live")) == "still live"
    manager._purge_shadow_modules.assert_called_once_with("shadow.demo")


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
    manager._load_shadow_module = Mock(return_value=(ModuleType("shadow.demo.main"), "shadow.demo"))
    manager._initialize_shadow_plugin = AsyncMock()
    manager._shutdown_plugin_instance = AsyncMock(side_effect=[False, True])
    manager._get_mtime = Mock(return_value=1.0)
    manager._purge_shadow_modules = Mock()
    manager._purge_plugin_modules = Mock()
    manager.router.clear_plugin = Mock()

    await manager.reload_plugin("demo")

    assert old_gate.closed is True
    assert manager._plugins["demo"] is old_plugin
    assert manager._execution_gates["demo"] is old_gate
    assert manager._plugin_states["demo"] is old_state
    assert "demo" in manager._quarantined_plugins
    assert manager._shutdown_plugin_instance.await_count == 2
    manager._purge_shadow_modules.assert_called_once_with("shadow.demo")
    manager._purge_plugin_modules.assert_not_called()
    manager.router.clear_plugin.assert_not_called()


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
    manager._load_shadow_module = Mock(return_value=(ModuleType("shadow.demo.main"), "shadow.demo"))
    manager._initialize_shadow_plugin = AsyncMock()
    manager._get_mtime = Mock(return_value=1.0)
    manager._purge_shadow_modules = Mock()

    canonical_module = ModuleType("plugins.demo.main")

    async def load_canonical(_plugin_dir, _definition, gate):
        return LoadedPlugin(
            definition=definition,
            module=canonical_module,
            mtime=1.0,
            execution_gate=gate,
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
async def test_watch_does_not_auto_reload_quarantined_plugin(tmp_path: Path, monkeypatch):
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
    manager._get_mtime_async = AsyncMock(return_value=1.0)
    manager.load_plugin = Mock()
    manager.reload_plugin = AsyncMock()
    sleep_calls = 0

    async def one_poll_then_stop(_interval):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr("core.plugin_manager.asyncio.sleep", one_poll_then_stop)

    with pytest.raises(asyncio.CancelledError):
        await manager.watch()

    manager.load_plugin.assert_not_called()
    manager.reload_plugin.assert_not_awaited()


@pytest.mark.asyncio
async def test_reload_cancellation_quarantines_old_and_cleans_shadow(tmp_path: Path):
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    shutdown_started = asyncio.Event()
    staged_shutdown_called = asyncio.Event()
    old_module = ModuleType("plugins.demo.main")
    staged_module = ModuleType("shadow.demo.main")

    async def old_shutdown():
        shutdown_started.set()
        await asyncio.Event().wait()

    async def staged_shutdown():
        staged_shutdown_called.set()

    old_module.shutdown = old_shutdown
    staged_module.shutdown = staged_shutdown
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
    manager._load_shadow_module = Mock(return_value=(staged_module, "shadow.demo"))
    manager._initialize_shadow_plugin = AsyncMock()
    manager._get_mtime = Mock(return_value=1.0)
    manager._purge_shadow_modules = Mock()

    reload_task = asyncio.create_task(manager.reload_plugin("demo"))
    await asyncio.wait_for(shutdown_started.wait(), timeout=1)
    reload_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reload_task

    staged_gate = manager._initialize_shadow_plugin.await_args.args[2]
    assert old_gate.closed is True
    assert staged_gate.closed is True
    assert staged_shutdown_called.is_set()
    assert manager._plugins["demo"] is old_plugin
    assert "demo" in manager._quarantined_plugins
    manager._purge_shadow_modules.assert_called_once_with("shadow.demo")


@pytest.mark.asyncio
async def test_reload_quarantines_old_when_shadow_shutdown_fails(tmp_path: Path):
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
    manager._load_shadow_module = Mock(return_value=(ModuleType("shadow.demo.main"), "shadow.demo"))
    manager._initialize_shadow_plugin = AsyncMock()
    manager._shutdown_plugin_instance = AsyncMock(side_effect=[True, False])
    manager._get_mtime = Mock(return_value=1.0)
    manager._purge_shadow_modules = Mock()
    manager._load_canonical_candidate = AsyncMock()

    await manager.reload_plugin("demo")

    staged_gate = manager._initialize_shadow_plugin.await_args.args[2]
    assert old_gate.closed is True
    assert staged_gate.closed is True
    assert manager._plugins["demo"] is old_plugin
    assert "demo" in manager._quarantined_plugins
    manager._load_canonical_candidate.assert_not_awaited()


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
    manager._register_commands(definition, old_module)
    manager._load_definition = Mock(return_value=definition)
    manager._load_shadow_module = Mock(return_value=(ModuleType("shadow.demo.main"), "shadow.demo"))
    manager._get_mtime = Mock(return_value=1.0)
    manager._purge_shadow_modules = Mock()
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
        assert manager.router.resolve("demo") is not None
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
    manager._purge_plugin_modules("demo")
    importlib.invalidate_caches()
    old_module = importlib.import_module("plugins.demo.main")
    definition = _build_definition()
    definition.commands = [{"name": "demo", "triggers": ["demo"], "help": "demo"}]
    old_gate = PluginExecutionGate("parallel", plugin_name="demo")
    old_state = {"sentinel": object()}
    old_plugin = LoadedPlugin(
        definition=definition,
        module=old_module,
        mtime=0.0,
        execution_gate=old_gate,
    )
    manager._plugins["demo"] = old_plugin
    manager._plugin_states["demo"] = old_state
    manager._execution_gates["demo"] = old_gate
    manager._register_commands(definition, old_module)

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
    manager._load_definition = Mock(return_value=definition)
    manager._load_shadow_module = Mock(return_value=(ModuleType("shadow.demo.main"), "shadow.demo"))
    manager._purge_shadow_modules = Mock()

    try:
        await manager.reload_plugin("demo")

        assert manager._plugins["demo"] is old_plugin
        assert manager._plugin_states["demo"] is old_state
        assert manager._execution_gates["demo"].closed is False
        assert sys.modules["plugins.demo.main"] is old_module
        assert old_module.INITIALIZATIONS == 1
        assert manager.router.resolve("demo")[0].handler is old_module.handle
        assert not manager._init_tasks
        assert not manager._init_task_plugins
    finally:
        manager._purge_plugin_modules("demo")


def test_load_module_rejects_same_file_under_noncanonical_alias(tmp_path: Path):
    import sys

    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "alias_demo"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    entry = plugin_dir / "main.py"
    entry.write_text("VALUE = 1\n", encoding="utf-8")
    alias = ModuleType("alias_demo.main")
    alias.__file__ = str(entry)
    sys.modules[alias.__name__] = alias
    try:
        with pytest.raises(Exception, match="Non-canonical plugin module aliases"):
            manager._load_module(plugin_dir, _build_definition("alias_demo"))
    finally:
        sys.modules.pop(alias.__name__, None)
        manager._purge_plugin_modules("alias_demo")


def test_purge_plugin_modules_removes_legacy_alias_by_realpath(tmp_path: Path):
    import sys

    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "purge_demo"
    plugin_dir.mkdir()
    entry = plugin_dir / "main.py"
    entry.write_text("", encoding="utf-8")
    alias = ModuleType("purge_demo.main")
    alias.__file__ = str(entry)
    sys.modules[alias.__name__] = alias
    manager._purge_plugin_modules("purge_demo")
    assert alias.__name__ not in sys.modules


@pytest.mark.asyncio
async def test_wait_inits_unloads_plugin_when_async_init_fails(tmp_path: Path):
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    module = ModuleType("demo.main")
    manager._plugins["demo"] = LoadedPlugin(definition=definition, module=module, mtime=0.0)

    async def fail():
        raise RuntimeError("boom")

    task = asyncio.create_task(fail())
    manager._init_tasks.append(task)
    manager._init_task_plugins[task] = "demo"
    manager.unload_plugin = AsyncMock(side_effect=lambda name: manager._plugins.pop(name, None))

    await manager.wait_inits()

    manager.unload_plugin.assert_awaited_once_with("demo")
    assert "demo" not in manager._plugins


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
    assert manager.router.resolve("demo") is not None


def test_get_mtime_tracks_submodule_changes(tmp_path: Path):
    manager = _build_manager(tmp_path)
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

    before = manager._get_mtime(plugin_dir, definition)
    helper.write_text("value = 2\n", encoding="utf-8")
    os.utime(helper, None)
    after = manager._get_mtime(plugin_dir, definition)

    assert after != before


def test_get_mtime_distinguishes_offsetting_file_mtimes(tmp_path: Path):
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    definition = _build_definition()
    main = plugin_dir / "main.py"
    helper = plugin_dir / "helper.py"
    (plugin_dir / "plugin.json").write_text(
        '{"name":"demo","version":"1.0.0","entry":"main.py","commands":[],"schedule":[],"concurrency":"parallel","enabled":true}',
        encoding="utf-8",
    )
    main.write_text("VALUE = 1\n", encoding="utf-8")
    helper.write_text("HELPER = 1\n", encoding="utf-8")

    base = time.time_ns()
    os.utime(main, ns=(base + 1_000, base + 1_000))
    os.utime(helper, ns=(base + 3_000, base + 3_000))
    before = manager._get_mtime(plugin_dir, definition)

    os.utime(main, ns=(base + 2_000, base + 2_000))
    os.utime(helper, ns=(base + 2_000, base + 2_000))
    after = manager._get_mtime(plugin_dir, definition)

    assert after != before


def test_iter_watch_files_ignores_runtime_data_dir(tmp_path: Path):
    manager = _build_manager(tmp_path)
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
    runtime_state = data_dir / "state.json"
    runtime_state.write_text('{"count": 1}\n', encoding="utf-8")

    files = manager._iter_watch_files(plugin_dir, definition)

    assert runtime_state not in files


def test_build_context_ensures_data_dir_once(tmp_path: Path, monkeypatch):
    manager = _build_manager(tmp_path)
    calls: list[Path] = []

    def fake_ensure_dir(path: Path) -> None:
        calls.append(path)
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("core.plugin_manager.ensure_dir", fake_ensure_dir)

    manager.build_context("demo")
    manager.build_context("demo")

    assert calls == [manager.plugins_dir / "demo" / "data"]


@pytest.mark.asyncio
async def test_unload_plugin_clears_pending_plugin_state(tmp_path: Path):
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    task = asyncio.create_task(asyncio.sleep(0))
    manager._init_tasks.append(task)
    manager._init_task_plugins[task] = "demo"
    manager._pending_plugins[task] = (definition, ModuleType("demo.main"), 0.0)
    manager._plugin_states["demo"] = {"value": 1}

    await manager.unload_plugin("demo")

    assert "demo" not in manager._plugin_states


@pytest.mark.asyncio
async def test_unload_cancels_running_plugin_gate_before_shutdown(tmp_path: Path):
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    gate = PluginExecutionGate("sequential")
    entered = asyncio.Event()
    shutdown_called = asyncio.Event()
    module = ModuleType("demo.main")

    async def shutdown():
        shutdown_called.set()

    module.shutdown = shutdown
    manager._plugins["demo"] = LoadedPlugin(
        definition=definition,
        module=module,
        mtime=0.0,
        execution_gate=gate,
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


@pytest.mark.asyncio
async def test_unload_quarantines_until_timed_out_sync_callback_really_finishes(
    tmp_path: Path,
):
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    gate = PluginExecutionGate(
        "sequential",
        plugin_name="demo",
        policy=PluginExecutionPolicy(
            timeout_seconds=0.01,
            drain_timeout_seconds=0.01,
        ),
    )
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    shutdown_called = asyncio.Event()
    module = ModuleType("plugins.demo.main")

    def blocking_handler() -> None:
        started.set()
        release.wait(timeout=2)
        finished.set()

    async def shutdown() -> None:
        shutdown_called.set()

    module.shutdown = shutdown
    plugin = LoadedPlugin(
        definition=definition,
        module=module,
        mtime=0.0,
        execution_gate=gate,
    )
    state = {"owned": object()}
    manager._plugins["demo"] = plugin
    manager._plugin_states["demo"] = state
    manager._execution_gates["demo"] = gate
    manager._purge_plugin_modules = Mock()

    with pytest.raises(PluginExecutionTimeout):
        await gate.run(lambda: call_plugin_callback(blocking_handler))
    assert started.is_set()

    await manager.unload_plugin("demo")

    assert manager._plugins["demo"] is plugin
    assert manager._plugin_states["demo"] is state
    assert manager._execution_gates["demo"] is gate
    assert "demo" in manager._quarantined_plugins
    assert shutdown_called.is_set() is False
    manager._purge_plugin_modules.assert_not_called()

    release.set()
    assert await asyncio.to_thread(finished.wait, 1)
    await asyncio.sleep(0)
    await manager.unload_plugin("demo")

    assert shutdown_called.is_set()
    assert "demo" not in manager._plugins
    assert "demo" not in manager._plugin_states
    assert "demo" not in manager._execution_gates
    assert "demo" not in manager._quarantined_plugins
    manager._purge_plugin_modules.assert_called_once_with("demo")


@pytest.mark.asyncio
async def test_reload_never_installs_candidate_beside_timed_out_sync_callback(
    tmp_path: Path,
):
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    gate = PluginExecutionGate(
        "sequential",
        plugin_name="demo",
        policy=PluginExecutionPolicy(
            timeout_seconds=0.01,
            drain_timeout_seconds=0.01,
        ),
    )
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_handler() -> None:
        started.set()
        release.wait(timeout=2)
        finished.set()

    old_plugin = LoadedPlugin(
        definition=definition,
        module=ModuleType("plugins.demo.main"),
        mtime=0.0,
        execution_gate=gate,
    )
    manager._plugins["demo"] = old_plugin
    manager._execution_gates["demo"] = gate
    manager._plugin_states["demo"] = {"old": True}
    manager._load_definition = Mock(return_value=definition)
    manager._load_shadow_module = Mock(return_value=(ModuleType("shadow.demo.main"), "shadow.demo"))
    manager._initialize_shadow_plugin = AsyncMock()
    manager._shutdown_plugin_instance = AsyncMock(return_value=True)
    manager._get_mtime = Mock(return_value=1.0)
    manager._purge_shadow_modules = Mock()
    manager._load_canonical_candidate = AsyncMock()

    with pytest.raises(PluginExecutionTimeout):
        await gate.run(lambda: call_plugin_callback(blocking_handler))
    assert started.is_set()

    await manager.reload_plugin("demo")

    assert manager._plugins["demo"] is old_plugin
    assert manager._execution_gates["demo"] is gate
    assert gate.closed is True
    assert "demo" in manager._quarantined_plugins
    manager._load_canonical_candidate.assert_not_awaited()
    manager._shutdown_plugin_instance.assert_awaited_once()

    release.set()
    assert await asyncio.to_thread(finished.wait, 1)
    await asyncio.sleep(0)
    assert (await gate.close()).drained is True
