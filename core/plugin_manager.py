"""插件发现、授权导入、生命周期切换与热重载管理。

插件源码在导入前后都要通过路径、内容快照和命名空间归属检查；加载、替换与卸载
则按代数隔离执行中的回调，避免旧插件在新版本生效后继续获得运行权限。
"""

import asyncio
import importlib
import logging
import os
import sys
import threading
import weakref
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from .constants import DEFAULT_PLUGIN_POLL_INTERVAL_SECONDS
from .interfaces import PluginContextProtocol, PluginPrincipal
from .lifecycle import LazyAsyncLock as _LazyAsyncLock
from .log import LogEvent
from .plugin_data import PluginDataMixin
from .plugin_execution import (
    DEFAULT_GLOBAL_SYNC_QUEUE_LIMIT,
    DEFAULT_SYNC_WORKERS,
    PluginExecutionGate,
    PluginExecutionPolicy,
    PluginSyncBroker,
)
from .plugin_generation import PluginGenerationMixin
from .plugin_manager_support import (
    _PLUGIN_IMPORT_BARRIER_COORDINATOR,
    _PLUGIN_IMPORT_DRAIN_TIMEOUT_SECONDS,
    _PLUGIN_IMPORT_LOCK,
    _PLUGIN_NAMESPACE_GUARD,
    _PLUGIN_NAMESPACE_OWNERS,
    LoadedPlugin,
    LoadedPluginService,
    PluginDefinition,
    PluginPathError,
    _acquire_process_import_path,
    _install_namespace_tombstone_locked,
    _meta_path_identity_index,
    _PluginDataDirectory,
    _ProcessImportPathLeaseKey,
    _release_process_import_paths,
    _SourceOnlyPluginFinder,
)
from .plugin_runtime import PluginRuntimeMixin, _reason_code
from .plugin_watcher import PluginWatcherMixin
from .router import CommandRouter

logger = logging.getLogger(__name__)

__all__ = ["PluginManager"]


class PluginManager(
    PluginGenerationMixin,
    PluginWatcherMixin,
    PluginRuntimeMixin,
    PluginDataMixin,
):
    def __init__(
        self,
        plugins_dir: Path,
        router: CommandRouter,
        context_factory: Any,
        poll_interval: float   = DEFAULT_PLUGIN_POLL_INTERVAL_SECONDS,
        data_root: Path | None = None,
    ):
        import_barrier                      = _PLUGIN_IMPORT_BARRIER_COORDINATOR.capability()
        self._hot_reload_supported          = import_barrier.available
        self._hot_reload_unavailable_reason = import_barrier.reason
        if not import_barrier.available:
            logger.warning(
                LogEvent(
                    "plugin.hot_reload.unavailable",
                    reason_code=_reason_code(import_barrier.reason or "capability probe failed"),
                )
            )
        self.plugins_dir     = plugins_dir
        configured_data_root = (
            Path(data_root) if data_root is not None else plugins_dir.parent / "data"
        )
        if not configured_data_root.is_absolute():
            configured_data_root = plugins_dir.parent / configured_data_root
        self.data_root = configured_data_root.absolute()
        try:
            self.data_root.resolve(strict=False).relative_to(self.plugins_dir.resolve(strict=True))
        except ValueError:
            pass
        except OSError as exc:
            raise PluginPathError("cannot resolve plugin data root") from exc
        else:
            raise PluginPathError("plugin data root must be outside the plugin source tree")
        self.router                                    = router
        self.context_factory                           = context_factory
        self._plugins: dict[str, LoadedPlugin]         = {}
        self._services: dict[str, LoadedPluginService] = {}
        parsed_poll_interval                           = self._parse_poll_interval(poll_interval)
        self._poll_interval                            = (
            parsed_poll_interval
            if parsed_poll_interval is not None
            else DEFAULT_PLUGIN_POLL_INTERVAL_SECONDS
        )
        self._poll_revision = 0
        self._watch_waiters_lock = threading.Lock()
        self._watch_waiters: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Event]] = {}
        self._next_watch_waiter_id = 0
        self._change_handlers: list[Any] = []
        self._deferred_lifecycle_errors: dict[str, list[BaseException]] = {}
        self._plugin_states: dict[str, dict[str, Any]] = {}
        self._execution_gates: dict[str, PluginExecutionGate] = {}
        self._quarantined_plugins: set[str] = set()
        self._restart_required_plugins: set[str] = set()
        self._lifecycle_lock = _LazyAsyncLock()
        self._execution_policy = PluginExecutionPolicy()
        self._execution_policy_overrides: dict[str, dict[str, Any]] = {}
        self._global_sync_queue_limit = DEFAULT_GLOBAL_SYNC_QUEUE_LIMIT
        self._sync_broker = PluginSyncBroker(
            max_workers        = DEFAULT_SYNC_WORKERS,
            global_queue_limit = self._global_sync_queue_limit,
        )
        # Watcher failures can repeat every poll while an editor, deployer, or
        # filesystem keeps a path transiently unavailable.  Buckets are fixed
        # strings, so both log volume and limiter memory remain bounded.
        self._watch_error_next_log_at: dict[str, float]                = {}
        self._watch_error_suppressed: dict[str, int]                   = {}
        self._data_directories: dict[str, _PluginDataDirectory]        = {}
        self._source_finders: dict[str, _SourceOnlyPluginFinder]       = {}
        self._namespace_owner_token                                    = object()
        self._owned_plugin_modules: dict[str, dict[str, ModuleType]]   = {}
        self._private_plugin_modules: dict[str, dict[str, ModuleType]] = {}
        self._module_origin_cache: OrderedDict[
            tuple[str, int, str, tuple[int, int, int, int]], Path | None
        ] = OrderedDict()
        self._import_path_lease_keys: tuple[_ProcessImportPathLeaseKey, ...] = ()
        self._import_path_finalizer: weakref.finalize | None                 = None

        # 一次性注册规范 plugins namespace；具体源码仍由受限 finder 加载。
        self._setup_sys_path()

    @property
    def hot_reload_supported(self) -> bool:
        """Whether this interpreter passed the atomic import barrier probe."""

        return self._hot_reload_supported

    def _require_hot_reload(self) -> None:
        if self._hot_reload_supported:
            return
        reason = self._hot_reload_unavailable_reason or "capability probe failed"
        raise PluginPathError(
            "plugin hot reload is unavailable; restart the process to apply plugin changes "
            f"({reason})"
        )

    def _setup_sys_path(self) -> None:
        """Lease the canonical package paths used by source-only loaders.

        The process-level entries are reference-counted across managers. Entries
        that predate every manager are never claimed, while entries inserted by
        this registry are removed after the last owning manager closes.
        """

        # Plugins are always imported as ``plugins.<plugin_name>``.  Adding the
        # *parent* of the package (rather than the plugins directory itself)
        # prevents the same file from being importable both as ``pendo.main``
        # and ``plugins.pendo.main``.  The latter is our one canonical module
        # name and is particularly important for plugins with module state.
        lease_keys: list[_ProcessImportPathLeaseKey] = []
        try:
            project_root = os.path.abspath(self.plugins_dir.parent)
            lease_keys.append(_acquire_process_import_path(sys.path, project_root))

            # ``plugins`` may already be imported by another manager. Extend that
            # exact package path rather than enabling a top-level plugin alias.
            package = importlib.import_module("plugins")
            if not isinstance(package, ModuleType):
                raise PluginPathError("canonical plugins package is not a module")
            paths = getattr(package, "__path__", None)
            if paths is None or not callable(getattr(paths, "insert", None)):
                raise PluginPathError("canonical plugins package has no mutable package path")
            package_path = os.path.abspath(self.plugins_dir)
            lease_keys.append(_acquire_process_import_path(paths, package_path))
            with _PLUGIN_IMPORT_LOCK:
                _install_namespace_tombstone_locked()
        except BaseException:
            _release_process_import_paths(tuple(lease_keys))
            raise

        self._plugins_package        = package
        self._import_path_lease_keys = tuple(lease_keys)
        self._import_path_finalizer  = weakref.finalize(
            self,
            _release_process_import_paths,
            self._import_path_lease_keys,
        )
        logger.debug(LogEvent("plugin.import_roots.leased"))

    def _release_import_paths(self) -> None:
        """Idempotently release this manager's process-global import path leases."""

        finalizer = self._import_path_finalizer
        if finalizer is not None and finalizer.alive:
            finalizer()
            importlib.invalidate_caches()

    def _claim_plugin_namespace(self, plugin_name: str) -> bool:
        """Claim the process-global canonical namespace for this manager."""

        with _PLUGIN_IMPORT_LOCK:
            owner = _PLUGIN_NAMESPACE_OWNERS.get(plugin_name)
            if owner is self._namespace_owner_token:
                return False
            if owner is not None:
                raise PluginPathError(
                    f"plugin namespace is already owned by another manager: {plugin_name}"
                )
            # Claim and deny ordinary import fallback in one critical section.
            # The tombstone remains authoritative until a frozen generation
            # finder atomically replaces it.
            _install_namespace_tombstone_locked()
            _PLUGIN_NAMESPACE_OWNERS[plugin_name] = self._namespace_owner_token
            self._owned_plugin_modules.setdefault(plugin_name, {})
            return True

    def _owns_plugin_namespace(self, plugin_name: str) -> bool:
        with _PLUGIN_IMPORT_LOCK:
            return _PLUGIN_NAMESPACE_OWNERS.get(plugin_name) is self._namespace_owner_token

    def _record_owned_module(
        self,
        plugin_name: str,
        module_name: str,
        module: ModuleType,
    ) -> None:
        canonical = f"plugins.{plugin_name}"
        if module_name != canonical and not module_name.startswith(f"{canonical}."):
            raise PluginPathError("plugin loader attempted to own a non-canonical module")
        with _PLUGIN_IMPORT_LOCK:
            if _PLUGIN_NAMESPACE_OWNERS.get(plugin_name) is not self._namespace_owner_token:
                raise PluginPathError("plugin module loaded without namespace ownership")
            current = sys.modules.get(module_name)
            if current is not module:
                raise PluginPathError("plugin module lost its canonical cache binding")
            finder = self._source_finders.get(plugin_name)
            if finder is None or not finder._active or finder._compromised:
                self._private_plugin_modules.setdefault(plugin_name, {})[module_name] = module
                self._detach_exact_module_locked(plugin_name, module_name, module)
                raise PluginPathError("closed plugin generation completed a late import")
            self._owned_plugin_modules.setdefault(plugin_name, {})[module_name] = module

    def _detach_exact_module_locked(
        self,
        plugin_name: str,
        module_name: str,
        module: ModuleType,
        *,
        owned: Mapping[str, ModuleType] | None = None,
    ) -> None:
        """Remove one exact generation object without touching a replacement."""

        expected_modules = owned or self._owned_plugin_modules.get(plugin_name, {})
        parent_name, separator, child_name = module_name.rpartition(".")
        if separator:
            parent = (
                self._plugins_package
                if parent_name == "plugins"
                else expected_modules.get(parent_name)
            )
            if isinstance(parent, ModuleType) and sys.modules.get(parent_name) is parent:
                namespace = ModuleType.__getattribute__(parent, "__dict__")
                if namespace.get(child_name) is module:
                    namespace.pop(child_name, None)
        if sys.modules.get(module_name) is module:
            sys.modules.pop(module_name, None)

    def _detach_owned_generation_locked(self, plugin_name: str) -> None:
        """Move every known generation object out of the public import graph."""

        owned = dict(self._owned_plugin_modules.get(plugin_name, {}))
        if not owned:
            self._owned_plugin_modules[plugin_name] = {}
            return
        private = self._private_plugin_modules.setdefault(plugin_name, {})
        private.update(owned)
        for module_name, module in sorted(
            owned.items(),
            key     = lambda item: item[0].count("."),
            reverse = True,
        ):
            self._detach_exact_module_locked(
                plugin_name,
                module_name,
                module,
                owned=owned,
            )
        self._owned_plugin_modules[plugin_name] = {}

    def _validate_owned_namespace(
        self,
        plugin_name: str,
        entry_module_name: str,
        entry_module: ModuleType,
    ) -> None:
        """Prove that publication still refers to this manager's exact objects."""

        with _PLUGIN_IMPORT_LOCK:
            if _PLUGIN_NAMESPACE_OWNERS.get(plugin_name) is not self._namespace_owner_token:
                raise PluginPathError("plugin namespace ownership was lost before publication")
            owned = self._owned_plugin_modules.get(plugin_name, {})
            if owned.get(entry_module_name) is not entry_module:
                raise PluginPathError("plugin entry is not owned by this import generation")
            canonical = f"plugins.{plugin_name}"
            cached    = {
                module_name: module
                for module_name, module in list(sys.modules.items())
                if module_name == canonical or module_name.startswith(f"{canonical}.")
            }
            if cached.keys() != owned.keys() or any(
                cached[module_name] is not module for module_name, module in owned.items()
            ):
                raise PluginPathError("canonical plugin cache is not the exact owned generation")
            for module_name, module in owned.items():
                if sys.modules.get(module_name) is not module:
                    raise PluginPathError(
                        f"owned plugin module lost its canonical binding: {module_name}"
                    )
                parent_name, _, child_name = module_name.rpartition(".")
                parent: ModuleType | None = (
                    self._plugins_package if parent_name == "plugins" else owned.get(parent_name)
                )
                if not isinstance(parent, ModuleType) or sys.modules.get(parent_name) is not parent:
                    raise PluginPathError(
                        f"owned plugin module has a foreign parent: {module_name}"
                    )
                namespace = ModuleType.__getattribute__(parent, "__dict__")
                if namespace.get(child_name) is not module:
                    raise PluginPathError(
                        f"owned plugin module lost its parent binding: {module_name}"
                    )

    def _quiesce_source_generation_for_publication(
        self,
        plugin_name: str,
    ) -> _SourceOnlyPluginFinder | None:
        """Drain detached imports and hold lazy admission through publication."""

        with _PLUGIN_IMPORT_LOCK:
            finder = self._source_finders.get(plugin_name)
        if finder is None:
            return None
        try:
            module_names = finder.pause_for_publication()
            self._wait_for_module_import_barriers(module_names)
            with _PLUGIN_IMPORT_LOCK:
                if self._source_finders.get(plugin_name) is not finder:
                    raise PluginPathError(
                        "plugin source finder changed during publication quiescence"
                    )
                if _PLUGIN_NAMESPACE_OWNERS.get(plugin_name) is not self._namespace_owner_token:
                    raise PluginPathError(
                        "plugin namespace ownership changed during publication quiescence"
                    )
                if finder._active_loads or not finder._publication_paused:
                    raise PluginPathError("plugin imports did not remain quiescent")
            return finder
        except PluginPathError as exc:
            self._mark_source_finder_compromised(plugin_name, str(exc))
            raise

    def _resume_source_generation_after_publication(
        self,
        plugin_name: str,
        finder: _SourceOnlyPluginFinder | None,
        execution_gate: PluginExecutionGate,
        publication_token: object,
    ) -> None:
        """Commit finder, gate, and quarantine state as one generation epoch."""

        with _PLUGIN_IMPORT_LOCK:
            if finder is not None:
                if self._source_finders.get(plugin_name) is not finder:
                    raise PluginPathError("plugin source finder changed before publication resume")
                finder.resume_after_publication()
            execution_gate.release_publication_hold(publication_token)
            self._quarantined_plugins.discard(plugin_name)
            self._restart_required_plugins.discard(plugin_name)

    def _abort_source_generation_publication(
        self,
        plugin_name: str,
        finder: _SourceOnlyPluginFinder | None,
        execution_gate: PluginExecutionGate,
    ) -> None:
        """Close and detach a candidate that failed before atomic publication."""

        execution_gate.close_admission()
        with _PLUGIN_IMPORT_LOCK:
            if finder is not None and self._source_finders.get(plugin_name) is finder:
                finder._active             = False
                finder._publication_paused = True
            self._detach_owned_generation_locked(plugin_name)

    def _release_plugin_namespace(self, plugin_name: str) -> None:
        """Release ownership while retaining an authoritative deny tombstone."""

        with _PLUGIN_IMPORT_LOCK:
            owner = _PLUGIN_NAMESPACE_OWNERS.get(plugin_name)
            if owner is not self._namespace_owner_token:
                return
            owned = self._owned_plugin_modules.get(plugin_name, {})
            if owned:
                raise PluginPathError(
                    f"cannot release a plugin namespace with owned modules: {plugin_name}"
                )
            canonical = f"plugins.{plugin_name}"
            if any(
                module_name == canonical or module_name.startswith(f"{canonical}.")
                for module_name in list(sys.modules)
            ):
                raise PluginPathError(
                    f"cannot release a plugin namespace with live cache entries: {plugin_name}"
                )
            plugins_namespace = ModuleType.__getattribute__(self._plugins_package, "__dict__")
            if plugin_name in plugins_namespace:
                raise PluginPathError(
                    f"cannot release a plugin namespace with a live parent binding: {plugin_name}"
                )
            finder = self._source_finders.get(plugin_name)
            if finder is not None:
                if finder._active or finder._active_loads:
                    raise PluginPathError(
                        f"cannot release an active plugin source finder: {plugin_name}"
                    )
            # Replace the inactive generation finder before dropping ownership.
            # There is never a list position where PathFinder can rediscover
            # source from ``plugins.__path__``.
            _install_namespace_tombstone_locked(replace=finder)
            self._source_finders.pop(plugin_name, None)
            _PLUGIN_NAMESPACE_OWNERS.pop(plugin_name, None)
            self._owned_plugin_modules.pop(plugin_name, None)
            self._private_plugin_modules.pop(plugin_name, None)
            self._data_directories.pop(plugin_name, None)

    def _source_finder_is_current(
        self,
        plugin_name: str,
        finder: _SourceOnlyPluginFinder,
    ) -> bool:
        return (
            _PLUGIN_NAMESPACE_OWNERS.get(plugin_name) is self._namespace_owner_token
            and self._source_finders.get(plugin_name) is finder
        )

    def _mark_source_finder_compromised(self, plugin_name: str, reason: str) -> None:
        """Fail closed when a lazy import escapes the generation protocol."""

        with _PLUGIN_IMPORT_LOCK:
            self._restart_required_plugins.add(plugin_name)
            self._quarantined_plugins.add(plugin_name)
            finder = self._source_finders.get(plugin_name)
            if finder is not None:
                finder._active      = False
                finder._compromised = True
            gate = self._execution_gates.get(plugin_name)
        if gate is not None:
            gate.close_admission()
        with _PLUGIN_IMPORT_LOCK:
            self._detach_owned_generation_locked(plugin_name)
        self.router.clear_plugin(plugin_name)
        logger.error(
            LogEvent(
                "plugin.import_generation.compromised",
                plugin      = plugin_name,
                reason_code = _reason_code(reason),
            )
        )

    def _freeze_source_generation(self, plugin_name: str) -> None:
        """Revoke all future imports while retaining restart-only evidence."""

        with _PLUGIN_IMPORT_LOCK:
            finder = self._source_finders.get(plugin_name)
        if finder is not None:
            module_names = finder.deactivate_and_wait()
            self._wait_for_module_import_barriers(module_names)
        with _PLUGIN_IMPORT_LOCK:
            if finder is not None and self._source_finders.get(plugin_name) is not finder:
                raise PluginPathError("plugin source generation changed while freezing")
            self._detach_owned_generation_locked(plugin_name)

    async def _freeze_source_generation_async(self, plugin_name: str) -> None:
        await self._await_uncancellable_thread_transaction(
            self._freeze_source_generation,
            plugin_name,
        )

    def _new_source_finder(
        self,
        plugin_name: str,
        plugin_root: Path,
        sources: Mapping[str, bytes],
    ) -> _SourceOnlyPluginFinder:
        return _SourceOnlyPluginFinder(
            plugin_root,
            plugin_name,
            sources,
            lambda module_name, module: self._record_owned_module(
                plugin_name,
                module_name,
                module,
            ),
            is_current=lambda finder: self._source_finder_is_current(
                plugin_name,
                finder,
            ),
            on_compromised=lambda reason: self._mark_source_finder_compromised(
                plugin_name,
                reason,
            ),
        )

    def _activate_source_finder(
        self,
        plugin_name: str,
        plugin_root: Path,
        sources: Mapping[str, bytes],
    ) -> _SourceOnlyPluginFinder:
        """Keep source-only imports active for init, callbacks, and lazy imports."""

        with _PLUGIN_IMPORT_LOCK:
            finder        = self._source_finders.get(plugin_name)
            same_snapshot = (
                finder is not None
                and finder._plugin_root == plugin_root
                and dict(finder._sources) == dict(sources)
            )
            if finder is not None and finder._active and not same_snapshot:
                raise PluginPathError("cannot replace an active plugin source generation")
            if finder is not None and finder._active_loads:
                raise PluginPathError("cannot replace a busy plugin source generation")
            if finder is None or not finder._active:
                replacement = self._new_source_finder(plugin_name, plugin_root, sources)
                if finder is not None:
                    index = _meta_path_identity_index(finder)
                    if index is None:
                        raise PluginPathError(
                            "plugin namespace lost its authoritative import finder"
                        )
                    sys.meta_path[index] = replacement
                else:
                    guard_index = _meta_path_identity_index(_PLUGIN_NAMESPACE_GUARD)
                    if guard_index is None:
                        raise PluginPathError(
                            "plugin namespace lost its authoritative import guard"
                        )
                    sys.meta_path.insert(guard_index, replacement)
                finder                            = replacement
                self._source_finders[plugin_name] = finder
            else:
                assert finder is not None
                if finder.compromised:
                    raise PluginPathError("cannot reuse a compromised plugin source generation")
            if _meta_path_identity_index(finder) is None:
                sys.meta_path.insert(0, finder)
            return finder

    def _prepare_inactive_source_finder(
        self,
        plugin_name: str,
        plugin_root: Path,
        sources: Mapping[str, bytes],
    ) -> _SourceOnlyPluginFinder:
        """Atomically install a fresh blocking finder for a restore transaction."""

        with _PLUGIN_IMPORT_LOCK:
            previous = self._source_finders.get(plugin_name)
            if previous is not None and (previous._active or previous._active_loads):
                raise PluginPathError("cannot prepare restore over an active source finder")
            replacement         = self._new_source_finder(plugin_name, plugin_root, sources)
            replacement._active = False
            if previous is not None:
                index = _meta_path_identity_index(previous)
                if index is None:
                    raise PluginPathError("plugin namespace lost its authoritative import finder")
                sys.meta_path[index] = replacement
            else:
                guard_index = _meta_path_identity_index(_PLUGIN_NAMESPACE_GUARD)
                if guard_index is None:
                    raise PluginPathError("plugin namespace lost its authoritative import guard")
                sys.meta_path.insert(guard_index, replacement)
            self._source_finders[plugin_name] = replacement
            return replacement

    @staticmethod
    def _wait_for_module_import_barriers(
        module_names: tuple[str, ...] | list[str] | set[str],
        *,
        timeout: float = _PLUGIN_IMPORT_DRAIN_TIMEOUT_SECONDS,
    ) -> None:
        """Cross CPython module locks after loader callbacks have returned.

        ``exec_module`` finishes before importlib performs its final cache and
        parent-attribute updates.  Waiting for each module lock, without holding
        our own import lock, closes that tail window.  The daemon worker gives
        hostile module hooks a bounded opportunity to finish. A timeout leaves
        the finder tombstone and namespace claim intact for quarantine.
        """

        names = tuple(sorted(set(module_names)))
        if names:
            _PLUGIN_IMPORT_BARRIER_COORDINATOR.cross(names, timeout=timeout)

    def build_context(
        self,
        plugin_name: str,
        user_id: int | None               = None,
        group_id: int | None              = None,
        request_id: str | None            = None,
        principal: PluginPrincipal | None = None,
        *,
        definition: PluginDefinition | None = None,
    ) -> PluginContextProtocol:
        """给插件的一次回调建上下文。

        能力和服务按已登记的那一代的声明签发。``init`` 跑的时候这一代还没登记（首次加载时
        登记表里没有它，热重载时登记的是上一代），由加载方把正在加载的 ``definition`` 传进来。
        """

        plugin_dir = self.plugins_dir / plugin_name
        data_dir   = self._ensure_plugin_data_dir(plugin_name)

        # 获取或创建插件状态
        state = self._plugin_states.setdefault(plugin_name, {})
        if definition is None:
            loaded     = self._plugins.get(plugin_name)
            definition = loaded.definition if loaded is not None else None
        capabilities  = definition.capabilities if definition is not None else frozenset()
        uses_services = definition.uses_services if definition is not None else frozenset()

        return cast(
            PluginContextProtocol,
            self.context_factory(
                plugin_name,
                plugin_dir,
                data_dir,
                state,
                user_id,
                group_id,
                request_id,
                principal,
                capabilities,
                uses_services,
            ),
        )

    def schedule_definitions(self) -> list[LoadedPlugin]:
        return list(self._plugins.values())
