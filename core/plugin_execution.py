"""Execution gates for plugin manifest concurrency declarations."""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import Future as ConcurrentFuture
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

PluginConcurrency = Literal["parallel", "sequential"]
T = TypeVar("T")
logger = logging.getLogger(__name__)
_SYNC_PLUGIN_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="xiaoqing-plugin",
)
_CURRENT_EXECUTION_GATE: ContextVar[PluginExecutionGate | None] = ContextVar(
    "xiaoqing_current_plugin_execution_gate",
    default=None,
)


@dataclass(frozen=True)
class PluginExecutionPolicy:
    """Runtime limits shared by all callback types of one plugin."""

    timeout_seconds: float | None = 60.0
    parallel_limit: int = 4
    failure_threshold: int = 3
    cooldown_seconds: float = 60.0
    drain_timeout_seconds: float = 5.0

    @classmethod
    def from_mapping(
        cls,
        values: dict[str, Any] | None,
        *,
        fallback: PluginExecutionPolicy | None = None,
    ) -> PluginExecutionPolicy:
        base = fallback or cls()
        values = values if isinstance(values, dict) else {}

        raw_timeout = values.get("timeout_seconds", base.timeout_seconds)
        try:
            timeout = None if raw_timeout in (None, 0, 0.0) else max(0.1, float(raw_timeout))
        except (TypeError, ValueError):
            timeout = base.timeout_seconds

        def positive_int(name: str, default: int) -> int:
            try:
                return max(1, int(values.get(name, default)))
            except (TypeError, ValueError):
                return default

        def positive_float(name: str, default: float) -> float:
            try:
                return max(0.1, float(values.get(name, default)))
            except (TypeError, ValueError):
                return default

        return cls(
            timeout_seconds=timeout,
            parallel_limit=positive_int("parallel_limit", base.parallel_limit),
            failure_threshold=positive_int("failure_threshold", base.failure_threshold),
            cooldown_seconds=positive_float("cooldown_seconds", base.cooldown_seconds),
            drain_timeout_seconds=positive_float(
                "drain_timeout_seconds",
                base.drain_timeout_seconds,
            ),
        )


@dataclass(frozen=True)
class PluginExecutionDrainResult:
    """Bounded close result, including work Python cannot force-stop."""

    drained: bool
    pending_async_tasks: int
    pending_sync_callbacks: int
    waited_seconds: float


class PluginExecutionClosed(RuntimeError):
    """Raised when a plugin is unloading and cannot accept more work."""


class PluginExecutionUnavailable(RuntimeError):
    """Raised while a plugin circuit is open after repeated failures."""


class PluginExecutionTimeout(TimeoutError):
    """Raised when a plugin callback exceeds its configured hard limit."""


class PluginExecutionGate:
    """Run one plugin's entry points according to its manifest declaration.

    ``parallel`` invokes operations immediately.  ``sequential`` admits one
    operation at a time, in asyncio lock order.  Closing a gate is terminal:
    it rejects new operations and cancels any active or queued caller tasks so
    a reload cannot leave old plugin code running alongside the new module.
    """

    def __init__(
        self,
        mode: PluginConcurrency,
        *,
        plugin_name: str = "<unknown>",
        policy: PluginExecutionPolicy | None = None,
    ) -> None:
        if mode not in {"parallel", "sequential"}:
            raise ValueError(f"unsupported plugin concurrency mode: {mode}")
        self.mode = mode
        self.plugin_name = plugin_name
        self._policy = policy or PluginExecutionPolicy()
        self._serial_lock: asyncio.Lock | None = None
        self._state_lock: asyncio.Lock | None = None
        self._parallel_semaphore: asyncio.Semaphore | None = None
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._operation_tasks: set[asyncio.Task[Any]] = set()
        self._sync_futures: set[ConcurrentFuture[Any]] = set()
        self._closed = False
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._poisoned_by_timeout = False
        self._poisoned_by_sync_callback = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def policy(self) -> PluginExecutionPolicy:
        return self._policy

    @property
    def pending_sync_callbacks(self) -> int:
        return sum(not future.done() for future in self._sync_futures)

    @property
    def drained(self) -> bool:
        return not any(not task.done() for task in self._active_tasks | self._operation_tasks) and not any(
            not future.done() for future in self._sync_futures
        )

    def set_policy(self, policy: PluginExecutionPolicy) -> None:
        """Apply new limits to future calls without disturbing active work."""

        self._policy = policy
        if not self._active_tasks:
            self._parallel_semaphore = None

    def _get_serial_lock(self) -> asyncio.Lock:
        if self._serial_lock is None:
            self._serial_lock = asyncio.Lock()
        return self._serial_lock

    def _get_state_lock(self) -> asyncio.Lock:
        if self._state_lock is None:
            self._state_lock = asyncio.Lock()
        return self._state_lock

    def _get_parallel_semaphore(self) -> asyncio.Semaphore:
        if self._parallel_semaphore is None:
            self._parallel_semaphore = asyncio.Semaphore(self._policy.parallel_limit)
        return self._parallel_semaphore

    def _ensure_available_locked(self, *, allow_closed: bool) -> None:
        if self._closed and not allow_closed:
            raise PluginExecutionClosed("plugin is unloading")
        if allow_closed:
            return
        if (
            self._poisoned_by_timeout
            or self._poisoned_by_sync_callback
            or time.monotonic() < self._circuit_open_until
        ):
            raise PluginExecutionUnavailable("plugin circuit is temporarily open")

    async def _record_success(self) -> None:
        async with self._get_state_lock():
            self._consecutive_failures = 0
            if not self._poisoned_by_timeout and not self._poisoned_by_sync_callback:
                self._circuit_open_until = 0.0

    async def _record_failure(self, *, force_open: bool = False) -> None:
        async with self._get_state_lock():
            self._consecutive_failures += 1
            if force_open or self._consecutive_failures >= self._policy.failure_threshold:
                self._circuit_open_until = time.monotonic() + self._policy.cooldown_seconds

    def _discard_operation_task(self, task: asyncio.Task[Any]) -> None:
        self._operation_tasks.discard(task)
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass
        if self._poisoned_by_timeout and not any(
            not operation.done() for operation in self._operation_tasks
        ):
            self._poisoned_by_timeout = False

    def _discard_sync_future(self, future: ConcurrentFuture[Any]) -> None:
        self._sync_futures.discard(future)
        try:
            future.exception()
        except Exception:
            pass
        if self._poisoned_by_sync_callback and not any(
            not item.done() for item in self._sync_futures
        ):
            self._poisoned_by_sync_callback = False

    def _register_sync_future(self, future: ConcurrentFuture[Any]) -> None:
        """Track the actual executor future, not its cancellable asyncio facade."""

        self._sync_futures.add(future)
        loop = asyncio.get_running_loop()

        def completed(item: ConcurrentFuture[Any]) -> None:
            try:
                loop.call_soon_threadsafe(self._discard_sync_future, item)
            except RuntimeError:
                # Event-loop teardown cannot make a running thread disappear;
                # retain only genuinely unfinished futures for diagnostics.
                self._sync_futures.discard(item)

        future.add_done_callback(completed)

    def _mark_unfinished_child(self, task: asyncio.Task[Any]) -> None:
        if not task.done():
            self._poisoned_by_timeout = True
        if any(not future.done() for future in self._sync_futures):
            self._poisoned_by_sync_callback = True

    async def _run_bounded(self, operation: Callable[[], Awaitable[T]]) -> T:
        async def run_in_gate_scope() -> T:
            token = _CURRENT_EXECUTION_GATE.set(self)
            try:
                return await operation()
            finally:
                _CURRENT_EXECUTION_GATE.reset(token)

        task = asyncio.create_task(run_in_gate_scope())
        self._operation_tasks.add(task)
        task.add_done_callback(self._discard_operation_task)
        try:
            timeout = self._policy.timeout_seconds
            if timeout is None:
                result = await task
            else:
                result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError as exc:
            await self._record_failure(force_open=True)
            task.cancel()
            self._mark_unfinished_child(task)
            logger.warning(
                "Plugin callback timed out: plugin=%s timeout=%.1fs",
                self.plugin_name,
                self._policy.timeout_seconds or 0.0,
            )
            raise PluginExecutionTimeout("plugin callback timed out") from exc
        except asyncio.CancelledError:
            task.cancel()
            self._mark_unfinished_child(task)
            raise
        except Exception:
            await self._record_failure()
            raise
        else:
            await self._record_success()
            return result

    async def run(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        allow_closed: bool = False,
    ) -> T:
        """Execute an async operation while tracking its caller for unload."""

        task = asyncio.current_task()
        if task is None:
            # asyncio entry points always have a current task, but retaining a
            # fail-closed branch makes the contract explicit for embedders.
            async with self._get_state_lock():
                self._ensure_available_locked(allow_closed=allow_closed)
            return await self._run_bounded(operation)

        state_lock = self._get_state_lock()
        async with state_lock:
            self._ensure_available_locked(allow_closed=allow_closed)
            self._active_tasks.add(task)

        try:
            if self.mode == "sequential":
                async with self._get_serial_lock():
                    async with state_lock:
                        self._ensure_available_locked(allow_closed=allow_closed)
                    return await self._run_bounded(operation)
            async with self._get_parallel_semaphore():
                async with state_lock:
                    self._ensure_available_locked(allow_closed=allow_closed)
                return await self._run_bounded(operation)
        finally:
            async with state_lock:
                self._active_tasks.discard(task)

    async def close(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> PluginExecutionDrainResult:
        """Close admission and wait a bounded time for real executor work."""

        started = time.monotonic()
        timeout = (
            self._policy.drain_timeout_seconds
            if timeout_seconds is None
            else max(0.0, float(timeout_seconds))
        )
        deadline = started + timeout
        current_task = asyncio.current_task()
        async with self._get_state_lock():
            self._closed = True
            tasks_to_cancel = {
                task
                for task in self._active_tasks | self._operation_tasks
                if task is not current_task and not task.done()
            }

        for task in tasks_to_cancel:
            task.cancel()

        while True:
            pending_async = {
                task
                for task in self._active_tasks | self._operation_tasks
                if task is not current_task and not task.done()
            }
            pending_sync = {
                future for future in self._sync_futures if not future.done()
            }
            if not pending_async and not pending_sync:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.01, remaining))

        pending_async_count = sum(
            task is not current_task and not task.done()
            for task in self._active_tasks | self._operation_tasks
        )
        pending_sync_count = sum(not future.done() for future in self._sync_futures)
        drained = pending_async_count == 0 and pending_sync_count == 0
        waited = time.monotonic() - started
        if drained and tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
        if not drained:
            logger.warning(
                "Plugin gate close left quarantined work: plugin=%s async=%d sync=%d waited=%.2fs",
                self.plugin_name,
                pending_async_count,
                pending_sync_count,
                waited,
            )
        return PluginExecutionDrainResult(
            drained=drained,
            pending_async_tasks=pending_async_count,
            pending_sync_callbacks=pending_sync_count,
            waited_seconds=waited,
        )


async def invoke_loaded_plugin(
    plugin: Any,
    operation: Callable[[], Awaitable[T]],
    *,
    allow_closed: bool = False,
) -> T:
    """Run an operation through a loaded plugin's gate when one exists.

    The fallback retains compatibility with test doubles and external registry
    implementations that predate manifest concurrency support.
    """

    gate = getattr(plugin, "execution_gate", None)
    if isinstance(gate, PluginExecutionGate):
        return await gate.run(operation, allow_closed=allow_closed)
    return await operation()


async def call_plugin_callback(callback: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Call an async callback or offload a synchronous callback to a bounded pool."""

    if inspect.iscoroutinefunction(callback):
        result = callback(*args, **kwargs)
    else:
        loop = asyncio.get_running_loop()
        future = _SYNC_PLUGIN_EXECUTOR.submit(
            functools.partial(callback, *args, **kwargs)
        )
        gate = _CURRENT_EXECUTION_GATE.get()
        if gate is not None:
            gate._register_sync_future(future)
        wrapped = asyncio.wrap_future(future, loop=loop)
        try:
            result = await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            # Queued work can still be cancelled; a callback already running
            # in a Python thread remains registered until its real future ends.
            future.cancel()
            raise
    if inspect.isawaitable(result):
        return await result
    return result
