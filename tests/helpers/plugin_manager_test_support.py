"""插件管理器测试共享 fixture、导入和私有 helper。"""

import asyncio
import os
import sys
import textwrap
import threading
import time
from concurrent.futures import Future as ConcurrentFuture
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, Mock

import pytest

from core.exceptions import PluginLifecycleFatalError, PluginLoadError
from core.plugin_execution import (
    PluginExecutionClosed,
    PluginExecutionDrainResult,
    PluginExecutionGate,
    PluginExecutionPolicy,
    PluginExecutionTimeout,
    call_plugin_callback,
)
from core.plugin_manager import (
    _PLUGIN_NAMESPACE_OWNERS,
    _PROCESS_IMPORT_PATH_LEASES,
    LoadedPlugin,
    PluginDefinition,
    PluginManager,
    PluginPathError,
    PluginServiceDefinition,
)
from core.router import CommandRouter, CommandSpec


class _FatalLifecycleError(BaseException):
    pass


class _AsyncConcurrencyProbe:
    """记录异步桩的调用次数与最大并发数。"""

    def __init__(self) -> None:
        self.active         = 0
        self.maximum_active = 0
        self.calls          = 0

    async def run(self, *_args) -> None:
        self.calls += 1
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            await asyncio.sleep(0)
        finally:
            self.active -= 1


@pytest.fixture(autouse=True)
def _isolate_process_global_plugin_import_state():  # type: ignore[no-untyped-def]
    """Keep deliberate restart-only test generations from leaking to the next case."""

    original_owners      = dict(_PLUGIN_NAMESPACE_OWNERS)
    original_path_leases = {
        key: type(lease)(
            container = lease.container,
            owners    = lease.owners,
            inserted  = lease.inserted,
        )
        for key, lease in _PROCESS_IMPORT_PATH_LEASES.items()
    }
    original_meta_path    = list(sys.meta_path)
    original_sys_path     = list(sys.path)
    plugins_package       = sys.modules.get("plugins")
    original_plugins_path = (
        list(plugins_package.__path__)
        if isinstance(plugins_package, ModuleType) and hasattr(plugins_package, "__path__")
        else None
    )
    original_modules = {
        name: module for name, module in sys.modules.items() if name.startswith("plugins.")
    }
    original_parent_bindings: dict[str, tuple[ModuleType, str, bool, object]] = {}
    for name in original_modules:
        parent_name, _, child_name = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if isinstance(parent, ModuleType):
            namespace                      = ModuleType.__getattribute__(parent, "__dict__")
            original_parent_bindings[name] = (
                parent,
                child_name,
                child_name in namespace,
                namespace.get(child_name),
            )
    yield
    current_names = [name for name in sys.modules if name.startswith("plugins.")]
    for name in sorted(current_names, key=lambda value: value.count("."), reverse=True):
        module = sys.modules.get(name)
        parent_name, _, child_name = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if isinstance(parent, ModuleType):
            namespace = ModuleType.__getattribute__(parent, "__dict__")
            if namespace.get(child_name) is module:
                namespace.pop(child_name, None)
        sys.modules.pop(name, None)
    for name, module in sorted(
        original_modules.items(),
        key=lambda item: item[0].count("."),
    ):
        sys.modules[name] = module
    for parent, child_name, existed, previous in original_parent_bindings.values():
        namespace = ModuleType.__getattribute__(parent, "__dict__")
        if existed:
            namespace[child_name] = previous
        else:
            namespace.pop(child_name, None)
    sys.meta_path[:] = original_meta_path
    sys.path[:]      = original_sys_path
    if isinstance(plugins_package, ModuleType):
        sys.modules["plugins"] = plugins_package
        if original_plugins_path is not None:
            plugins_package.__path__[:] = original_plugins_path  # type: ignore[attr-defined]
    else:
        sys.modules.pop("plugins", None)
    _PLUGIN_NAMESPACE_OWNERS.clear()
    _PLUGIN_NAMESPACE_OWNERS.update(original_owners)
    _PROCESS_IMPORT_PATH_LEASES.clear()
    _PROCESS_IMPORT_PATH_LEASES.update(original_path_leases)


def _build_manager(tmp_path: Path) -> PluginManager:
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "__init__.py").write_text("", encoding="utf-8")
    return PluginManager(
        plugins_dir     = plugins_dir,
        router          = CommandRouter(),
        context_factory = lambda *args, **kwargs: Mock(),
    )


def _build_definition(name: str = "demo") -> PluginDefinition:
    return PluginDefinition(
        name        = name,
        version     = "1.0.0",
        entry       = "main.py",
        commands    = [],
        schedule    = [],
        concurrency = "parallel",
        enabled     = True,
    )


def _register_test_command(
    manager: PluginManager,
    gate: PluginExecutionGate,
) -> CommandSpec:
    async def handler(*_args):
        return []

    spec = CommandSpec(
        plugin         = "demo",
        name           = "demo",
        triggers       = ["/demo"],
        help_text      = "demo",
        admin_only     = False,
        handler        = handler,
        execution_gate = gate,
    )
    manager.router.register(spec)
    return spec


def _service_definition(
    *,
    owner: str                      = "voice",
    name: str                       = "voice.synthesize_text",
    callback: str                   = "synthesize",
    callers: frozenset[str]         = frozenset({"smalltalk"}),
    required_capability: str | None = None,
) -> PluginDefinition:
    definition          = _build_definition(owner)
    definition.services = (
        PluginServiceDefinition(
            name                = name,
            callback            = callback,
            callers             = callers,
            required_capability = required_capability,
        ),
    )
    return definition


def _write_runtime_manifest(plugin_dir: Path, name: str, entry: str = "main.py") -> None:
    (plugin_dir / "plugin.json").write_text(
        (
            f'{{"name":"{name}","version":"1.0.0","entry":"{entry}",'
            '"commands":[],"schedule":[],"concurrency":"parallel","enabled":true}'
        ),
        encoding="utf-8",
    )


def _runtime_symlink_or_skip(target: Path, link: Path, *, directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")


__all__ = (
    "AsyncMock",
    "CommandRouter",
    "CommandSpec",
    "ConcurrentFuture",
    "LoadedPlugin",
    "Mock",
    "ModuleType",
    "Path",
    "PluginDefinition",
    "PluginExecutionClosed",
    "PluginExecutionDrainResult",
    "PluginExecutionGate",
    "PluginExecutionPolicy",
    "PluginExecutionTimeout",
    "PluginLifecycleFatalError",
    "PluginLoadError",
    "PluginManager",
    "PluginPathError",
    "PluginServiceDefinition",
    "_AsyncConcurrencyProbe",
    "_FatalLifecycleError",
    "_PLUGIN_NAMESPACE_OWNERS",
    "_PROCESS_IMPORT_PATH_LEASES",
    "_build_definition",
    "_build_manager",
    "_isolate_process_global_plugin_import_state",
    "_register_test_command",
    "_runtime_symlink_or_skip",
    "_service_definition",
    "_write_runtime_manifest",
    "asyncio",
    "call_plugin_callback",
    "os",
    "pytest",
    "sys",
    "textwrap",
    "threading",
    "time",
)
