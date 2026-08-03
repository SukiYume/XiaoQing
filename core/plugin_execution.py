"""插件回调的有界执行与代际生命周期隔离。

异步入口先通过当前插件 generation 的 gate；同步回调再进入 manager 持有的公平 broker，
防止单个插件独占共享线程池或制造无界 executor 队列。broker job 只能沿
``queued -> running -> terminal`` 或 ``queued -> cancelled`` 迁移。

Python 无法安全杀死正在运行的线程，因此超时/调用方取消只能断开结果接收，真实 Future
仍由原 gate 持有到终止。普通关闭后的 gate 永不重开；热重载通过发布新 generation
恢复服务，只有注册表发布事务使用带身份 token 的短暂 hold/release。
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import logging
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import Future as ConcurrentFuture
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, TypeVar, cast

from .lifecycle import LazyAsyncLock as _LazyAsyncLock

PluginConcurrency = Literal["parallel", "sequential"]
T = TypeVar("T")
logger = logging.getLogger(__name__)


def callback_accepts_positional_context(callback: Callable[..., Any]) -> bool:
    """Return whether a lifecycle callback declares a positional context slot."""

    parameters = inspect.signature(callback).parameters.values()
    return any(
        parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        for parameter in parameters
    )


class PluginCallbackFatalError(Exception):
    """Ordinary Task-safe carrier for a plugin callback's BaseException."""

    def __init__(self, original: BaseException):
        super().__init__(f"plugin callback raised {type(original).__name__}")
        self.original = original


class PluginExecutionClosed(RuntimeError):
    """Raised when a plugin generation cannot accept more work."""


class PluginExecutionUnavailable(RuntimeError):
    """Raised while a plugin circuit is open after repeated failures."""


class PluginExecutionOverloaded(PluginExecutionUnavailable):
    """Raised when a bounded plugin or broker queue has no free admission."""


class PluginExecutionTimeout(TimeoutError):
    """Raised when a plugin callback exceeds its end-to-end time limit."""


@dataclass(frozen=True)
class PluginExecutionPolicy:
    """Runtime limits shared by all callback types of one plugin generation."""

    timeout_seconds: float | None = 60.0
    parallel_limit: int = 4
    admission_queue_limit: int = 64
    sync_parallel_limit: int = 1
    sync_queue_limit: int = 16
    failure_threshold: int = 3
    cooldown_seconds: float = 60.0
    drain_timeout_seconds: float = 5.0

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, Any] | None,
        *,
        fallback: PluginExecutionPolicy | None = None,
    ) -> PluginExecutionPolicy:
        """Build a defensive policy for non-schema embedders and test doubles."""

        base = fallback or cls()
        values = values if isinstance(values, Mapping) else {}

        raw_timeout = values.get("timeout_seconds", base.timeout_seconds)
        try:
            timeout = (
                None
                if raw_timeout in (None, 0, 0.0) and not isinstance(raw_timeout, bool)
                else min(86400.0, max(0.1, float(raw_timeout)))
            )
        except (TypeError, ValueError):
            timeout = base.timeout_seconds

        def bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
            raw = values.get(name, default)
            if isinstance(raw, bool):
                return default
            try:
                return min(maximum, max(minimum, int(raw)))
            except (TypeError, ValueError, OverflowError):
                return default

        def bounded_float(name: str, default: float, *, maximum: float) -> float:
            raw = values.get(name, default)
            if isinstance(raw, bool):
                return default
            try:
                return min(maximum, max(0.1, float(raw)))
            except (TypeError, ValueError, OverflowError):
                return default

        return cls(
            timeout_seconds=timeout,
            parallel_limit=bounded_int(
                "parallel_limit", base.parallel_limit, minimum=1, maximum=1024
            ),
            admission_queue_limit=bounded_int(
                "admission_queue_limit",
                base.admission_queue_limit,
                minimum=0,
                maximum=10000,
            ),
            sync_parallel_limit=bounded_int(
                "sync_parallel_limit",
                base.sync_parallel_limit,
                minimum=1,
                maximum=3,
            ),
            sync_queue_limit=bounded_int(
                "sync_queue_limit",
                base.sync_queue_limit,
                minimum=0,
                maximum=10000,
            ),
            failure_threshold=bounded_int(
                "failure_threshold", base.failure_threshold, minimum=1, maximum=10000
            ),
            cooldown_seconds=bounded_float(
                "cooldown_seconds", base.cooldown_seconds, maximum=86400.0
            ),
            drain_timeout_seconds=bounded_float(
                "drain_timeout_seconds",
                base.drain_timeout_seconds,
                maximum=3600.0,
            ),
        )


@dataclass(frozen=True)
class PluginExecutionDrainResult:
    """Bounded gate close result, including work Python cannot force-stop."""

    drained: bool
    pending_async_tasks: int
    pending_sync_callbacks: int
    waited_seconds: float


@dataclass(frozen=True)
class PluginSyncBrokerDrainResult:
    """Result of a bounded shared synchronous-executor shutdown attempt."""

    drained: bool
    pending_callbacks: int
    queued_callbacks: int
    waited_seconds: float


@dataclass
class _PluginExecutionScope:
    gate: PluginExecutionGate
    allow_closed: bool
    active: bool = True


_CURRENT_EXECUTION_SCOPE: ContextVar[_PluginExecutionScope | None] = ContextVar(
    "xiaoqing_current_plugin_execution_scope",
    default=None,
)


@dataclass(eq=False)
class _SyncCallbackJob:
    lane: object
    gate: PluginExecutionGate | None
    loop: asyncio.AbstractEventLoop
    callback: Callable[[], Any] | None
    completion: asyncio.Future[Any]
    allow_closed: bool = False
    state: Literal["queued", "running", "terminal", "cancelled"] = "queued"
    actual_future: ConcurrentFuture[Any] | None = None
    detached: bool = False
    delivered: bool = False
    outcome_kind: Literal["result", "error", "cancelled"] | None = None
    outcome: Any = None
    fatal_deferred: bool = False
    delivery_pending: bool = False


class PluginSyncBroker:
    """多个插件 gate 共用的公平、有界同步回调舱壁。

    broker 本身线程安全，进程级 fallback 因而也能服务多个事件循环。只有取得真实 worker
    槽的回调才提交给 ``ThreadPoolExecutor``；其余任务留在 broker 可撤销的有界 FIFO
    lane 中，executor 自身绝不形成第二条不可控队列。
    """

    def __init__(
        self,
        *,
        max_workers: int = 4,
        global_queue_limit: int = 256,
        thread_name_prefix: str = "xiaoqing-plugin",
    ) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        if (
            isinstance(global_queue_limit, bool)
            or not isinstance(global_queue_limit, int)
            or global_queue_limit < 1
        ):
            raise ValueError("global_queue_limit must be a positive integer")
        self.max_workers = int(max_workers)
        self._global_queue_limit = int(global_queue_limit)
        self._thread_name_prefix = thread_name_prefix
        self._lock = threading.RLock()
        self._executor: ThreadPoolExecutor | None = None
        # 以下队列、计数与 ready 集合只能在 _lock 内修改；任一 job 在全局和 lane
        # 计数中各占一个位置，迁移状态时必须在同一临界区同时释放。
        self._lane_queues: dict[object, deque[_SyncCallbackJob]] = {}
        self._ready_lanes: deque[object] = deque()
        self._ready_lane_set: set[object] = set()
        self._running_by_lane: dict[object, int] = {}
        self._queued_count = 0
        self._running_count = 0
        self._pending_delivery_count = 0
        self._closed = False
        self._executor_shutdown = False
        self._fallback_lane = object()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def drained(self) -> bool:
        with self._lock:
            return (
                self._queued_count == 0
                and self._running_count == 0
                and self._pending_delivery_count == 0
            )

    @property
    def queued_callbacks(self) -> int:
        with self._lock:
            return self._queued_count

    @property
    def running_callbacks(self) -> int:
        with self._lock:
            return self._running_count

    @property
    def worker_thread_count(self) -> int:
        with self._lock:
            executor = self._executor
            # 仅用于诊断和关闭测试。ThreadPoolExecutor 没有公开的线程数 API；
            # 私有字段不可用时返回 0，执行正确性不依赖此数值。
            threads = getattr(executor, "_threads", ()) if executor is not None else ()
            return len(threads)

    def configure(self, *, global_queue_limit: int) -> None:
        """Apply a queue limit to future admissions without dropping old jobs."""

        if isinstance(global_queue_limit, bool) or not isinstance(global_queue_limit, int):
            raise ValueError("global_queue_limit must be an integer")
        if not 1 <= global_queue_limit <= 100000:
            raise ValueError("global_queue_limit must be between 1 and 100000")
        with self._lock:
            self._global_queue_limit = global_queue_limit
            self._dispatch_locked()

    def notify_policy_change(self, gate: PluginExecutionGate) -> None:
        """Reconsider a lane after a hot limit increase or decrease."""

        with self._lock:
            if gate in self._lane_queues:
                self._queue_lane_locked(gate)
            self._dispatch_locked()

    def close_gate_admission(self, gate: PluginExecutionGate) -> None:
        """Physically cancel this generation's queued non-lifecycle jobs."""

        with self._lock:
            queue = self._lane_queues.get(gate)
            if not queue:
                return
            retained: deque[_SyncCallbackJob] = deque()
            for job in queue:
                if job.allow_closed:
                    retained.append(job)
                    continue
                job.state = "cancelled"
                job.detached = True
                job.callback = None
                self._queued_count -= 1
                try:
                    job.loop.call_soon_threadsafe(job.completion.cancel)
                except RuntimeError:
                    pass
            self._drop_stale_ready_lane_locked(gate)
            if retained:
                self._lane_queues[gate] = retained
                self._queue_lane_locked(gate)
            elif self._running_by_lane.get(gate, 0) == 0:
                self._lane_queues.pop(gate, None)
            else:
                self._lane_queues[gate] = retained
            self._dispatch_locked()

    def _lane_running_limit_locked(self, lane: object) -> int:
        if isinstance(lane, PluginExecutionGate):
            configured = lane.policy.sync_parallel_limit
            # With at least two workers, retain one worker for another lane.
            cross_plugin_cap = self.max_workers - 1 if self.max_workers > 1 else 1
            return max(1, min(configured, cross_plugin_cap))
        return self.max_workers

    def _lane_queue_limit_locked(self, lane: object) -> int:
        if isinstance(lane, PluginExecutionGate):
            return lane.policy.sync_queue_limit
        return self._global_queue_limit

    def _queue_lane_locked(self, lane: object) -> None:
        queue = self._lane_queues.get(lane)
        if not queue or lane in self._ready_lane_set:
            return
        running = self._running_by_lane.get(lane, 0)
        if running >= self._lane_running_limit_locked(lane):
            return
        self._ready_lanes.append(lane)
        self._ready_lane_set.add(lane)

    def _drop_stale_ready_lane_locked(self, lane: object) -> None:
        if lane not in self._ready_lane_set:
            return
        self._ready_lane_set.discard(lane)
        self._ready_lanes = deque(item for item in self._ready_lanes if item is not lane)

    def _ensure_executor_locked(self) -> ThreadPoolExecutor:
        if self._executor is None:
            if self._executor_shutdown:
                raise PluginExecutionClosed("plugin sync broker is shut down")
            self._executor = ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix=self._thread_name_prefix,
            )
        return self._executor

    def _release_running_slot_locked(self, lane: object) -> None:
        """原子释放全局与 lane 的同一个 worker 槽，并清除零值 lane。"""

        assert self._running_count > 0
        self._running_count -= 1
        lane_running = self._running_by_lane.get(lane, 0)
        assert lane_running > 0
        if lane_running == 1:
            self._running_by_lane.pop(lane, None)
        else:
            self._running_by_lane[lane] = lane_running - 1

    def _take_drained_executor_locked(self) -> ThreadPoolExecutor | None:
        """Detach the executor exactly once after a closed broker converges."""

        if (
            not self._closed
            or self._queued_count
            or self._running_count
            or self._pending_delivery_count
            or self._executor_shutdown
            or self._executor is None
        ):
            return None
        executor = self._executor
        self._executor = None
        self._executor_shutdown = True
        return executor

    def submit(
        self,
        callback: Callable[[], Any],
        *,
        gate: PluginExecutionGate | None,
        allow_closed: bool = False,
    ) -> _SyncCallbackJob:
        """Queue one callback or fail immediately when a hard bound is full."""

        loop = asyncio.get_running_loop()
        lane: object = gate if gate is not None else self._fallback_lane
        completion: asyncio.Future[Any] = loop.create_future()
        job = _SyncCallbackJob(
            lane,
            gate,
            loop,
            callback,
            completion,
            allow_closed=allow_closed,
        )
        with self._lock:
            if self._closed:
                raise PluginExecutionClosed("plugin sync broker is closed")
            lane_queue = self._lane_queues.setdefault(lane, deque())
            running = self._running_by_lane.get(lane, 0)
            immediate = (
                self._running_count < self.max_workers
                and running < self._lane_running_limit_locked(lane)
                and not lane_queue
                and not self._ready_lanes
            )
            if not immediate and len(lane_queue) >= self._lane_queue_limit_locked(lane):
                if not lane_queue and running == 0:
                    self._lane_queues.pop(lane, None)
                raise PluginExecutionOverloaded(
                    f"plugin sync queue is full: plugin={getattr(gate, 'plugin_name', '<ungated>')}"
                )
            if not immediate and self._queued_count >= self._global_queue_limit:
                if not lane_queue and running == 0:
                    self._lane_queues.pop(lane, None)
                raise PluginExecutionOverloaded("global plugin sync queue is full")
            lane_queue.append(job)
            self._queued_count += 1
            self._queue_lane_locked(lane)
            self._dispatch_locked()
        return job

    def _dispatch_locked(self) -> None:
        while self._running_count < self.max_workers and self._ready_lanes:
            lane = self._ready_lanes.popleft()
            self._ready_lane_set.discard(lane)
            queue = self._lane_queues.get(lane)
            if not queue:
                self._lane_queues.pop(lane, None)
                continue
            if self._running_by_lane.get(lane, 0) >= self._lane_running_limit_locked(lane):
                continue

            job = queue.popleft()
            self._queued_count -= 1
            if job.gate is not None and job.gate.closed and not job.allow_closed:
                job.state = "cancelled"
                job.detached = True
                job.callback = None
                try:
                    job.loop.call_soon_threadsafe(job.completion.cancel)
                except RuntimeError:
                    pass
                if queue:
                    self._queue_lane_locked(lane)
                elif self._running_by_lane.get(lane, 0) == 0:
                    self._lane_queues.pop(lane, None)
                continue
            job.state = "running"
            self._running_count += 1
            self._running_by_lane[lane] = self._running_by_lane.get(lane, 0) + 1
            callback = job.callback
            assert callback is not None
            start_barrier = threading.Event()

            def run_tracked_callback(
                callback: Callable[[], Any] = callback,
                start_barrier: threading.Event = start_barrier,
            ) -> Any:
                # submit 返回前 worker 就可能启动；必须先把真实 Future 登记到 gate，
                # 再允许进入插件代码，否则并发卸载会在这个发布窗口误报已排空。
                start_barrier.wait()
                return callback()

            try:
                future = self._ensure_executor_locked().submit(run_tracked_callback)
            except BaseException as exc:
                self._release_running_slot_locked(lane)
                job.callback = None
                job.state = "terminal"
                job.outcome_kind = "error"
                job.outcome = exc
                self._schedule_delivery_locked(job)
            else:
                job.actual_future = future
                job.callback = None
                if job.gate is not None:
                    job.gate._register_sync_future(future)
                future.add_done_callback(functools.partial(self._complete_from_worker, job))
                start_barrier.set()

            if queue:
                self._queue_lane_locked(lane)
            elif self._running_by_lane.get(lane, 0) == 0:
                self._lane_queues.pop(lane, None)
            # A synchronous submit failure released a slot immediately.
            if job.actual_future is None:
                self._queue_lane_locked(lane)

    def _complete_from_worker(
        self,
        job: _SyncCallbackJob,
        future: ConcurrentFuture[Any],
    ) -> None:
        executor_to_shutdown: ThreadPoolExecutor | None = None
        with self._lock:
            if job.state != "running":
                return
            lane = job.lane
            job.state = "terminal"
            self._release_running_slot_locked(lane)
            try:
                job.outcome = future.result()
            except BaseException as exc:
                job.outcome_kind = "cancelled" if future.cancelled() else "error"
                job.outcome = exc
            else:
                job.outcome_kind = "result"
            self._schedule_delivery_locked(job)
            if self._lane_queues.get(lane):
                self._queue_lane_locked(lane)
            else:
                self._lane_queues.pop(lane, None)
            self._dispatch_locked()
            executor_to_shutdown = self._take_drained_executor_locked()
        if executor_to_shutdown is not None:
            # This callback may be running on the executor itself, so waiting
            # here would attempt to join the current worker.  With no queued or
            # running callbacks left, the non-waiting shutdown is terminal and
            # its idle worker exits immediately after this callback returns.
            executor_to_shutdown.shutdown(wait=False, cancel_futures=True)

    def _schedule_delivery_locked(self, job: _SyncCallbackJob) -> None:
        if job.detached:
            # There is no consumer left on the owner loop.  Finalize directly
            # in the worker so a stopped-but-not-yet-closed loop cannot accept
            # and then silently discard the queued delivery callback.
            job.delivered = True
            fatal = self._defer_fatal_locked(job)
            if fatal is not None and job.gate is not None:
                job.gate._defer_fatal_error(fatal)
            if job.gate is not None and job.actual_future is not None:
                job.gate._discard_sync_future(job.actual_future)
            return
        self._pending_delivery_count += 1
        job.delivery_pending = True
        try:
            job.loop.call_soon_threadsafe(self._deliver, job)
        except RuntimeError:
            # A dead event loop has no attached consumer.  Complete ownership
            # transfer synchronously so a gate cannot stay poisoned forever or
            # lose a fatal outcome during loop teardown.
            job.detached = True
            fatal = self._defer_fatal_locked(job)
            job.delivered = True
            job.delivery_pending = False
            self._pending_delivery_count -= 1
            if fatal is not None and job.gate is not None:
                job.gate._defer_fatal_error(fatal)
            if job.gate is not None and job.actual_future is not None:
                job.gate._discard_sync_future(job.actual_future)

    def _defer_fatal_locked(self, job: _SyncCallbackJob) -> BaseException | None:
        error = job.outcome if job.outcome_kind == "error" else None
        if (
            job.gate is not None
            and isinstance(error, BaseException)
            and not isinstance(error, Exception)
            and not job.fatal_deferred
        ):
            job.fatal_deferred = True
            return error
        return None

    def _deliver(self, job: _SyncCallbackJob) -> None:
        fatal: BaseException | None = None
        future_to_discard: ConcurrentFuture[Any] | None = None
        executor_to_shutdown: ThreadPoolExecutor | None = None
        with self._lock:
            if job.delivered:
                return
            job.delivered = True
            if job.delivery_pending:
                job.delivery_pending = False
                self._pending_delivery_count = max(0, self._pending_delivery_count - 1)
            if job.detached:
                fatal = self._defer_fatal_locked(job)
            elif not job.completion.done():
                if job.outcome_kind == "result":
                    job.completion.set_result(job.outcome)
                elif job.outcome_kind == "cancelled":
                    job.completion.cancel()
                else:
                    job.completion.set_exception(job.outcome)
            future_to_discard = job.actual_future
            executor_to_shutdown = self._take_drained_executor_locked()
        if fatal is not None and job.gate is not None:
            job.gate._defer_fatal_error(fatal)
        if future_to_discard is not None and job.gate is not None:
            job.gate._discard_sync_future(future_to_discard)
        if executor_to_shutdown is not None:
            executor_to_shutdown.shutdown(wait=False, cancel_futures=True)

    def cancel_or_detach(self, job: _SyncCallbackJob) -> None:
        """Physically remove queued work, or detach an already-running thread."""

        fatal: BaseException | None = None
        detached_future: ConcurrentFuture[Any] | None = None
        future_to_discard: ConcurrentFuture[Any] | None = None
        executor_to_shutdown: ThreadPoolExecutor | None = None
        with self._lock:
            if job.state == "queued":
                queue = self._lane_queues.get(job.lane)
                if queue is not None:
                    try:
                        queue.remove(job)
                    except ValueError:
                        pass
                    else:
                        self._queued_count -= 1
                job.state = "cancelled"
                job.detached = True
                job.callback = None
                self._drop_stale_ready_lane_locked(job.lane)
                if queue:
                    self._queue_lane_locked(job.lane)
                elif self._running_by_lane.get(job.lane, 0) == 0:
                    self._lane_queues.pop(job.lane, None)
                if not job.completion.done():
                    job.completion.cancel()
                self._dispatch_locked()
                return

            job.detached = True
            future = job.actual_future
            if future is not None and not future.done():
                detached_future = future
                future.cancel()
            if job.state == "terminal":
                fatal = self._defer_fatal_locked(job)
                if not job.delivered and job.delivery_pending:
                    # The worker finished first, but its owner-loop delivery is
                    # still queued.  Cancellation now owns terminal cleanup;
                    # the already-queued callback becomes an idempotent no-op.
                    job.delivered = True
                    job.delivery_pending = False
                    self._pending_delivery_count = max(
                        0,
                        self._pending_delivery_count - 1,
                    )
                    future_to_discard = job.actual_future
                if job.completion.done() and not job.completion.cancelled():
                    try:
                        job.completion.exception()
                    except (asyncio.CancelledError, Exception):
                        pass
                elif not job.completion.done():
                    job.completion.cancel()
                executor_to_shutdown = self._take_drained_executor_locked()
        if fatal is not None and job.gate is not None:
            job.gate._defer_fatal_error(fatal)
        if detached_future is not None and job.gate is not None:
            job.gate._mark_detached_sync_future(detached_future)
        if future_to_discard is not None and job.gate is not None:
            job.gate._discard_sync_future(future_to_discard)
        if executor_to_shutdown is not None:
            executor_to_shutdown.shutdown(wait=False, cancel_futures=True)

    def _cancel_all_queued_locked(self) -> None:
        for queue in self._lane_queues.values():
            for job in queue:
                if job.state != "queued":
                    continue
                job.state = "cancelled"
                job.detached = True
                job.callback = None
                try:
                    job.loop.call_soon_threadsafe(job.completion.cancel)
                except RuntimeError:
                    pass
        self._lane_queues.clear()
        self._ready_lanes.clear()
        self._ready_lane_set.clear()
        self._queued_count = 0

    async def close(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> PluginSyncBrokerDrainResult:
        """Reject new jobs and wait a bounded time for real worker callbacks."""

        started = time.monotonic()
        timeout = 5.0 if timeout_seconds is None else max(0.0, float(timeout_seconds))
        deadline = started + timeout
        with self._lock:
            self._closed = True
            self._cancel_all_queued_locked()

        while True:
            with self._lock:
                pending = self._running_count + self._pending_delivery_count
            if pending == 0 or time.monotonic() >= deadline:
                break
            await asyncio.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

        with self._lock:
            pending = self._running_count + self._pending_delivery_count
            queued = self._queued_count
            drained = pending == 0 and queued == 0
            executor = self._take_drained_executor_locked() if drained else None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        waited = time.monotonic() - started
        if not drained:
            logger.warning(
                "Plugin sync broker close left running callbacks: running=%d waited=%.2fs",
                pending,
                waited,
            )
        return PluginSyncBrokerDrainResult(
            drained=drained,
            pending_callbacks=pending,
            queued_callbacks=queued,
            waited_seconds=waited,
        )


# 插件内部 ``run_sync`` 也可能由独立嵌入者在 manager gate 外调用；这些真实调用仍必须
# 共享一个有界 broker。它归进程而非单个 App 所有，关闭一个 App 不能破坏另一个 App。
_FALLBACK_SYNC_BROKER = PluginSyncBroker(global_queue_limit=256)


@dataclass(eq=False)
class _GateWaiter:
    ready: asyncio.Future[None]
    allow_closed: bool
    granted: bool = False


class PluginExecutionGate:
    """限制并追踪一个确定插件 generation 的全部入口。"""

    def __init__(
        self,
        mode: PluginConcurrency,
        *,
        plugin_name: str = "<unknown>",
        policy: PluginExecutionPolicy | None = None,
        sync_broker: PluginSyncBroker | None = None,
    ) -> None:
        if mode not in {"parallel", "sequential"}:
            raise ValueError(f"unsupported plugin concurrency mode: {mode}")
        self.mode = mode
        self.plugin_name = plugin_name
        self._policy = policy or PluginExecutionPolicy()
        self._sync_broker = sync_broker or _FALLBACK_SYNC_BROKER
        self._state_lock = _LazyAsyncLock()
        # gate_running + gate_waiters 是 admission 状态；active/operation 是外层
        # 调用与真实子任务的所有权状态。两组不能合并，否则取消抵抗型子任务会提前消失。
        self._gate_running = 0
        self._gate_waiters: deque[_GateWaiter] = deque()
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._operation_tasks: set[asyncio.Task[Any]] = set()
        self._detached_operation_tasks: set[asyncio.Task[Any]] = set()
        self._policy_tasks: set[asyncio.Task[Any]] = set()
        self._sync_futures_lock = threading.RLock()
        self._sync_futures: set[ConcurrentFuture[Any]] = set()
        # broker Future 必须等终态结果送达（或同步转存）后才能解绑；单独记录来源，
        # 让 close 的重试能清掉已终止的无主 tracker，又不会抢走 fatal 的唯一交付权。
        self._broker_owned_sync_futures: set[ConcurrentFuture[Any]] = set()
        self._detached_sync_futures: set[ConcurrentFuture[Any]] = set()
        self._deferred_fatal_lock = threading.RLock()
        self._deferred_fatal_errors: list[BaseException] = []
        self._admission_lock = threading.RLock()
        self._closed = False
        self._publication_token: object | None = None
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._poisoned_by_timeout = False
        self._poisoned_by_sync_callback = False

    @property
    def closed(self) -> bool:
        with self._admission_lock:
            return self._closed

    @property
    def policy(self) -> PluginExecutionPolicy:
        return self._policy

    @property
    def sync_broker(self) -> PluginSyncBroker:
        return self._sync_broker

    @property
    def pending_sync_callbacks(self) -> int:
        with self._sync_futures_lock:
            return len(self._sync_futures)

    @property
    def admitted_operations(self) -> int:
        return self._gate_running + len(self._gate_waiters)

    @property
    def drained(self) -> bool:
        with self._sync_futures_lock:
            pending_sync = bool(self._sync_futures)
        return (
            not any(not task.done() for task in self._active_tasks | self._operation_tasks)
            and not pending_sync
        )

    def close_admission(self) -> None:
        """Synchronously reject normal work before asynchronous drain starts."""

        with self._admission_lock:
            self._closed = True
            self._publication_token = None
        self._sync_broker.close_gate_admission(self)

    def hold_for_publication(self) -> object:
        """Temporarily reject work until registry and import state commit together."""

        with self._admission_lock:
            if self._closed:
                raise PluginExecutionClosed("plugin generation is already closed")
            token = object()
            self._publication_token = token
            self._closed = True
        self._sync_broker.close_gate_admission(self)
        return token

    def release_publication_hold(self, token: object) -> None:
        """Open a gate only when it was closed by ``hold_for_publication``."""

        with self._admission_lock:
            if self._publication_token is not token:
                raise PluginExecutionClosed("plugin publication hold was revoked")
            self._publication_token = None
            self._closed = False
        self._sync_broker.notify_policy_change(self)

    def set_policy(self, policy: PluginExecutionPolicy) -> None:
        """Apply hot limits without cancelling work already granted a slot."""

        self._policy = policy
        self._sync_broker.notify_policy_change(self)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._reconcile_policy())
        self._policy_tasks.add(task)
        task.add_done_callback(self._policy_tasks.discard)

    async def _reconcile_policy(self) -> None:
        async with self._state_lock.get():
            self._grant_waiters_locked()

    def _execution_capacity(self) -> int:
        return 1 if self.mode == "sequential" else self._policy.parallel_limit

    def _ensure_available_locked(self, *, allow_closed: bool) -> None:
        if self.closed and not allow_closed:
            raise PluginExecutionClosed("plugin is unloading")
        if allow_closed:
            return
        if (
            self._poisoned_by_timeout
            or self._poisoned_by_sync_callback
            or time.monotonic() < self._circuit_open_until
        ):
            raise PluginExecutionUnavailable("plugin circuit is temporarily open")

    def _grant_waiters_locked(self) -> None:
        capacity = self._execution_capacity()
        while self._gate_waiters and self._gate_running < capacity:
            waiter = self._gate_waiters[0]
            if waiter.ready.done():
                self._gate_waiters.popleft()
                continue
            if self.closed and not waiter.allow_closed:
                self._gate_waiters.popleft()
                waiter.ready.set_exception(PluginExecutionClosed("plugin is unloading"))
                continue
            self._gate_waiters.popleft()
            waiter.granted = True
            self._gate_running += 1
            waiter.ready.set_result(None)

    async def _record_success(self) -> None:
        async with self._state_lock.get():
            self._consecutive_failures = 0
            if not self._poisoned_by_timeout and not self._poisoned_by_sync_callback:
                self._circuit_open_until = 0.0

    async def _record_failure(self, *, force_open: bool = False) -> None:
        async with self._state_lock.get():
            self._consecutive_failures += 1
            if force_open or self._consecutive_failures >= self._policy.failure_threshold:
                self._circuit_open_until = time.monotonic() + self._policy.cooldown_seconds

    def _defer_fatal_error(self, error: BaseException) -> None:
        with self._deferred_fatal_lock:
            if all(existing is not error for existing in self._deferred_fatal_errors):
                self._deferred_fatal_errors.append(error)

    def _discard_operation_task(self, task: asyncio.Task[Any]) -> None:
        if task not in self._operation_tasks:
            return
        self._operation_tasks.discard(task)
        detached = task in self._detached_operation_tasks
        self._detached_operation_tasks.discard(task)
        try:
            error = task.exception()
        except (asyncio.CancelledError, Exception):
            error = None
        if detached:
            if isinstance(error, PluginCallbackFatalError):
                self._defer_fatal_error(error.original)
            elif isinstance(error, BaseException) and not isinstance(error, Exception):
                self._defer_fatal_error(error)
        if self._poisoned_by_timeout and not any(
            operation in self._detached_operation_tasks and not operation.done()
            for operation in self._operation_tasks
        ):
            self._poisoned_by_timeout = False

    def _discard_sync_future(self, future: ConcurrentFuture[Any]) -> None:
        with self._sync_futures_lock:
            if future not in self._sync_futures:
                return
            self._sync_futures.discard(future)
            self._broker_owned_sync_futures.discard(future)
            self._detached_sync_futures.discard(future)
        try:
            future.exception()
        except BaseException:
            # The broker owns delivery/deferred-fatal semantics.  This read is
            # only to consume the concurrent future's terminal outcome.
            pass
        with self._sync_futures_lock:
            pending_detached_sync = any(not item.done() for item in self._detached_sync_futures)
        if self._poisoned_by_sync_callback and not pending_detached_sync:
            self._poisoned_by_sync_callback = False

    def _mark_detached_sync_future(self, future: ConcurrentFuture[Any]) -> None:
        """Quarantine only the real sync callback detached by its own caller."""

        with self._sync_futures_lock:
            if future in self._sync_futures:
                self._detached_sync_futures.add(future)
                self._poisoned_by_sync_callback = True

    def _register_sync_future(
        self,
        future: ConcurrentFuture[Any],
    ) -> None:
        """Track a real future until its broker outcome has been delivered."""

        with self._sync_futures_lock:
            self._sync_futures.add(future)
            self._broker_owned_sync_futures.add(future)

    def _discard_terminal_unowned_sync_futures(self) -> None:
        """Drop terminal tracker entries that have no broker delivery owner.

        Production worker futures are registered by ``PluginSyncBroker`` and
        remain tracked until its result delivery is complete.  A lifecycle
        retry must nevertheless be able to recover from a terminal orphan
        entry (for example, state reconstructed after an interrupted import)
        instead of reporting the same already-finished callback forever.
        """

        with self._sync_futures_lock:
            terminal = tuple(
                future
                for future in self._sync_futures
                if future.done() and future not in self._broker_owned_sync_futures
            )
        for future in terminal:
            self._discard_sync_future(future)

    def consume_deferred_fatal_error(self) -> BaseException | None:
        """Return one late fatal callback outcome after its work is terminal."""

        with self._deferred_fatal_lock:
            if not self._deferred_fatal_errors:
                return None
            return self._deferred_fatal_errors.pop(0)

    def _mark_unfinished_child(self, task: asyncio.Task[Any]) -> None:
        if not task.done():
            self._poisoned_by_timeout = True

    async def _run_bounded(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        timeout_seconds: float | None,
        allow_closed: bool,
    ) -> T:
        async def run_in_gate_scope() -> T:
            scope = _PluginExecutionScope(self, allow_closed)
            token = _CURRENT_EXECUTION_SCOPE.set(scope)
            try:
                try:
                    return await operation()
                except (Exception, asyncio.CancelledError):
                    raise
                except BaseException as exc:
                    raise PluginCallbackFatalError(exc) from None
            finally:
                scope.active = False
                _CURRENT_EXECUTION_SCOPE.reset(token)

        task = asyncio.create_task(run_in_gate_scope())
        self._operation_tasks.add(task)
        task.add_done_callback(self._discard_operation_task)
        try:
            if timeout_seconds is None:
                result = await task
            else:
                if timeout_seconds <= 0:
                    raise asyncio.TimeoutError
                result = await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=timeout_seconds,
                )
        except asyncio.TimeoutError as exc:
            await self._record_failure(force_open=True)
            self._detached_operation_tasks.add(task)
            task.cancel()
            # Give an ordinary cancellable child one loop turn to physically
            # remove any broker-queued job before reporting the timeout.  A
            # cancellation-resistant child remains tracked and quarantined.
            await asyncio.sleep(0)
            self._mark_unfinished_child(task)
            logger.warning(
                "Plugin callback timed out: plugin=%s timeout=%.1fs",
                self.plugin_name,
                self._policy.timeout_seconds or 0.0,
            )
            raise PluginExecutionTimeout("plugin callback timed out") from exc
        except asyncio.CancelledError:
            self._detached_operation_tasks.add(task)
            task.cancel()
            await asyncio.sleep(0)
            self._mark_unfinished_child(task)
            raise
        except (PluginExecutionClosed, PluginExecutionUnavailable):
            raise
        except Exception:
            await self._record_failure()
            raise
        else:
            await self._record_success()
            return result

    async def _remove_or_release_waiter(self, waiter: _GateWaiter) -> None:
        async with self._state_lock.get():
            if waiter.granted:
                waiter.granted = False
                self._gate_running = max(0, self._gate_running - 1)
            else:
                try:
                    self._gate_waiters.remove(waiter)
                except ValueError:
                    pass
            self._grant_waiters_locked()

    async def run(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        allow_closed: bool = False,
    ) -> T:
        """Execute one operation with bounded FIFO admission and total timeout."""

        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("plugin execution requires an asyncio Task")
        started = time.monotonic()
        timeout = self._policy.timeout_seconds
        deadline = None if timeout is None else started + timeout
        state_lock = self._state_lock.get()
        waiter: _GateWaiter | None = None
        granted = False

        async with state_lock:
            self._ensure_available_locked(allow_closed=allow_closed)
            capacity = self._execution_capacity()
            admitted = self._gate_running + len(self._gate_waiters)
            admission_limit = capacity + self._policy.admission_queue_limit
            if admitted >= admission_limit:
                raise PluginExecutionOverloaded(
                    f"plugin admission queue is full: plugin={self.plugin_name}"
                )
            self._active_tasks.add(task)
            if self._gate_running < capacity and not self._gate_waiters:
                self._gate_running += 1
                granted = True
            else:
                waiter = _GateWaiter(asyncio.get_running_loop().create_future(), allow_closed)
                self._gate_waiters.append(waiter)

        try:
            if waiter is not None:
                try:
                    if deadline is None:
                        await asyncio.shield(waiter.ready)
                    else:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise asyncio.TimeoutError
                        await asyncio.wait_for(asyncio.shield(waiter.ready), timeout=remaining)
                    granted = waiter.granted
                except asyncio.TimeoutError as exc:
                    await self._remove_or_release_waiter(waiter)
                    await self._record_failure(force_open=True)
                    raise PluginExecutionTimeout("plugin callback timed out while queued") from exc
                except BaseException:
                    await self._remove_or_release_waiter(waiter)
                    raise

            async with state_lock:
                self._ensure_available_locked(allow_closed=allow_closed)
            remaining_timeout = None if deadline is None else deadline - time.monotonic()
            return await self._run_bounded(
                operation,
                timeout_seconds=remaining_timeout,
                allow_closed=allow_closed,
            )
        finally:
            async with state_lock:
                if granted:
                    self._gate_running = max(0, self._gate_running - 1)
                self._active_tasks.discard(task)
                self._grant_waiters_locked()

    async def close(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> PluginExecutionDrainResult:
        """Close normal admission and wait a bounded time for real executor work."""

        started = time.monotonic()
        self.close_admission()
        timeout = (
            self._policy.drain_timeout_seconds
            if timeout_seconds is None
            else max(0.0, float(timeout_seconds))
        )
        deadline = started + timeout
        current_task = asyncio.current_task()
        async with self._state_lock.get():
            tasks_to_cancel = {
                task
                for task in self._active_tasks | self._operation_tasks
                if task is not current_task and not task.done()
            }
            for waiter in tuple(self._gate_waiters):
                if not waiter.allow_closed and not waiter.ready.done():
                    waiter.ready.set_exception(PluginExecutionClosed("plugin is unloading"))

        for task in tasks_to_cancel:
            task.cancel()

        while True:
            self._discard_terminal_unowned_sync_futures()
            pending_async = {
                task
                for task in self._active_tasks | self._operation_tasks
                if task is not current_task and not task.done()
            }
            with self._sync_futures_lock:
                pending_sync = set(self._sync_futures)
            if not pending_async and not pending_sync:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.01, remaining))

        self._discard_terminal_unowned_sync_futures()
        pending_async_count = sum(
            task is not current_task and not task.done()
            for task in self._active_tasks | self._operation_tasks
        )
        with self._sync_futures_lock:
            pending_sync_count = len(self._sync_futures)
        drained = pending_async_count == 0 and pending_sync_count == 0
        waited = time.monotonic() - started
        if drained and tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
        for task in tuple(self._operation_tasks):
            if task.done():
                self._discard_operation_task(task)
        with self._sync_futures_lock:
            sync_futures = tuple(self._sync_futures)
        for future in sync_futures:
            if future.done():
                self._discard_sync_future(future)
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
    """Run an operation through a loaded plugin's gate when one exists."""

    gate = getattr(plugin, "execution_gate", None)
    if isinstance(gate, PluginExecutionGate):
        return await gate.run(operation, allow_closed=allow_closed)
    return await operation()


def _resolve_sync_broker() -> tuple[
    PluginSyncBroker,
    PluginExecutionGate | None,
    bool,
]:
    scope = _CURRENT_EXECUTION_SCOPE.get()
    if scope is None:
        return _FALLBACK_SYNC_BROKER, None, False
    gate = scope.gate
    if not scope.active:
        raise PluginExecutionClosed("plugin execution scope is no longer active")
    if gate.closed and not scope.allow_closed:
        raise PluginExecutionClosed("plugin is unloading")
    return gate.sync_broker, gate, scope.allow_closed


async def offload_plugin_sync(
    callback: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Run a known synchronous callable through the current plugin bulkhead."""

    broker, gate, allow_closed = _resolve_sync_broker()
    context = contextvars.copy_context()
    bound = functools.partial(callback, *args, **kwargs)
    job = broker.submit(
        functools.partial(context.run, bound),
        gate=gate,
        allow_closed=allow_closed,
    )
    try:
        return await asyncio.shield(job.completion)
    except asyncio.CancelledError:
        broker.cancel_or_detach(job)
        raise


async def call_plugin_callback(
    callback: Callable[..., T | Awaitable[T]],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Call an async callback or offload a sync callback through its bulkhead."""

    result: Any
    if inspect.iscoroutinefunction(callback):
        result = callback(*args, **kwargs)
    else:
        result = await offload_plugin_sync(
            cast(Callable[..., Any], callback),
            *args,
            **kwargs,
        )
    if inspect.isawaitable(result):
        return cast(T, await result)
    return cast(T, result)
