import asyncio
import hashlib
import importlib
import inspect
import logging
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any, Callable, Mapping, Optional

from .constants import PLUGIN_INIT_TIMEOUT_SECONDS, VALID_PLUGIN_NAME_PATTERN
from .exceptions import PluginLoadError
from .interfaces import PluginContextProtocol, PluginPrincipal
from .models import PluginManifest
from .plugin_base import ensure_dir, load_json
from .plugin_execution import (
    PluginConcurrency,
    PluginExecutionDrainResult,
    PluginExecutionGate,
    PluginExecutionPolicy,
    call_plugin_callback,
    invoke_loaded_plugin,
)
from .router import CommandRouter, CommandSpec

logger = logging.getLogger(__name__)
_TRUSTED_ADMIN_TIMEOUT_EXEMPT_PLUGINS = frozenset({"codex", "jupyter", "qingssh", "shell"})


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
    concurrency: PluginConcurrency
    enabled: bool = True  # 插件是否启用
    description: str | None = None
    author: str | None = None
    dependencies: list[str] | None = None
    services: tuple["PluginServiceDefinition", ...] = ()


@dataclass(frozen=True, slots=True)
class PluginServiceDefinition:
    name: str
    callback: str
    callers: frozenset[str]
    required_capability: str | None = None


@dataclass(frozen=True, slots=True)
class LoadedPluginService:
    owner: str
    definition: PluginServiceDefinition
    callback: Callable[..., Any]


@dataclass
class LoadedPlugin:
    definition: PluginDefinition
    module: ModuleType
    mtime: int | float
    execution_gate: PluginExecutionGate | None = None
    services: Mapping[str, LoadedPluginService] = field(
        default_factory=lambda: MappingProxyType({}),
    )


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
        self._services: dict[str, LoadedPluginService] = {}
        self._poll_interval = float(poll_interval)
        self._change_handlers: list[Any] = []
        self._init_tasks: list[asyncio.Task[None]] = []
        self._init_task_plugins: dict[asyncio.Task[None], str] = {}
        self._pending_plugins: dict[
            asyncio.Task[None], tuple[PluginDefinition, ModuleType, float]
        ] = {}
        self._plugin_states: dict[str, dict[str, Any]] = {}
        self._execution_gates: dict[str, PluginExecutionGate] = {}
        self._quarantined_plugins: set[str] = set()
        self._execution_policy = PluginExecutionPolicy()
        self._execution_policy_overrides: dict[str, dict[str, Any]] = {
            name: {"timeout_seconds": 0} for name in _TRUSTED_ADMIN_TIMEOUT_EXEMPT_PLUGINS
        }
        self._ensured_data_dirs: set[str] = set()

        # 一次性设置 sys.path（使用绝对路径防止路径遍历攻击）
        self._setup_sys_path()

    def _setup_sys_path(self) -> None:
        """将 plugins 目录添加到 sys.path（仅一次）

        Note: Using sys.path instead of importlib.spec_from_file_location
        because plugins rely on relative imports (e.g., from .submodule import X).
        The tradeoff is potential module name conflicts with stdlib/third-party packages.
        Plugin directory names should be chosen to avoid such conflicts.
        """
        import os
        import sys

        # Plugins are always imported as ``plugins.<plugin_name>``.  Adding the
        # *parent* of the package (rather than the plugins directory itself)
        # prevents the same file from being importable both as ``pendo.main``
        # and ``plugins.pendo.main``.  The latter is our one canonical module
        # name and is particularly important for plugins with module state.
        project_root = os.path.abspath(self.plugins_dir.parent)
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
            logger.debug("Added %s to sys.path", project_root)

        # ``plugins`` may already be imported by another manager (notably in
        # tests that create an isolated plugin root).  Extend its package path
        # explicitly so the canonical namespace still resolves this manager's
        # plugins instead of falling back to a top-level alias.
        package = importlib.import_module("plugins")
        package_path = str(self.plugins_dir)
        paths = package.__path__  # type: ignore[attr-defined]
        if package_path not in paths:
            paths.insert(0, package_path)

    def on_change(self, handler) -> None:
        self._change_handlers.append(handler)

    def update_poll_interval(self, poll_interval: float) -> None:
        self._poll_interval = float(poll_interval)

    def configure_execution(self, raw_policy: dict[str, Any] | None) -> None:
        """Apply global and per-plugin callback limits from runtime config."""

        raw_policy = raw_policy if isinstance(raw_policy, dict) else {}
        self._execution_policy = PluginExecutionPolicy.from_mapping(raw_policy)
        raw_overrides = raw_policy.get("overrides", {})
        configured_overrides = (
            {
                str(name): values
                for name, values in raw_overrides.items()
                if isinstance(name, str) and isinstance(values, dict)
            }
            if isinstance(raw_overrides, dict)
            else {}
        )
        self._execution_policy_overrides = {
            name: {"timeout_seconds": 0} for name in _TRUSTED_ADMIN_TIMEOUT_EXEMPT_PLUGINS
        }
        for name, values in configured_overrides.items():
            self._execution_policy_overrides[name] = {
                **self._execution_policy_overrides.get(name, {}),
                **values,
            }

        for name, gate in self._execution_gates.items():
            gate.set_policy(self._execution_policy_for(name))

    def _execution_policy_for(self, plugin_name: str) -> PluginExecutionPolicy:
        return PluginExecutionPolicy.from_mapping(
            self._execution_policy_overrides.get(plugin_name),
            fallback=self._execution_policy,
        )

    def _notify_change(self, name: str) -> None:
        for handler in self._change_handlers:
            try:
                handler(name)
            except Exception as exc:
                logger.warning("Plugin change handler failed: %s", exc)

    def list_plugins(self) -> list[str]:
        return sorted(self._plugins.keys())

    def _quarantine_undrained_gate(
        self,
        name: str,
        plugin: LoadedPlugin | None,
        gate: PluginExecutionGate,
        result: PluginExecutionDrainResult,
        *,
        phase: str,
    ) -> None:
        """Keep code/state/modules alive while an executor callback still runs."""

        if plugin is not None:
            self._plugins[name] = plugin
            plugin.execution_gate = gate
        self._execution_gates[name] = gate
        self.router.clear_plugin(name)
        self._quarantined_plugins.add(name)
        logger.warning(
            "Plugin %s quarantined during %s: async=%d sync=%d waited=%.2fs",
            name,
            phase,
            result.pending_async_tasks,
            result.pending_sync_callbacks,
            result.waited_seconds,
        )
        self._notify_change(name)

    def get(self, name: str) -> Optional[LoadedPlugin]:
        return self._plugins.get(name)

    @staticmethod
    def _bind_declared_services(
        definition: PluginDefinition,
        module: ModuleType,
    ) -> Mapping[str, LoadedPluginService]:
        bindings: dict[str, LoadedPluginService] = {}
        for service in definition.services:
            callback = getattr(module, service.callback, None)
            if not callable(callback):
                raise PluginLoadError(
                    definition.name,
                    f"Declared service callback is unavailable: {service.callback}",
                )
            bindings[service.name] = LoadedPluginService(
                owner=definition.name,
                definition=service,
                callback=callback,
            )
        return MappingProxyType(bindings)

    def _unregister_services_owned_by(self, plugin_name: str) -> None:
        for service_name, binding in list(self._services.items()):
            if binding.owner == plugin_name:
                self._services.pop(service_name, None)

    def resolve_service(
        self,
        *,
        caller_plugin: str,
        service_name: str,
        granted_capabilities: frozenset[str] = frozenset(),
    ) -> tuple[LoadedPlugin, LoadedPluginService]:
        """Resolve the current immutable binding and recheck lifecycle policy.

        This method is deliberately available only to the core.  Plugin
        contexts receive fixed, typed capability objects and cannot provide an
        arbitrary service or callback name.
        """

        binding = self._services.get(service_name)
        if binding is None:
            raise RuntimeError(f"plugin service is unavailable: {service_name}")
        loaded = self._plugins.get(binding.owner)
        if loaded is None or loaded.services.get(service_name) is not binding:
            raise RuntimeError(f"plugin service is stale: {service_name}")
        gate = loaded.execution_gate
        if (
            not loaded.definition.enabled
            or binding.owner in self._quarantined_plugins
            or gate is None
            or gate.closed
        ):
            raise RuntimeError(f"plugin service is not accepting calls: {service_name}")
        if caller_plugin not in binding.definition.callers:
            raise PermissionError(
                f"plugin {caller_plugin} is not allowed to call service {service_name}"
            )
        required = binding.definition.required_capability
        if required is not None and required not in granted_capabilities:
            raise PermissionError(f"service {service_name} requires capability {required}")
        return loaded, binding

    def _is_plugin_dir(self, path: Path) -> bool:
        """检查是否为有效的插件目录（排除特殊目录与 deprecated 目录）"""
        name = path.name
        if name.startswith("__") or name.startswith("."):
            return False
        if name.endswith("_deprecated"):
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
        failed_plugins: set[str] = set()
        for task, result in zip(tasks, results):
            plugin_name = self._init_task_plugins.pop(task, None)
            pending = self._pending_plugins.pop(task, None)
            if isinstance(result, BaseException):
                if isinstance(result, (KeyboardInterrupt, SystemExit)):
                    logger.warning("Plugin init interrupted")
                    raise result
                if isinstance(result, asyncio.CancelledError):
                    logger.debug("Plugin init task cancelled")
                    continue
                if plugin_name:
                    failed_plugins.add(plugin_name)
                if isinstance(result, asyncio.TimeoutError):
                    logger.warning(
                        "Plugin %s init timed out (>%ss)",
                        plugin_name or "<unknown>",
                        PLUGIN_INIT_TIMEOUT_SECONDS,
                    )
                else:
                    logger.warning("Plugin init error: %s", result)
                if plugin_name:
                    self._purge_plugin_modules(plugin_name)
            elif pending:
                definition, module, mtime = pending
                self._register_loaded_plugin(definition, module, mtime)
        for plugin_name in failed_plugins:
            if plugin_name in self._plugins:
                logger.warning(
                    "Plugin %s init failed; unloading partially initialized plugin", plugin_name
                )
                await self.unload_plugin(plugin_name)

    def load_plugin(self, plugin_dir: Path) -> None:
        # 验证插件目录名是否安全
        if not _validate_plugin_name(plugin_dir.name):
            logger.warning(
                "Skipping plugin with invalid name '%s': must match %s",
                plugin_dir.name,
                VALID_PLUGIN_NAME_PATTERN,
            )
            return

        definition = self._load_definition(plugin_dir)
        if not definition:
            return
        # 检查插件是否启用
        if not definition.enabled:
            logger.info("Plugin %s is disabled, skipping", definition.name)
            return
        self._ensure_plugin_data_dir(definition.name, force=True)
        try:
            module, init_task = self._load_module(plugin_dir, definition)
        except PluginLoadError as exc:
            self._execution_gates.pop(definition.name, None)
            logger.error("%s", exc, exc_info=True)
            return
        if not module:
            self._execution_gates.pop(definition.name, None)
            return
        mtime = self._get_mtime(plugin_dir, definition)
        if init_task:
            self._pending_plugins[init_task] = (definition, module, mtime)
        else:
            self._register_loaded_plugin(definition, module, mtime)

    async def unload_plugin(self, name: str) -> None:
        tasks_to_cancel = [
            task
            for task, plugin_name in list(self._init_task_plugins.items())
            if plugin_name == name
        ]
        for task in tasks_to_cancel:
            self._init_task_plugins.pop(task, None)
            if task in self._init_tasks:
                self._init_tasks.remove(task)
            if not task.done():
                task.cancel()
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
        for task, pending in list(self._pending_plugins.items()):
            definition, _, _ = pending
            if definition.name == name:
                self._pending_plugins.pop(task, None)

        plugin = self._plugins.get(name)
        gate = self._execution_gates.get(name) or (
            plugin.execution_gate if plugin is not None else None
        )
        if gate is not None:
            drain = await gate.close()
            if not drain.drained:
                self._quarantine_undrained_gate(
                    name,
                    plugin,
                    gate,
                    drain,
                    phase="unload admission drain",
                )
                return

        plugin = self._plugins.pop(name, None)
        if not plugin:
            self._unregister_services_owned_by(name)
            self._plugin_states.pop(name, None)
            self._execution_gates.pop(name, None)
            self._quarantined_plugins.discard(name)
            self._purge_plugin_modules(name)
            return
        self.router.clear_plugin(name)

        if hasattr(plugin.module, "shutdown"):
            try:
                shutdown = plugin.module.shutdown

                async def run_shutdown() -> None:
                    if len(inspect.signature(shutdown).parameters) > 0:
                        await call_plugin_callback(shutdown, self.build_context(name))
                    else:
                        await call_plugin_callback(shutdown)

                await asyncio.wait_for(
                    invoke_loaded_plugin(plugin, run_shutdown, allow_closed=True),
                    timeout=PLUGIN_INIT_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Plugin %s shutdown timed out (>%ss)", name, PLUGIN_INIT_TIMEOUT_SECONDS
                )
            except Exception as exc:
                logger.warning("Plugin %s shutdown error: %s", name, exc)

        if gate is not None:
            shutdown_drain = await gate.close()
            if not shutdown_drain.drained:
                self._quarantine_undrained_gate(
                    name,
                    plugin,
                    gate,
                    shutdown_drain,
                    phase="shutdown callback drain",
                )
                return

        # 清理插件状态
        self._plugin_states.pop(name, None)
        self._execution_gates.pop(name, None)
        self._quarantined_plugins.discard(name)
        self._unregister_services_owned_by(name)

        # 清理 sys.modules 中的相关模块，确保 reload 能加载新代码
        self._purge_plugin_modules(name)

        logger.info("Unloaded plugin %s", name)
        self._notify_change(name)

    async def reload_plugin(self, name: str) -> None:
        old_plugin = self._plugins.get(name)
        if old_plugin is None:
            return

        plugin_dir = self.plugins_dir / name
        definition = self._load_definition(plugin_dir)
        if definition is None or not definition.enabled:
            self._unregister_services_owned_by(name)
            logger.warning("Plugin reload rejected for %s: invalid or disabled definition", name)
            return

        staged_state: dict[str, Any] = {}
        staged_gate = PluginExecutionGate(
            definition.concurrency,
            plugin_name=definition.name,
            policy=self._execution_policy_for(definition.name),
        )
        try:
            staged_module, staged_package = self._load_shadow_module(plugin_dir, definition)
            await self._initialize_shadow_plugin(
                definition, staged_module, staged_gate, staged_state
            )
        except Exception as exc:
            logger.warning("Plugin %s shadow reload failed; keeping old instance: %s", name, exc)
            await staged_gate.close()
            self._purge_shadow_modules(locals().get("staged_package"))
            return

        staged_plugin = LoadedPlugin(
            definition=definition,
            module=staged_module,
            mtime=self._get_mtime(plugin_dir, definition),
            execution_gate=staged_gate,
        )
        old_state = self._plugin_states.setdefault(name, {})
        old_state_snapshot = dict(old_state)
        canonical_prefix = f"plugins.{name}"
        old_modules = {
            module_name: module
            for module_name, module in list(sys.modules.items())
            if module_name == canonical_prefix or module_name.startswith(f"{canonical_prefix}.")
        }

        # Stop admission before teardown starts.  ``close`` is terminal and
        # drains/cancels work already admitted through the old instance; the
        # shutdown callback itself is still allowed through the closed gate by
        # ``_shutdown_plugin_instance(..., allow_closed=True)``.
        old_gate = old_plugin.execution_gate or self._execution_gates.get(name)
        if old_gate is not None:
            old_plugin.execution_gate = old_gate
            old_drain = await old_gate.close()
            if not old_drain.drained:
                self._quarantine_undrained_gate(
                    name,
                    old_plugin,
                    old_gate,
                    old_drain,
                    phase="reload admission drain",
                )
                await self._dispose_staged_plugin(
                    name,
                    staged_plugin,
                    staged_state,
                    staged_package,
                )
                return
        try:
            old_shutdown_ok = await self._shutdown_plugin_instance(name, old_plugin)
        except BaseException:
            self._plugins[name] = old_plugin
            if old_gate is not None:
                self._execution_gates[name] = old_gate
            self._quarantined_plugins.add(name)
            self._notify_change(name)
            await self._dispose_staged_plugin(
                name,
                staged_plugin,
                staged_state,
                staged_package,
            )
            raise
        if not old_shutdown_ok:
            # A terminally closed gate cannot safely be reopened, and a failed
            # shutdown may already have torn down only part of the instance.
            # Keep the exact old instance/state/modules quarantined behind its
            # closed gate.  Removing it would make the watcher auto-load a new
            # copy beside resources that the failed shutdown may have leaked.
            logger.warning("Plugin %s shutdown failed; quarantining old instance", name)
            self._plugins[name] = old_plugin
            if old_gate is not None:
                self._execution_gates[name] = old_gate
            self._quarantined_plugins.add(name)
            self._notify_change(name)
            await self._dispose_staged_plugin(
                name,
                staged_plugin,
                staged_state,
                staged_package,
            )
            return

        # The shadow instance exists only to validate import/init while the old
        # canonical instance remains available.  It must never become the live
        # plugin: doing so would leave relative imports and globals under a
        # second namespace.  Shut it down and load one fresh canonical module.
        try:
            staged_shutdown_ok = await self._dispose_staged_plugin(
                name,
                staged_plugin,
                staged_state,
                staged_package,
            )
        except BaseException:
            self._plugins[name] = old_plugin
            if old_gate is not None:
                self._execution_gates[name] = old_gate
            self._quarantined_plugins.add(name)
            self._notify_change(name)
            raise
        if not staged_shutdown_ok:
            logger.warning("Plugin %s shadow shutdown failed; quarantining old instance", name)
            self._plugins[name] = old_plugin
            if old_gate is not None:
                self._execution_gates[name] = old_gate
            self._quarantined_plugins.add(name)
            self._notify_change(name)
            return

        # Build and initialize the canonical replacement while the old
        # registry/router entries remain intact (their terminally closed gate
        # keeps them unavailable during the short swap window).  Registry and
        # command routing are committed only after canonical init succeeds.
        candidate_state: dict[str, Any] = {}
        candidate_gate = PluginExecutionGate(
            definition.concurrency,
            plugin_name=definition.name,
            policy=self._execution_policy_for(definition.name),
        )
        self._plugin_states[name] = candidate_state
        self._execution_gates[name] = candidate_gate
        self._purge_plugin_modules(name)
        try:
            candidate = await self._load_canonical_candidate(
                plugin_dir,
                definition,
                candidate_gate,
            )
        except BaseException as exc:
            entry_stem = definition.entry.removesuffix(".py").replace("/", ".").replace("\\", ".")
            candidate_module = sys.modules.get(f"plugins.{name}.{entry_stem}")
            if isinstance(candidate_module, ModuleType):
                await self._shutdown_plugin_instance(
                    name,
                    LoadedPlugin(
                        definition=definition,
                        module=candidate_module,
                        mtime=0,
                        execution_gate=candidate_gate,
                    ),
                    state=candidate_state,
                )
            await candidate_gate.close()
            self._purge_plugin_modules(name)
            sys.modules.update(old_modules)

            # Shutdown callbacks are allowed to mutate the state mapping.  Put
            # its pre-reload entries back before reinitializing the old module.
            old_state.clear()
            old_state.update(old_state_snapshot)
            recovery_gate = PluginExecutionGate(
                old_plugin.definition.concurrency,
                plugin_name=old_plugin.definition.name,
                policy=self._execution_policy_for(old_plugin.definition.name),
            )
            old_plugin.execution_gate = recovery_gate
            self._plugin_states[name] = old_state
            self._execution_gates[name] = recovery_gate
            try:
                await self._initialize_shadow_plugin(
                    old_plugin.definition,
                    old_plugin.module,
                    recovery_gate,
                    old_state,
                )
            except BaseException as restore_exc:
                await recovery_gate.close()
                self._plugins[name] = old_plugin
                self._execution_gates[name] = recovery_gate
                self._plugin_states[name] = old_state
                self._quarantined_plugins.add(name)
                self._notify_change(name)
                logger.error(
                    "Plugin %s canonical reload failed and old instance could not be restored; quarantined: %s",
                    name,
                    restore_exc,
                )
            else:
                self._plugins[name] = old_plugin
                self._quarantined_plugins.discard(name)
                logger.error(
                    "Plugin %s canonical reload failed; restored old instance: %s",
                    name,
                    exc,
                )
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            return

        self.router.clear_plugin(name)
        self._register_loaded_plugin(
            candidate.definition,
            candidate.module,
            candidate.mtime,
        )
        logger.info("Reloaded canonical plugin %s version %s", name, definition.version)

    async def _load_canonical_candidate(
        self,
        plugin_dir: Path,
        definition: PluginDefinition,
        gate: PluginExecutionGate,
    ) -> LoadedPlugin:
        """Import and fully initialize a canonical module without registering it."""

        module, init_task = self._load_module(plugin_dir, definition)
        if module is None:
            raise PluginLoadError(definition.name, "Canonical entry could not be loaded")
        if init_task is not None:
            try:
                await init_task
            finally:
                if init_task in self._init_tasks:
                    self._init_tasks.remove(init_task)
                self._init_task_plugins.pop(init_task, None)
                self._pending_plugins.pop(init_task, None)
        return LoadedPlugin(
            definition=definition,
            module=module,
            mtime=self._get_mtime(plugin_dir, definition),
            execution_gate=gate,
        )

    async def _dispose_staged_plugin(
        self,
        name: str,
        plugin: LoadedPlugin,
        state: dict[str, Any],
        package_name: str,
    ) -> bool:
        """Best-effort staged shutdown with unconditional gate/module cleanup."""

        try:
            return await self._shutdown_plugin_instance(name, plugin, state=state)
        finally:
            gate = plugin.execution_gate
            if gate is not None:
                await gate.close()
            self._purge_shadow_modules(package_name)

    def _load_shadow_module(
        self,
        plugin_dir: Path,
        definition: PluginDefinition,
    ) -> tuple[ModuleType, str]:
        """Load a plugin under a temporary package so relative imports stay isolated."""
        entry_path = plugin_dir / definition.entry
        if not entry_path.exists():
            raise PluginLoadError(definition.name, f"Entry missing: {entry_path}")
        package_name = f"_xiaoqing_shadow_{definition.name}_{uuid.uuid4().hex}"
        package = ModuleType(package_name)
        package.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
        package.__package__ = package_name
        sys.modules[package_name] = package
        entry_stem = definition.entry.removesuffix(".py").replace("/", ".").replace("\\", ".")
        module_name = f"{package_name}.{entry_stem}"
        spec = importlib.util.spec_from_file_location(module_name, entry_path)
        if spec is None or spec.loader is None:
            self._purge_shadow_modules(package_name)
            raise PluginLoadError(definition.name, "Could not build shadow module spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            self._bind_declared_services(definition, module)
        except Exception:
            self._purge_shadow_modules(package_name)
            raise
        return module, package_name

    async def _initialize_shadow_plugin(
        self,
        definition: PluginDefinition,
        module: ModuleType,
        gate: PluginExecutionGate,
        state: dict[str, Any],
    ) -> None:
        init_func = getattr(module, "init", None)
        if init_func is None:
            return
        plugin_dir = self.plugins_dir / definition.name
        context = self.context_factory(
            definition.name,
            plugin_dir,
            self._ensure_plugin_data_dir(definition.name),
            state,
            None,
            None,
            None,
        )

        async def run_init() -> None:
            if len(inspect.signature(init_func).parameters) > 0:
                await call_plugin_callback(init_func, context)
            else:
                await call_plugin_callback(init_func)

        await asyncio.wait_for(gate.run(run_init), timeout=PLUGIN_INIT_TIMEOUT_SECONDS)

    async def _shutdown_plugin_instance(
        self,
        name: str,
        plugin: LoadedPlugin,
        *,
        state: dict[str, Any] | None = None,
    ) -> bool:
        shutdown = getattr(plugin.module, "shutdown", None)
        if shutdown is None:
            return True
        try:
            context = None
            if len(inspect.signature(shutdown).parameters) > 0:
                context = self.context_factory(
                    name,
                    self.plugins_dir / name,
                    self._ensure_plugin_data_dir(name),
                    self._plugin_states.get(name, {}) if state is None else state,
                    None,
                    None,
                    None,
                )

            async def run_shutdown() -> None:
                if context is None:
                    await call_plugin_callback(shutdown)
                else:
                    await call_plugin_callback(shutdown, context)

            await asyncio.wait_for(
                invoke_loaded_plugin(plugin, run_shutdown, allow_closed=True),
                timeout=PLUGIN_INIT_TIMEOUT_SECONDS,
            )
            return True
        except Exception as exc:
            logger.warning("Plugin %s shutdown error during reload: %s", name, exc)
            return False

    @staticmethod
    def _purge_shadow_modules(package_name: str | None) -> None:
        if not package_name:
            return
        for module_name in list(sys.modules):
            if module_name == package_name or module_name.startswith(f"{package_name}."):
                del sys.modules[module_name]

    async def watch(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            plugin_dirs = await asyncio.to_thread(
                lambda: [p for p in self.plugins_dir.iterdir() if self._is_plugin_dir(p)]
            )
            current_names = {plugin_dir.name for plugin_dir in plugin_dirs}
            for existing_name in list(self._plugins):
                if existing_name not in current_names:
                    logger.info("Detected deleted plugin %s", existing_name)
                    await self.unload_plugin(existing_name)
            for plugin_dir in plugin_dirs:
                if not self._is_plugin_dir(plugin_dir):
                    continue
                definition = await asyncio.to_thread(self._load_definition, plugin_dir)
                if not definition:
                    continue
                mtime = await self._get_mtime_async(plugin_dir, definition)
                existing = self._plugins.get(definition.name)
                if definition.name in self._quarantined_plugins:
                    logger.debug(
                        "Skipping automatic reload of quarantined plugin %s", definition.name
                    )
                    continue
                if not existing:
                    self.load_plugin(plugin_dir)
                    await self.wait_inits()
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

        if not self._dependencies_available(manifest):
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
            description=manifest.description,
            author=manifest.author,
            dependencies=[dependency.name for dependency in manifest.dependencies],
            services=tuple(
                PluginServiceDefinition(
                    name=service.name,
                    callback=service.callback,
                    callers=frozenset(service.callers),
                    required_capability=service.required_capability,
                )
                for service in manifest.services
            ),
        )

    @staticmethod
    def _dependencies_available(manifest: PluginManifest) -> bool:
        """Fail closed when a manifest's required Python modules are unavailable."""

        for dependency in manifest.dependencies:
            try:
                available = importlib.util.find_spec(dependency.name) is not None
            except (ImportError, ModuleNotFoundError, ValueError):
                available = False
            if available:
                continue
            if dependency.required:
                logger.error(
                    "Plugin %s requires Python dependency %s, but it is not importable",
                    manifest.name,
                    dependency.name,
                )
                return False
            logger.info(
                "Plugin %s optional Python dependency %s is unavailable",
                manifest.name,
                dependency.name,
            )
        return True

    def _load_module(
        self, plugin_dir: Path, definition: PluginDefinition
    ) -> tuple[Optional[ModuleType], asyncio.Task[None] | None]:
        entry_path = plugin_dir / definition.entry
        if not entry_path.exists():
            logger.error("Plugin %s entry missing: %s", definition.name, entry_path)
            return None, None

        # Import through the repository's canonical namespace.  Do not add the
        # plugins directory itself to sys.path: doing so creates a second
        # top-level package (for example ``pendo``) with independent globals.
        module_name = f"plugins.{plugin_dir.name}"
        entry_stem = definition.entry.removesuffix(".py").replace("/", ".").replace("\\", ".")
        full_module_name = f"{module_name}.{entry_stem}"

        aliases = self._plugin_module_aliases(plugin_dir, definition.name)
        if aliases:
            raise PluginLoadError(
                definition.name,
                f"Non-canonical plugin module aliases detected: {', '.join(aliases)}",
            )

        gate = self._execution_gate_for(definition)
        try:
            module = sys.modules.get(full_module_name)
            if module:
                module = importlib.reload(module)
            else:
                module = importlib.import_module(full_module_name)

            aliases = self._plugin_module_aliases(plugin_dir, definition.name)
            if aliases:
                raise PluginLoadError(
                    definition.name,
                    f"Non-canonical plugin module aliases detected: {', '.join(aliases)}",
                )

            self._bind_declared_services(definition, module)

            if hasattr(module, "init"):
                init_func = module.init

                async def run_init() -> None:
                    if len(inspect.signature(init_func).parameters) > 0:
                        await call_plugin_callback(init_func, self.build_context(definition.name))
                    else:
                        await call_plugin_callback(init_func)

                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    # Synchronous embedding remains supported; async plugin
                    # initialization has always required an event loop.
                    if len(inspect.signature(init_func).parameters) > 0:
                        result = init_func(self.build_context(definition.name))
                    else:
                        result = init_func()
                    if inspect.isawaitable(result):
                        raise RuntimeError("async plugin init requires a running event loop")
                else:
                    init_task = asyncio.create_task(
                        asyncio.wait_for(
                            gate.run(run_init),
                            timeout=PLUGIN_INIT_TIMEOUT_SECONDS,
                        )
                    )
                    self._init_tasks.append(init_task)
                    self._init_task_plugins[init_task] = definition.name
                    return module, init_task
            return module, None
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
                usage=command.get("usage"),
            )
            self.router.register(spec)

    def _get_mtime(self, plugin_dir: Path, definition: PluginDefinition) -> int:
        """获取插件文件的聚合修改指纹。

        指纹包含相对路径、`st_mtime_ns` 与文件大小，避免多个文件 mtime
        一增一减时被简单求和抵消。
        """
        digest = hashlib.blake2b(digest_size=16)
        for path in sorted(
            self._iter_watch_files(plugin_dir, definition), key=lambda item: item.as_posix()
        ):
            stat_result = path.stat()
            try:
                relative = path.relative_to(plugin_dir).as_posix()
            except ValueError:
                relative = path.as_posix()
            digest.update(relative.encode("utf-8", errors="surrogatepass"))
            digest.update(b"\0")
            digest.update(str(stat_result.st_mtime_ns).encode("ascii"))
            digest.update(b"\0")
            digest.update(str(stat_result.st_size).encode("ascii"))
            digest.update(b"\0")
        return int.from_bytes(digest.digest(), "big")

    async def _get_mtime_async(self, plugin_dir: Path, definition: PluginDefinition) -> int:
        """获取插件文件的修改时间（异步版本，用于监控时避免阻塞事件循环）"""
        return await asyncio.to_thread(self._get_mtime, plugin_dir, definition)

    def _iter_watch_files(self, plugin_dir: Path, definition: PluginDefinition) -> list[Path]:
        files: list[Path] = []
        for path in plugin_dir.rglob("*"):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts:
                continue
            if "data" in path.parts:
                continue
            if path.suffix.lower() not in {".py", ".json"}:
                continue
            files.append(path)
        if not files:
            files.extend([plugin_dir / definition.entry, plugin_dir / "plugin.json"])
        return files

    def _register_loaded_plugin(
        self, definition: PluginDefinition, module: ModuleType, mtime: float
    ) -> None:
        services = self._bind_declared_services(definition, module)
        for service_name in services:
            existing = self._services.get(service_name)
            if existing is not None and existing.owner != definition.name:
                raise PluginLoadError(
                    definition.name,
                    f"Service name is already registered by {existing.owner}: {service_name}",
                )
        self._register_commands(definition, module)
        loaded = LoadedPlugin(
            definition=definition,
            module=module,
            mtime=mtime,
            execution_gate=self._execution_gate_for(definition),
            services=services,
        )
        self._unregister_services_owned_by(definition.name)
        self._services.update(services)
        self._plugins[definition.name] = loaded
        self._quarantined_plugins.discard(definition.name)
        logger.info(
            "Loaded plugin name=%s version=%s author=%s description=%s",
            definition.name,
            definition.version,
            definition.author or "-",
            definition.description or "-",
        )
        self._notify_change(definition.name)

    def _execution_gate_for(self, definition: PluginDefinition) -> PluginExecutionGate:
        gate = self._execution_gates.get(definition.name)
        if gate is None:
            gate = PluginExecutionGate(
                definition.concurrency,
                plugin_name=definition.name,
                policy=self._execution_policy_for(definition.name),
            )
            self._execution_gates[definition.name] = gate
        return gate

    def _purge_plugin_modules(self, name: str) -> None:
        import sys

        to_delete = []
        canonical_name = f"plugins.{name}"
        prefix = f"{canonical_name}."
        plugin_dir = (self.plugins_dir / name).resolve(strict=False)
        for mod_name, module in list(sys.modules.items()):
            if mod_name == canonical_name or mod_name.startswith(prefix):
                to_delete.append(mod_name)
                continue
            if mod_name.startswith(f"_xiaoqing_shadow_{name}_"):
                continue
            module_file = getattr(module, "__file__", None)
            if not module_file:
                continue
            try:
                Path(module_file).resolve(strict=False).relative_to(plugin_dir)
            except (OSError, ValueError):
                continue
            to_delete.append(mod_name)

        for mod_name in to_delete:
            del sys.modules[mod_name]

    @staticmethod
    def _plugin_module_aliases(plugin_dir: Path, name: str) -> list[str]:
        """Return non-canonical module names backed by files in one plugin."""
        canonical = f"plugins.{name}"
        root = plugin_dir.resolve(strict=False)
        aliases: list[str] = []
        for module_name, module in list(sys.modules.items()):
            if module_name == canonical or module_name.startswith(f"{canonical}."):
                continue
            if module_name.startswith(f"_xiaoqing_shadow_{name}_"):
                continue
            module_file = getattr(module, "__file__", None)
            if not module_file:
                continue
            try:
                Path(module_file).resolve(strict=False).relative_to(root)
            except (OSError, ValueError):
                continue
            aliases.append(module_name)
        return sorted(aliases)

    def _plugin_data_dir(self, plugin_name: str) -> Path:
        return self.plugins_dir / plugin_name / "data"

    def _ensure_plugin_data_dir(self, plugin_name: str, *, force: bool = False) -> Path:
        data_dir = self._plugin_data_dir(plugin_name)
        if force or plugin_name not in self._ensured_data_dirs:
            ensure_dir(data_dir)
            self._ensured_data_dirs.add(plugin_name)
        return data_dir

    def build_context(
        self,
        plugin_name: str,
        user_id: Optional[int] = None,
        group_id: Optional[int] = None,
        request_id: Optional[str] = None,
        principal: PluginPrincipal | None = None,
    ) -> PluginContextProtocol:
        plugin_dir = self.plugins_dir / plugin_name
        data_dir = self._ensure_plugin_data_dir(plugin_name)

        # 获取或创建插件状态
        state = self._plugin_states.setdefault(plugin_name, {})

        return self.context_factory(
            plugin_name,
            plugin_dir,
            data_dir,
            state,
            user_id,
            group_id,
            request_id,
            principal,
        )

    def schedule_definitions(self) -> list[LoadedPlugin]:
        return list(self._plugins.values())
