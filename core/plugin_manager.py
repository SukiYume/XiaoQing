import asyncio
import importlib
import inspect
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Optional

from .constants import PLUGIN_INIT_TIMEOUT_SECONDS, VALID_PLUGIN_NAME_PATTERN
from .exceptions import PluginLoadError
from .interfaces import PluginContextProtocol
from .models import PluginManifest
from .router import CommandRouter, CommandSpec
from .plugin_base import ensure_dir, load_json

logger = logging.getLogger(__name__)

def _validate_plugin_name(name: str) -> bool:
    """
    验证插件名称是否安全

    Args:
        name: 插件名称

    Returns:
        是否安全（只包含字母数字下划线）
    """
    return bool(re.match(VALID_PLUGIN_NAME_PATTERN, name))

@dataclass
class PluginDefinition:
    name: str
    version: str
    entry: str
    commands: list[dict[str, Any]]
    schedule: list[dict[str, Any]]
    concurrency: str
    enabled: bool = True  # 插件是否启用

@dataclass
class LoadedPlugin:
    definition: PluginDefinition
    module: ModuleType
    mtime: float

class PluginManager:
    def __init__(
        self,
        plugins_dir: Path,
        router: CommandRouter,
        context_factory: Any,
        poll_interval: float = 3600.0,
    ):
        self.plugins_dir = plugins_dir
        self.router = router
        self.context_factory = context_factory
        self._plugins: dict[str, LoadedPlugin] = {}
        self._poll_interval = float(poll_interval)
        self._change_handlers: list[Any] = []
        self._init_tasks: list[asyncio.Task[None]] = []
        self._plugin_states: dict[str, dict[str, Any]] = {}

        # 一次性设置 sys.path（使用绝对路径防止路径遍历攻击）
        self._setup_sys_path()

    def _setup_sys_path(self) -> None:
        """将 plugins 目录添加到 sys.path（仅一次）

        Note: Using sys.path instead of importlib.spec_from_file_location
        because plugins rely on relative imports (e.g., from .submodule import X).
        The tradeoff is potential module name conflicts with stdlib/third-party packages.
        Plugin directory names should be chosen to avoid such conflicts.
        """
        import sys
        import os

        plugins_parent = os.path.abspath(self.plugins_dir)
        if plugins_parent not in sys.path:
            sys.path.insert(0, plugins_parent)
            logger.debug("Added %s to sys.path", plugins_parent)

    def on_change(self, handler) -> None:
        self._change_handlers.append(handler)

    def _notify_change(self, name: str) -> None:
        for handler in self._change_handlers:
            try:
                handler(name)
            except Exception as exc:
                logger.warning("Plugin change handler failed: %s", exc)

    def list_plugins(self) -> list[str]:
        return sorted(self._plugins.keys())

    def get(self, name: str) -> Optional[LoadedPlugin]:
        return self._plugins.get(name)

    def _is_plugin_dir(self, path: Path) -> bool:
        """检查是否为有效的插件目录（排除 __pycache__ 等特殊目录）"""
        name = path.name
        if name.startswith("__") or name.startswith("."):
            return False
        return path.is_dir()

    def load_all(self) -> None:
        for plugin_dir in self.plugins_dir.iterdir():
            if self._is_plugin_dir(plugin_dir):
                self.load_plugin(plugin_dir)

    async def wait_inits(self) -> None:
        if not self._init_tasks:
            return
        tasks = list(self._init_tasks)
        self._init_tasks.clear()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                if isinstance(result, (KeyboardInterrupt, SystemExit)):
                    logger.warning("Plugin init interrupted")
                    raise result
                if isinstance(result, asyncio.CancelledError):
                    logger.debug("Plugin init task cancelled")
                    continue
                if isinstance(result, (ImportError, AttributeError, ValueError, SyntaxError)):
                    logger.warning("Plugin init error: %s", result)
                else:
                    logger.warning("Plugin init error: %s", result)

    def load_plugin(self, plugin_dir: Path) -> None:
        # 验证插件目录名是否安全
        if not _validate_plugin_name(plugin_dir.name):
            logger.warning(
                "Skipping plugin with invalid name '%s': must match %s",
                plugin_dir.name,
                VALID_PLUGIN_NAME_PATTERN
            )
            return

        definition = self._load_definition(plugin_dir)
        if not definition:
            return
        # 检查插件是否启用
        if not definition.enabled:
            logger.info("Plugin %s is disabled, skipping", definition.name)
            return
        try:
            module = self._load_module(plugin_dir, definition)
        except PluginLoadError as exc:
            logger.error("%s", exc, exc_info=True)
            return
        if not module:
            return
        self._register_commands(definition, module)
        mtime = self._get_mtime(plugin_dir, definition)
        self._plugins[definition.name] = LoadedPlugin(definition=definition, module=module, mtime=mtime)
        logger.info("Loaded plugin %s", definition.name)
        self._notify_change(definition.name)

    async def unload_plugin(self, name: str) -> None:
        plugin = self._plugins.pop(name, None)
        if not plugin:
            return
        self.router.clear_plugin(name)
        
        if hasattr(plugin.module, "shutdown"):
            try:
                shutdown = plugin.module.shutdown
                if len(inspect.signature(shutdown).parameters) > 0:
                    result = shutdown(self.build_context(name))
                else:
                    result = shutdown()
                if asyncio.iscoroutine(result):
                    await asyncio.wait_for(result, timeout=PLUGIN_INIT_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.warning("Plugin %s shutdown timed out (>%ss)", name, PLUGIN_INIT_TIMEOUT_SECONDS)
            except Exception as exc:
                logger.warning("Plugin %s shutdown error: %s", name, exc)
        
        # 清理插件状态
        self._plugin_states.pop(name, None)
        
        # 清理 sys.modules 中的相关模块，确保 reload 能加载新代码
        import sys
        to_delete = []
        # 假设插件目录名就是插件名
        prefix = f"{name}."
        for mod_name in sys.modules:
            if mod_name == name or mod_name.startswith(prefix):
                to_delete.append(mod_name)
               
        for mod_name in to_delete:
            del sys.modules[mod_name]
        
        logger.info("Unloaded plugin %s", name)
        self._notify_change(name)

    async def reload_plugin(self, name: str) -> None:
        plugin = self._plugins.get(name)
        if not plugin:
            return
        await self.unload_plugin(name)
        plugin_dir = self.plugins_dir / name
        self.load_plugin(plugin_dir)

    async def watch(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            plugin_dirs = await asyncio.to_thread(
                lambda: [p for p in self.plugins_dir.iterdir() if self._is_plugin_dir(p)]
            )
            for plugin_dir in plugin_dirs:
                if not self._is_plugin_dir(plugin_dir):
                    continue
                definition = await asyncio.to_thread(self._load_definition, plugin_dir)
                if not definition:
                    continue
                mtime = await self._get_mtime_async(plugin_dir, definition)
                existing = self._plugins.get(definition.name)
                if not existing:
                    self.load_plugin(plugin_dir)
                elif mtime != existing.mtime:
                    logger.info("Detected changes in plugin %s", definition.name)
                    await self.reload_plugin(definition.name)

    def _load_definition(self, plugin_dir: Path) -> Optional[PluginDefinition]:
        definition_path = plugin_dir / "plugin.json"
        data = load_json(definition_path)
        if not data:
            logger.warning("Missing plugin.json in %s", plugin_dir)
            return None

        try:
            manifest = PluginManifest.model_validate(data)
        except Exception as exc:
            logger.error("Invalid plugin.json in %s: %s", plugin_dir, exc)
            return None

        if manifest.name != plugin_dir.name:
            logger.error(
                "Invalid plugin.json in %s: name must match directory name (name=%s dir=%s)",
                plugin_dir,
                manifest.name,
                plugin_dir.name,
            )
            return None

        return PluginDefinition(
            name=manifest.name,
            version=manifest.version,
            entry=manifest.entry,
            commands=[c.model_dump() for c in manifest.commands],
            schedule=[s.model_dump() for s in manifest.schedule],
            concurrency=manifest.concurrency,
            enabled=manifest.enabled,
        )

    def _load_module(self, plugin_dir: Path, definition: PluginDefinition) -> Optional[ModuleType]:
        entry_path = plugin_dir / definition.entry
        if not entry_path.exists():
            logger.error("Plugin %s entry missing: %s", definition.name, entry_path)
            return None

        # 导入插件包（使用目录名作为模块名）
        import sys

        module_name = plugin_dir.name
        entry_stem = definition.entry.removesuffix('.py').replace('/', '.').replace('\\', '.')
        full_module_name = f"{module_name}.{entry_stem}"

        try:
            module = sys.modules.get(full_module_name)
            if module:
                module = importlib.reload(module)
            else:
                module = importlib.import_module(full_module_name)

            if hasattr(module, "init"):
                init_func = module.init
                if len(inspect.signature(init_func).parameters) > 0:
                    result = init_func(self.build_context(definition.name))
                else:
                    result = init_func()

                if asyncio.iscoroutine(result):
                    # 为异步 init 添加超时控制
                    init_task = asyncio.create_task(asyncio.wait_for(result, PLUGIN_INIT_TIMEOUT_SECONDS))
                    self._init_tasks.append(init_task)
            return module
        except asyncio.TimeoutError:
            raise PluginLoadError(
                definition.name,
                f"Plugin init timeout (> {PLUGIN_INIT_TIMEOUT_SECONDS}s)",
                None
            ) from None
        except Exception as exc:
            raise PluginLoadError(definition.name, "Failed to load plugin", exc) from exc

    def _register_commands(self, definition: PluginDefinition, module: ModuleType) -> None:
        if not hasattr(module, "handle"):
            logger.warning("Plugin %s missing handle()", definition.name)
            return
        for command in definition.commands:
            spec = CommandSpec(
                plugin=definition.name,
                name=command.get("name", ""),
                triggers=command.get("triggers", []),
                help_text=command.get("help", ""),
                admin_only=command.get("admin_only", False),
                handler=module.handle,
                priority=command.get("priority", 0),
            )
            self.router.register(spec)

    def _get_mtime(self, plugin_dir: Path, definition: PluginDefinition) -> float:
        """获取插件文件的修改时间（同步版本，用于启动时加载）"""
        entry_path = plugin_dir / definition.entry
        definition_path = plugin_dir / "plugin.json"
        return max(entry_path.stat().st_mtime, definition_path.stat().st_mtime)

    async def _get_mtime_async(self, plugin_dir: Path, definition: PluginDefinition) -> float:
        """获取插件文件的修改时间（异步版本，用于监控时避免阻塞事件循环）"""
        entry_path = plugin_dir / definition.entry
        definition_path = plugin_dir / "plugin.json"
        # 使用 asyncio.to_thread 在线程池中执行文件 I/O
        entry_mtime, def_mtime = await asyncio.to_thread(
            lambda: (entry_path.stat().st_mtime, definition_path.stat().st_mtime)
        )
        return max(entry_mtime, def_mtime)

    def build_context(
        self,
        plugin_name: str,
        user_id: Optional[int] = None,
        group_id: Optional[int] = None,
        request_id: Optional[str] = None,
    ) -> PluginContextProtocol:
        plugin_dir = self.plugins_dir / plugin_name
        data_dir = plugin_dir / "data"
        ensure_dir(data_dir)
        
        # 获取或创建插件状态
        state = self._plugin_states.setdefault(plugin_name, {})
        
        return self.context_factory(plugin_name, plugin_dir, data_dir, state, user_id, group_id, request_id)

    def schedule_definitions(self) -> list[LoadedPlugin]:
        return list(self._plugins.values())
