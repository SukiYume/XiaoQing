# mypy: disable-error-code=attr-defined
"""Plugin lifecycle transactions, generation publication, and rollback."""

from __future__ import annotations

import asyncio
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import logging
import os
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from .constants import PLUGIN_INIT_TIMEOUT_SECONDS
from .exceptions import PluginLifecycleFatalError, PluginLoadError
from .plugin_execution import (
    PluginCallbackFatalError,
    PluginExecutionGate,
    call_plugin_callback,
    callback_accepts_positional_context,
    invoke_loaded_plugin,
)
from .plugin_manager_support import (
    _MAX_MODULE_ORIGIN_CACHE_ENTRIES,
    _PLUGIN_IMPORT_BARRIER_COORDINATOR,
    _PLUGIN_IMPORT_DRAIN_TIMEOUT_SECONDS,
    _PLUGIN_IMPORT_LOCK,
    _PLUGIN_NAMESPACE_OWNERS,
    LoadedPlugin,
    PluginDefinition,
    PluginPathError,
    _LifecycleTaskOutcome,
    _PluginContentFingerprint,
    _PluginLoadTransaction,
    _ReloadAuthorization,
    _ReloadCandidateGeneration,
    _RetiredPluginGeneration,
    _SourceOnlyPluginFinder,
    _validate_plugin_name,
    resolve_plugin_entry,
    resolve_plugin_root,
)
from .router import CommandConflictError, CommandSpec

logger = logging.getLogger(__name__)


class PluginGenerationMixin:
    _execution_gates: dict[str, PluginExecutionGate]
    _init_wait_task: asyncio.Task[Any] | None
    _pending_finalizers: dict[str, asyncio.Task[Any]]
    _plugins_package: ModuleType

    def load_all(self) -> None:
        """Load every plugin without blocking a running event loop on snapshots.

        Synchronous embedding remains synchronous.  During normal async startup,
        the serialized load joins the existing initialization barrier so callers
        retain the established ``load_all(); await wait_inits()`` contract.
        """

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._load_all_sync()
            return

        task = asyncio.create_task(self._capture_lifecycle(self._load_all_async()))
        self._init_tasks.append(task)

    def _load_all_sync(self) -> None:
        plugin_paths = sorted(self.plugins_dir.iterdir(), key=lambda path: path.name)
        for plugin_dir in plugin_paths:
            try:
                is_plugin_dir = self._is_plugin_dir(plugin_dir)
            except OSError as exc:
                logger.error(
                    "Skipping plugin entry %s because its metadata is unavailable: %s",
                    plugin_dir.name,
                    exc,
                )
                continue
            if not is_plugin_dir:
                continue
            try:
                self.load_plugin(plugin_dir)
            except PluginLifecycleFatalError:
                raise
            except Exception as exc:
                # A plugin-local import or filesystem failure must not suppress
                # later independent plugins in the deterministic startup order.
                logger.exception(
                    "Plugin %s failed during startup and was isolated: %s",
                    plugin_dir.name,
                    exc,
                )

    async def _load_all_async(self) -> None:
        """Serialize startup discovery through the non-blocking reconcile path."""

        async with self._lifecycle_lock.get():
            await self._reconcile_plugins_once()

    async def _rollback_pending_plugin(
        self,
        definition: PluginDefinition,
        module: ModuleType,
        mtime: float,
        *,
        retain_quarantine: bool = False,
    ) -> bool:
        """Undo a never-published plugin initialization in reverse order."""

        name = definition.name
        had_registered_generation = name in self._plugins
        if retain_quarantine:
            # Incomplete import/external-alias uncertainty is irreversible in
            # this process.  Commit restart-only ownership before the first
            # cancellation point so an undrained rollback cannot downgrade it
            # to an ordinary retryable quarantine.
            self._restart_required_plugins.add(name)
            await self._freeze_source_generation_async(name)
        gate = self._execution_gates.get(name)
        if gate is None:
            gate = self._new_execution_gate(
                definition.concurrency,
                name,
                definition.capabilities,
            )
        plugin = LoadedPlugin(
            definition=definition,
            module=module,
            mtime=mtime,
            execution_gate=gate,
        )

        drain = await self._close_generation_gate(
            name,
            plugin,
            gate,
            phase="failed initialization drain",
        )
        if not drain.drained:
            return False

        try:
            shutdown_ok = await self._shutdown_plugin_instance(name, plugin)
        except BaseException as exc:
            self._quarantine_closed_plugin(
                name,
                plugin,
                gate,
                phase="failed initialization rollback",
                reason=f"shutdown interrupted ({type(exc).__name__})",
            )
            raise
        if not shutdown_ok:
            self._quarantine_closed_plugin(
                name,
                plugin,
                gate,
                phase="failed initialization rollback",
                reason="shutdown failed",
            )
            return False

        shutdown_drain = await self._close_generation_gate(
            name,
            plugin,
            gate,
            phase="failed initialization shutdown drain",
        )
        if not shutdown_drain.drained:
            return False

        if retain_quarantine:
            self._quarantine_closed_plugin(
                name,
                plugin,
                gate,
                phase="partial import rollback",
                reason="module execution did not reach a known-complete import state",
            )
            return False

        await self._purge_generation_modules_async(
            name,
            plugin,
            gate,
            phase="failed initialization module purge",
        )
        self.router.clear_plugin(name)
        self._unregister_services_owned_by(name)
        self._plugins.pop(name, None)
        self._plugin_states.pop(name, None)
        self._execution_gates.pop(name, None)
        self._quarantined_plugins.discard(name)
        self._restart_required_plugins.discard(name)
        if not had_registered_generation:
            self._release_plugin_namespace(name)
        return True

    def _start_pending_rollback(
        self,
        pending: tuple[PluginDefinition, ModuleType, float],
        *,
        init_task: asyncio.Task[Any] | None = None,
        retain_quarantine: bool = False,
    ) -> asyncio.Task[Any]:
        """Claim one pending generation and give it exactly one finalizer."""

        definition, module, mtime = pending
        gate = self._execution_gates.get(definition.name)
        if gate is not None:
            # ``load_plugin`` is synchronous even when embedded in an event
            # loop.  Close admission before scheduling async rollback so a
            # fatal import/publish path has no one-tick fail-open window.
            gate.close_admission()
        existing = self._pending_finalizers.get(definition.name)
        if existing is not None:
            return existing

        async def finalize() -> bool:
            if init_task is not None:
                if not init_task.done():
                    init_task.cancel()
                await asyncio.gather(init_task, return_exceptions=True)
            return await self._rollback_pending_plugin(
                definition,
                module,
                mtime,
                retain_quarantine=retain_quarantine,
            )

        finalizer = asyncio.create_task(self._capture_lifecycle(finalize()))
        self._pending_finalizers[definition.name] = finalizer
        return finalizer

    def _run_or_schedule_pending_rollback(
        self,
        pending: tuple[PluginDefinition, ModuleType, float],
        *,
        retain_quarantine: bool = False,
        original_error: BaseException | None = None,
    ) -> bool:
        """Bridge the synchronous loader to terminal async rollback.

        Returns whether a fatal original error was deferred for ``wait_inits``
        because immediately raising it through a running asyncio Task would
        tear down the loop before the scheduled cleanup can finish.
        """

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(
                    self._rollback_pending_plugin(
                        *pending,
                        retain_quarantine=retain_quarantine,
                    )
                )
            except BaseException as cleanup_error:
                if original_error is not None:
                    self._raise_preferred_lifecycle_error(
                        original_error,
                        cleanup_error,
                    )
                raise
            return False

        self._start_pending_rollback(
            pending,
            retain_quarantine=retain_quarantine,
        )
        if original_error is not None and self._is_fatal_base_exception(original_error):
            self._deferred_lifecycle_errors.setdefault(pending[0].name, []).append(original_error)
            return True
        return False

    @staticmethod
    def _is_fatal_base_exception(exc: BaseException) -> bool:
        """Return whether an error must outrank routine task cancellation."""

        return not isinstance(exc, (Exception, asyncio.CancelledError))

    @staticmethod
    async def _capture_lifecycle(awaitable: Awaitable[Any]) -> _LifecycleTaskOutcome:
        """Keep SystemExit/KeyboardInterrupt inside a managed task until cleanup."""

        try:
            return _LifecycleTaskOutcome(value=await awaitable)
        except BaseException as exc:
            return _LifecycleTaskOutcome(error=exc)

    @staticmethod
    def _unwrap_lifecycle_outcome(value: Any) -> Any:
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, _LifecycleTaskOutcome):
            if value.error is not None:
                error = value.error
                if isinstance(error, PluginCallbackFatalError):
                    raise error.original from error
                raise error
            return value.value
        return value

    @classmethod
    def _raise_preferred_lifecycle_error(
        cls,
        primary: BaseException,
        cleanup: BaseException,
    ) -> None:
        """Raise fatal errors first while retaining the secondary failure as cause."""

        if cls._is_fatal_base_exception(cleanup):
            raise cleanup from primary
        if cls._is_fatal_base_exception(primary):
            raise primary from cleanup
        if isinstance(cleanup, asyncio.CancelledError):
            raise cleanup from primary
        if isinstance(primary, asyncio.CancelledError):
            raise primary from cleanup
        raise cleanup from primary

    @classmethod
    def _raise_collected_lifecycle_errors(cls, errors: list[BaseException]) -> None:
        """Raise one deterministic representative after all cleanup is terminal."""

        if not errors:
            return
        for error in errors:
            if cls._is_fatal_base_exception(error):
                raise error
        for error in errors:
            if isinstance(error, asyncio.CancelledError):
                raise error
        raise errors[0]

    @classmethod
    def _raise_task_safe_lifecycle_errors(
        cls,
        errors: list[BaseException],
        *,
        plugin_name: str,
    ) -> None:
        try:
            cls._raise_collected_lifecycle_errors(errors)
        except BaseException as exc:
            if cls._is_fatal_base_exception(exc):
                raise PluginLifecycleFatalError(plugin_name, exc) from None
            raise

    def _take_deferred_lifecycle_errors(
        self,
        names: set[str],
    ) -> list[BaseException]:
        errors: list[BaseException] = []
        for name in names:
            errors.extend(self._deferred_lifecycle_errors.pop(name, ()))
        return errors

    @classmethod
    async def _await_lifecycle_task(cls, task: asyncio.Task[Any]) -> Any:
        """Wait through repeated caller cancellation until cleanup is terminal."""

        interrupted: BaseException | None = None
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except BaseException as exc:
                if not task.done():
                    interrupted = interrupted or exc
                    continue
                try:
                    result = task.result()
                except BaseException as task_exc:
                    if cls._is_fatal_base_exception(task_exc):
                        if interrupted is not None:
                            raise task_exc from interrupted
                        raise
                    if interrupted is not None:
                        raise interrupted from task_exc
                    raise
                interrupted = interrupted or exc

        try:
            result = cls._unwrap_lifecycle_outcome(result)
        except BaseException as task_exc:
            if cls._is_fatal_base_exception(task_exc):
                if interrupted is not None:
                    raise task_exc from interrupted
                raise
            if interrupted is not None:
                raise interrupted from task_exc
            raise
        if interrupted is not None:
            raise interrupted
        return result

    @classmethod
    async def _await_cancellable_initialization(cls, task: asyncio.Task[Any]) -> Any:
        """Cancel candidate init promptly, then wait for its terminal outcome."""

        try:
            raw_result = await asyncio.shield(task)
        except BaseException as interrupted:
            if not task.done():
                task.cancel()
            try:
                await cls._await_lifecycle_task(task)
            except BaseException as terminal_error:
                cls._raise_preferred_lifecycle_error(interrupted, terminal_error)
            raise
        return cls._unwrap_lifecycle_outcome(raw_result)

    async def _await_pending_finalizer(self, finalizer: asyncio.Task[Any]) -> bool:
        """Observe terminal cleanup before releasing its retained result."""

        try:
            return bool(await self._await_lifecycle_task(finalizer))
        finally:
            for name, owned in list(self._pending_finalizers.items()):
                if owned is finalizer:
                    self._pending_finalizers.pop(name, None)

    async def _wait_for_pending_finalizers(self) -> None:
        """Wait until every already-claimed pending generation reaches a terminal state."""

        fatal_error: BaseException | None = None
        interruption_error: BaseException | None = None
        ordinary_error: BaseException | None = None
        while self._pending_finalizers:
            finalizers = tuple(dict.fromkeys(self._pending_finalizers.values()))
            for finalizer in finalizers:
                try:
                    await self._await_pending_finalizer(finalizer)
                except BaseException as exc:
                    if self._is_fatal_base_exception(exc):
                        fatal_error = fatal_error or exc
                    elif isinstance(exc, asyncio.CancelledError):
                        interruption_error = interruption_error or exc
                    else:
                        ordinary_error = ordinary_error or exc
        if fatal_error is not None:
            raise fatal_error
        if interruption_error is not None:
            raise interruption_error
        if ordinary_error is not None:
            raise ordinary_error

    async def wait_inits(self) -> None:
        """Task-safe public startup-initialization barrier."""

        try:
            await self._wait_inits_unsafe()
        except BaseException as exc:
            if self._is_fatal_base_exception(exc):
                raise PluginLifecycleFatalError("<initialization>", exc) from None
            raise

    async def _wait_inits_unsafe(self) -> None:
        """Wait for the one shared startup-init batch and its terminal cleanup."""

        waiter = self._init_wait_task
        owns_batch = waiter is None or waiter.done()
        if owns_batch:
            waiter = asyncio.create_task(self._capture_lifecycle(self._drain_initializations()))
            self._init_wait_task = waiter

            def release(done: asyncio.Task[None]) -> None:
                if self._init_wait_task is done:
                    self._init_wait_task = None

            waiter.add_done_callback(release)

        assert waiter is not None

        interrupted: BaseException | None = None
        while True:
            try:
                self._unwrap_lifecycle_outcome(await asyncio.shield(waiter))
                break
            except BaseException as exc:
                if not waiter.done():
                    if not owns_batch:
                        raise
                    interrupted = interrupted or exc
                    for init_task in tuple(self._init_tasks):
                        if not init_task.done():
                            init_task.cancel()
                    continue
                try:
                    self._unwrap_lifecycle_outcome(waiter.result())
                except BaseException as wait_exc:
                    if self._is_fatal_base_exception(wait_exc):
                        if interrupted is not None:
                            raise wait_exc from interrupted
                        raise
                    if interrupted is not None:
                        raise interrupted from wait_exc
                    raise
                interrupted = interrupted or exc
                break
        if interrupted is not None:
            raise interrupted

    async def _drain_initializations(self) -> None:
        """Own and finalize every startup init visible to this shared batch."""

        terminal_errors: list[BaseException] = []
        owned_names = set(self._pending_finalizers)
        while self._init_tasks:
            tasks = list(dict.fromkeys(self._init_tasks))
            owned_names.update(
                name for task in tasks if (name := self._init_task_plugins.get(task)) is not None
            )
            owned_names.update(
                pending[0].name
                for task in tasks
                if (pending := self._pending_plugins.get(task)) is not None
            )
            raw_results = await asyncio.gather(*tasks, return_exceptions=True)
            results: list[None | BaseException] = []
            for raw_result in raw_results:
                try:
                    self._unwrap_lifecycle_outcome(raw_result)
                except BaseException as exc:
                    results.append(exc)
                else:
                    results.append(None)
            try:
                await self._finalize_init_results(tasks, results)
            except BaseException as exc:
                terminal_errors.append(exc)
        try:
            owned_names.update(self._pending_finalizers)
            await self._wait_for_pending_finalizers()
        except BaseException as exc:
            terminal_errors.append(exc)
        terminal_errors.extend(self._take_deferred_lifecycle_errors(owned_names))
        self._raise_collected_lifecycle_errors(terminal_errors)

    async def _finalize_init_results(
        self,
        tasks: list[asyncio.Task[None]],
        results: list[None | BaseException],
    ) -> None:
        """Atomically claim each result without hiding later work from unload."""

        fatal_error: BaseException | None = None
        interruption_error: BaseException | None = None
        for task, result in zip(tasks, results, strict=True):
            finalizer: asyncio.Task[bool] | None = None
            plugin_name: str | None = None
            publish_error: BaseException | None = None
            async with self._lifecycle_lock.get():
                while task in self._init_tasks:
                    self._init_tasks.remove(task)
                plugin_name = self._init_task_plugins.pop(task, None)
                pending = self._pending_plugins.pop(task, None)
                transaction = self._pending_transactions.pop(task, None)

                if isinstance(result, BaseException):
                    if pending is not None:
                        finalizer = self._start_pending_rollback(pending)
                    elif plugin_name is not None:
                        finalizer = self._pending_finalizers.get(plugin_name)
                        if finalizer is None and plugin_name in self._plugins:
                            await self._unload_plugin_once(plugin_name)
                elif pending is not None:
                    definition, module, mtime = pending
                    plugin_dir = self.plugins_dir / definition.name
                    try:
                        authorization_current = transaction is None or (
                            await self._definition_is_current_async(
                                plugin_dir,
                                definition,
                                mtime,
                                module=module,
                                authorized_entry=transaction.authorized_entry,
                                transaction=transaction,
                            )
                        )
                    except BaseException as exc:
                        publish_error = exc
                        authorization_current = False
                    if definition.name in self._quarantined_plugins or not authorization_current:
                        finalizer = self._start_pending_rollback(
                            pending,
                            retain_quarantine=(
                                transaction.uncertain_external_code
                                if transaction is not None
                                else False
                            ),
                        )
                    else:
                        try:
                            if transaction is None:
                                await self._register_loaded_plugin_async(
                                    definition,
                                    module,
                                    mtime,
                                )
                            else:
                                await self._register_loaded_plugin_async(
                                    definition,
                                    module,
                                    mtime,
                                    authorized_entry=transaction.authorized_entry,
                                )
                        except BaseException as exc:
                            if transaction is not None and isinstance(exc, PluginPathError):
                                transaction.uncertain_external_code = True
                            publish_error = exc
                            finalizer = self._start_pending_rollback(
                                pending,
                                retain_quarantine=(
                                    transaction.uncertain_external_code
                                    if transaction is not None
                                    else False
                                ),
                            )
                elif plugin_name is not None:
                    finalizer = self._pending_finalizers.get(plugin_name)

            if finalizer is not None:
                try:
                    await self._await_pending_finalizer(finalizer)
                except BaseException as cleanup_exc:
                    if self._is_fatal_base_exception(cleanup_exc):
                        fatal_error = fatal_error or cleanup_exc
                    elif isinstance(cleanup_exc, asyncio.CancelledError):
                        interruption_error = interruption_error or cleanup_exc
                    else:
                        logger.error(
                            "Pending plugin cleanup failed for %s: %s",
                            plugin_name or "<unknown>",
                            cleanup_exc,
                        )

            if isinstance(result, BaseException):
                if self._is_fatal_base_exception(result):
                    logger.warning("Plugin init interrupted")
                    fatal_error = fatal_error or result
                elif isinstance(result, asyncio.CancelledError):
                    logger.debug("Plugin init task cancelled")
                else:
                    if isinstance(result, asyncio.TimeoutError):
                        logger.warning(
                            "Plugin %s init timed out (>%ss)",
                            plugin_name or "<unknown>",
                            PLUGIN_INIT_TIMEOUT_SECONDS,
                        )
                    else:
                        logger.warning("Plugin init error: %s", result)
            if publish_error is not None:
                if self._is_fatal_base_exception(publish_error):
                    fatal_error = fatal_error or publish_error
                elif isinstance(publish_error, asyncio.CancelledError):
                    interruption_error = interruption_error or publish_error
                else:
                    logger.warning(
                        "Plugin %s publish error: %s",
                        plugin_name or "<unknown>",
                        publish_error,
                    )

        if fatal_error is not None:
            raise fatal_error
        if interruption_error is not None:
            raise interruption_error

    def load_plugin(self, plugin_dir: Path) -> None:
        # 验证插件目录名是否安全
        if not _validate_plugin_name(plugin_dir.name):
            logger.warning(
                "Skipping plugin with invalid name '%s': must be a lowercase ASCII "
                "Python identifier",
                plugin_dir.name,
            )
            return

        if plugin_dir.name in self._quarantined_plugins:
            logger.warning(
                "Refusing to load quarantined plugin %s; explicit operator cleanup or "
                "restart is required",
                plugin_dir.name,
            )
            return

        if self._has_plugin_runtime(plugin_dir.name):
            logger.warning(
                "Refusing to create a second runtime generation for plugin %s",
                plugin_dir.name,
            )
            return

        definition = self._load_definition(plugin_dir)
        if not isinstance(definition, PluginDefinition):
            return
        # 检查插件是否启用
        if not definition.enabled:
            logger.info("Plugin %s is disabled, skipping", definition.name)
            return
        try:
            self._ensure_plugin_data_dir(definition.name, force=True)
        except (OSError, PluginPathError) as exc:
            logger.error("Cannot prepare data directory for plugin %s: %s", definition.name, exc)
            return
        try:
            # Build the complete transaction fingerprint before importing code:
            # an import may start async initialization immediately, after which
            # every exit path must have a pending transaction to finalize.
            mtime = self._authorize_plugin_snapshot(plugin_dir, definition)
        except (OSError, PluginPathError) as exc:
            logger.error("Cannot fingerprint plugin %s: %s", definition.name, exc)
            return
        transaction = _PluginLoadTransaction(
            definition=definition,
            gate=self._execution_gate_for(definition),
            mtime=mtime,
        )
        try:
            module, init_task = self._load_module(
                plugin_dir,
                definition,
                transaction=transaction,
            )
        except BaseException as exc:
            fatal_deferred = False
            if transaction.module is not None:
                pending = (definition, transaction.module, mtime)
                retain_quarantine = transaction.uncertain_external_code or (
                    transaction.import_attempted and not transaction.import_completed
                )
                fatal_deferred = self._run_or_schedule_pending_rollback(
                    pending,
                    retain_quarantine=retain_quarantine,
                    original_error=exc,
                )
            elif transaction.import_attempted or transaction.uncertain_external_code:
                transaction.gate.close_admission()
                self._restart_required_plugins.add(definition.name)
                self._freeze_source_generation(definition.name)
                self._quarantine_gate_without_module(
                    definition.name,
                    transaction.gate,
                    phase="partial initial import",
                    reason=f"module execution failed without a handle ({type(exc).__name__})",
                )
            else:
                transaction.gate.close_admission()
                try:
                    if not transaction.unowned_canonical_namespace:
                        self._purge_generation_modules(
                            definition.name,
                            None,
                            transaction.gate,
                            phase="failed pre-import module purge",
                        )
                except BaseException as cleanup_error:
                    self._raise_preferred_lifecycle_error(exc, cleanup_error)
                self.router.clear_plugin(definition.name)
                self._unregister_services_owned_by(definition.name)
                self._plugins.pop(definition.name, None)
                self._plugin_states.pop(definition.name, None)
                self._execution_gates.pop(definition.name, None)
                if transaction.namespace_claim_new:
                    self._release_plugin_namespace(definition.name)
            if isinstance(exc, PluginLoadError):
                logger.error("%s", exc, exc_info=True)
                return
            if fatal_deferred:
                return
            raise
        if not module:
            transaction.gate.close_admission()
            self._purge_generation_modules(
                definition.name,
                None,
                transaction.gate,
                phase="empty initial load module purge",
            )
            self._plugin_states.pop(definition.name, None)
            self._execution_gates.pop(definition.name, None)
            if transaction.namespace_claim_new:
                self._release_plugin_namespace(definition.name)
            return
        if init_task:
            self._pending_plugins[init_task] = (definition, module, mtime)
            self._pending_transactions[init_task] = transaction
        else:
            pending = (definition, module, mtime)
            try:
                authorization_current = self._definition_is_current(
                    plugin_dir,
                    definition,
                    mtime,
                    module=module,
                    authorized_entry=transaction.authorized_entry,
                    transaction=transaction,
                )
            except BaseException as exc:
                fatal_deferred = self._run_or_schedule_pending_rollback(
                    pending,
                    original_error=exc,
                )
                if fatal_deferred:
                    return
                raise
            if not authorization_current:
                self._run_or_schedule_pending_rollback(
                    pending,
                    retain_quarantine=transaction.uncertain_external_code,
                )
                logger.warning(
                    "Plugin %s changed or lost authorization before publication",
                    definition.name,
                )
                return
            try:
                self._register_loaded_plugin(
                    definition,
                    module,
                    mtime,
                    authorized_entry=transaction.authorized_entry,
                )
            except BaseException as exc:
                if isinstance(exc, PluginPathError):
                    transaction.uncertain_external_code = True
                fatal_deferred = self._run_or_schedule_pending_rollback(
                    pending,
                    retain_quarantine=transaction.uncertain_external_code,
                    original_error=exc,
                )
                if fatal_deferred:
                    return
                if not isinstance(exc, Exception):
                    raise
                logger.warning("Plugin %s publish error: %s", definition.name, exc)

    async def unload_plugin(
        self,
        name: str,
        *,
        drain_timeout_seconds: float | None = None,
    ) -> None:
        """Unload one plugin inside the manager-wide lifecycle transaction."""

        shutdown_deadline = (
            None
            if drain_timeout_seconds is None
            else time.monotonic() + max(0.0, float(drain_timeout_seconds))
        )
        errors: list[BaseException] = []
        try:
            async with self._lifecycle_lock.get():
                if shutdown_deadline is None:
                    await self._unload_plugin_once(name)
                else:
                    await self._unload_plugin_once(
                        name,
                        shutdown_deadline=shutdown_deadline,
                    )
        except BaseException as exc:
            errors.append(exc)
        if name not in self._plugin_runtime_names():
            errors.extend(self._take_deferred_lifecycle_errors({name}))
        self._raise_task_safe_lifecycle_errors(errors, plugin_name=name)

    async def _unload_plugin_once(
        self,
        name: str,
        *,
        shutdown_deadline: float | None = None,
    ) -> None:
        """Unload one generation while the caller owns the lifecycle lock."""

        # Canonical modules are process-global, but lifecycle ownership is
        # manager-local.  A manager with no runtime ledger for ``name`` must
        # not purge handles that may belong to another manager using the same
        # canonical plugin name.
        if not self._has_plugin_runtime(name):
            if self._owns_plugin_namespace(name):
                await self._await_uncancellable_thread_transaction(
                    self._purge_plugin_modules,
                    name,
                )
                self._release_plugin_namespace(name)
            return

        deadline_kwargs: dict[str, Any] = (
            {} if shutdown_deadline is None else {"shutdown_deadline": shutdown_deadline}
        )
        if name in self._restart_required_plugins:
            gate = self._execution_gates.get(name)
            if gate is not None:
                gate.close_admission()
            self.router.clear_plugin(name)
            self._quarantined_plugins.add(name)
            logger.error(
                "Plugin %s has an incomplete import generation and cannot be "
                "safely forgotten before process restart",
                name,
            )
            return

        existing_finalizer = self._pending_finalizers.get(name)
        if existing_finalizer is not None:
            await self._await_pending_finalizer(existing_finalizer)
            return

        tasks_to_cancel = {
            task for task, plugin_name in self._init_task_plugins.items() if plugin_name == name
        }
        tasks_to_cancel.update(
            task
            for task, (definition, _module, _mtime) in self._pending_plugins.items()
            if definition.name == name
        )
        finalizers: set[asyncio.Task[Any]] = set()
        orphan_tasks: list[asyncio.Task[None]] = []
        for task in tasks_to_cancel:
            self._init_task_plugins.pop(task, None)
            if task in self._init_tasks:
                self._init_tasks.remove(task)
            pending = self._pending_plugins.pop(task, None)
            self._pending_transactions.pop(task, None)
            if pending is not None:
                finalizers.add(self._start_pending_rollback(pending, init_task=task))
            else:
                if not task.done():
                    task.cancel()
                orphan_tasks.append(task)
        if orphan_tasks:
            await asyncio.gather(*orphan_tasks, return_exceptions=True)
        if finalizers:
            for finalizer in finalizers:
                await self._await_pending_finalizer(finalizer)
            return

        plugin = self._plugins.get(name)
        gate = self._execution_gates.get(name) or (
            plugin.execution_gate if plugin is not None else None
        )
        if plugin is not None and gate is None:
            gate = self._new_execution_gate(
                plugin.definition.concurrency,
                name,
                plugin.definition.capabilities,
            )
            plugin.execution_gate = gate
            self._execution_gates[name] = gate
        if gate is not None:
            drain = await self._close_generation_gate(
                name,
                plugin,
                gate,
                phase="unload admission drain",
                **deadline_kwargs,
            )
            if not drain.drained:
                return

        plugin = self._plugins.get(name)
        if not plugin:
            if gate is not None:
                await self._purge_generation_modules_async(
                    name,
                    None,
                    gate,
                    phase="orphan unload module purge",
                )
            else:
                gate = self._new_execution_gate("parallel", name)
                gate.close_admission()
                self._execution_gates[name] = gate
                await self._purge_generation_modules_async(
                    name,
                    None,
                    gate,
                    phase="unowned orphan module purge",
                )
            self._unregister_services_owned_by(name)
            self._plugin_states.pop(name, None)
            self._execution_gates.pop(name, None)
            self._quarantined_plugins.discard(name)
            self._restart_required_plugins.discard(name)
            self._release_plugin_namespace(name)
            return
        self.router.clear_plugin(name)

        try:
            shutdown_ok = await self._shutdown_plugin_instance(
                name,
                plugin,
                **deadline_kwargs,
            )
        except BaseException as exc:
            assert gate is not None
            self._quarantine_closed_plugin(
                name,
                plugin,
                gate,
                phase="unload shutdown",
                reason=f"shutdown interrupted ({type(exc).__name__})",
            )
            raise
        if not shutdown_ok:
            assert gate is not None
            self._quarantine_closed_plugin(
                name,
                plugin,
                gate,
                phase="unload shutdown",
                reason="shutdown failed",
            )
            return

        if gate is not None:
            shutdown_drain = await self._close_generation_gate(
                name,
                plugin,
                gate,
                phase="shutdown callback drain",
                **deadline_kwargs,
            )
            if not shutdown_drain.drained:
                return

        assert gate is not None
        await self._purge_generation_modules_async(
            name,
            plugin,
            gate,
            phase="unload module purge",
        )
        self._plugins.pop(name, None)
        # 清理插件状态
        self._plugin_states.pop(name, None)
        self._execution_gates.pop(name, None)
        self._quarantined_plugins.discard(name)
        self._restart_required_plugins.discard(name)
        self._unregister_services_owned_by(name)
        self._release_plugin_namespace(name)

        logger.info("Unloaded plugin %s", name)
        self._notify_change(name)

    async def reload_plugin(self, name: str) -> None:
        """Reload one plugin inside the manager-wide lifecycle transaction."""

        self._require_hot_reload()
        errors: list[BaseException] = []
        try:
            async with self._lifecycle_lock.get():
                await self._reload_plugin_once(name)
        except BaseException as exc:
            errors.append(exc)
        if name not in self._plugin_runtime_names():
            errors.extend(self._take_deferred_lifecycle_errors({name}))
        self._raise_task_safe_lifecycle_errors(errors, plugin_name=name)

    async def reload_all_plugins(
        self,
        *,
        before_reload: Callable[[str], Awaitable[Any]] | None = None,
    ) -> bool:
        """Task-safe public bulk reload."""

        self._require_hot_reload()
        try:
            return await self._reload_all_plugins_unsafe(before_reload=before_reload)
        except BaseException as exc:
            if self._is_fatal_base_exception(exc):
                raise PluginLifecycleFatalError("<reload-all>", exc) from None
            raise

    async def _reload_all_plugins_unsafe(
        self,
        *,
        before_reload: Callable[[str], Awaitable[Any]] | None = None,
    ) -> bool:
        """Reload every live generation atomically with watcher reconciliation.

        Returns ``False`` as soon as any generation is quarantined.  The caller
        must not continue with later plugins or a final reconcile in that case.
        """

        async with self._lifecycle_lock.get():
            if self._quarantined_plugins:
                logger.error(
                    "Plugin reload refused while runtime quarantine is non-empty: %s",
                    ", ".join(sorted(self._quarantined_plugins)),
                )
                return False
            for name in self.list_plugins():
                if name in self._quarantined_plugins:
                    logger.error(
                        "Plugin reload stopped: %s is quarantined and requires "
                        "explicit operator cleanup or restart",
                        name,
                    )
                    return False
                if before_reload is not None:
                    await before_reload(name)
                if name in self._quarantined_plugins:
                    return False
                await self._reload_plugin_once(name)
                if name in self._quarantined_plugins:
                    logger.error("Plugin reload stopped after %s entered quarantine", name)
                    return False

            await self._reconcile_plugins_once()
        await self.wait_inits()
        return not self._quarantined_plugins

    async def _prepare_reload_authorization(
        self,
        name: str,
        old_plugin: LoadedPlugin,
    ) -> _ReloadAuthorization | None:
        """Load and fingerprint the candidate before retiring the live generation."""

        plugin_dir = self.plugins_dir / name
        try:
            definition = await asyncio.to_thread(self._load_definition, plugin_dir)
        except BaseException as exc:
            try:
                await self._unload_plugin_once(name)
            except BaseException as cleanup_error:
                self._raise_preferred_lifecycle_error(exc, cleanup_error)
            if self._is_fatal_base_exception(exc):
                raise
            logger.error("Plugin %s definition load failed: %s", name, exc)
            return None
        if not isinstance(definition, PluginDefinition):
            bucket = getattr(definition, "bucket", "invalid")
            if bucket in {"dependency", "read"}:
                logger.warning(
                    "Plugin reload deferred for %s because manifest check is transient (%s)",
                    name,
                    bucket,
                )
                return None
            logger.warning(
                "Plugin reload rejected for %s: manifest rejected (%s)",
                name,
                bucket,
            )
            await self._unload_plugin_once(name)
            return None
        if not definition.enabled:
            logger.warning("Plugin reload rejected for %s: definition is disabled", name)
            await self._unload_plugin_once(name)
            return None

        definition_changed = definition != old_plugin.definition
        try:
            candidate_mtime = await asyncio.to_thread(
                self._authorize_plugin_snapshot,
                plugin_dir,
                definition,
            )
        except BaseException as exc:
            if self._is_fatal_base_exception(exc):
                try:
                    await self._unload_plugin_once(name)
                except BaseException as cleanup_error:
                    self._raise_preferred_lifecycle_error(exc, cleanup_error)
                raise
            if definition_changed:
                logger.error(
                    "Plugin %s authorization changed but the replacement fingerprint "
                    "failed; retiring the no-longer-authorized generation: %s",
                    name,
                    exc,
                )
                await self._unload_plugin_once(name)
            else:
                logger.warning(
                    "Plugin %s reload fingerprint failed; keeping unchanged old instance: %s",
                    name,
                    exc,
                )
            return None

        return _ReloadAuthorization(
            plugin_dir=plugin_dir,
            definition=definition,
            mtime=candidate_mtime,
            definition_changed=definition_changed,
        )

    async def _retire_plugin_for_reload(
        self,
        name: str,
        old_plugin: LoadedPlugin,
    ) -> _RetiredPluginGeneration | None:
        """Drain, stop, and detach the old generation while retaining rollback data."""

        old_state = self._plugin_states.setdefault(name, {})
        old_state_snapshot = dict(old_state)
        old_gate = old_plugin.execution_gate or self._execution_gates.get(name)
        if old_gate is None:
            old_gate = self._new_execution_gate(
                old_plugin.definition.concurrency,
                name,
                old_plugin.definition.capabilities,
            )
        old_plugin.execution_gate = old_gate
        self._execution_gates[name] = old_gate

        old_drain = await self._close_generation_gate(
            name,
            old_plugin,
            old_gate,
            phase="reload admission drain",
        )
        if not old_drain.drained:
            return None
        try:
            old_shutdown_ok = await self._shutdown_plugin_instance(name, old_plugin)
        except BaseException as exc:
            self._quarantine_closed_plugin(
                name,
                old_plugin,
                old_gate,
                phase="reload old-generation shutdown",
                reason=f"shutdown interrupted ({type(exc).__name__})",
            )
            raise
        if not old_shutdown_ok:
            self._quarantine_closed_plugin(
                name,
                old_plugin,
                old_gate,
                phase="reload old-generation shutdown",
                reason="shutdown failed",
            )
            return None

        old_shutdown_drain = await self._close_generation_gate(
            name,
            old_plugin,
            old_gate,
            phase="reload old-generation shutdown drain",
        )
        if not old_shutdown_drain.drained:
            return None

        self.router.clear_plugin(name)
        old_modules = await self._purge_generation_modules_async(
            name,
            old_plugin,
            old_gate,
            phase="reload old-generation module purge",
        )
        old_generation_errors = self._take_deferred_lifecycle_errors({name})
        if old_generation_errors:
            self._plugins.pop(name, None)
            self._plugin_states.pop(name, None)
            self._execution_gates.pop(name, None)
            self._quarantined_plugins.discard(name)
            self._unregister_services_owned_by(name)
            self._raise_collected_lifecycle_errors(old_generation_errors)

        return _RetiredPluginGeneration(
            plugin=old_plugin,
            state=old_state,
            state_snapshot=old_state_snapshot,
            gate=old_gate,
            modules=old_modules,
        )

    def _create_reload_candidate(
        self,
        name: str,
        authorization: _ReloadAuthorization,
    ) -> _ReloadCandidateGeneration:
        candidate_gate = self._new_execution_gate(
            authorization.definition.concurrency,
            authorization.definition.name,
            authorization.definition.capabilities,
        )
        self._plugin_states[name] = {}
        self._execution_gates[name] = candidate_gate
        transaction = _PluginLoadTransaction(
            definition=authorization.definition,
            gate=candidate_gate,
            mtime=authorization.mtime,
            track_init_task=False,
        )
        return _ReloadCandidateGeneration(gate=candidate_gate, transaction=transaction)

    async def _load_and_publish_reload_candidate(
        self,
        authorization: _ReloadAuthorization,
        candidate: _ReloadCandidateGeneration,
    ) -> None:
        candidate.plugin = await self._load_canonical_candidate(
            authorization.plugin_dir,
            candidate.transaction,
        )
        try:
            candidate.authorization = await self._definition_is_current_async(
                authorization.plugin_dir,
                authorization.definition,
                authorization.mtime,
                module=candidate.plugin.module,
                authorized_entry=candidate.transaction.authorized_entry,
                transaction=candidate.transaction,
            )
        except BaseException as exc:
            candidate.authorization_error = exc
            raise
        if not candidate.authorization:
            raise PluginLoadError(
                authorization.definition.name,
                "Manifest authorization or source fingerprint changed during reload",
            )
        await self._register_loaded_plugin_async(
            candidate.plugin.definition,
            candidate.plugin.module,
            candidate.plugin.mtime,
            loaded_plugin=candidate.plugin,
            authorized_entry=candidate.transaction.authorized_entry,
        )

    async def _rollback_reload_candidate(
        self,
        name: str,
        authorization: _ReloadAuthorization,
        candidate: _ReloadCandidateGeneration,
        original_error: BaseException,
    ) -> bool:
        """Remove a failed candidate and report whether old-generation restore is safe."""

        if isinstance(original_error, PluginPathError):
            candidate.transaction.uncertain_external_code = True
        candidate_module = (
            candidate.plugin.module
            if candidate.plugin is not None
            else candidate.transaction.module
        )
        rollback_clean = True
        try:
            if isinstance(candidate_module, ModuleType):
                rollback_clean = await self._rollback_pending_plugin(
                    authorization.definition,
                    candidate_module,
                    authorization.mtime,
                    retain_quarantine=(
                        candidate.transaction.uncertain_external_code
                        or (
                            candidate.transaction.import_attempted
                            and not candidate.transaction.import_completed
                        )
                    ),
                )
            else:
                candidate_drain = await self._close_generation_gate(
                    name,
                    None,
                    candidate.gate,
                    phase="candidate without-module drain",
                    discard_registered_plugin=True,
                )
                rollback_clean = candidate_drain.drained
                if (
                    candidate.transaction.import_attempted
                    or candidate.transaction.uncertain_external_code
                ):
                    if rollback_clean:
                        self._restart_required_plugins.add(name)
                        await self._freeze_source_generation_async(name)
                        self._quarantine_gate_without_module(
                            name,
                            candidate.gate,
                            phase="partial candidate import",
                            reason=(
                                "module execution failed without a handle "
                                f"({type(original_error).__name__})"
                            ),
                        )
                    rollback_clean = False
                elif rollback_clean:
                    await self._purge_generation_modules_async(
                        name,
                        None,
                        candidate.gate,
                        phase="candidate without-module purge",
                    )
                    self.router.clear_plugin(name)
                    self._unregister_services_owned_by(name)
                    self._plugins.pop(name, None)
                    self._plugin_states.pop(name, None)
                    self._execution_gates.pop(name, None)
        except BaseException as cleanup_error:
            self._raise_preferred_lifecycle_error(original_error, cleanup_error)
        return rollback_clean

    async def _reevaluate_candidate_authorization(
        self,
        authorization: _ReloadAuthorization,
        candidate: _ReloadCandidateGeneration,
    ) -> tuple[bool, BaseException | None]:
        authorization_error = candidate.authorization_error
        if candidate.authorization is None and authorization_error is None:
            try:
                candidate.authorization = await self._definition_is_current_async(
                    authorization.plugin_dir,
                    authorization.definition,
                    authorization.mtime,
                    module=(
                        candidate.plugin.module
                        if candidate.plugin is not None
                        else candidate.transaction.module
                    ),
                    authorized_entry=candidate.transaction.authorized_entry,
                    transaction=candidate.transaction,
                )
            except BaseException as exc:
                authorization_error = exc
        return bool(candidate.authorization), authorization_error

    async def _recover_failed_reload_candidate(
        self,
        name: str,
        authorization: _ReloadAuthorization,
        retired: _RetiredPluginGeneration,
        candidate: _ReloadCandidateGeneration,
        original_error: BaseException,
    ) -> None:
        rollback_clean = await self._rollback_reload_candidate(
            name,
            authorization,
            candidate,
            original_error,
        )
        if not rollback_clean:
            return

        authorization_current, authorization_error = await self._reevaluate_candidate_authorization(
            authorization,
            candidate,
        )
        if authorization.definition_changed or not authorization_current:
            self._retain_retired_plugin_quarantine(
                name=name,
                plugin=retired.plugin,
                gate=retired.gate,
                state=retired.state,
                state_snapshot=retired.state_snapshot,
                modules=retired.modules,
                reason=(
                    f"candidate authorization or load failed ({type(original_error).__name__})"
                ),
            )
            if authorization_error is not None:
                raise authorization_error from original_error
        else:
            await self._restore_old_generation(
                name=name,
                plugin=retired.plugin,
                state=retired.state,
                state_snapshot=retired.state_snapshot,
                modules=retired.modules,
                plugin_dir=authorization.plugin_dir,
                authorization_definition=authorization.definition,
                authorization_mtime=authorization.mtime,
                original_error=original_error,
            )
        deferred_candidate_errors = self._take_deferred_lifecycle_errors({name})
        if deferred_candidate_errors:
            self._raise_collected_lifecycle_errors(deferred_candidate_errors)

    async def _reload_plugin_once(
        self,
        name: str,
        *,
        authorization: _ReloadAuthorization | None = None,
    ) -> None:
        """Reload one generation while the caller owns the lifecycle lock."""

        if name in self._quarantined_plugins:
            logger.warning(
                "Refusing to reload quarantined plugin %s; explicit operator cleanup or "
                "restart is required",
                name,
            )
            return
        old_plugin = self._plugins.get(name)
        if old_plugin is None:
            return

        if authorization is None:
            authorization = await self._prepare_reload_authorization(name, old_plugin)
        if authorization is None:
            return
        retired = await self._retire_plugin_for_reload(name, old_plugin)
        if retired is None:
            return

        candidate = self._create_reload_candidate(name, authorization)
        try:
            await self._load_and_publish_reload_candidate(authorization, candidate)
        except BaseException as exc:
            await self._recover_failed_reload_candidate(
                name,
                authorization,
                retired,
                candidate,
                exc,
            )
            if not isinstance(exc, Exception):
                raise
            return

        logger.info(
            "Reloaded canonical plugin %s version %s",
            name,
            authorization.definition.version,
        )

    def _retain_retired_plugin_quarantine(
        self,
        *,
        name: str,
        plugin: LoadedPlugin,
        gate: PluginExecutionGate,
        state: dict[str, Any],
        state_snapshot: dict[str, Any],
        modules: Mapping[str, ModuleType],
        reason: str,
    ) -> None:
        """Keep a cleanly stopped old generation as a closed diagnostic tombstone."""

        # A stopped generation must never be republished into the canonical
        # import cache: callers could import the module and invoke its handler
        # without passing through the closed execution gate.  Retain exact
        # helper-module objects only as private diagnostic evidence while the
        # aggregate namespace guard keeps every public import denied.
        with _PLUGIN_IMPORT_LOCK:
            self._private_plugin_modules[name] = dict(modules)
        state.clear()
        state.update(state_snapshot)
        plugin.execution_gate = gate
        plugin.shutdown_attempted = True
        plugin.shutdown_completed = True
        self._plugin_states[name] = state
        self._execution_gates[name] = gate
        self._quarantine_closed_plugin(
            name,
            plugin,
            gate,
            phase="candidate rollback",
            reason=reason,
        )

    async def _restore_old_generation(
        self,
        *,
        name: str,
        plugin: LoadedPlugin,
        state: dict[str, Any],
        state_snapshot: dict[str, Any],
        modules: Mapping[str, ModuleType],
        plugin_dir: Path,
        authorization_definition: PluginDefinition,
        authorization_mtime: int | float,
        original_error: BaseException,
    ) -> bool:
        """Restore a cleanly retired generation after candidate rollback."""

        old_sources = (
            plugin.mtime.sources if isinstance(plugin.mtime, _PluginContentFingerprint) else None
        )
        try:
            plugin_root = (
                resolve_plugin_root(self.plugins_dir, plugin_dir)
                if old_sources is not None
                else None
            )
        except PluginPathError as restore_collision:
            self._restart_required_plugins.add(name)
            state.clear()
            state.update(state_snapshot)
            plugin.execution_gate = plugin.execution_gate or self._new_execution_gate(
                plugin.definition.concurrency,
                name,
                plugin.definition.capabilities,
            )
            plugin.execution_gate.close_admission()
            self._plugin_states[name] = state
            self._quarantine_closed_plugin(
                name,
                plugin,
                plugin.execution_gate,
                phase="old-generation cache restore",
                reason=str(restore_collision),
            )
            logger.error(
                "Plugin %s old generation was not restored because canonical cache slots "
                "were occupied: %s",
                name,
                restore_collision,
            )
            return False
        state.clear()
        state.update(state_snapshot)
        recovery_gate = self._new_execution_gate(
            plugin.definition.concurrency,
            plugin.definition.name,
            plugin.definition.capabilities,
        )
        staging_gate = self._new_execution_gate(
            plugin.definition.concurrency,
            plugin.definition.name,
            plugin.definition.capabilities,
        )
        staging_gate.close_admission()
        plugin.execution_gate = staging_gate
        plugin.shutdown_attempted = False
        plugin.shutdown_completed = False
        plugin.shutdown_task = None
        self._unregister_services_owned_by(name)
        self._plugins[name] = plugin
        self._plugin_states[name] = state
        self._execution_gates[name] = staging_gate
        try:
            try:
                await self._await_uncancellable_thread_transaction(
                    self._restore_generation_modules,
                    name,
                    modules,
                    plugin_root=plugin_root if old_sources is not None else None,
                    sources=old_sources,
                )
            except PluginPathError as restore_collision:
                # A foreign canonical cache object must never be replaced, and
                # old initialization must not run against a partial graph.  The
                # stopped generation remains only as private diagnostic state.
                recovery_gate.close_admission()
                self._restart_required_plugins.add(name)
                self._retain_retired_plugin_quarantine(
                    name=name,
                    plugin=plugin,
                    gate=staging_gate,
                    state=state,
                    state_snapshot=state_snapshot,
                    modules=modules,
                    reason=str(restore_collision),
                )
                logger.error(
                    "Plugin %s old generation cache restore collided before recovery init: %s",
                    name,
                    restore_collision,
                )
                return False

            # Plugin modules are trusted Python extension code, not a sandbox.
            # Their exact canonical graph must be present so ordinary relative
            # imports and importlib work during recovery.  Framework authority
            # remains closed: the manager exposes the staging gate and owns no
            # command or service binding until init and reauthorization finish.
            await self._initialize_plugin_instance(
                plugin.definition,
                plugin.module,
                recovery_gate,
                state,
            )
            if not await self._definition_is_current_async(
                plugin_dir,
                authorization_definition,
                authorization_mtime,
                module=plugin.module if plugin.authorized_entry is not None else None,
                authorized_entry=plugin.authorized_entry,
            ):
                raise PluginLoadError(
                    name,
                    "Manifest authorization or source fingerprint changed during "
                    "old-generation recovery",
                )
            # Publication is synchronous after the last authorization check.
            # Until this point the externally visible plugin is held behind a
            # separate closed gate and owns no resolvable service bindings.
            plugin.execution_gate = recovery_gate
            self._execution_gates[name] = recovery_gate
            await self._register_loaded_plugin_async(
                plugin.definition,
                plugin.module,
                plugin.mtime,
                loaded_plugin=plugin,
                authorized_entry=plugin.authorized_entry,
                current_authorization=(
                    authorization_definition,
                    authorization_mtime,
                ),
            )
        except BaseException as restore_exc:
            try:
                recovery_drain = await self._close_generation_gate(
                    name,
                    plugin,
                    recovery_gate,
                    phase="old-generation recovery init drain",
                )
            except BaseException as cleanup_error:
                self._restart_required_plugins.add(name)
                self._raise_preferred_lifecycle_error(restore_exc, cleanup_error)
            if not recovery_drain.drained:
                # The recovery init may have resisted cancellation.  Keep its
                # exact gate, state, modules and finder strongly owned; running
                # shutdown or purge here would let the detached callback race
                # with objects the manager has already forgotten.
                self._restart_required_plugins.add(name)
                self._unregister_services_owned_by(name)
                logger.error(
                    "Plugin %s old generation recovery did not drain; retained in quarantine: %s",
                    name,
                    restore_exc,
                )
                if isinstance(
                    restore_exc,
                    asyncio.CancelledError,
                ) or self._is_fatal_base_exception(restore_exc):
                    raise restore_exc
                return False
            plugin.execution_gate = staging_gate
            self._execution_gates[name] = staging_gate
            failed_state_snapshot = dict(state)
            try:
                rollback_clean = await self._rollback_pending_plugin(
                    plugin.definition,
                    plugin.module,
                    plugin.mtime,
                )
            except BaseException as cleanup_error:
                self._raise_preferred_lifecycle_error(restore_exc, cleanup_error)
            if rollback_clean:
                self._retain_retired_plugin_quarantine(
                    name=name,
                    plugin=plugin,
                    gate=staging_gate,
                    state=state,
                    state_snapshot=failed_state_snapshot,
                    modules=modules,
                    reason=f"old generation restore failed ({type(restore_exc).__name__})",
                )
            logger.error(
                "Plugin %s candidate failed and old generation could not be restored; "
                "quarantined: %s",
                name,
                restore_exc,
            )
            if isinstance(restore_exc, asyncio.CancelledError) or self._is_fatal_base_exception(
                restore_exc
            ):
                raise
            return False

        logger.error(
            "Plugin %s canonical reload failed; restored old instance: %s",
            name,
            original_error,
        )
        return True

    async def _load_canonical_candidate(
        self,
        plugin_dir: Path,
        transaction: _PluginLoadTransaction,
    ) -> LoadedPlugin:
        """Import and fully initialize a canonical module without registering it."""

        definition = transaction.definition
        fingerprint = await self._capture_plugin_snapshot_async(plugin_dir, definition)
        prepared = await asyncio.to_thread(
            self._prepare_module_load,
            plugin_dir,
            definition,
            transaction,
            fingerprint=fingerprint,
        )
        module, init_task = self._load_module(
            plugin_dir,
            definition,
            transaction=transaction,
            prepared=prepared,
        )
        if module is None:
            raise PluginLoadError(definition.name, "Canonical entry could not be loaded")
        if init_task is not None:
            try:
                await self._await_cancellable_initialization(init_task)
            finally:
                if init_task in self._init_tasks:
                    self._init_tasks.remove(init_task)
                self._init_task_plugins.pop(init_task, None)
                self._pending_plugins.pop(init_task, None)
        return LoadedPlugin(
            definition=definition,
            module=module,
            mtime=transaction.mtime,
            execution_gate=transaction.gate,
            authorized_entry=transaction.authorized_entry,
        )

    async def _initialize_plugin_instance(
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
            None,
            definition.capabilities,
            definition.uses_services,
        )

        async def run_init() -> None:
            if callback_accepts_positional_context(init_func):
                await call_plugin_callback(init_func, context)
            else:
                await call_plugin_callback(init_func)

        try:
            outcome = await self._capture_lifecycle(
                asyncio.wait_for(gate.run(run_init), timeout=PLUGIN_INIT_TIMEOUT_SECONDS)
            )
            self._unwrap_lifecycle_outcome(outcome)
        except PluginCallbackFatalError as exc:
            raise exc.original from exc

    async def _shutdown_plugin_instance(
        self,
        name: str,
        plugin: LoadedPlugin,
        *,
        state: dict[str, Any] | None = None,
        shutdown_deadline: float | None = None,
    ) -> bool:
        if plugin.shutdown_attempted:
            return plugin.shutdown_completed
        shutdown = getattr(plugin.module, "shutdown", None)
        if shutdown is None:
            plugin.shutdown_attempted = True
            plugin.shutdown_completed = True
            return True

        async def perform_shutdown_once() -> bool:
            success = False
            fallback_data: tempfile.TemporaryDirectory | None = None
            try:
                context = None
                if callback_accepts_positional_context(shutdown):
                    data_dir = self._shutdown_data_dir(name, plugin)
                    if data_dir is None:
                        fallback_data = tempfile.TemporaryDirectory(
                            prefix=f"xiaoqing-{name}-shutdown-"
                        )
                        data_dir = Path(fallback_data.name)
                    shutdown_state = (
                        state if state is not None else self._plugin_states.setdefault(name, {})
                    )
                    context = self.context_factory(
                        name,
                        self.plugins_dir / name,
                        data_dir,
                        shutdown_state,
                        None,
                        None,
                        None,
                        None,
                        plugin.definition.capabilities,
                        plugin.definition.uses_services,
                    )

                async def run_shutdown() -> None:
                    if context is None:
                        await call_plugin_callback(shutdown)
                    else:
                        await call_plugin_callback(shutdown, context)

                shutdown_timeout = PLUGIN_INIT_TIMEOUT_SECONDS
                deadline_remaining: float | None = None
                if shutdown_deadline is not None:
                    deadline_remaining = max(0.0, shutdown_deadline - time.monotonic())
                    shutdown_timeout = min(
                        shutdown_timeout,
                        deadline_remaining,
                    )
                if shutdown_timeout <= 0:
                    logger.warning(
                        "Plugin %s shutdown skipped: deadline already elapsed "
                        "(shutdown_timeout=%.3fs, deadline_remaining=%.3fs)",
                        name,
                        shutdown_timeout,
                        deadline_remaining or 0.0,
                    )
                    return False
                try:
                    await asyncio.wait_for(
                        invoke_loaded_plugin(plugin, run_shutdown, allow_closed=True),
                        timeout=shutdown_timeout,
                    )
                except TimeoutError:
                    remaining = (
                        None
                        if shutdown_deadline is None
                        else max(0.0, shutdown_deadline - time.monotonic())
                    )
                    logger.warning(
                        "Plugin %s shutdown timed out "
                        "(shutdown_timeout=%.3fs, deadline_remaining=%s)",
                        name,
                        shutdown_timeout,
                        "not-set" if remaining is None else f"{remaining:.3f}s",
                    )
                    return False
                success = True
                return True
            except PluginCallbackFatalError as exc:
                raise exc.original from exc
            except Exception as exc:
                logger.warning("Plugin %s shutdown error: %s", name, exc)
                return False
            finally:
                if fallback_data is not None:
                    fallback_data.cleanup()
                plugin.shutdown_attempted = True
                plugin.shutdown_completed = success

        if plugin.shutdown_task is None:
            plugin.shutdown_task = asyncio.create_task(
                self._capture_lifecycle(perform_shutdown_once())
            )
        return bool(await self._await_lifecycle_task(plugin.shutdown_task))

    def _register_loaded_plugin(
        self,
        definition: PluginDefinition,
        module: ModuleType,
        mtime: float,
        *,
        loaded_plugin: LoadedPlugin | None = None,
        authorized_entry: Path | None = None,
        current_authorization: tuple[PluginDefinition, int | float] | None = None,
        _publication_finder: _SourceOnlyPluginFinder | None = None,
        _generation_quiesced: bool = False,
        _authorization_verified: bool = False,
    ) -> LoadedPlugin:
        effective_entry = authorized_entry or (
            loaded_plugin.authorized_entry if loaded_plugin is not None else None
        )
        publication_finder: _SourceOnlyPluginFinder | None = None
        if effective_entry is not None:
            publication_finder = (
                _publication_finder
                if _generation_quiesced
                else self._quiesce_source_generation_for_publication(definition.name)
            )
            plugin_dir = self.plugins_dir / definition.name
            current_entry = resolve_plugin_entry(
                self.plugins_dir,
                plugin_dir,
                definition.entry,
            )
            if current_entry != effective_entry:
                raise PluginPathError("plugin entry changed before final publication")
            authorization_definition, authorization_mtime = current_authorization or (
                definition,
                mtime,
            )
            if (
                isinstance(
                    authorization_mtime,
                    _PluginContentFingerprint,
                )
                and not _authorization_verified
                and not self._definition_is_current(
                    plugin_dir,
                    authorization_definition,
                    authorization_mtime,
                    module=module,
                    authorized_entry=effective_entry,
                )
            ):
                raise PluginLoadError(
                    definition.name,
                    "Manifest authorization or source fingerprint changed during final publication",
                )
            module_suffix = definition.entry.removesuffix(".py").replace("/", ".")
            entry_module_name = f"plugins.{definition.name}.{module_suffix}"
            self._validate_owned_namespace(
                definition.name,
                entry_module_name,
                module,
            )
            self._validate_entry_module(
                module,
                entry_module_name,
                effective_entry,
            )
        services = self._bind_declared_services(definition, module)
        for service_name in services:
            existing = self._services.get(service_name)
            if existing is not None and existing.owner != definition.name:
                raise PluginLoadError(
                    definition.name,
                    f"Service name is already registered by {existing.owner}: {service_name}",
                )
        execution_gate = self._execution_gate_for(definition)
        command_specs = self._build_command_specs(definition, module, execution_gate)
        previous_plugin = self._plugins.get(definition.name)
        previous_services = {
            service_name: binding
            for service_name, binding in self._services.items()
            if binding.owner == definition.name
        }
        previous_commands: tuple[CommandSpec, ...] = ()
        publication_token = execution_gate.hold_for_publication()
        try:
            previous_commands = self.router.replace_plugin(definition.name, command_specs)
            self._unregister_services_owned_by(definition.name)
            self._services.update(services)
            loaded = loaded_plugin or LoadedPlugin(
                definition=definition,
                module=module,
                mtime=mtime,
                execution_gate=execution_gate,
                services=services,
                authorized_entry=effective_entry,
                data_dir=(
                    self._data_directories[definition.name].path
                    if definition.name in self._data_directories
                    else None
                ),
            )
            loaded.definition = definition
            loaded.module = module
            loaded.mtime = mtime
            loaded.execution_gate = execution_gate
            loaded.shutdown_attempted = False
            loaded.shutdown_completed = False
            loaded.shutdown_task = None
            loaded.services = services
            loaded.authorized_entry = effective_entry
            data_record = self._data_directories.get(definition.name)
            if data_record is not None:
                loaded.data_dir = data_record.path
            self._plugins[definition.name] = loaded
            self._resume_source_generation_after_publication(
                definition.name,
                publication_finder,
                execution_gate,
                publication_token,
            )
        except BaseException as exc:
            self._abort_source_generation_publication(
                definition.name,
                publication_finder,
                execution_gate,
            )
            # CommandConflictError is raised before CommandRouter mutates its
            # registry, so restoring the empty sentinel would incorrectly erase
            # a previously published generation.  Other publication failures may
            # occur after the atomic replacement and still require rollback.
            if not isinstance(exc, CommandConflictError):
                self.router.replace_plugin(definition.name, list(previous_commands))
            self._unregister_services_owned_by(definition.name)
            self._services.update(previous_services)
            if previous_plugin is None:
                self._plugins.pop(definition.name, None)
            else:
                self._plugins[definition.name] = previous_plugin
            raise
        logger.info(
            "Loaded plugin name=%s version=%s author=%s description=%s",
            definition.name,
            definition.version,
            definition.author or "-",
            definition.description or "-",
        )
        self._notify_change(definition.name)
        return loaded

    async def _register_loaded_plugin_async(
        self,
        definition: PluginDefinition,
        module: ModuleType,
        mtime: float,
        *,
        loaded_plugin: LoadedPlugin | None = None,
        authorized_entry: Path | None = None,
        current_authorization: tuple[PluginDefinition, int | float] | None = None,
    ) -> LoadedPlugin:
        """Quiesce import threads without blocking the asyncio event loop."""

        effective_entry = authorized_entry or (
            loaded_plugin.authorized_entry if loaded_plugin is not None else None
        )
        if effective_entry is None:
            if loaded_plugin is None and authorized_entry is None:
                return self._register_loaded_plugin(definition, module, mtime)
            return self._register_loaded_plugin(
                definition,
                module,
                mtime,
                loaded_plugin=loaded_plugin,
                authorized_entry=authorized_entry,
                current_authorization=current_authorization,
            )
        finder = await self._await_uncancellable_thread_transaction(
            self._quiesce_source_generation_for_publication,
            definition.name,
        )
        authorization_definition, authorization_mtime = current_authorization or (
            definition,
            mtime,
        )
        if isinstance(authorization_mtime, _PluginContentFingerprint) and not (
            await self._definition_is_current_async(
                self.plugins_dir / definition.name,
                authorization_definition,
                authorization_mtime,
                module=module,
                authorized_entry=effective_entry,
            )
        ):
            raise PluginLoadError(
                definition.name,
                "Manifest authorization or source fingerprint changed during final publication",
            )
        return self._register_loaded_plugin(
            definition,
            module,
            mtime,
            loaded_plugin=loaded_plugin,
            authorized_entry=authorized_entry,
            current_authorization=current_authorization,
            _publication_finder=finder,
            _generation_quiesced=True,
            _authorization_verified=True,
        )

    def _execution_gate_for(self, definition: PluginDefinition) -> PluginExecutionGate:
        gate = self._execution_gates.get(definition.name)
        if gate is None:
            gate = self._new_execution_gate(
                definition.concurrency,
                definition.name,
                definition.capabilities,
            )
            self._execution_gates[definition.name] = gate
        return gate

    def _collect_purge_candidates(
        self,
        name: str,
        canonical_name: str,
        prefix: str,
        owner: object | None,
    ) -> dict[str, ModuleType]:
        """按精确对象台账收集可删除模块，绝不只凭模块名判定所有权。"""

        owned = self._owned_plugin_modules.get(name)
        if owner is self._namespace_owner_token:
            return dict(owned or {})

        candidates = dict(self._private_plugin_modules.get(name, {}))
        registered = self._plugins.get(name)
        if not isinstance(registered, LoadedPlugin):
            return candidates
        registered_module = registered.module
        registered_name = ModuleType.__getattribute__(registered_module, "__name__")
        if registered_name != canonical_name and not registered_name.startswith(prefix):
            return candidates
        candidates.setdefault(registered_name, registered_module)

        # 私有加载台账可能只记录入口；沿精确的父子绑定补齐其包链。
        child_name = registered_name
        child_module = registered_module
        while child_name != canonical_name:
            parent_name, separator, attribute = child_name.rpartition(".")
            if not separator or parent_name == "plugins":
                break
            parent_module = sys.modules.get(parent_name)
            if not isinstance(parent_module, ModuleType):
                break
            namespace = ModuleType.__getattribute__(parent_module, "__dict__")
            if namespace.get(attribute) is not child_module:
                break
            candidates.setdefault(parent_name, parent_module)
            child_name = parent_name
            child_module = parent_module
        return candidates

    def _build_purge_parent_bindings(
        self,
        candidates: Mapping[str, ModuleType],
        canonical_name: str,
        prefix: str,
        owner: object | None,
        missing: object,
    ) -> dict[str, tuple[ModuleType, dict[str, Any], str]]:
        """在修改缓存前验证所有模块槽和父包属性仍指向预期对象。"""

        if owner is self._namespace_owner_token:
            foreign = [
                module_name
                for module_name, module in list(sys.modules.items())
                if (module_name == canonical_name or module_name.startswith(prefix))
                and candidates.get(module_name) is not module
            ]
            if foreign:
                raise PluginPathError(
                    "canonical plugin cache contains unowned objects: " + ", ".join(sorted(foreign))
                )

        parent_bindings: dict[str, tuple[ModuleType, dict[str, Any], str]] = {}
        for module_name, expected in candidates.items():
            parent_name, separator, child_name = module_name.rpartition(".")
            if not separator:
                continue
            expected_parent = (
                self._plugins_package if parent_name == "plugins" else candidates.get(parent_name)
            )
            if expected_parent is None:
                if sys.modules.get(parent_name) is not None:
                    raise PluginPathError(
                        f"plugin module has a foreign parent during purge: {module_name}"
                    )
                continue
            if sys.modules.get(parent_name) is not expected_parent:
                raise PluginPathError(f"plugin parent module changed during purge: {module_name}")
            namespace = cast(
                dict[str, Any],
                ModuleType.__getattribute__(expected_parent, "__dict__"),
            )
            child_binding = namespace.get(child_name, missing)
            if child_binding is not missing and child_binding is not expected:
                raise PluginPathError(
                    f"plugin parent binding is foreign during purge: {module_name}"
                )
            parent_bindings[module_name] = (expected_parent, namespace, child_name)
        return parent_bindings

    def _remove_purge_candidates(
        self,
        candidates: Mapping[str, ModuleType],
        parent_bindings: Mapping[str, tuple[ModuleType, dict[str, Any], str]],
        canonical_name: str,
        prefix: str,
        owner: object | None,
        missing: object,
    ) -> None:
        """按子到父顺序删除模块；任一身份校验失败时恢复全部已删对象。"""

        removed_modules: dict[str, ModuleType] = {}
        removed_parent_attributes: list[tuple[ModuleType, str, ModuleType]] = []
        try:
            for module_name, expected in sorted(
                candidates.items(),
                key=lambda item: item[0].count("."),
                reverse=True,
            ):
                parent_binding = parent_bindings.get(module_name)
                if parent_binding is not None:
                    parent, namespace, child_name = parent_binding
                    current_parent_binding = namespace.get(child_name, missing)
                    if current_parent_binding is expected:
                        removed = namespace.pop(child_name, missing)
                        if removed is expected:
                            removed_parent_attributes.append((parent, child_name, expected))
                        elif removed is not missing:
                            namespace.setdefault(child_name, removed)
                            raise PluginPathError(
                                f"plugin parent binding changed during purge: {module_name}"
                            )
                    elif current_parent_binding is not missing:
                        raise PluginPathError(
                            f"plugin parent binding changed during purge: {module_name}"
                        )

                current = sys.modules.get(module_name, missing)
                if current is missing:
                    continue
                removed = sys.modules.pop(module_name, missing)
                if removed is expected:
                    removed_modules[module_name] = expected
                    continue
                if removed is not missing:
                    sys.modules.setdefault(module_name, removed)
                raise PluginPathError(f"plugin module binding changed during purge: {module_name}")

            remaining = [
                module_name
                for module_name in list(sys.modules)
                if module_name == canonical_name or module_name.startswith(prefix)
            ]
            if remaining and owner is self._namespace_owner_token:
                raise PluginPathError(
                    "canonical plugin modules appeared during purge: "
                    + ", ".join(sorted(remaining))
                )
        except BaseException:
            for module_name, module in sorted(
                removed_modules.items(),
                key=lambda item: item[0].count("."),
            ):
                sys.modules.setdefault(module_name, module)
            for parent, child_name, module in removed_parent_attributes:
                ModuleType.__getattribute__(parent, "__dict__").setdefault(
                    child_name,
                    module,
                )
            raise

    def _purge_plugin_modules(self, name: str) -> dict[str, ModuleType]:
        canonical_name = f"plugins.{name}"
        prefix = f"{canonical_name}."
        finder: _SourceOnlyPluginFinder | None
        with _PLUGIN_IMPORT_LOCK:
            owner = _PLUGIN_NAMESPACE_OWNERS.get(name)
            if owner is not None and owner is not self._namespace_owner_token:
                return {}
            finder = self._source_finders.get(name)
            module_names: set[str] = {
                module_name
                for module_name in list(sys.modules)
                if module_name == canonical_name or module_name.startswith(prefix)
            }
        if finder is not None:
            try:
                module_names.update(finder.deactivate_and_wait())
            except PluginPathError as exc:
                self._mark_source_finder_compromised(name, str(exc))
                raise
            if finder.compromised:
                raise PluginPathError(f"plugin source generation is compromised: {name}")
        try:
            self._wait_for_module_import_barriers(module_names)
        except PluginPathError as exc:
            if finder is not None:
                self._mark_source_finder_compromised(name, str(exc))
            raise

        missing = object()
        with _PLUGIN_IMPORT_LOCK:
            owner = _PLUGIN_NAMESPACE_OWNERS.get(name)
            if owner is not None and owner is not self._namespace_owner_token:
                raise PluginPathError(f"plugin namespace owner changed during purge: {name}")
            if finder is not None and self._source_finders.get(name) is not finder:
                raise PluginPathError(f"plugin source generation changed during purge: {name}")
            candidates = self._collect_purge_candidates(
                name,
                canonical_name,
                prefix,
                owner,
            )
            parent_bindings = self._build_purge_parent_bindings(
                candidates,
                canonical_name,
                prefix,
                owner,
                missing,
            )
            self._remove_purge_candidates(
                candidates,
                parent_bindings,
                canonical_name,
                prefix,
                owner,
                missing,
            )

            if owner is self._namespace_owner_token:
                self._owned_plugin_modules[name] = {}
            else:
                self._private_plugin_modules.pop(name, None)
            return candidates

    def _validate_restore_commit(
        self,
        plugin_name: str,
        modules: Mapping[str, ModuleType],
        canonical: str,
        prefix: str,
        prepared_finder: _SourceOnlyPluginFinder | None,
    ) -> None:
        """在持有全局导入锁时复核恢复事务的所有权与空闲槽位。"""

        if _PLUGIN_NAMESPACE_OWNERS.get(plugin_name) is not self._namespace_owner_token:
            raise PluginPathError("restore namespace ownership changed before commit")
        if (
            prepared_finder is not None
            and self._source_finders.get(plugin_name) is not prepared_finder
        ):
            raise PluginPathError("restore source finder changed before commit")
        foreign_modules = [
            module_name
            for module_name, current in list(sys.modules.items())
            if (module_name == canonical or module_name.startswith(prefix))
            and modules.get(module_name) is not current
        ]
        if foreign_modules:
            raise PluginPathError(
                "cannot restore over foreign canonical modules: "
                + ", ".join(sorted(foreign_modules))
            )

    def _prepare_restore_bindings(
        self,
        ordered: list[tuple[str, ModuleType]],
        modules: Mapping[str, ModuleType],
        prepared_finder: _SourceOnlyPluginFinder | None,
        missing: object,
    ) -> tuple[
        dict[str, tuple[dict[str, Any], str] | None],
        set[str],
        set[str],
    ]:
        """验证待恢复模块的父链，并记录真正需要写入的空槽位。"""

        parent_bindings: dict[str, tuple[dict[str, Any], str] | None] = {}
        missing_module_slots: set[str] = set()
        missing_parent_slots: set[str] = set()
        for module_name, module in ordered:
            current = sys.modules.get(module_name, missing)
            if current is not missing and current is not module:
                raise PluginPathError(
                    f"canonical module slot is occupied during restore: {module_name}"
                )
            if current is missing:
                missing_module_slots.add(module_name)
            parent_name, _, child_name = module_name.rpartition(".")
            parent = self._plugins_package if parent_name == "plugins" else modules.get(parent_name)
            if not isinstance(parent, ModuleType):
                # 无源码快照的旧测试夹具可能只保留入口模块；仅允许父槽仍为空的
                # 这种狭窄形态，真实源码代际必须恢复完整且精确的父链。
                if (
                    prepared_finder is None
                    and parent_name != "plugins"
                    and sys.modules.get(parent_name, missing) is missing
                ):
                    parent_bindings[module_name] = None
                    continue
                raise PluginPathError(f"restore mapping has no exact parent for: {module_name}")
            cached_parent = sys.modules.get(parent_name, missing)
            if parent_name == "plugins":
                if cached_parent is not parent:
                    raise PluginPathError("canonical plugins package changed during restore")
            elif cached_parent is not missing and cached_parent is not parent:
                raise PluginPathError(
                    f"canonical parent module is foreign during restore: {module_name}"
                )
            namespace = ModuleType.__getattribute__(parent, "__dict__")
            parent_current = namespace.get(child_name, missing)
            if parent_current is not missing and parent_current is not module:
                raise PluginPathError(
                    f"canonical parent binding is occupied during restore: {module_name}"
                )
            if parent_current is missing:
                missing_parent_slots.add(module_name)
            parent_bindings[module_name] = (namespace, child_name)
        return parent_bindings, missing_module_slots, missing_parent_slots

    def _mark_restore_specs_initializing(
        self,
        ordered: list[tuple[str, ModuleType]],
        missing: object,
    ) -> list[tuple[dict[str, Any], object, object, bool]]:
        """暂时设置 CPython 导入状态，使并发导入等待原子发布完成。"""

        spec_states: list[tuple[dict[str, Any], object, object, bool]] = []
        try:
            for module_name, module in ordered:
                namespace = ModuleType.__getattribute__(module, "__dict__")
                original_spec = namespace.get("__spec__", missing)
                if type(original_spec) is importlib.machinery.ModuleSpec:
                    spec_namespace = vars(original_spec)
                    had_initializing = "_initializing" in spec_namespace
                    original_initializing = spec_namespace.get("_initializing", missing)
                    spec_namespace["_initializing"] = True
                    spec_states.append(
                        (
                            namespace,
                            original_spec,
                            original_initializing,
                            had_initializing,
                        )
                    )
                    continue
                temporary_spec = importlib.machinery.ModuleSpec(module_name, loader=None)
                vars(temporary_spec)["_initializing"] = True
                namespace["__spec__"] = temporary_spec
                spec_states.append((namespace, original_spec, temporary_spec, False))
        except BaseException:
            self._restore_module_spec_states(spec_states, missing)
            raise
        return spec_states

    @staticmethod
    def _restore_module_spec_states(
        spec_states: list[tuple[dict[str, Any], object, object, bool]],
        missing: object,
    ) -> None:
        """恢复发布前的 ``__spec__`` 及 ``_initializing`` 状态。"""

        for namespace, original_spec, state, had_initializing in reversed(spec_states):
            if type(original_spec) is importlib.machinery.ModuleSpec:
                spec_namespace = vars(original_spec)
                if had_initializing:
                    spec_namespace["_initializing"] = state
                else:
                    spec_namespace.pop("_initializing", None)
            elif namespace.get("__spec__") is state:
                if original_spec is missing:
                    namespace.pop("__spec__", None)
                else:
                    namespace["__spec__"] = original_spec

    def _publish_restored_modules(
        self,
        plugin_name: str,
        modules: Mapping[str, ModuleType],
        ordered: list[tuple[str, ModuleType]],
        parent_bindings: Mapping[str, tuple[dict[str, Any], str] | None],
        missing_module_slots: set[str],
        missing_parent_slots: set[str],
        prepared_finder: _SourceOnlyPluginFinder | None,
        missing: object,
    ) -> None:
        """写入完整代际；发布中途失败时只撤销本事务插入的精确对象。"""

        inserted_modules: list[tuple[str, ModuleType]] = []
        inserted_parents: list[tuple[dict[str, Any], str, ModuleType]] = []
        spec_states = self._mark_restore_specs_initializing(ordered, missing)
        try:
            for module_name, module in ordered:
                current = sys.modules.setdefault(module_name, module)
                if current is not module:
                    raise PluginPathError(
                        f"canonical module slot changed during restore: {module_name}"
                    )
                if module_name in missing_module_slots:
                    inserted_modules.append((module_name, module))
                parent_binding = parent_bindings[module_name]
                if parent_binding is None:
                    continue
                namespace, child_name = parent_binding
                current_parent = namespace.setdefault(child_name, module)
                if current_parent is not module:
                    raise PluginPathError(
                        f"canonical parent binding changed during restore: {module_name}"
                    )
                if module_name in missing_parent_slots:
                    inserted_parents.append((namespace, child_name, module))
            self._owned_plugin_modules[plugin_name] = dict(modules)
            if prepared_finder is not None:
                prepared_finder._active = True
        except BaseException:
            if prepared_finder is not None:
                prepared_finder._active = False
            self._owned_plugin_modules[plugin_name] = {}
            for namespace, child_name, module in reversed(inserted_parents):
                if namespace.get(child_name, missing) is module:
                    namespace.pop(child_name, None)
            for module_name, module in reversed(inserted_modules):
                if sys.modules.get(module_name, missing) is module:
                    sys.modules.pop(module_name, None)
            raise
        finally:
            self._restore_module_spec_states(spec_states, missing)

    def _restore_generation_modules(
        self,
        plugin_name: str,
        modules: Mapping[str, ModuleType],
        *,
        plugin_root: Path | None = None,
        sources: Mapping[str, bytes] | None = None,
    ) -> None:
        """Atomically restore exact objects while CPython import locks hide staging."""

        if (plugin_root is None) != (sources is None):
            raise PluginPathError("restored source finder requires both root and sources")
        canonical = f"plugins.{plugin_name}"
        prefix = f"{canonical}."
        missing = object()
        ordered = sorted(modules.items(), key=lambda item: item[0].count("."))
        if any(
            module_name != canonical and not module_name.startswith(prefix)
            for module_name in modules
        ):
            raise PluginPathError("restore mapping contains non-canonical module names")

        prepared_finder: _SourceOnlyPluginFinder | None = None
        with _PLUGIN_IMPORT_LOCK:
            self._claim_plugin_namespace(plugin_name)
            if plugin_root is not None and sources is not None:
                prepared_finder = self._prepare_inactive_source_finder(
                    plugin_name,
                    plugin_root,
                    sources,
                )

        lock_names = set(modules) | {canonical}
        if prepared_finder is not None:
            lock_names.update(prepared_finder.possible_module_names())

        def commit() -> None:
            with _PLUGIN_IMPORT_LOCK:
                self._validate_restore_commit(
                    plugin_name,
                    modules,
                    canonical,
                    prefix,
                    prepared_finder,
                )
                (
                    parent_bindings,
                    missing_module_slots,
                    missing_parent_slots,
                ) = self._prepare_restore_bindings(
                    ordered,
                    modules,
                    prepared_finder,
                    missing,
                )
                self._publish_restored_modules(
                    plugin_name,
                    modules,
                    ordered,
                    parent_bindings,
                    missing_module_slots,
                    missing_parent_slots,
                    prepared_finder,
                    missing,
                )

        _PLUGIN_IMPORT_BARRIER_COORDINATOR.run_locked(
            tuple(sorted(lock_names)),
            commit,
            timeout=_PLUGIN_IMPORT_DRAIN_TIMEOUT_SECONDS,
        )

    def _plugin_module_aliases(self, plugin_dir: Path, name: str) -> list[str]:
        """Return non-canonical module names backed by files in one plugin."""
        canonical = f"plugins.{name}"
        root = plugin_dir.resolve(strict=False)
        root_text = os.path.normcase(os.fspath(root))
        root_prefix = root_text if root_text.endswith(os.sep) else f"{root_text}{os.sep}"
        aliases: list[str] = []
        for module_name, module in list(sys.modules.items()):
            if type(module_name) is not str or not isinstance(module, ModuleType):
                continue
            if module_name == canonical or module_name.startswith(f"{canonical}."):
                continue
            try:
                namespace = ModuleType.__getattribute__(module, "__dict__")
            except TypeError:
                # Some C-extension proxies claim ModuleType instance checks but
                # are not accepted by the base descriptor.
                continue
            module_file = namespace.get("__file__")
            if type(module_file) is not str or not module_file or len(module_file) > 32768:
                continue
            try:
                lexical_path = os.path.normcase(os.path.abspath(module_file))
            except (OSError, ValueError):
                continue
            if lexical_path != root_text and not lexical_path.startswith(root_prefix):
                continue
            module_path = Path(module_file)
            try:
                metadata = module_path.stat()
                identity = self._watch_file_identity(metadata)
                cache_key = (module_name, id(module), module_file, identity)
                missing = object()
                resolved = self._module_origin_cache.get(cache_key, missing)
                if resolved is missing:
                    resolved = module_path.resolve(strict=False)
                    self._module_origin_cache[cache_key] = resolved
                    if len(self._module_origin_cache) > _MAX_MODULE_ORIGIN_CACHE_ENTRIES:
                        self._module_origin_cache.popitem(last=False)
                else:
                    self._module_origin_cache.move_to_end(cache_key)
                assert isinstance(resolved, Path)
            except (OSError, RuntimeError):
                # A live alias remains dangerous after its source was moved or
                # deleted.  ``strict=False`` and finally a lexical absolute
                # path still identify ordinary aliases below this plugin root.
                try:
                    resolved = module_path.resolve(strict=False)
                except (OSError, RuntimeError):
                    resolved = module_path.absolute()
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            aliases.append(module_name)
        return sorted(aliases)
