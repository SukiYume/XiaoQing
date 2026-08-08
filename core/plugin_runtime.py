# mypy: disable-error-code=attr-defined
"""Plugin execution policy, service binding, and runtime drain responsibilities."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable, Mapping
from types import MappingProxyType, ModuleType
from typing import Any, cast

from .exceptions import PluginLoadError
from .plugin_execution import (
    PluginConcurrency,
    PluginExecutionDrainResult,
    PluginExecutionGate,
    PluginExecutionPolicy,
    PluginSyncBroker,
    PluginSyncBrokerDrainResult,
)
from .plugin_manager_support import (
    _DEFAULT_GLOBAL_SYNC_QUEUE_LIMIT,
    _MIN_PLUGIN_POLL_INTERVAL_SECONDS,
    _PLUGIN_SYNC_WORKERS,
    LoadedPlugin,
    LoadedPluginService,
    PluginDefinition,
    _PluginLoadTransaction,
)

logger = logging.getLogger(__name__)


class PluginRuntimeMixin:
    _plugins: dict[str, LoadedPlugin]
    _quarantined_plugins: set[str]
    _services: dict[str, LoadedPluginService]
    _pending_plugins: dict[
        asyncio.Task[Any],
        tuple[PluginDefinition, ModuleType, float],
    ]
    _pending_transactions: dict[asyncio.Task[Any], _PluginLoadTransaction]
    _poll_interval: float
    _poll_revision: int
    _sync_broker: PluginSyncBroker
    _purge_plugin_modules: Callable[[str], dict[str, ModuleType]]

    def on_change(self, handler) -> None:
        self._change_handlers.append(handler)

    @staticmethod
    def _parse_poll_interval(poll_interval: Any) -> float | None:
        if isinstance(poll_interval, bool):
            return None
        try:
            parsed = float(poll_interval)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(parsed) or parsed <= 0:
            return None
        return max(_MIN_PLUGIN_POLL_INTERVAL_SECONDS, parsed)

    def update_poll_interval(self, poll_interval: float) -> bool:
        """Update future watch sleeps and report whether the value changed."""

        parsed = self._parse_poll_interval(poll_interval)
        if parsed is None:
            self._log_watch_error(
                "poll-interval",
                "Ignoring invalid plugin watcher poll interval",
                ValueError(repr(poll_interval)),
            )
            return False
        with self._watch_waiters_lock:
            changed = parsed != self._poll_interval
            self._poll_interval = parsed
            if not changed:
                return False
            self._poll_revision += 1
            waiters = tuple(self._watch_waiters.values())
        for loop, wakeup in waiters:
            if loop.is_closed():
                continue
            try:
                loop.call_soon_threadsafe(wakeup.set)
            except RuntimeError:
                # A generation can unregister while a cross-thread config
                # update is publishing.  Its finally block remains the owner
                # of cleanup; the next generation snapshots the latest value.
                continue
        return changed

    def configure_execution(self, raw_policy: Mapping[str, Any] | None) -> None:
        """Apply global and per-plugin callback limits from runtime config."""

        raw_policy = raw_policy if isinstance(raw_policy, Mapping) else {}
        self._execution_policy = PluginExecutionPolicy.from_mapping(raw_policy)
        raw_queue_limit = raw_policy.get(
            "global_sync_queue_limit",
            _DEFAULT_GLOBAL_SYNC_QUEUE_LIMIT,
        )
        if (
            not isinstance(raw_queue_limit, int)
            or isinstance(raw_queue_limit, bool)
            or not 1 <= raw_queue_limit <= 100000
        ):
            self._global_sync_queue_limit = _DEFAULT_GLOBAL_SYNC_QUEUE_LIMIT
        else:
            self._global_sync_queue_limit = raw_queue_limit
        if not self._sync_broker.closed:
            self._sync_broker.configure(
                global_queue_limit=self._global_sync_queue_limit,
            )
        raw_overrides = raw_policy.get("overrides", {})
        configured_overrides = (
            {
                str(name): values
                for name, values in raw_overrides.items()
                if isinstance(name, str) and isinstance(values, Mapping)
            }
            if isinstance(raw_overrides, Mapping)
            else {}
        )
        self._execution_policy_overrides = {
            name: dict(values) for name, values in configured_overrides.items()
        }

        for name, gate in self._execution_gates.items():
            gate.set_policy(self._execution_policy_for(name, self._runtime_capabilities(name)))

    @property
    def execution_drain_timeout_seconds(self) -> float:
        """Return the one application-wide plugin shutdown budget."""

        return float(self._execution_policy.drain_timeout_seconds)

    def _ensure_execution_broker(self) -> PluginSyncBroker:
        """Return the live broker, recreating it only after a clean full drain."""

        broker = self._sync_broker
        if not broker.closed:
            return broker
        runtime_names = self._plugin_runtime_names()
        if runtime_names:
            raise RuntimeError(
                "plugin sync broker is closed while runtimes remain: "
                + ", ".join(sorted(runtime_names))
            )
        if not broker.drained:
            raise RuntimeError("plugin sync broker still owns undrained callbacks")
        broker = PluginSyncBroker(
            max_workers=_PLUGIN_SYNC_WORKERS,
            global_queue_limit=self._global_sync_queue_limit,
        )
        self._sync_broker = broker
        return broker

    def _new_execution_gate(
        self,
        mode: PluginConcurrency,
        plugin_name: str,
        capabilities: frozenset[str] = frozenset(),
    ) -> PluginExecutionGate:
        """Construct a gate attached to this manager's shared sync broker."""

        return PluginExecutionGate(
            mode,
            plugin_name=plugin_name,
            policy=self._execution_policy_for(plugin_name, capabilities),
            sync_broker=self._ensure_execution_broker(),
        )

    async def close_execution_broker(
        self,
        timeout_seconds: float | None = None,
    ) -> PluginSyncBrokerDrainResult:
        """Close the shared broker after every plugin generation is gone."""

        runtime_names = self._plugin_runtime_names()
        if runtime_names:
            raise RuntimeError(
                "cannot close plugin sync broker while runtimes remain: "
                + ", ".join(sorted(runtime_names))
            )
        timeout = (
            self._execution_policy.drain_timeout_seconds
            if timeout_seconds is None
            else max(0.0, float(timeout_seconds))
        )
        return await self._sync_broker.close(timeout_seconds=timeout)

    async def close(
        self,
        timeout_seconds: float | None = None,
    ) -> PluginSyncBrokerDrainResult:
        """Terminally drain the manager and release its process import paths."""

        result = await self.close_execution_broker(timeout_seconds=timeout_seconds)
        if result.drained:
            self._release_import_paths()
        return result

    def _runtime_capabilities(self, plugin_name: str) -> frozenset[str]:
        loaded = self._plugins.get(plugin_name)
        if loaded is not None:
            return loaded.definition.capabilities
        for definition, _module, _mtime in self._pending_plugins.values():
            if definition.name == plugin_name:
                return definition.capabilities
        for transaction in self._pending_transactions.values():
            if transaction.definition.name == plugin_name:
                return transaction.definition.capabilities
        return frozenset()

    def has_capability(self, plugin_name: str, capability: str) -> bool:
        """Read one validated manifest capability from the live generation."""

        loaded = self._plugins.get(plugin_name)
        return loaded is not None and capability in loaded.definition.capabilities

    def _execution_policy_for(
        self,
        plugin_name: str,
        capabilities: frozenset[str] = frozenset(),
    ) -> PluginExecutionPolicy:
        manifest_defaults: dict[str, Any] = {}
        if "execution_timeout_exempt" in capabilities:
            manifest_defaults["timeout_seconds"] = 0
        return PluginExecutionPolicy.from_mapping(
            {
                **manifest_defaults,
                **self._execution_policy_overrides.get(plugin_name, {}),
            },
            fallback=self._execution_policy,
        )

    def _notify_change(self, name: str) -> None:
        for handler in self._change_handlers:
            try:
                handler(name)
            except Exception as exc:
                logger.warning("Plugin change handler failed: plugin=%s error=%s", name, exc)

    def list_plugins(self) -> list[str]:
        return sorted(self._plugins.keys())

    def list_runtime_plugins(self) -> list[str]:
        """Return live, pending, finalizing, and quarantined runtime names."""

        return sorted(self._plugin_runtime_names())

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

    def _quarantine_closed_plugin(
        self,
        name: str,
        plugin: LoadedPlugin,
        gate: PluginExecutionGate,
        *,
        phase: str,
        reason: str,
    ) -> None:
        """Keep an uncertain plugin generation intact behind a closed gate."""

        plugin.execution_gate = gate
        self._plugins[name] = plugin
        self._execution_gates[name] = gate
        self.router.clear_plugin(name)
        self._quarantined_plugins.add(name)
        logger.warning("Plugin %s quarantined during %s: %s", name, phase, reason)
        self._notify_change(name)

    def _quarantine_gate_without_module(
        self,
        name: str,
        gate: PluginExecutionGate,
        *,
        phase: str,
        reason: str,
    ) -> None:
        """Retain orphan state/gate metadata when no module can own the runtime."""

        self._plugins.pop(name, None)
        self._unregister_services_owned_by(name)
        self._execution_gates[name] = gate
        self.router.clear_plugin(name)
        self._quarantined_plugins.add(name)
        logger.warning("Plugin %s quarantined during %s: %s", name, phase, reason)
        self._notify_change(name)

    def _quarantine_gate_result(
        self,
        name: str,
        plugin: LoadedPlugin | None,
        gate: PluginExecutionGate,
        result: PluginExecutionDrainResult | None,
        *,
        phase: str,
        reason: str,
        discard_registered_plugin: bool,
    ) -> None:
        """Record the exact generation left behind by an interrupted close."""

        if discard_registered_plugin:
            self._plugins.pop(name, None)
            self._unregister_services_owned_by(name)
        if result is not None and not result.drained:
            self._quarantine_undrained_gate(name, plugin, gate, result, phase=phase)
        elif plugin is not None:
            self._quarantine_closed_plugin(
                name,
                plugin,
                gate,
                phase=phase,
                reason=reason,
            )
        else:
            self._quarantine_gate_without_module(
                name,
                gate,
                phase=phase,
                reason=reason,
            )

    def _purge_generation_modules(
        self,
        name: str,
        plugin: LoadedPlugin | None,
        gate: PluginExecutionGate,
        *,
        phase: str,
    ) -> dict[str, ModuleType]:
        """Purge before dropping lifecycle ownership; quarantine on any failure."""

        try:
            return self._purge_plugin_modules(name)
        except BaseException as exc:
            self._quarantine_gate_result(
                name,
                plugin,
                gate,
                None,
                phase=phase,
                reason=f"module purge failed ({type(exc).__name__})",
                discard_registered_plugin=plugin is None,
            )
            raise

    async def _purge_generation_modules_async(
        self,
        name: str,
        plugin: LoadedPlugin | None,
        gate: PluginExecutionGate,
        *,
        phase: str,
    ) -> dict[str, ModuleType]:
        """Run import-lock draining off the event loop, then quarantine locally."""

        try:
            return cast(
                dict[str, ModuleType],
                await self._await_uncancellable_thread_transaction(
                    self._purge_plugin_modules,
                    name,
                ),
            )
        except BaseException as exc:
            self._quarantine_gate_result(
                name,
                plugin,
                gate,
                None,
                phase=phase,
                reason=f"module purge failed ({type(exc).__name__})",
                discard_registered_plugin=plugin is None,
            )
            raise

    async def _await_uncancellable_thread_transaction(
        self,
        callback: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Wait for a mutating worker to settle before propagating interruption."""

        worker = asyncio.create_task(asyncio.to_thread(callback, *args, **kwargs))
        interrupted: BaseException | None = None
        current_task = asyncio.current_task()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError as exc:
                if current_task is not None and getattr(current_task, "cancelling", lambda: 0)():
                    interrupted = interrupted or exc
                if worker.done():
                    break
            except BaseException:
                # A non-cancellation error originates from the now-terminal
                # worker and is re-raised from ``worker.result()`` below.
                break
        try:
            result = worker.result()
        except BaseException as worker_error:
            if interrupted is not None:
                self._raise_preferred_lifecycle_error(interrupted, worker_error)
            raise
        if interrupted is not None:
            raise interrupted
        return result

    async def _close_generation_gate(
        self,
        name: str,
        plugin: LoadedPlugin | None,
        gate: PluginExecutionGate,
        *,
        phase: str,
        discard_registered_plugin: bool = False,
        shutdown_deadline: float | None = None,
    ) -> PluginExecutionDrainResult:
        """Close and drain one generation without exposing a cancellation tear.

        Admission closes synchronously.  The bounded drain then runs in its own
        task and is shielded until it reaches a terminal result.  A caller
        cancellation is propagated only after the affected generation has been
        quarantined with its matching state and gate.
        """

        gate.close_admission()
        close_timeout = (
            None if shutdown_deadline is None else max(0.0, shutdown_deadline - time.monotonic())
        )
        close_task = asyncio.create_task(
            self._capture_lifecycle(gate.close(timeout_seconds=close_timeout))
        )
        interrupted: BaseException | None = None
        result: PluginExecutionDrainResult | None = None

        while result is None:
            try:
                result = self._unwrap_lifecycle_outcome(await asyncio.shield(close_task))
            except BaseException as exc:
                if not close_task.done():
                    interrupted = interrupted or exc
                    continue
                try:
                    result = self._unwrap_lifecycle_outcome(close_task.result())
                except BaseException as close_exc:
                    self._quarantine_gate_result(
                        name,
                        plugin,
                        gate,
                        None,
                        phase=phase,
                        reason=f"gate close failed ({type(close_exc).__name__})",
                        discard_registered_plugin=discard_registered_plugin,
                    )
                    if self._is_fatal_base_exception(close_exc):
                        if interrupted is not None:
                            raise close_exc from interrupted
                        raise
                    if interrupted is not None:
                        raise interrupted from close_exc
                    raise
                interrupted = interrupted or exc

        if not result.drained:
            self._quarantine_gate_result(
                name,
                plugin,
                gate,
                result,
                phase=phase,
                reason="gate drain did not complete",
                discard_registered_plugin=discard_registered_plugin,
            )
        deferred_fatal = gate.consume_deferred_fatal_error()
        if deferred_fatal is not None:
            # Preserve the fatal outcome, but let the lifecycle owner finish
            # shutdown/purge first.  Public lifecycle entry points or
            # ``wait_inits`` replay it only after cleanup is terminal.
            self._deferred_lifecycle_errors.setdefault(name, []).append(deferred_fatal)
        if interrupted is not None:
            if result.drained:
                self._quarantine_gate_result(
                    name,
                    plugin,
                    gate,
                    result,
                    phase=phase,
                    reason=f"gate close interrupted ({type(interrupted).__name__})",
                    discard_registered_plugin=discard_registered_plugin,
                )
            raise interrupted
        return result

    def get(self, name: str) -> LoadedPlugin | None:
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
