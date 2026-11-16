"""插件管理器测试共享 fixture、导入和私有 helper。"""

import asyncio
import json
import os
import sys
import textwrap
import threading
from concurrent.futures import Future as ConcurrentFuture
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import Mock

import pytest

from core.exceptions import PluginLifecycleFatalError, PluginLoadError
from core.plugin_execution import (
    PluginExecutionClosed,
    PluginExecutionGate,
    PluginExecutionPolicy,
    PluginExecutionTimeout,
    call_plugin_callback,
)
from core.plugin_manager import PluginManager
from core.plugin_manager_support import (
    _PLUGIN_NAMESPACE_OWNERS,
    _PROCESS_IMPORT_PATH_LEASES,
    LoadedPlugin,
    PluginDefinition,
    PluginPathError,
)
from core.router import CommandRouter
from tests.helpers.plugin_probe import PROBE


class FatalLifecycleError(BaseException):
    pass


@pytest.fixture(autouse=True)
def _isolate_process_global_plugin_import_state():  # type: ignore[no-untyped-def]
    """Keep deliberate restart-only test generations from leaking to the next case."""

    PROBE.reset()
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


def build_manager(tmp_path: Path) -> PluginManager:
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "__init__.py").write_text("", encoding="utf-8")
    return PluginManager(
        plugins_dir     = plugins_dir,
        router          = CommandRouter(),
        context_factory = lambda *args, **kwargs: Mock(),
    )


def build_definition(name: str = "demo") -> PluginDefinition:
    return PluginDefinition(
        name        = name,
        version     = "1.0.0",
        entry       = "main.py",
        commands    = [],
        schedule    = [],
        concurrency = "parallel",
        enabled     = True,
    )


def write_runtime_manifest(
    plugin_dir: Path,
    name: str,
    entry: str = "main.py",
    **fields: Any,
) -> None:
    """写 ``plugin.json``；``fields`` 里的 ``commands``、``capabilities`` 等覆盖默认值。"""

    manifest = {
        "name"       : name,
        "version"    : "1.0.0",
        "entry"      : entry,
        "commands"   : [],
        "schedule"   : [],
        "concurrency": "parallel",
        "enabled"    : True,
        **fields,
    }
    (plugin_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")


DEMO_COMMAND = {"name": "demo", "triggers": ["demo"], "help": "demo"}

# 观察都经 PROBE：导入、``init``、``shutdown`` 各记一笔；后两者再执行测试放进 ``PROBE.slots`` 的协程函数。
HOOKED_PLUGIN_SOURCE = """
from tests.helpers.plugin_probe import PROBE

PROBE.log("import").append(1)

async def init(context=None):
    PROBE.log("init").append(context)
    hook = PROBE.slots.get("on_init")
    if hook is not None:
        await hook(context)

async def shutdown(context=None):
    PROBE.log("shutdown").append(context)
    hook = PROBE.slots.get("on_shutdown")
    if hook is not None:
        await hook(context)

async def handle(command, args, event, context):
    return []
"""


def write_plugin(
    manager: PluginManager,
    name: str   = "demo",
    source: str = HOOKED_PLUGIN_SOURCE,
    **manifest: Any,
) -> Path:
    """在 ``manager.plugins_dir`` 下写（或改写）一个真实插件目录，返回目录。

    ``source`` 是 ``main.py`` 的内容；``manifest`` 里的字段写进 ``plugin.json``。
    """

    plugin_dir: Path = manager.plugins_dir / name
    plugin_dir.mkdir(exist_ok=True)
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "main.py").write_text(textwrap.dedent(source).strip() + "\n", encoding="utf-8")
    write_runtime_manifest(plugin_dir, name, **manifest)
    return plugin_dir


def runtime_symlink_or_skip(target: Path, link: Path, *, directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")


def detach_repository_plugin_modules(manager: PluginManager, name: str) -> None:
    """摘下同一进程里别的用例留下的同名插件模块（仓库里的真实插件，或别的临时目录里的插件）。

    Core 拒绝加载规范模块名（``plugins.<name>``）在导入前就已存在于 ``sys.modules`` 的插件。
    只在加载辅助里调用：测试要验证“外来缓存模块被拒绝”时直接用 ``load_plugin_dir``，不经这里。
    用例结束时由 ``_isolate_process_global_plugin_import_state`` 放回。
    """

    if manager.get(name) is not None:
        return
    canonical = f"plugins.{name}"
    for module_name in [
        module_name
        for module_name in sys.modules
        if module_name == canonical or module_name.startswith(f"{canonical}.")
    ]:
        sys.modules.pop(module_name)
    plugins_package = sys.modules.get("plugins")
    if isinstance(plugins_package, ModuleType):
        vars(plugins_package).pop(name, None)


async def load_plugin_dir(manager: PluginManager, plugin_dir: Path) -> None:
    """像生产上的启动和热加载那样加载一个插件目录：持有生命周期锁，走候选代路径。

    普通的加载失败只记日志并把插件隔离在外；致命错误和取消原样抛出。
    """

    async with manager._lifecycle_lock.get():
        await manager._reconcile_plugin_path(plugin_dir)


async def load_plugin(
    manager: PluginManager,
    name: str   = "demo",
    source: str = HOOKED_PLUGIN_SOURCE,
    **manifest: Any,
) -> LoadedPlugin:
    """写一个真实插件目录并经生产的加载路径装入，返回发布出来的这一代。"""

    detach_repository_plugin_modules(manager, name)
    await load_plugin_dir(manager, write_plugin(manager, name, source, **manifest))
    loaded = manager.get(name)
    assert loaded is not None, f"插件 {name} 没有加载成功（加载失败只写日志）"
    return loaded


class BlockedSyncWork:
    """经插件执行门卸到线程里、被拦住的同步回调：放行之前这一代的执行门排不空。

    ``start`` 要在插件的执行范围里调用（插件回调里，或 ``await gate.run(work.start)``）。
    """

    def __init__(self) -> None:
        self.started                         = threading.Event()
        self.release                         = threading.Event()
        self.finished                        = threading.Event()
        self.task: asyncio.Task[None] | None = None

    def _run(self) -> None:
        self.started.set()
        self.release.wait(timeout=5)
        self.finished.set()

    async def start(self) -> None:
        self.task = asyncio.create_task(call_plugin_callback(self._run))
        assert await asyncio.to_thread(self.started.wait, 2)

    async def release_and_wait(self) -> None:
        self.release.set()
        assert await asyncio.to_thread(self.finished.wait, 2)
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)


__all__ = (
    "load_plugin_dir",
    "CommandRouter",
    "ConcurrentFuture",
    "LoadedPlugin",
    "Mock",
    "ModuleType",
    "Path",
    "PluginDefinition",
    "PluginExecutionClosed",
    "PluginExecutionGate",
    "PluginExecutionPolicy",
    "PluginExecutionTimeout",
    "PluginLifecycleFatalError",
    "PluginLoadError",
    "PluginManager",
    "PluginPathError",
    "FatalLifecycleError",
    "_PLUGIN_NAMESPACE_OWNERS",
    "_PROCESS_IMPORT_PATH_LEASES",
    "build_definition",
    "build_manager",
    "_isolate_process_global_plugin_import_state",
    "runtime_symlink_or_skip",
    "write_runtime_manifest",
    "asyncio",
    "call_plugin_callback",
    "os",
    "pytest",
    "sys",
    "textwrap",
)
