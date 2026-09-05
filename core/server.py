"""
Inbound Server 模块

提供 HTTP/WebSocket 入站服务器，接收来自外部的事件推送。
"""

import asyncio
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Protocol

from aiohttp import ContentTypeError, web
from pydantic import ValidationError

from .auth import verify_bearer_token
from .constants import (
    DEFAULT_INBOUND_WS_BROADCAST_TIMEOUT_SECONDS,
    DEFAULT_INBOUND_WS_MAX_WORKERS,
    DEFAULT_INBOUND_WS_QUEUE_SIZE,
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
)
from .delivery import resolve_action_handoff, strip_receipt
from .inbound_policy import is_loopback_host, parse_inbound_listener, validate_inbound_listener
from .lifecycle import (
    DeferredCancellation as _DeferredCancellation,
)
from .lifecycle import FatalErrorCarrier as _FatalErrorCarrier
from .lifecycle import LazyAsyncLock as _LazyAsyncLock
from .lifecycle import (
    OwnedTaskFatalError as _OwnedCleanupFatalError,
)
from .lifecycle import (
    await_owned_task as _await_cleanup_task,
)
from .lifecycle import (
    run_owned_operation as _run_owned_cleanup,
)
from .message import ValidatedInboundEvent, normalize_inbound_message
from .models import OneBotEvent
from .version import VERSION

logger = logging.getLogger(__name__)


async def _resolve_delivery_actions(
    actions: list[dict[str, Any]], *, delivered: bool
) -> list[dict[str, Any]]:
    return [
        await asyncio.shield(resolve_action_handoff(action, delivered=delivered))
        for action in actions
    ]


async def _finalize_http_action_response(
    request: Any,
    actions: list[dict[str, Any]],
    *,
    write_to_transport: bool,
    standard_onebot: bool = False,
) -> web.Response:
    """Serialize actions and resolve receipts only after the HTTP write boundary.

    Constructing an aiohttp ``Response`` does not write any bytes.  For real
    aiohttp requests this helper prepares the response and completes
    ``write_eof`` before acknowledging delivery; disconnects and serialization
    failures roll back every attached receipt.
    """

    clean_actions = [strip_receipt(action) for action in actions]
    try:
        response = web.json_response(
            {} if standard_onebot else {"actions": clean_actions},
            dumps=lambda obj: json.dumps(obj, ensure_ascii=False),
        )
        if write_to_transport:
            await response.prepare(request)
            await response.write_eof()
    except BaseException:
        await _resolve_delivery_actions(actions, delivered=False)
        raise
    await _resolve_delivery_actions(actions, delivered=True)
    return response


class InboundLifecycleFatalError(RuntimeError):
    """Task-safe public carrier for a non-Exception lifecycle failure."""

    def __init__(self, original: BaseException) -> None:
        super().__init__(f"inbound lifecycle failed: {type(original).__name__}: {original}")
        self.original = original


class _InboundLifecycleOwner(Protocol):
    """启动流程需要的最小生命周期接口。"""

    _lifecycle_lock: _LazyAsyncLock

    async def _start_locked(
        self,
        deferred_cancellation: _DeferredCancellation,
        *,
        accept_events: bool,
    ) -> None: ...


async def _start_inbound_lifecycle(owner: _InboundLifecycleOwner, *, accept_events: bool) -> None:
    """统一处理入站启动的串行化、延迟取消和致命异常包装。"""
    deferred_cancellation = _DeferredCancellation()
    try:
        async with owner._lifecycle_lock.get():
            await owner._start_locked(deferred_cancellation, accept_events=accept_events)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        deferred_cancellation.raise_if_requested(cause=exc)
        raise
    except BaseException as exc:
        deferred_cancellation.raise_if_requested(cause=exc)
        raise InboundLifecycleFatalError(exc) from None
    deferred_cancellation.raise_if_requested()


@dataclass(frozen=True, slots=True)
class _InboundAuthState:
    """One atomically replaceable inbound credential generation."""

    token: str
    generation: int = 0


@dataclass(frozen=True, slots=True)
class _InboundBroadcastCommit:
    """Linearization token for a send admitted before inbound auth rotation."""

    auth_state: _InboundAuthState
    ws: Any


@dataclass(frozen=True, slots=True)
class BroadcastResult:
    """Delivery counts for one logical WebSocket broadcast."""

    target_count: int  = 0
    success_count: int = 0
    failure_count: int = 0
    timeout_count: int = 0

    def __post_init__(self) -> None:
        counts = (
            self.target_count,
            self.success_count,
            self.failure_count,
            self.timeout_count,
        )
        if any(type(count) is not int or count < 0 for count in counts):
            raise ValueError("broadcast counts must be non-negative integers")
        if self.target_count != self.success_count + self.failure_count + self.timeout_count:
            raise ValueError("target_count must equal the sum of broadcast outcomes")

    @property
    def delivered(self) -> bool:
        """Whether at least one target accepted the action."""

        return self.success_count > 0

    def __bool__(self) -> bool:
        return self.delivered

    def __add__(self, other: object) -> "BroadcastResult":
        if not isinstance(other, BroadcastResult):
            return NotImplemented
        return BroadcastResult(
            target_count  = self.target_count + other.target_count,
            success_count = self.success_count + other.success_count,
            failure_count = self.failure_count + other.failure_count,
            timeout_count = self.timeout_count + other.timeout_count,
        )


class _BroadcastFatalError(_FatalErrorCarrier):
    """Task-safe carrier for a child broadcast's fatal BaseException."""


class InboundEventUnavailable(RuntimeError):
    """The inbound dispatcher is not accepting a new event."""


class InboundEventOverloaded(InboundEventUnavailable):
    """The bounded inbound dispatcher has no remaining admission capacity."""


class InboundEventRevoked(InboundEventUnavailable):
    """An admitted event lost its authenticated generation before execution."""


class _InboundHandlerFatalError(_FatalErrorCarrier, RuntimeError):
    """Task-safe carrier for a handler BaseException."""


@dataclass(eq=False, slots=True)
class _InboundEventTicket:
    """One event admitted at a precise dispatcher sequence number."""

    sequence: int
    key: str
    payload: dict[str, Any]
    result: asyncio.Future[list[dict[str, Any]]]
    auth_guard: Callable[[], bool] | None                         = None
    started: bool                                                 = False
    finished: bool                                                = False
    operation_task: "asyncio.Task[_InboundHandlerOutcome] | None" = None


@dataclass(slots=True)
class _InboundEventLane:
    """FIFO pending work and the sole running ticket for one session key."""

    pending: deque[_InboundEventTicket] = field(default_factory=deque)
    running: _InboundEventTicket | None = None
    ready: bool                         = False


@dataclass(frozen=True, slots=True)
class _InboundHandlerOutcome:
    """A task-safe handler result; raw BaseException never escapes its child task."""

    actions: Any                = None
    error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class _WebSocketSendOutcome:
    """Task-safe result for external WebSocket send code."""

    error: BaseException | None = None


class _InboundEventDispatcher:
    """Bounded, fair, per-session FIFO dispatcher shared by HTTP and WS.

    ``admit`` is the core linearization point.  It runs synchronously on the
    owner event loop after transport authentication and parsing, assigns a
    monotonically increasing sequence, and appends the ticket to exactly one
    keyed lane.  A lane has at most one running handler, while independent
    lanes may consume separate workers.  Completed hot lanes are appended to
    the back of the ready queue, so a cold lane cannot be starved.
    """

    def __init__(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[list[dict[str, Any]]]],
        *,
        max_workers: int,
        queue_size: int,
        drain_timeout_seconds: float = 5.0,
        allow_lazy_start: bool       = False,
    ) -> None:
        self._handler     = handler
        self._max_workers = max(1, int(max_workers))
        self._queue_size  = max(0, int(queue_size))
        # queue_size is the waiting backlog; running workers are additional
        # bounded slots.  Even queue_size=0 is therefore bounded, not unlimited.
        self._capacity              = self._max_workers + self._queue_size
        self._drain_timeout_seconds = max(0.01, float(drain_timeout_seconds))
        self._allow_lazy_start      = allow_lazy_start

        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready_keys: asyncio.Queue[str] | None  = None
        self._lanes: dict[str, _InboundEventLane]    = {}
        self._workers: set[asyncio.Task[None]]       = set()
        self._drained: asyncio.Event | None          = None
        self._sequence                               = 0
        self._worker_starts                          = 0
        self._inflight                               = 0
        self._accepting                              = False
        self._started_once                           = False
        self._stopping                               = False

    @property
    def is_stopped(self) -> bool:
        return (
            not self._accepting
            and self._inflight == 0
            and not any(not task.done() for task in self._workers)
        )

    @property
    def is_quiescent(self) -> bool:
        """Whether no admitted work or live worker remains."""

        return self._inflight == 0 and not any(not task.done() for task in self._workers)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._accepting:
            if self._loop is not loop:
                raise RuntimeError("inbound dispatcher belongs to another event loop")
            return
        if self._inflight or any(not task.done() for task in self._workers):
            raise RuntimeError("inbound dispatcher has an incomplete previous generation")
        self._start_on_loop(loop)

    def _start_on_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop       = loop
        self._ready_keys = asyncio.Queue()
        self._lanes.clear()
        self._workers = {task for task in self._workers if not task.done()}
        self._drained = asyncio.Event()
        self._drained.set()
        self._sequence     = 0
        self._inflight     = 0
        self._accepting    = True
        self._started_once = True
        self._stopping     = False

    @staticmethod
    def _event_key(payload: dict[str, Any], sequence: int) -> str:
        user_id = payload.get("user_id")
        if user_id is None:
            # Events without a conversational identity have no shared session
            # state and therefore receive an independent lane.
            return f"event:{sequence}"
        group_id = payload.get("group_id")
        if group_id is None:
            return f"user:{user_id}"
        return f"group:{group_id}:user:{user_id}"

    def admit(
        self,
        payload: dict[str, Any],
        *,
        auth_guard: Callable[[], bool] | None = None,
    ) -> _InboundEventTicket:
        loop = asyncio.get_running_loop()
        if not self._accepting:
            if self._allow_lazy_start and not self._started_once and not self._stopping:
                self._start_on_loop(loop)
            else:
                raise InboundEventUnavailable("inbound dispatcher is not accepting events")
        if self._loop is not loop:
            raise RuntimeError("inbound dispatcher called from a non-owner event loop")
        if self._inflight >= self._capacity:
            raise InboundEventOverloaded(
                f"inbound event capacity exhausted ({self._capacity} admitted events)"
            )

        self._sequence += 1
        sequence                                     = self._sequence
        key                                          = self._event_key(payload, sequence)
        result: asyncio.Future[list[dict[str, Any]]] = loop.create_future()
        ticket                                       = _InboundEventTicket(
            sequence   = sequence,
            key        = key,
            payload    = payload,
            result     = result,
            auth_guard = auth_guard,
        )
        lane = self._lanes.setdefault(key, _InboundEventLane())
        lane.pending.append(ticket)
        self._inflight += 1
        assert self._drained is not None
        self._drained.clear()
        result.add_done_callback(partial(self._cancel_if_result_cancelled, ticket))
        self._schedule_lane(key, lane)
        return ticket

    async def dispatch(
        self,
        payload: dict[str, Any],
        *,
        auth_guard: Callable[[], bool] | None = None,
    ) -> list[dict[str, Any]]:
        return await self.wait(self.admit(payload, auth_guard=auth_guard))

    async def wait(self, ticket: _InboundEventTicket) -> list[dict[str, Any]]:
        try:
            return await ticket.result
        except asyncio.CancelledError:
            self.cancel(ticket)
            raise

    def cancel(self, ticket: _InboundEventTicket) -> None:
        """Cancel a ticket and physically remove it when it has not started."""

        if ticket.finished:
            return
        if not ticket.result.done():
            ticket.result.cancel()
        if ticket.started:
            operation = ticket.operation_task
            if operation is not None and not operation.done():
                operation.cancel()
            return

        lane = self._lanes.get(ticket.key)
        if lane is None:
            return
        try:
            lane.pending.remove(ticket)
        except ValueError:
            return
        ticket.finished = True
        self._decrement_inflight()
        self._discard_lane_if_idle(ticket.key, lane)

    def discard(self, ticket: _InboundEventTicket) -> None:
        """Cancel queued work and consume any already-terminal outcome."""

        self.cancel(ticket)
        if ticket.result.done() and not ticket.result.cancelled():
            try:
                ticket.result.exception()
            except BaseException:
                # Retrieving is sufficient; the transport intentionally drops
                # a revoked/stopped result and must not re-raise it.
                pass

    def _cancel_if_result_cancelled(
        self,
        ticket: _InboundEventTicket,
        result: asyncio.Future[list[dict[str, Any]]],
    ) -> None:
        if result.cancelled():
            self.cancel(ticket)

    def _schedule_lane(
        self,
        key: str,
        lane: _InboundEventLane,
        *,
        spawn_worker: bool = True,
    ) -> None:
        if lane.ready or lane.running is not None or not lane.pending:
            return
        ready = self._ready_keys
        if ready is None:
            raise RuntimeError("inbound dispatcher is not started")
        lane.ready = True
        ready.put_nowait(key)
        if spawn_worker:
            self._ensure_workers()

    def _ensure_workers(self) -> None:
        ready = self._ready_keys
        if ready is None:
            return
        self._workers = {task for task in self._workers if not task.done()}
        desired       = min(self._max_workers, len(self._workers) + ready.qsize())
        while len(self._workers) < desired:
            task = asyncio.create_task(self._worker_loop())
            self._worker_starts += 1
            self._workers.add(task)
            task.add_done_callback(self._worker_done)

    def _worker_done(self, task: asyncio.Task[None]) -> None:
        self._workers.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except BaseException as exc:
            # No handler failure should reach this boundary.  If dispatcher
            # bookkeeping itself fails, keep servicing other admitted lanes.
            logger.critical(
                "Inbound dispatcher worker failed unexpectedly: %s: %s",
                type(exc).__name__,
                exc,
            )
        if (
            self._ready_keys is not None
            and not self._ready_keys.empty()
            and (not self._stopping or self._inflight > 0)
        ):
            self._ensure_workers()

    async def _worker_loop(self) -> None:
        while True:
            ready = self._ready_keys
            if ready is None:
                return
            try:
                key = ready.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                lane = self._lanes.get(key)
                if lane is None:
                    continue
                lane.ready = False
                if lane.running is not None or not lane.pending:
                    self._discard_lane_if_idle(key, lane)
                    continue
                ticket = lane.pending.popleft()
                if ticket.finished or ticket.result.cancelled():
                    if not ticket.finished:
                        ticket.finished = True
                        self._decrement_inflight()
                    self._discard_lane_if_idle(key, lane)
                    if lane.pending:
                        self._schedule_lane(key, lane, spawn_worker=False)
                    continue
                lane.running   = ticket
                ticket.started = True
                await self._execute_ticket(ticket)
            finally:
                ready.task_done()

    async def _execute_ticket(self, ticket: _InboundEventTicket) -> None:
        lane = self._lanes[ticket.key]
        try:
            if ticket.auth_guard is not None:
                try:
                    authorized = ticket.auth_guard()
                except BaseException as exc:
                    self._set_ticket_exception(ticket, _InboundHandlerFatalError(exc))
                    return
                if not authorized:
                    self._set_ticket_exception(
                        ticket,
                        InboundEventRevoked("inbound authentication generation was revoked"),
                    )
                    return

            operation = asyncio.create_task(self._capture_handler_outcome(ticket.payload))
            ticket.operation_task = operation
            try:
                outcome = await operation
            except asyncio.CancelledError:
                worker = asyncio.current_task()
                if worker is not None and worker.cancelling():
                    if not ticket.result.done():
                        ticket.result.cancel()
                    raise
                # Caller cancellation and forced dispatcher shutdown both
                # cancel the operation deliberately.  Their ticket result is
                # already terminal, so no synthetic handler error is needed.
                if not ticket.result.done():
                    ticket.result.cancel()
                return

            if outcome.error is not None:
                self._set_ticket_exception(ticket, outcome.error)
                return
            actions = outcome.actions
            if not isinstance(actions, list) or any(
                not isinstance(action, dict) for action in actions
            ):
                self._set_ticket_exception(
                    ticket,
                    TypeError("inbound event handler must return a list of action objects"),
                )
                return
            if not ticket.result.done():
                ticket.result.set_result(actions)
        finally:
            ticket.operation_task = None
            ticket.finished       = True
            if lane.running is ticket:
                lane.running = None
            self._decrement_inflight()
            if lane.pending:
                # The current worker immediately loops over the ready queue;
                # spawning here would create one throwaway task per same-key
                # event.  If this worker is cancelled, _worker_done repairs
                # the ready queue with a replacement.
                self._schedule_lane(ticket.key, lane, spawn_worker=False)
            else:
                self._discard_lane_if_idle(ticket.key, lane)

    async def _capture_handler_outcome(
        self,
        payload: dict[str, Any],
    ) -> _InboundHandlerOutcome:
        """Invoke even a malformed handler without leaking fatal task outcomes."""

        try:
            awaitable = self._handler(payload)
            actions   = await awaitable
        except asyncio.CancelledError as exc:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            return _InboundHandlerOutcome(error=_InboundHandlerFatalError(exc))
        except Exception as exc:
            return _InboundHandlerOutcome(error=exc)
        except BaseException as exc:
            return _InboundHandlerOutcome(error=_InboundHandlerFatalError(exc))
        return _InboundHandlerOutcome(actions=actions)

    @staticmethod
    def _set_ticket_exception(ticket: _InboundEventTicket, error: BaseException) -> None:
        if not ticket.result.done():
            ticket.result.set_exception(error)

    def _decrement_inflight(self) -> None:
        self._inflight -= 1
        if self._inflight < 0:
            raise RuntimeError("inbound dispatcher inflight accounting underflow")
        if self._inflight == 0 and self._drained is not None:
            self._drained.set()

    def _discard_lane_if_idle(self, key: str, lane: _InboundEventLane) -> None:
        if not lane.pending and lane.running is None and not lane.ready:
            if self._lanes.get(key) is lane:
                del self._lanes[key]

    async def stop(self) -> None:
        """Stop admission, drain accepted events, then bound forced cancellation."""

        self._accepting = False
        self._stopping  = True
        if self._loop is None:
            self._stopping = False
            return
        if self._loop is not asyncio.get_running_loop():
            raise RuntimeError("inbound dispatcher stopped from a non-owner event loop")

        drained = self._drained
        if drained is not None and self._inflight:
            try:
                await asyncio.wait_for(
                    drained.wait(),
                    timeout=self._drain_timeout_seconds,
                )
            except TimeoutError:
                self._abort_pending()
                for lane in tuple(self._lanes.values()):
                    running = lane.running
                    if running is not None:
                        if not running.result.done():
                            running.result.set_exception(
                                InboundEventUnavailable("inbound dispatcher stopped")
                            )
                        operation = running.operation_task
                        if operation is not None and not operation.done():
                            operation.cancel()
                if self._inflight:
                    try:
                        await asyncio.wait_for(
                            drained.wait(),
                            timeout=self._drain_timeout_seconds,
                        )
                    except TimeoutError:
                        raise RuntimeError(
                            f"{self._inflight} inbound event handler(s) ignored cancellation"
                        ) from None

        workers = tuple(task for task in self._workers if not task.done())
        for task in workers:
            task.cancel()
        if workers:
            done, pending = await asyncio.wait(workers, timeout=self._drain_timeout_seconds)
            if done:
                await asyncio.gather(*done, return_exceptions=True)
            if pending:
                raise RuntimeError(
                    f"{len(pending)} inbound dispatcher worker(s) ignored cancellation"
                )
        self._finalize_stop()

    def _abort_pending(self) -> None:
        for key, lane in tuple(self._lanes.items()):
            while lane.pending:
                ticket = lane.pending.popleft()
                if ticket.finished:
                    continue
                ticket.finished = True
                if not ticket.result.done():
                    ticket.result.set_exception(
                        InboundEventUnavailable("inbound dispatcher stopped")
                    )
                self._decrement_inflight()
            self._discard_lane_if_idle(key, lane)

    def _finalize_stop(self) -> None:
        ready = self._ready_keys
        if ready is not None:
            while True:
                try:
                    ready.get_nowait()
                except asyncio.QueueEmpty:
                    break
                else:
                    ready.task_done()
        self._lanes.clear()
        self._workers.clear()
        self._ready_keys = None
        self._drained    = None
        self._loop       = None
        self._stopping   = False


class InboundServer:
    """
    入站服务器

    提供 HTTP POST 和 WebSocket 两种方式接收事件。
    """

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        handler: Callable[[dict[str, Any]], Awaitable[list[dict[str, Any]]]],
        *,
        enable_http: bool = True,
        enable_ws: bool = True,
        ws_path: str = "/ws",
        ws_max_workers: int = 8,
        ws_queue_size: int = 200,
        ws_broadcast_timeout_seconds: float = DEFAULT_INBOUND_WS_BROADCAST_TIMEOUT_SECONDS,
        trusted_tls_proxy: bool = False,
        event_dispatcher: _InboundEventDispatcher | None = None,
    ) -> None:
        if type(trusted_tls_proxy) is not bool:
            raise TypeError("trusted_tls_proxy must be a boolean")
        self.host                                          = host
        self.port                                          = port
        self.ws_path                                       = ws_path
        self._auth_state                                   = _InboundAuthState(token)
        self._auth_state_lock                              = threading.RLock()
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self.handler                                       = handler
        self.enable_http                                   = bool(enable_http)
        self.enable_ws                                     = bool(enable_ws)
        self.trusted_tls_proxy                             = trusted_tls_proxy
        self._ws_broadcast_timeout_seconds                 = _parse_positive_float(
            ws_broadcast_timeout_seconds,
            default=DEFAULT_INBOUND_WS_BROADCAST_TIMEOUT_SECONDS,
        )
        self.app = web.Application()
        routes   = []
        if self.enable_http:
            routes.extend(
                [
                    web.get("/health", self.health),
                    web.get("/metrics", self.metrics),
                    web.post("/event", self.post_event),
                ]
            )
        if self.enable_ws:
            routes.append(web.get(self.ws_path, self.ws_handler))
        self.app.add_routes(routes)

        # 状态追踪
        self._start_time                   = time.time()
        self._request_count                = 0
        self._ws_connections               = 0
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None     = None
        self._running                      = False
        self._lifecycle_lock               = _LazyAsyncLock()

        self._ws_event_queue: (
            asyncio.Queue[
                tuple[
                    web.WebSocketResponse,
                    _InboundAuthState,
                    dict[str, Any] | _InboundEventTicket,
                ]
            ]
            | None
        ) = None
        self._ws_worker_tasks: list[asyncio.Task[None]] = []
        self._ws_close_tasks: set[asyncio.Task[None]]   = set()
        self._ws_max_workers = max(1, _parse_positive_int(ws_max_workers, default=1))
        # Outbound fan-out shares the configured WS concurrency budget.  The
        # semaphore is global to this server, so overlapping broadcasts cannot
        # multiply active socket writes beyond the same fixed bound.
        self._ws_broadcast_slots = asyncio.Semaphore(self._ws_max_workers)
        try:
            max_queue = int(ws_queue_size)
        except (TypeError, ValueError):
            max_queue = 0
        if max_queue < 0:
            max_queue = 0
        self._event_queue_size = max_queue
        # A configured zero means "no waiting handler backlog", not an
        # unbounded delivery queue.  Keep enough bounded delivery slots for
        # the workers that may already be running.
        delivery_queue_size = max_queue or self._ws_max_workers
        if self.enable_ws:
            self._ws_event_queue = asyncio.Queue(maxsize=delivery_queue_size)

        self._handler_drain_timeout_seconds = 5.0
        self._owns_event_dispatcher         = event_dispatcher is None
        self._event_dispatcher              = event_dispatcher or self._new_owned_event_dispatcher()

        # 可选：外部注入的状态获取函数
        self._get_plugins_count: Callable[[], int] | None      = None
        self._get_sessions_count: Callable[[], int] | None     = None
        self._get_pending_jobs: Callable[[], int] | None       = None
        self._get_metrics: Callable[[], dict[str, Any]] | None = None

        # 活跃的 WebSocket 连接集合
        self._active_sockets: set[web.WebSocketResponse]                         = set()
        self._socket_auth_states: dict[web.WebSocketResponse, _InboundAuthState] = {}
        # A constructed server may be invoked directly by embedders/tests;
        # network admission is still impossible until ``start`` binds a site.
        self._accepting_events                             = True
        self._active_handler_tasks: set[asyncio.Task[Any]] = set()

    async def _call_handler(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Resolve the mutable standalone-server handler at execution time."""

        return await self.handler(payload)

    def _new_owned_event_dispatcher(self) -> _InboundEventDispatcher:
        return _InboundEventDispatcher(
            self._call_handler,
            max_workers           = self._ws_max_workers,
            queue_size            = self._event_queue_size,
            drain_timeout_seconds = self._handler_drain_timeout_seconds,
            allow_lazy_start      = True,
        )

    @property
    def token(self) -> str:
        return self._auth_state.token

    def _auth_is_current(self, auth: _InboundAuthState | None) -> bool:
        current = self._auth_state
        if auth is None:
            return True
        return auth is current

    def set_status_providers(
        self,
        plugins_count: Callable[[], int] | None      = None,
        sessions_count: Callable[[], int] | None     = None,
        pending_jobs: Callable[[], int] | None       = None,
        metrics: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        """设置状态提供函数"""
        self._get_plugins_count  = plugins_count
        self._get_sessions_count = sessions_count
        self._get_pending_jobs   = pending_jobs
        self._get_metrics        = metrics

    def update_token(self, token: str) -> None:
        """Replace the inbound token and revoke older WebSocket sessions."""
        with self._auth_state_lock:
            previous_state = self._auth_state
            if token == previous_state.token:
                return
            next_state       = _InboundAuthState(token, previous_state.generation + 1)
            self._auth_state = next_state

        # The state swap above makes every old request/socket generation fail
        # closed immediately.  aiohttp-owned collections and sockets are only
        # touched on their owning loop.
        self._schedule_auth_rotation(previous_state)

    def _schedule_auth_rotation(self, previous_state: _InboundAuthState) -> None:
        loop = self._event_loop
        if loop is None or loop.is_closed():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return

        def revoke_old_sockets() -> None:
            # A socket admitted after this update carries a different state and
            # must not be closed by this generation's delayed callback.
            sockets = tuple(
                ws
                for ws in self._active_sockets
                if self._socket_auth_states.get(ws) in (None, previous_state)
            )
            for ws in sockets:
                self._active_sockets.discard(ws)
                self._socket_auth_states.pop(ws, None)
                self._schedule_ws_close(ws, reason=b"inbound token rotated")

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            revoke_old_sockets()
        else:
            try:
                loop.call_soon_threadsafe(revoke_old_sockets)
            except RuntimeError:
                # The generation swap already makes every old socket invisible
                # to admission and broadcast.  If its owning loop closed in the
                # scheduling race there is no safe callback target left.
                logger.debug("Inbound auth rotation raced with event-loop shutdown")

    def _schedule_ws_close(
        self,
        ws: web.WebSocketResponse,
        *,
        reason: bytes,
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Runtime updates are performed on the owning event loop.  If an
            # embedder calls from another thread, generation checks still
            # revoke subsequent frames; physical close happens on the next
            # socket activity or server shutdown.
            logger.warning("Unable to schedule revoked WebSocket close outside the event loop")
            return

        async def close_socket() -> None:
            try:
                await ws.close(code=1008, message=reason)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Closing revoked WebSocket failed: %s", exc)
            except BaseException as exc:
                raise _OwnedCleanupFatalError(exc) from None

        task = loop.create_task(close_socket())
        self._ws_close_tasks.add(task)

        def discard(done: asyncio.Task[None]) -> None:
            self._ws_close_tasks.discard(done)
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except _OwnedCleanupFatalError as exc:
                logger.error("Fatal failure while closing a revoked WebSocket: %s", exc)
            except Exception as exc:
                logger.debug("Closing revoked WebSocket task failed: %s", exc)

        task.add_done_callback(discard)

    def _increment_ws_connections(self) -> None:
        self._ws_connections += 1

    def _decrement_ws_connections(self) -> None:
        self._ws_connections -= 1

    def _get_ws_connections(self) -> int:
        return self._ws_connections

    @staticmethod
    def _unauthorized_response() -> web.Response:
        return web.json_response({"status": "unauthorized"}, status=401)

    async def _send_ws_json_bounded(
        self,
        ws: web.WebSocketResponse,
        payload: dict[str, Any],
    ) -> bool:
        """Send a WS response without letting a stalled peer pin a worker."""

        text = json.dumps(payload, ensure_ascii=False)
        try:
            outcome = await asyncio.wait_for(
                self._capture_ws_send(ws, text),
                timeout=self._ws_broadcast_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.warning("WebSocket inbound response timed out")
            self._schedule_ws_close(ws, reason=b"inbound response timed out")
            return False
        except Exception as exc:
            logger.warning("WebSocket inbound response failed: %s", exc)
            self._schedule_ws_close(ws, reason=b"inbound response failed")
            return False
        except BaseException as exc:
            logger.critical(
                "Fatal WebSocket response failure was isolated: %s: %s",
                type(exc).__name__,
                exc,
            )
            self._schedule_ws_close(ws, reason=b"inbound response failed")
            return False
        if outcome.error is not None:
            outcome_error = outcome.error
            if isinstance(outcome_error, Exception):
                logger.warning("WebSocket inbound response failed: %s", outcome_error)
            else:
                logger.critical(
                    "Fatal WebSocket response failure was isolated: %s: %s",
                    type(outcome_error).__name__,
                    outcome_error,
                )
            self._schedule_ws_close(ws, reason=b"inbound response failed")
            return False
        return True

    async def _close_ws_bounded(
        self,
        ws: web.WebSocketResponse,
        *,
        code: int,
        message: bytes,
    ) -> None:
        """Bound an inline protocol close and isolate external fatal errors."""

        try:
            await asyncio.wait_for(
                _run_owned_cleanup(lambda: ws.close(code=code, message=message)),
                timeout=self._ws_broadcast_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            logger.debug("Closing WebSocket inline failed: %s", exc)

    @staticmethod
    async def _capture_ws_send(
        ws: web.WebSocketResponse,
        text: str,
    ) -> _WebSocketSendOutcome:
        """Keep raw BaseException from escaping an asyncio child task step."""

        try:
            awaitable = ws.send_str(text)
            await awaitable
        except asyncio.CancelledError as exc:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            return _WebSocketSendOutcome(error=exc)
        except BaseException as exc:
            return _WebSocketSendOutcome(error=exc)
        return _WebSocketSendOutcome()

    @staticmethod
    def _payload_validation_error(payload: dict[str, Any]) -> tuple[str, int] | None:
        """Run cheap transport checks before canonical model validation."""

        post_type = payload.get("post_type")
        if not isinstance(post_type, str) or not post_type.strip():
            return "Missing or invalid post_type", 400
        if post_type != "message":
            return None

        message_type = payload.get("message_type")
        if not isinstance(message_type, str) or not message_type.strip():
            return "Missing or invalid message_type", 400

        user_id = payload.get("user_id")
        if user_id is None or isinstance(user_id, bool) or not str(user_id).strip():
            return "Missing or invalid user_id", 400

        message     = payload.get("message")
        raw_message = str(payload.get("raw_message") or "")
        if message is None and not raw_message.strip():
            return "Missing message or raw_message", 400
        if message is not None and not isinstance(message, (list, str)):
            return "Invalid message payload", 400

        if message_type == "group":
            group_id = payload.get("group_id")
            if group_id is None or isinstance(group_id, bool) or not str(group_id).strip():
                return "Missing or invalid group_id", 400

        return None

    @classmethod
    def _validate_payload(
        cls,
        payload: dict[str, Any],
    ) -> tuple[ValidatedInboundEvent | None, tuple[str, int] | None]:
        """Validate and detach one transport payload exactly once."""

        validation_error = cls._payload_validation_error(payload)
        if validation_error is not None:
            return None, validation_error
        try:
            validated = OneBotEvent.model_validate(payload)
        except ValidationError:
            return None, ("Invalid message payload", 400)
        return ValidatedInboundEvent(validated.model_dump()), None

    async def health(self, request: web.Request) -> web.Response:
        """
        健康检查端点

        返回服务器状态信息，包括：
        - status: 服务状态
        - version: 版本号
        - uptime_seconds: 运行时间
        - plugins_loaded: 已加载插件数
        - active_sessions: 活跃会话数
        - pending_jobs: 待处理任务数
        - request_count: 请求计数
        - ws_connections: WebSocket 连接数
        """
        if not self._authorized(request):
            return self._unauthorized_response()

        uptime = time.time() - self._start_time

        response = {
            "status": "ok",
            "version": VERSION,
            "uptime_seconds": round(uptime, 1),
            "uptime_human": self._format_uptime(uptime),
            "request_count": self._request_count,
            "ws_connections": self._get_ws_connections(),
        }

        # 添加可选状态信息
        if self._get_plugins_count:
            try:
                response["plugins_loaded"] = self._get_plugins_count()
            except Exception as exc:
                logger.warning("Plugins count unavailable: %s", exc)
            except BaseException as exc:
                logger.critical(
                    "Fatal plugins-count provider failure was isolated: %s: %s",
                    type(exc).__name__,
                    exc,
                )

        if self._get_sessions_count:
            try:
                response["active_sessions"] = self._get_sessions_count()
            except Exception as exc:
                logger.warning("Sessions count unavailable: %s", exc)
            except BaseException as exc:
                logger.critical(
                    "Fatal sessions-count provider failure was isolated: %s: %s",
                    type(exc).__name__,
                    exc,
                )

        if self._get_pending_jobs:
            try:
                response["pending_jobs"] = self._get_pending_jobs()
            except Exception as exc:
                logger.warning("Pending jobs count unavailable: %s", exc)
            except BaseException as exc:
                logger.critical(
                    "Fatal pending-jobs provider failure was isolated: %s: %s",
                    type(exc).__name__,
                    exc,
                )

        return web.json_response(response)

    async def metrics(self, request: web.Request) -> web.Response:
        """
        性能指标端点

        返回详细的性能指标数据。需要配置 metrics 提供函数。
        """
        if not self._authorized(request):
            return self._unauthorized_response()

        if not self._get_metrics:
            return web.json_response({"error": "Metrics not configured"}, status=501)

        try:
            metrics_data = self._get_metrics()
            return web.json_response(metrics_data)
        except Exception as exc:
            logger.exception("Failed to get metrics: %s", exc)
            return web.json_response({"error": "Metrics unavailable"}, status=500)
        except BaseException as exc:
            logger.critical(
                "Fatal metrics provider failure was isolated: %s: %s",
                type(exc).__name__,
                exc,
            )
            return web.json_response({"error": "Metrics unavailable"}, status=500)

    def _format_uptime(self, seconds: float) -> str:
        """格式化运行时间为人类可读格式"""
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < SECONDS_PER_HOUR:
            minutes = int(seconds / 60)
            secs    = int(seconds % 60)
            return f"{minutes}m {secs}s"
        elif seconds < SECONDS_PER_DAY:
            hours   = int(seconds / SECONDS_PER_HOUR)
            minutes = int((seconds % SECONDS_PER_HOUR) / 60)
            return f"{hours}h {minutes}m"
        else:
            days  = int(seconds / SECONDS_PER_DAY)
            hours = int((seconds % SECONDS_PER_DAY) / SECONDS_PER_HOUR)
            return f"{days}d {hours}h"

    def _authorized(
        self,
        request: web.Request,
        auth_state: _InboundAuthState | None = None,
    ) -> bool:
        auth_state = self._auth_state if auth_state is None else auth_state
        auth       = request.headers.get("Authorization", "")
        return verify_bearer_token(auth, auth_state.token)

    async def post_event(self, request: web.Request) -> web.Response:
        """处理 HTTP POST 事件"""
        auth_state = self._auth_state
        if not self._accepting_events:
            return web.json_response({"status": "shutting_down"}, status=503)
        if not self._authorized(request, auth_state):
            return self._unauthorized_response()

        content_type = request.headers.get("Content-Type", "")
        media_type   = content_type.partition(";")[0].strip().lower()
        if media_type != "application/json" and not (
            media_type.startswith("application/") and media_type.endswith("+json")
        ):
            return web.json_response({"error": "Unsupported Content-Type"}, status=415)

        self._request_count += 1
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            logger.warning("Invalid JSON payload: %s", exc)
            return web.json_response({"error": "Invalid JSON"}, status=400)
        except ContentTypeError:
            return web.json_response({"error": "Unsupported Content-Type"}, status=415)
        if not isinstance(payload, dict):
            return web.json_response({"error": "Payload must be a JSON object"}, status=400)
        normalized, validation_error = self._validate_payload(payload)
        if validation_error is not None:
            message, status = validation_error
            return web.json_response({"error": message}, status=status)
        if not self._auth_is_current(auth_state):
            return self._unauthorized_response()
        try:
            assert normalized is not None
            normalized["_source"] = "inbound_http"
            # 只有显式请求 action-envelope 的测试/自定义客户端接收动作列表。
            # 标准 HTTP 上报由应用通过 action API 投递，响应正文仅确认接收。
            normalized["_http_action_delivery"] = (
                request.headers.get("X-XiaoQing-Response-Mode", "onebot") != "actions"
            )
            actions = await self._invoke_handler(
                normalized,
                auth_generation=auth_state,
            )
            if not self._auth_is_current(auth_state):
                await _resolve_delivery_actions(actions, delivered=False)
                return self._unauthorized_response()
        except asyncio.CancelledError:
            raise
        except InboundEventRevoked:
            return self._unauthorized_response()
        except InboundEventOverloaded as exc:
            logger.warning("HTTP inbound event rejected by bounded dispatcher: %s", exc)
            return web.json_response({"error": "Inbound event queue overloaded"}, status=503)
        except InboundEventUnavailable:
            return web.json_response({"status": "shutting_down"}, status=503)
        except Exception as exc:
            logger.exception("HTTP event handler error: %s", exc)
            return web.json_response({"error": "Event handler unavailable"}, status=500)
        except BaseException as exc:
            logger.critical(
                "Fatal HTTP event handler failure was isolated: %s: %s",
                type(exc).__name__,
                exc,
            )
            return web.json_response({"error": "Event handler unavailable"}, status=500)
        return await _finalize_http_action_response(
            request,
            actions,
            # Unit tests call handlers with a small request double.  A real
            # aiohttp Request owns the payload writer and therefore uses the
            # explicit prepare/write_eof acknowledgement boundary.
            write_to_transport = isinstance(request, web.Request),
            standard_onebot    = bool(normalized.get("_http_action_delivery")),
        )

    async def ws_handler(self, request: web.Request) -> web.StreamResponse:
        """处理 WebSocket 连接"""
        if not self._accepting_events:
            raise web.HTTPServiceUnavailable()
        if self._event_loop is None:
            self._event_loop = asyncio.get_running_loop()
        connection_auth = self._auth_state
        if not self._authorized(request, connection_auth):
            raise web.HTTPUnauthorized()
        if not self.enable_ws:
            raise web.HTTPNotFound()
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if not self._accepting_events:
            await self._close_ws_bounded(
                ws,
                code    = 1013,
                message = b"inbound server is not accepting events",
            )
            return ws
        with self._auth_state_lock:
            if connection_auth is not self._auth_state:
                stale_auth = True
            else:
                stale_auth = False
                self._active_sockets.add(ws)
                self._socket_auth_states[ws] = connection_auth
        if stale_auth:
            await self._close_ws_bounded(
                ws,
                code    = 1008,
                message = b"inbound token rotated",
            )
            return ws

        self._increment_ws_connections()
        try:
            async for msg in ws:
                if not self._auth_is_current(connection_auth):
                    await self._close_ws_bounded(
                        ws,
                        code    = 1008,
                        message = b"inbound token rotated",
                    )
                    break
                if not self._accepting_events:
                    break
                if msg.type == web.WSMsgType.TEXT:
                    self._request_count += 1
                    try:
                        payload = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        if not await self._send_ws_json_bounded(
                            ws,
                            {"error": "Payload must be a JSON object"},
                        ):
                            break
                        continue
                    normalized, validation_error = self._validate_payload(payload)
                    if validation_error is not None:
                        message, _status = validation_error
                        if not await self._send_ws_json_bounded(ws, {"error": message}):
                            break
                        continue
                    queue = self._ws_event_queue
                    if queue is None:
                        continue
                    assert normalized is not None
                    normalized["_source"] = "inbound_ws"
                    # This is immediately adjacent to dispatcher admission;
                    # stop cannot interleave in the same event-loop turn.
                    if not self._accepting_events:
                        break
                    if not self._auth_is_current(connection_auth):
                        break
                    try:
                        ticket = self._event_dispatcher.admit(
                            normalized,
                            auth_guard=partial(self._auth_is_current, connection_auth),
                        )
                    except InboundEventOverloaded:
                        if not await self._send_ws_json_bounded(
                            ws,
                            {"error": "Inbound event queue overloaded"},
                        ):
                            break
                        continue
                    except InboundEventUnavailable:
                        break
                    try:
                        queue.put_nowait((ws, connection_auth, ticket))
                    except asyncio.QueueFull:
                        # Admission and delivery reservation occur in the same
                        # event-loop turn.  The ticket cannot have started yet,
                        # so this physically removes it from its keyed lane.
                        self._event_dispatcher.cancel(ticket)
                        if not await self._send_ws_json_bounded(
                            ws,
                            {"error": "Inbound event queue overloaded"},
                        ):
                            break
                        continue
                    self._ensure_ws_workers()
        finally:
            self._active_sockets.discard(ws)
            self._socket_auth_states.pop(ws, None)
            self._decrement_ws_connections()
        return ws

    async def broadcast(self, action: dict[str, Any]) -> BroadcastResult:
        """向当前所有 WebSocket 客户端并发广播，并报告实际投递结果。"""
        auth_state = self._auth_state
        sockets    = tuple(
            ws
            for ws in self._active_sockets
            if self._socket_auth_states.get(ws, auth_state) is auth_state
        )
        if not sockets:
            return BroadcastResult()

        text = json.dumps(action, ensure_ascii=False)

        def drop_socket(ws: web.WebSocketResponse) -> None:
            was_tracked = ws in self._active_sockets or ws in self._socket_auth_states
            self._active_sockets.discard(ws)
            self._socket_auth_states.pop(ws, None)
            if was_tracked:
                self._schedule_ws_close(ws, reason=b"broadcast delivery failed")

        async def send_one(ws: web.WebSocketResponse) -> str:
            # Token creation is the send/rotation linearization point.  Keep
            # external ``send_str`` code outside the threading lock so a
            # factory that joins an update thread cannot deadlock.  Rotation
            # after this commit does not revoke the already-admitted send.
            with self._auth_state_lock:
                if (
                    auth_state is not self._auth_state
                    or self._socket_auth_states.get(ws, auth_state) is not auth_state
                ):
                    commit = None
                else:
                    commit = _InboundBroadcastCommit(auth_state=auth_state, ws=ws)
            if commit is None:
                drop_socket(ws)
                return "failure"
            try:
                outcome = await asyncio.wait_for(
                    self._capture_ws_send(commit.ws, text),
                    timeout=self._ws_broadcast_timeout_seconds,
                )
                if outcome.error is not None:
                    raise outcome.error
            except TimeoutError:
                logger.warning(
                    "Broadcast timed out for one client after %.3fs",
                    self._ws_broadcast_timeout_seconds,
                )
                drop_socket(ws)
                return "timeout"
            except asyncio.CancelledError as exc:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                drop_socket(ws)
                raise _BroadcastFatalError(exc) from exc
            except Exception as exc:
                logger.warning("Broadcast failed for one client: %s", exc)
                drop_socket(ws)
                return "failure"
            except BaseException as exc:
                drop_socket(ws)
                raise _BroadcastFatalError(exc) from exc
            return "success"

        outcomes: list[str | None] = [None] * len(sockets)
        pending_sockets            = iter(enumerate(sockets))

        async def send_worker() -> None:
            while True:
                try:
                    index, ws = next(pending_sockets)
                except StopIteration:
                    return
                async with self._ws_broadcast_slots:
                    outcomes[index] = await send_one(ws)

        # Never allocate one task per connection.  A fixed-size worker set
        # walks the immutable socket snapshot and slow peers are still bounded
        # by ``_ws_broadcast_timeout_seconds`` and then disconnected.
        tasks = [
            asyncio.create_task(send_worker())
            for _ in range(min(len(sockets), self._ws_max_workers))
        ]
        try:
            await asyncio.gather(*tasks)
        except BaseException as exc:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if isinstance(exc, _BroadcastFatalError):
                raise exc.original from None
            raise

        return BroadcastResult(
            target_count  = len(sockets),
            success_count = outcomes.count("success"),
            failure_count = outcomes.count("failure"),
            timeout_count = outcomes.count("timeout"),
        )

    def active_ws_connections(self) -> int:
        auth_state = self._auth_state
        return sum(
            1
            for ws in self._active_sockets
            if self._socket_auth_states.get(ws, auth_state) is auth_state
        )

    async def _handle_ws_event(
        self,
        ws: web.WebSocketResponse,
        payload: dict[str, Any] | _InboundEventTicket,
        auth_generation: _InboundAuthState | None = None,
    ) -> None:
        """处理 WebSocket 事件（非阻塞）。"""

        if not self._accepting_events:
            if isinstance(payload, _InboundEventTicket):
                self._event_dispatcher.discard(payload)
            return
        if not self._auth_is_current(auth_generation):
            if isinstance(payload, _InboundEventTicket):
                self._event_dispatcher.discard(payload)
            return
        try:
            if isinstance(payload, _InboundEventTicket):
                actions = await self._await_admitted_event(payload)
            else:
                payload            = normalize_inbound_message(payload)
                payload["_source"] = "inbound_ws"
                actions            = await self._invoke_handler(
                    payload,
                    auth_generation=auth_generation,
                )
            if not self._auth_is_current(auth_generation):
                await _resolve_delivery_actions(actions, delivered=False)
                return
            for index, action in enumerate(actions):
                sent = await self._send_ws_json_bounded(ws, strip_receipt(action))
                await asyncio.shield(resolve_action_handoff(action, delivered=sent))
                if not sent:
                    await _resolve_delivery_actions(actions[index + 1 :], delivered=False)
                    return
        except asyncio.CancelledError:
            raise
        except InboundEventRevoked:
            return
        except InboundEventOverloaded as exc:
            logger.warning("WebSocket inbound event rejected by bounded dispatcher: %s", exc)
            await self._send_ws_json_bounded(ws, {"error": "Inbound event queue overloaded"})
        except InboundEventUnavailable:
            return
        except Exception as exc:
            logger.exception("WebSocket event handler error: %s", exc)
        except BaseException as exc:
            # Worker tasks are fire-and-forget.  A raw SystemExit or similar
            # must never escape the worker and terminate the event loop.
            logger.critical(
                "Fatal WebSocket event handler failure was isolated: %s: %s",
                type(exc).__name__,
                exc,
            )

    def _ensure_ws_workers(self) -> None:
        if not self.enable_ws or self._ws_event_queue is None:
            return
        alive  = [task for task in self._ws_worker_tasks if not task.done()]
        needed = self._ws_max_workers - len(alive)
        for _ in range(needed):
            alive.append(asyncio.create_task(self._ws_worker_loop()))
        self._ws_worker_tasks = alive

    async def _ws_worker_loop(self) -> None:
        while True:
            try:
                queue = self._ws_event_queue
                if queue is None:
                    break
                ws, auth_generation, payload = await queue.get()
            except asyncio.CancelledError:
                break
            try:
                await self._handle_ws_event(ws, payload, auth_generation)
            finally:
                queue = self._ws_event_queue
                if queue:
                    queue.task_done()

    async def start(self, *, accept_events: bool = True) -> None:
        """Start exactly one listener generation and serialize it with stop."""
        await _start_inbound_lifecycle(self, accept_events=accept_events)

    async def _start_locked(
        self,
        deferred_cancellation: _DeferredCancellation,
        *,
        accept_events: bool,
    ) -> None:
        if self._running:
            if accept_events:
                self.commit_admission()
            return
        self._event_loop = asyncio.get_running_loop()
        completed_tasks  = [
            task
            for task in (
                *self._active_handler_tasks,
                *self._ws_worker_tasks,
                *self._ws_close_tasks,
            )
            if task.done()
        ]
        if completed_tasks:
            await asyncio.gather(*completed_tasks, return_exceptions=True)
        self._active_handler_tasks = {
            task for task in self._active_handler_tasks if not task.done()
        }
        self._ws_worker_tasks = [task for task in self._ws_worker_tasks if not task.done()]
        self._ws_close_tasks  = {task for task in self._ws_close_tasks if not task.done()}
        if (
            self._owns_event_dispatcher
            and self._event_dispatcher.is_stopped
            and self._event_dispatcher._started_once
        ):
            self._event_dispatcher = self._new_owned_event_dispatcher()
        queue_has_work = self._ws_event_queue is not None and not self._ws_event_queue.empty()
        if (
            self._runner is not None
            or self._site is not None
            or self._active_handler_tasks
            or self._ws_worker_tasks
            or self._ws_close_tasks
            or self._active_sockets
            or queue_has_work
            or (
                self._owns_event_dispatcher
                and not self._event_dispatcher.is_stopped
                and not self._event_dispatcher.is_quiescent
            )
        ):
            raise RuntimeError("inbound server has an incomplete previous start")
        if not self.token.strip():
            raise ValueError("inbound server requires a non-empty inbound token")
        if not is_loopback_host(self.host):
            if self.trusted_tls_proxy is not True:
                raise ValueError(
                    "non-loopback inbound server bind is plaintext and requires "
                    "trusted_tls_proxy=true"
                )
        self._accepting_events   = False
        runner                   = web.AppRunner(self.app)
        site: web.TCPSite | None = None
        self._runner             = runner
        try:
            await runner.setup()
            site = web.TCPSite(runner, self.host, self.port, ssl_context=None)
            self._site = site
            await site.start()
            if self._owns_event_dispatcher:
                await self._event_dispatcher.start()
        except BaseException:

            async def rollback() -> None:
                site_failed   = False
                runner_failed = False
                if site is not None:
                    try:
                        await site.stop()
                    except BaseException as cleanup_exc:
                        site_failed = True
                        logger.exception(
                            "Failed to stop partially started inbound site", exc_info=cleanup_exc
                        )
                try:
                    await runner.cleanup()
                except BaseException as cleanup_exc:
                    runner_failed = True
                    logger.exception(
                        "Failed to clean partially started inbound runner", exc_info=cleanup_exc
                    )
                if self._owns_event_dispatcher:
                    try:
                        await self._event_dispatcher.stop()
                    except BaseException as cleanup_exc:
                        logger.exception(
                            "Failed to stop partially started inbound dispatcher",
                            exc_info=cleanup_exc,
                        )
                self._site             = site if site_failed else None
                self._runner           = runner if runner_failed else None
                self._running          = False
                self._accepting_events = False

            cleanup_task = asyncio.create_task(_run_owned_cleanup(rollback))
            await _await_cleanup_task(cleanup_task, deferred_cancellation)
            raise
        self._running          = True
        self._accepting_events = accept_events
        logger.info(
            "Inbound server listening on %s:%s (http=%s ws=%s)",
            self.host,
            self.port,
            self.enable_http,
            self.enable_ws,
        )

    def commit_admission(self) -> None:
        """Atomically expose a listener after its manager generation is complete."""

        if not self._running:
            raise RuntimeError("cannot commit admission before inbound server start")
        self._accepting_events = True

    async def stop(self) -> None:
        """Serialize shutdown with start so a late bind cannot resurrect a listener."""
        async with self._lifecycle_lock.get():
            try:
                await self._stop_locked()
            except asyncio.CancelledError:
                raise
            except Exception:
                raise
            except BaseException as exc:
                raise InboundLifecycleFatalError(exc) from None

    async def _drain_handler_tasks(self) -> None:
        """等待已接纳处理器完成，再取消超过宽限期的任务。"""

        active = [
            task
            for task in self._active_handler_tasks
            if task is not asyncio.current_task() and not task.done()
        ]
        if not active:
            return
        done, pending = await asyncio.wait(active, timeout=self._handler_drain_timeout_seconds)
        if done:
            await asyncio.gather(*done, return_exceptions=True)
        for task in pending:
            task.cancel()
        if not pending:
            return
        done_after_cancel, still_pending = await asyncio.wait(
            pending,
            timeout=self._handler_drain_timeout_seconds,
        )
        if done_after_cancel:
            await asyncio.gather(*done_after_cancel, return_exceptions=True)
        if still_pending:
            raise RuntimeError(f"{len(still_pending)} inbound handler task(s) ignored cancellation")

    async def _drain_ws_close_tasks(self) -> None:
        """排空已启动的 WebSocket close，并报告忽略取消的任务。"""

        close_tasks = [task for task in self._ws_close_tasks if not task.done()]
        if not close_tasks:
            return
        done, pending = await asyncio.wait(
            close_tasks,
            timeout=self._handler_drain_timeout_seconds,
        )
        if done:
            await asyncio.gather(*done, return_exceptions=True)
        for task in pending:
            task.cancel()
        if not pending:
            return
        done_after_cancel, still_pending = await asyncio.wait(
            pending,
            timeout=self._handler_drain_timeout_seconds,
        )
        if done_after_cancel:
            await asyncio.gather(*done_after_cancel, return_exceptions=True)
        if still_pending:
            raise RuntimeError(f"{len(still_pending)} WebSocket close task(s) ignored cancellation")

    async def _stop_ws_workers(self) -> None:
        """取消 WebSocket 队列 worker，并保留未收敛任务供后续重试。"""

        tasks = list(self._ws_worker_tasks)
        for task in tasks:
            task.cancel()
        if not tasks:
            self._ws_worker_tasks.clear()
            return
        done, pending = await asyncio.wait(tasks, timeout=self._handler_drain_timeout_seconds)
        if done:
            await asyncio.gather(*done, return_exceptions=True)
        self._ws_worker_tasks = [task for task in pending if not task.done()]
        if pending:
            raise RuntimeError(f"{len(pending)} WebSocket worker task(s) ignored cancellation")

    def _discard_queued_ws_events(self) -> None:
        """在 worker 全部退出后撤销仍未处理事件的 dispatcher 票据。"""

        if self._ws_worker_tasks or self._ws_event_queue is None:
            return
        while True:
            try:
                _ws, _auth, queued_event = self._ws_event_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if isinstance(queued_event, _InboundEventTicket):
                self._event_dispatcher.discard(queued_event)
            self._ws_event_queue.task_done()

    async def _stop_owned_dispatcher(self) -> None:
        self._event_dispatcher._drain_timeout_seconds = self._handler_drain_timeout_seconds
        await self._event_dispatcher.stop()

    async def _stop_locked(self) -> None:
        """停止入站服务器，并在单个清理步骤失败时继续收敛其余资源。"""
        self._accepting_events                       = False
        self._running                                = False
        errors: list[tuple[str, BaseException]]      = []
        caller_cancel: asyncio.CancelledError | None = None
        cancelled_steps: set[str]                    = set()

        async def cleanup_step(name: str, operation: Callable[[], Awaitable[None]]) -> None:
            nonlocal caller_cancel
            try:
                await operation()
            except asyncio.CancelledError as exc:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    caller_cancel = caller_cancel or exc
                    cancelled_steps.add(name)
                else:
                    errors.append((name, exc))
            except BaseException as exc:
                errors.append((name, exc))

        site       = self._site
        self._site = None
        if site is not None:
            await cleanup_step("site", site.stop)

        await cleanup_step("handlers", self._drain_handler_tasks)

        runner       = self._runner
        self._runner = None
        if runner is not None:
            await cleanup_step("runner", runner.cleanup)

        await cleanup_step("WebSocket close tasks", self._drain_ws_close_tasks)
        self._ws_close_tasks = {task for task in self._ws_close_tasks if not task.done()}

        await cleanup_step("WebSocket workers", self._stop_ws_workers)
        self._discard_queued_ws_events()
        if self._owns_event_dispatcher:
            await cleanup_step("event dispatcher", self._stop_owned_dispatcher)
        if not self._ws_close_tasks and not any(
            not task.done() for task in self._active_handler_tasks
        ):
            self._active_sockets.clear()
            self._socket_auth_states.clear()

        failed_steps = {name for name, _ in errors} | cancelled_steps
        if site is not None and "site" in failed_steps:
            self._site = site
        if runner is not None and "runner" in failed_steps:
            self._runner = runner

        if caller_cancel is not None:
            if errors:
                logger.error(
                    "Inbound cleanup also encountered errors during cancellation: %r", errors
                )
            raise caller_cancel from None
        if errors:
            summary = "; ".join(f"{name}: {type(exc).__name__}: {exc}" for name, exc in errors)
            raise RuntimeError(f"inbound server cleanup failed: {summary}") from errors[0][1]

    async def _invoke_handler(
        self,
        payload: dict[str, Any],
        *,
        auth_generation: _InboundAuthState | None = None,
    ) -> list[dict[str, Any]]:
        if not self._accepting_events:
            raise InboundEventUnavailable("inbound server is shutting down")
        task = asyncio.current_task()
        if task is not None:
            self._active_handler_tasks.add(task)
        try:
            guard = (
                None if auth_generation is None else partial(self._auth_is_current, auth_generation)
            )
            return await self._event_dispatcher.dispatch(payload, auth_guard=guard)
        finally:
            if task is not None:
                self._active_handler_tasks.discard(task)

    async def _await_admitted_event(
        self,
        ticket: _InboundEventTicket,
    ) -> list[dict[str, Any]]:
        task = asyncio.current_task()
        if task is not None:
            self._active_handler_tasks.add(task)
        try:
            return await self._event_dispatcher.wait(ticket)
        finally:
            if task is not None:
                self._active_handler_tasks.discard(task)


def _parse_http_base(value: Any) -> tuple[str, int] | None:
    try:
        parts = parse_inbound_listener(value, "http")
    except ValueError:
        return None
    if parts is None or parts.hostname is None or parts.port is None:
        return None
    return parts.hostname, int(parts.port)


def _parse_ws_uri(value: Any, *, default_path: str = "/ws") -> tuple[str, int, str] | None:
    try:
        parts = parse_inbound_listener(value, "ws")
    except ValueError:
        return None
    if parts is None or parts.hostname is None or parts.port is None:
        return None
    path = parts.path or default_path
    return parts.hostname, int(parts.port), path


def _parse_non_negative_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(0, parsed)


def _parse_positive_int(value: Any, *, default: int, min_value: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(int(min_value), parsed)


def _parse_positive_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    if not 0 < parsed <= 300:
        parsed = float(default)
    return parsed


@dataclass(frozen=True, slots=True)
class _InboundManagerSpec:
    inbound_http_base: str
    inbound_ws_uri: str
    ws_max_workers: int
    ws_queue_size: int
    ws_broadcast_timeout_seconds: float
    trusted_tls_proxy: bool

    @property
    def config_key(self) -> tuple[str, str, int, int, float, bool]:
        return (
            self.inbound_http_base,
            self.inbound_ws_uri,
            self.ws_max_workers,
            self.ws_queue_size,
            self.ws_broadcast_timeout_seconds,
            self.trusted_tls_proxy,
        )


def _parse_inbound_manager_spec(
    config: Mapping[str, Any],
    *,
    token: str,
    default_ws_max_workers: int,
    default_ws_queue_size: int,
    default_ws_broadcast_timeout_seconds: float,
) -> _InboundManagerSpec | None:
    if not bool(config.get("enable_inbound_server", True)):
        return None

    inbound_http_base = str(config.get("inbound_http_base", "") or "").strip()
    inbound_ws_uri    = str(config.get("inbound_ws_uri", "") or "").strip()
    if not inbound_http_base and not inbound_ws_uri:
        return None
    if not token.strip():
        raise ValueError("configured inbound listeners require a non-empty inbound token")

    ws_max_workers = _parse_positive_int(
        config.get("inbound_ws_max_workers", default_ws_max_workers),
        default   = default_ws_max_workers,
        min_value = 1,
    )
    ws_queue_size = _parse_non_negative_int(
        config.get("ws_queue_size", default_ws_queue_size),
        default=default_ws_queue_size,
    )
    ws_broadcast_timeout_seconds = _parse_positive_float(
        config.get(
            "inbound_ws_broadcast_timeout_seconds",
            default_ws_broadcast_timeout_seconds,
        ),
        default=default_ws_broadcast_timeout_seconds,
    )
    raw_trusted_tls_proxy = config.get("inbound_trusted_tls_proxy", False)
    if type(raw_trusted_tls_proxy) is not bool:
        raise ValueError("inbound_trusted_tls_proxy must be a boolean")
    trusted_tls_proxy = raw_trusted_tls_proxy
    validate_inbound_listener(
        inbound_http_base,
        "http",
        trusted_tls_proxy=trusted_tls_proxy,
    )
    validate_inbound_listener(
        inbound_ws_uri,
        "ws",
        trusted_tls_proxy=trusted_tls_proxy,
    )
    return _InboundManagerSpec(
        inbound_http_base            = inbound_http_base,
        inbound_ws_uri               = inbound_ws_uri,
        ws_max_workers               = ws_max_workers,
        ws_queue_size                = ws_queue_size,
        ws_broadcast_timeout_seconds = ws_broadcast_timeout_seconds,
        trusted_tls_proxy            = trusted_tls_proxy,
    )


class InboundManager:
    def __init__(
        self,
        *,
        inbound_http_base: str,
        inbound_ws_uri: str,
        token: str,
        handler: Callable[[dict[str, Any]], Awaitable[list[dict[str, Any]]]],
        ws_max_workers: int                 = DEFAULT_INBOUND_WS_MAX_WORKERS,
        ws_queue_size: int                  = DEFAULT_INBOUND_WS_QUEUE_SIZE,
        ws_broadcast_timeout_seconds: float = DEFAULT_INBOUND_WS_BROADCAST_TIMEOUT_SECONDS,
        trusted_tls_proxy: bool             = False,
    ) -> None:
        if type(trusted_tls_proxy) is not bool:
            raise TypeError("trusted_tls_proxy must be a boolean")
        http_parts = validate_inbound_listener(
            inbound_http_base,
            "http",
            trusted_tls_proxy=trusted_tls_proxy,
        )
        ws_parts = validate_inbound_listener(
            inbound_ws_uri,
            "ws",
            trusted_tls_proxy=trusted_tls_proxy,
        )
        non_loopback = any(
            parts is not None and not is_loopback_host(parts.hostname or "")
            for parts in (http_parts, ws_parts)
        )
        if non_loopback and not token.strip():
            raise ValueError("non-loopback inbound listeners require a non-empty inbound token")
        self._inbound_http_base = inbound_http_base
        self._inbound_ws_uri    = inbound_ws_uri
        self._trusted_tls_proxy = trusted_tls_proxy
        self._token             = token
        self._auth_lock         = threading.RLock()
        self._handler           = handler
        self._ws_max_workers    = _parse_positive_int(
            ws_max_workers,
            default   = DEFAULT_INBOUND_WS_MAX_WORKERS,
            min_value = 1,
        )
        self._ws_queue_size = _parse_non_negative_int(
            ws_queue_size,
            default=DEFAULT_INBOUND_WS_QUEUE_SIZE,
        )
        self._ws_broadcast_timeout_seconds = _parse_positive_float(
            ws_broadcast_timeout_seconds,
            default=DEFAULT_INBOUND_WS_BROADCAST_TIMEOUT_SECONDS,
        )

        self.http_server: InboundServer | None                       = None
        self.ws_server: InboundServer | None                         = None
        self._event_dispatcher: _InboundEventDispatcher | None       = None
        self._running                                                = False
        self._lifecycle_lock                                         = _LazyAsyncLock()
        self._status_providers: dict[str, Callable[..., Any] | None] = {
            "plugins_count": None,
            "sessions_count": None,
            "pending_jobs": None,
            "metrics": None,
        }
        if trusted_tls_proxy:
            logger.warning(
                "Inbound non-loopback listeners rely on an explicitly trusted TLS proxy; "
                "direct plaintext access must be blocked"
            )

    @staticmethod
    def config_key_from_config(
        *,
        config: Mapping[str, Any],
        token: str,
        default_ws_max_workers: int                 = DEFAULT_INBOUND_WS_MAX_WORKERS,
        default_ws_queue_size: int                  = DEFAULT_INBOUND_WS_QUEUE_SIZE,
        default_ws_broadcast_timeout_seconds: float = (
            DEFAULT_INBOUND_WS_BROADCAST_TIMEOUT_SECONDS
        ),
    ) -> tuple[str, str, int, int, float, bool] | None:
        """只解析不可变监听器标识，不分配服务器或其他运行资源。"""

        spec = _parse_inbound_manager_spec(
            config,
            token                                = token,
            default_ws_max_workers               = default_ws_max_workers,
            default_ws_queue_size                = default_ws_queue_size,
            default_ws_broadcast_timeout_seconds = default_ws_broadcast_timeout_seconds,
        )
        return None if spec is None else spec.config_key

    @classmethod
    def from_config(
        cls,
        *,
        config: Mapping[str, Any],
        token: str,
        handler: Callable[[dict[str, Any]], Awaitable[list[dict[str, Any]]]],
        default_ws_max_workers: int                 = DEFAULT_INBOUND_WS_MAX_WORKERS,
        default_ws_queue_size: int                  = DEFAULT_INBOUND_WS_QUEUE_SIZE,
        default_ws_broadcast_timeout_seconds: float = (
            DEFAULT_INBOUND_WS_BROADCAST_TIMEOUT_SECONDS
        ),
    ) -> "InboundManager | None":
        spec = _parse_inbound_manager_spec(
            config,
            token                                = token,
            default_ws_max_workers               = default_ws_max_workers,
            default_ws_queue_size                = default_ws_queue_size,
            default_ws_broadcast_timeout_seconds = default_ws_broadcast_timeout_seconds,
        )
        if spec is None:
            if not bool(config.get("enable_inbound_server", True)):
                logger.info("Inbound server disabled")
            else:
                logger.info("Inbound server disabled (inbound_http_base/inbound_ws_uri are empty)")
            return None
        return cls(
            inbound_http_base            = spec.inbound_http_base,
            inbound_ws_uri               = spec.inbound_ws_uri,
            token                        = token,
            handler                      = handler,
            ws_max_workers               = spec.ws_max_workers,
            ws_queue_size                = spec.ws_queue_size,
            ws_broadcast_timeout_seconds = spec.ws_broadcast_timeout_seconds,
            trusted_tls_proxy            = spec.trusted_tls_proxy,
        )

    async def broadcast(self, action: dict[str, Any]) -> BroadcastResult:
        """广播到每个唯一 Inbound server，并汇总可审计的投递结果。"""
        servers: list[InboundServer] = []
        for server in (self.http_server, self.ws_server):
            if server is not None and all(server is not current for current in servers):
                servers.append(server)
        if not servers:
            return BroadcastResult()

        async def broadcast_one(server: InboundServer) -> BroadcastResult:
            try:
                raw_target_count = server.active_ws_connections()
            except asyncio.CancelledError as exc:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                raise _BroadcastFatalError(exc) from exc
            except Exception as exc:
                logger.warning("Unable to inspect inbound broadcast targets: %s", exc)
                target_count = 0
            except BaseException as exc:
                raise _BroadcastFatalError(exc) from exc
            else:
                target_count = (
                    raw_target_count
                    if type(raw_target_count) is int and raw_target_count >= 0
                    else 0
                )
            try:
                result = await server.broadcast(action)
            except asyncio.CancelledError as exc:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                raise _BroadcastFatalError(exc) from exc
            except Exception as exc:
                logger.warning("Inbound server broadcast failed: %s", exc)
                return BroadcastResult(
                    target_count  = target_count,
                    failure_count = target_count,
                )
            except BaseException as exc:
                raise _BroadcastFatalError(exc) from exc
            if not isinstance(result, BroadcastResult):
                logger.error(
                    "Inbound server returned an invalid broadcast result: %r",
                    type(result).__name__,
                )
                return BroadcastResult(
                    target_count  = target_count,
                    failure_count = target_count,
                )
            return result

        tasks = [asyncio.create_task(broadcast_one(server)) for server in servers]
        try:
            results = await asyncio.gather(*tasks)
        except BaseException as exc:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if isinstance(exc, _BroadcastFatalError):
                raise exc.original from None
            raise
        total = BroadcastResult()
        for result in results:
            total += result
        return total

    def active_ws_connections(self) -> int:
        total = 0
        for server in {self.http_server, self.ws_server}:
            if server:
                total += server.active_ws_connections()
        return total

    def has_active_ws_clients(self) -> bool:
        return self.active_ws_connections() > 0

    @property
    def config_key(self) -> tuple[str, str, int, int, float, bool]:
        return (
            self._inbound_http_base,
            self._inbound_ws_uri,
            self._ws_max_workers,
            self._ws_queue_size,
            self._ws_broadcast_timeout_seconds,
            self._trusted_tls_proxy,
        )

    @property
    def binding_ports(self) -> frozenset[int]:
        """Ports this manager would bind, deduplicating a shared HTTP/WS listener."""
        ports: set[int] = set()
        http_parsed     = _parse_http_base(self._inbound_http_base)
        ws_parsed       = _parse_ws_uri(self._inbound_ws_uri)
        if http_parsed is not None:
            ports.add(http_parsed[1])
        if ws_parsed is not None:
            ports.add(ws_parsed[1])
        return frozenset(ports)

    def set_status_providers(
        self,
        plugins_count: Callable[[], int] | None      = None,
        sessions_count: Callable[[], int] | None     = None,
        pending_jobs: Callable[[], int] | None       = None,
        metrics: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._status_providers = {
            "plugins_count": plugins_count,
            "sessions_count": sessions_count,
            "pending_jobs": pending_jobs,
            "metrics": metrics,
        }
        for server in {self.http_server, self.ws_server}:
            if server:
                self._apply_status_providers(server)

    def _apply_status_providers(self, server: InboundServer) -> None:
        server.set_status_providers(**self._status_providers)

    def update_token(self, token: str) -> None:
        with self._auth_lock:
            if token == self._token:
                return
            self._token = token
            for server in {self.http_server, self.ws_server}:
                if server:
                    server.update_token(token)

    async def start(self, *, accept_events: bool = True) -> None:
        """按配置启动全部入站监听器，并与停止流程串行执行。"""
        await _start_inbound_lifecycle(self, accept_events=accept_events)

    async def _start_locked(
        self,
        deferred_cancellation: _DeferredCancellation,
        *,
        accept_events: bool,
    ) -> None:
        if self._running:
            if accept_events:
                self.commit_admission()
            return
        with self._auth_lock:
            if not self._token.strip():
                raise ValueError("inbound manager requires a non-empty inbound token")
        if (
            self.http_server is not None
            or self.ws_server is not None
            or self._event_dispatcher is not None
        ):
            await self._stop_servers()
        try:
            await self._start_servers(accept_events=accept_events)
        except BaseException:
            self._running = False
            cleanup_task  = asyncio.create_task(_run_owned_cleanup(self._stop_servers))
            try:
                await _await_cleanup_task(cleanup_task, deferred_cancellation)
            except BaseException as cleanup_exc:
                logger.exception(
                    "Failed to roll back partially started inbound manager", exc_info=cleanup_exc
                )
            raise
        self._running = True

    async def _start_servers(self, *, accept_events: bool = True) -> None:
        http_parsed = _parse_http_base(self._inbound_http_base)
        ws_parsed   = _parse_ws_uri(self._inbound_ws_uri)

        if not http_parsed and not ws_parsed:
            return

        dispatcher = _InboundEventDispatcher(
            self._handler,
            max_workers           = self._ws_max_workers,
            queue_size            = self._ws_queue_size,
            drain_timeout_seconds = 5.0,
        )
        self._event_dispatcher = dispatcher
        await dispatcher.start()

        if http_parsed and ws_parsed and http_parsed == (ws_parsed[0], ws_parsed[1]):
            host, port = http_parsed
            _, _, path = ws_parsed
            with self._auth_lock:
                published_token = self._token
                server          = InboundServer(
                    host,
                    port,
                    published_token,
                    self._handler,
                    enable_http                  = True,
                    enable_ws                    = True,
                    ws_path                      = path,
                    ws_max_workers               = self._ws_max_workers,
                    ws_queue_size                = self._ws_queue_size,
                    ws_broadcast_timeout_seconds = self._ws_broadcast_timeout_seconds,
                    trusted_tls_proxy            = self._trusted_tls_proxy,
                    event_dispatcher             = dispatcher,
                )
                if published_token != self._token:
                    server.update_token(self._token)
                self._apply_status_providers(server)
                self.http_server = server
                self.ws_server   = server
            await server.start(accept_events=False)
            if accept_events:
                server.commit_admission()
            return

        if http_parsed:
            host, port = http_parsed
            with self._auth_lock:
                published_token = self._token
                server          = InboundServer(
                    host,
                    port,
                    published_token,
                    self._handler,
                    enable_http                  = True,
                    enable_ws                    = False,
                    ws_broadcast_timeout_seconds = self._ws_broadcast_timeout_seconds,
                    trusted_tls_proxy            = self._trusted_tls_proxy,
                    event_dispatcher             = dispatcher,
                )
                if published_token != self._token:
                    server.update_token(self._token)
                self._apply_status_providers(server)
                self.http_server = server
            await server.start(accept_events=False)

        if ws_parsed:
            host, port, path = ws_parsed
            with self._auth_lock:
                published_token = self._token
                server          = InboundServer(
                    host,
                    port,
                    published_token,
                    self._handler,
                    enable_http                  = False,
                    enable_ws                    = True,
                    ws_path                      = path,
                    ws_max_workers               = self._ws_max_workers,
                    ws_queue_size                = self._ws_queue_size,
                    ws_broadcast_timeout_seconds = self._ws_broadcast_timeout_seconds,
                    trusted_tls_proxy            = self._trusted_tls_proxy,
                    event_dispatcher             = dispatcher,
                )
                if published_token != self._token:
                    server.update_token(self._token)
                self._apply_status_providers(server)
                self.ws_server = server
            await server.start(accept_events=False)

        # Separate HTTP and WS listeners share one dispatcher and become
        # externally admissible only after both binds have succeeded.  There
        # is therefore no half-published manager generation.
        if accept_events:
            self.commit_admission()

    def commit_admission(self) -> None:
        """Publish every staged child listener in one non-awaiting loop turn."""

        if not self._running and self.http_server is None and self.ws_server is None:
            raise RuntimeError("cannot commit admission before inbound manager start")
        for server in {self.http_server, self.ws_server}:
            if server is not None:
                server.commit_admission()

    async def stop(self) -> None:
        async with self._lifecycle_lock.get():
            try:
                await self._stop_servers()
            except asyncio.CancelledError:
                raise
            except Exception:
                raise
            except BaseException as exc:
                raise InboundLifecycleFatalError(exc) from None

    async def _stop_servers(self) -> None:
        with self._auth_lock:
            original_http    = self.http_server
            original_ws      = self.ws_server
            self.http_server = None
            self.ws_server   = None
        dispatcher                   = self._event_dispatcher
        servers: list[InboundServer] = []
        for server in (original_http, original_ws):
            if server is not None and all(server is not current for current in servers):
                servers.append(server)
        self._running                                = False
        errors: list[BaseException]                  = []
        failed_servers: list[InboundServer]          = []
        dispatcher_failed                            = False
        caller_cancel: asyncio.CancelledError | None = None
        for server in servers:
            try:
                await server.stop()
            except asyncio.CancelledError as exc:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    caller_cancel = caller_cancel or exc
                else:
                    errors.append(exc)
                failed_servers.append(server)
            except BaseException as exc:
                errors.append(exc)
                failed_servers.append(server)
                logger.exception("Failed to stop one inbound server", exc_info=exc)
        if dispatcher is not None:
            try:
                await dispatcher.stop()
            except asyncio.CancelledError as exc:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    caller_cancel = caller_cancel or exc
                else:
                    errors.append(exc)
                dispatcher_failed = True
            except BaseException as exc:
                errors.append(exc)
                dispatcher_failed = True
                logger.exception("Failed to stop shared inbound event dispatcher", exc_info=exc)
        restored_servers: list[InboundServer] = []
        with self._auth_lock:
            if original_http is not None and any(
                original_http is failed for failed in failed_servers
            ):
                self.http_server = original_http
                restored_servers.append(original_http)
            if original_ws is not None and any(original_ws is failed for failed in failed_servers):
                self.ws_server = original_ws
                if all(original_ws is not server for server in restored_servers):
                    restored_servers.append(original_ws)
            # Linearize restoration with update_token.  An update either sees
            # the restored holders itself, or completes first and this block
            # reapplies its newest token before publishing the old children.
            restored_token = self._token
            for server in restored_servers:
                try:
                    server.update_token(restored_token)
                except BaseException as exc:
                    errors.append(exc)
                    logger.exception(
                        "Failed to restore current auth on a partially stopped inbound server",
                        exc_info=exc,
                    )
        # A partially stopped child still points at this shared generation.
        # Retain manager ownership until every child and the dispatcher itself
        # converge, so retry cannot create a second dispatcher alongside it.
        if failed_servers or dispatcher_failed:
            self._event_dispatcher = dispatcher
        else:
            self._event_dispatcher = None
        if caller_cancel is not None:
            if errors:
                logger.error(
                    "Inbound manager cleanup also encountered errors during cancellation: %r",
                    errors,
                )
            raise caller_cancel from None
        if errors:
            summary = "; ".join(f"{type(exc).__name__}: {exc}" for exc in errors)
            raise RuntimeError(f"inbound manager cleanup failed: {summary}") from errors[0]
