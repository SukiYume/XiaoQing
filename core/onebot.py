"""
OneBot 协议支持

提供 HTTP 发送器和 WebSocket 客户端。
"""

import asyncio
import json
import logging
import math
import random
import re
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp

from .bounded_http import (
    BodyLimits,
    JsonLimits,
    MimePolicy,
    aiohttp_request_bounded,
    parse_bounded_json,
)
from .constants import (
    DEFAULT_ONEBOT_HTTP_TIMEOUT_SECONDS,
    DEFAULT_ONEBOT_WS_ACTION_TIMEOUT_SECONDS,
    MAX_SHORT_TEXT_LENGTH,
)
from .interfaces import ACTION_RESULT_MESSAGE_ID_KEY
from .lifecycle import FatalErrorCarrier as _FatalErrorCarrier
from .lifecycle import LazyAsyncLock as _LazyAsyncLock
from .safe_http import redact_url_for_log

logger = logging.getLogger(__name__)


class OneBotActionOutcomeUnknown(RuntimeError):
    """A committed WebSocket action may have run, but no final response is known."""

    def __init__(self, action_name: str) -> None:
        self.action_name = action_name
        super().__init__(f"OneBot WebSocket action outcome is unknown: {action_name}")


@dataclass(frozen=True, slots=True)
class _EndpointAuthState:
    endpoint: str
    token: str
    generation: int = 0
    credentials_trusted: bool = True

    def __post_init__(self) -> None:
        if type(self.endpoint) is not str or type(self.token) is not str:
            raise TypeError("OneBot endpoint and token must be strings")
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("OneBot auth generation must be a non-negative integer")
        if type(self.credentials_trusted) is not bool:
            raise TypeError("OneBot credential trust flag must be a boolean")


@dataclass(frozen=True, slots=True)
class _ConnectionAttemptResult:
    """Connected lifetime plus the reason the listening phase ended."""

    connected_seconds: float
    error: Exception | None = None


@dataclass(frozen=True, slots=True)
class _QueuedOneBotEvent:
    """One event together with the connection generation that admitted it."""

    event: dict[str, Any]
    auth_state: _EndpointAuthState


@dataclass(frozen=True, slots=True)
class _OneBotEventCommit:
    """Linearization token proving an event was admitted before auth rotation."""

    auth_state: _EndpointAuthState


@dataclass(frozen=True, slots=True)
class _OneBotActionCommit:
    """Linearization token for one action committed to a specific connection."""

    auth_state: _EndpointAuthState
    ws: Any
    echo: str


_ONEBOT_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=DEFAULT_ONEBOT_HTTP_TIMEOUT_SECONDS)
_ONEBOT_BODY_LIMITS = BodyLimits(
    max_wire_bytes=2 * 1024 * 1024,
    max_decoded_bytes=2 * 1024 * 1024,
    max_decompression_ratio=20,
)
_ONEBOT_JSON_LIMITS = JsonLimits(
    max_bytes=2 * 1024 * 1024,
    max_depth=32,
    max_nodes=20_000,
    max_string_chars=512_000,
)
_ONEBOT_JSON_MIME_POLICY = MimePolicy(
    exact=frozenset({"application/json", "text/json", "text/plain"}),
    structured_suffixes=frozenset({"+json"}),
    allow_missing=True,
)
_ONEBOT_SUCCESS_STATUSES = range(200, 300)

_SENSITIVE_KEYS = {"token", "appid", "api_key", "secret", "password", "authorization"}

# 预编译敏感信息匹配模式（避免每次调用都重新编译）
_SENSITIVE_PATTERNS = [
    re.compile(rf"({key}\s*[:=]\s*)([^\s,;]+)", re.IGNORECASE) for key in _SENSITIVE_KEYS
]
_CONNECT_SIGNATURE_CACHE: dict[Any, tuple[Any, frozenset[str]]] = {}
_ONEBOT_WS_SHUTDOWN_TIMEOUT_SECONDS = 5.0
_ONEBOT_WS_RECONNECT_INITIAL_SECONDS = 5.0
_ONEBOT_WS_RECONNECT_MAX_SECONDS = 60.0
_ONEBOT_WS_RECONNECT_STABLE_SECONDS = 30.0
_ONEBOT_WS_RECONNECT_JITTER_RATIO = 0.2
_ONEBOT_WS_ROTATION_CANCEL_GRACE_SECONDS = 0.1


class _OneBotChildFatalError(_FatalErrorCarrier):
    """Task-safe carrier for a WebSocket child's non-Exception failure."""


class _UnsupportedWebSocketAuthentication(RuntimeError):
    """The installed client cannot prove that it will carry the configured token."""


class _RevokedWebSocketAuthentication(RuntimeError):
    """The credential source is unavailable and network access is revoked."""


async def _run_onebot_child(operation_factory: Callable[[], Awaitable[Any]]) -> Any:
    try:
        return await operation_factory()
    except asyncio.CancelledError:
        raise
    except Exception:
        raise
    except BaseException as exc:
        raise _OneBotChildFatalError(exc) from None


def _mask_sensitive_text(text: str) -> str:
    masked = text
    for pattern in _SENSITIVE_PATTERNS:
        masked = pattern.sub(r"\1********", masked)
    return masked


def _extract_message_preview(message: Any, max_len: int = MAX_SHORT_TEXT_LENGTH) -> str:
    """Return a bounded, redacted preview without raising on malformed input."""
    if not message:
        return "(empty)"
    if isinstance(message, str):
        text = _mask_sensitive_text(message)
        return text[:max_len] + ("..." if len(text) > max_len else "")
    if not isinstance(message, list):
        return "[invalid-message]"

    parts: list[str] = []
    for seg in message:
        if not isinstance(seg, Mapping):
            parts.append("[invalid-segment]")
            continue
        seg_type = str(seg.get("type", "") or "")
        raw_data = seg.get("data", {})
        data = raw_data if isinstance(raw_data, Mapping) else {}
        if seg_type == "text":
            parts.append(_mask_sensitive_text(str(data.get("text", "") or "")))
        elif seg_type == "emoji":
            parts.append("[表情包]")
        elif seg_type == "image":
            subtype = str(data.get("sub_type", "") or "").strip().lower()
            parts.append("[表情包]" if subtype == "emoji" else "[图片]")
        elif seg_type == "at":
            parts.append(f"[@{data.get('qq', '')}]")
        else:
            parts.append(f"[{seg_type or 'unknown'}]")

    text = "".join(parts)
    if len(text) > max_len:
        text = text[:max_len] + "..."
    return text


def _normalize_segment_for_onebot(seg: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(seg, Mapping):
        raise TypeError("OneBot message segments must be mappings")
    raw_type = seg.get("type")
    if type(raw_type) is not str or not raw_type.strip():
        raise ValueError("OneBot message segment type must be a non-empty string")
    seg_type = raw_type.strip()
    raw_data = seg.get("data", {})
    if raw_data is None:
        raw_data = {}
    if not isinstance(raw_data, Mapping):
        raise TypeError("OneBot message segment data must be a mapping")
    data = dict(raw_data)
    if seg_type == "text":
        text = data.get("text", "")
        if text is None:
            text = ""
        if not isinstance(text, (str, int, float, bool)):
            raise TypeError("OneBot text segment content must be a scalar")
        data["text"] = str(text)
    normalized = dict(seg)
    normalized["type"] = seg_type
    normalized["data"] = data
    if seg_type != "emoji":
        return normalized

    normalized_data = {"sub_type": "emoji", **data}
    return {"type": "image", "data": normalized_data}


def _normalize_action_for_onebot(action: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(action, dict):
        raise TypeError("OneBot action must be a mapping")
    action_name = action.get("action")
    if type(action_name) is not str or not action_name.strip():
        raise ValueError("OneBot action name must be a non-empty string")
    params = dict(action.get("params", {}) or {})
    message = params.get("message")
    if isinstance(message, list):
        params["message"] = [_normalize_segment_for_onebot(segment) for segment in message]
    elif message is not None and not isinstance(message, str):
        raise TypeError("OneBot message must be text or a segment list")
    normalized = dict(action)
    normalized["action"] = action_name.strip()
    normalized["params"] = params
    return normalized


def _onebot_action_succeeded(response: Any) -> bool:
    """仅在 OneBot 回执明确给出整数零状态码时确认业务成功。"""
    return (
        isinstance(response, dict)
        and response.get("status") == "ok"
        and type(response.get("retcode")) is int
        and response["retcode"] == 0
    )


def _finalize_onebot_action(
    transport: str,
    action: dict[str, Any],
    response: dict[str, Any] | None,
) -> bool:
    """统一处理两种传输的业务回执、消息 ID 回写与脱敏日志。"""

    raw_params = action.get("params", {})
    params = raw_params if isinstance(raw_params, Mapping) else {}
    target = params.get("group_id") or params.get("user_id")
    message = params.get("message", [])
    action_name = str(action.get("action", "unknown") or "unknown").strip()
    if _onebot_action_succeeded(response):
        data = response.get("data") if isinstance(response, dict) else None
        message_id = data.get("message_id") if isinstance(data, dict) else None
        if message_id not in (None, ""):
            action[ACTION_RESULT_MESSAGE_ID_KEY] = message_id
        logger.info(
            "[%s] Sent %s to %s: %s",
            transport,
            action_name,
            target,
            _extract_message_preview(message),
        )
        return True
    if response is not None:
        logger.warning(
            "[%s] OneBot rejected %s to %s (status=%r retcode=%r)",
            transport,
            action_name,
            target,
            response.get("status"),
            response.get("retcode"),
        )
    return False


def _summarize_event(event: dict[str, Any]) -> str:
    post_type = event.get("post_type")
    message_type = event.get("message_type")
    user_id = event.get("user_id")
    group_id = event.get("group_id")
    message = event.get("message")
    message_kind = type(message).__name__
    message_len = len(message) if isinstance(message, list) else None
    return (
        f"post_type={post_type} message_type={message_type} "
        f"user_id={user_id} group_id={group_id} "
        f"message_kind={message_kind} message_len={message_len}"
    )


def _get_connect_signature(websockets_module) -> frozenset[str]:
    """Return explicitly supported ``websockets.connect`` parameters.

    Inspection failure intentionally returns no parameters.  Guessing both
    historical header names would be unsafe because a configured token could
    otherwise be silently omitted by an adapter or compatibility wrapper.
    """
    import inspect

    connect_func = websockets_module.connect
    try:
        hash(connect_func)
    except TypeError:
        cache_key: Any = ("unhashable-connect", id(connect_func))
        require_identity = True
    else:
        # Bound methods compare and hash by (instance, function), so repeated
        # descriptor access reuses one cache entry rather than leaking entries.
        cache_key = connect_func
        require_identity = False
    cached = _CONNECT_SIGNATURE_CACHE.get(cache_key)
    if cached is not None and (not require_identity or cached[0] is connect_func):
        return cached[1]

    try:
        sig = inspect.signature(connect_func)
        keyword_kinds = {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
        result = frozenset(
            name for name, parameter in sig.parameters.items() if parameter.kind in keyword_kinds
        )
    except Exception:
        result = frozenset()
    # Retaining the callable alongside its id prevents a recycled id from
    # reusing another connector's security-sensitive signature decision.
    _CONNECT_SIGNATURE_CACHE[cache_key] = (connect_func, result)
    return result


def _load_websockets_module():
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("websockets module is required for the OneBot WS client") from exc
    return websockets


def _select_connect_header_parameter(websockets_module, token: str) -> str | None:
    """Select a proven authentication-header keyword or fail closed."""

    if not token:
        return None
    parameters = _get_connect_signature(websockets_module)
    if "additional_headers" in parameters:
        return "additional_headers"
    if "extra_headers" in parameters:
        return "extra_headers"
    raise _UnsupportedWebSocketAuthentication(
        "configured OneBot WebSocket authentication cannot be sent because "
        "the installed websockets.connect signature exposes neither "
        "additional_headers nor extra_headers"
    )


def _jittered_reconnect_delay(base_delay: float, random_sample: float) -> float:
    """Apply continuous bounded jitter, including at the maximum backoff."""

    sample = min(1.0, max(0.0, float(random_sample)))
    bounded_base = min(_ONEBOT_WS_RECONNECT_MAX_SECONDS, max(0.0, float(base_delay)))
    low = bounded_base * (1.0 - _ONEBOT_WS_RECONNECT_JITTER_RATIO)
    high = min(
        _ONEBOT_WS_RECONNECT_MAX_SECONDS,
        bounded_base * (1.0 + _ONEBOT_WS_RECONNECT_JITTER_RATIO),
    )
    return low + ((high - low) * sample)


class OneBotHttpSender:
    """OneBot HTTP 发送器"""

    def __init__(
        self,
        http_base: str,
        auth_token: str,
        session: aiohttp.ClientSession,
        *,
        credentials_trusted: bool = True,
    ) -> None:
        self._endpoint_auth = _EndpointAuthState(
            http_base.rstrip("/"),
            auth_token,
            credentials_trusted=credentials_trusted,
        )
        self.session = session

    @property
    def http_base(self) -> str:
        return self._endpoint_auth.endpoint

    @property
    def auth_token(self) -> str:
        return self._endpoint_auth.token

    @property
    def credentials_trusted(self) -> bool:
        return self._endpoint_auth.credentials_trusted

    def update(
        self,
        http_base: str,
        auth_token: str,
        *,
        credentials_trusted: bool = True,
    ) -> None:
        """更新配置"""
        self._endpoint_auth = _EndpointAuthState(
            http_base.rstrip("/"),
            auth_token,
            credentials_trusted=credentials_trusted,
        )

    async def request_action(self, action: dict[str, Any]) -> dict[str, Any] | None:
        """Send an action and return its parsed OneBot response envelope."""

        normalized_action = _normalize_action_for_onebot(action)
        endpoint_auth = self._endpoint_auth
        if not endpoint_auth.endpoint or not endpoint_auth.credentials_trusted:
            return None

        url = f"{endpoint_auth.endpoint}/{normalized_action['action']}"
        headers = {"Authorization": f"Bearer {endpoint_auth.token}"} if endpoint_auth.token else {}
        params = normalized_action.get("params", {})

        try:
            bounded_response = await aiohttp_request_bounded(
                self.session,
                "POST",
                url,
                limits=_ONEBOT_BODY_LIMITS,
                mime_policy=_ONEBOT_JSON_MIME_POLICY,
                success_statuses=_ONEBOT_SUCCESS_STATUSES,
                headers=headers,
                request_kwargs={"json": params, "timeout": _ONEBOT_HTTP_TIMEOUT},
            )
            response = parse_bounded_json(bounded_response, limits=_ONEBOT_JSON_LIMITS)
            return response if isinstance(response, dict) else None
        except Exception as exc:
            logger.warning("[HTTP] OneBot request failed error_type=%s", type(exc).__name__)
            return None

    async def send_action(self, action: dict[str, Any]) -> bool | None:
        """Return accepted, rejected/not-sent, or committed with unknown outcome."""

        endpoint_auth = self._endpoint_auth
        if not endpoint_auth.endpoint or not endpoint_auth.credentials_trusted:
            return False
        response = await self.request_action(action)
        if response is None:
            return None
        return _finalize_onebot_action("HTTP", action, response)


class OneBotWsClient:
    """OneBot WebSocket 客户端"""

    def __init__(
        self,
        ws_uri: str,
        auth_token: str,
        max_pending_events: int = 100,
        queue_size: int = 100,
        queue_ttl_seconds: float = 300.0,
        queue_cleanup_interval: float = 60.0,
        action_response_timeout_seconds: float = DEFAULT_ONEBOT_WS_ACTION_TIMEOUT_SECONDS,
        *,
        credentials_trusted: bool = True,
    ) -> None:
        if type(max_pending_events) is not int or max_pending_events <= 0:
            raise ValueError("max_pending_events must be a positive integer")
        if type(queue_size) is not int or queue_size < 0:
            raise ValueError("queue_size must be a non-negative integer")
        for name, value in (
            ("queue_ttl_seconds", queue_ttl_seconds),
            ("queue_cleanup_interval", queue_cleanup_interval),
            ("action_response_timeout_seconds", action_response_timeout_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive finite number")
        self._endpoint_auth = _EndpointAuthState(
            ws_uri,
            auth_token,
            credentials_trusted=credentials_trusted,
        )
        self._auth_state_lock = threading.RLock()
        self._connected_auth_generation: int | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any | None = None
        self._running = False
        self._accepting_events = True
        self._on_connect: Callable[[], Awaitable[None]] | None = None
        self._message_queues: dict[
            str,
            asyncio.Queue[_QueuedOneBotEvent | dict[str, Any]],
        ] = {}
        self._queue_tasks: dict[str, asyncio.Task[None]] = {}
        self._queue_last_activity: dict[str, float] = {}
        self._queue_size = queue_size
        self._queue_ttl_seconds = float(queue_ttl_seconds)
        self._queue_cleanup_interval = float(queue_cleanup_interval)
        self._pending_semaphore = asyncio.Semaphore(max_pending_events)
        self._cleanup_task: asyncio.Task[None] | None = None
        self._main_task: asyncio.Task[None] | None = None
        self._connection_attempt_tasks: set[asyncio.Task[Any]] = set()
        self._quarantined_connection_attempt_tasks: set[asyncio.Task[Any]] = set()
        self._ws_close_tasks: dict[int, tuple[Any, asyncio.Task[Any]]] = {}
        self._stop_lock = _LazyAsyncLock()
        self._shutdown_timeout_seconds = _ONEBOT_WS_SHUTDOWN_TIMEOUT_SECONDS
        self._reconnect_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        self._reconnect_random: Callable[[], float] = random.random
        self._reconnect_wakeup: asyncio.Event | None = None
        self._pending_action_futures: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._pending_action_auth_states: dict[str, _EndpointAuthState] = {}
        self._action_response_timeout_seconds = max(
            0.1,
            float(action_response_timeout_seconds),
        )

    def set_on_connect(self, callback: Callable[[], Awaitable[None]]) -> None:
        """设置连接成功回调"""
        self._on_connect = callback

    @property
    def ws_uri(self) -> str:
        return self._endpoint_auth.endpoint

    @property
    def auth_token(self) -> str:
        return self._endpoint_auth.token

    @property
    def credentials_trusted(self) -> bool:
        return self._endpoint_auth.credentials_trusted

    def _schedule_ws_close(self, ws: Any) -> asyncio.Task[Any]:
        """Own one close task per exact socket so rotations cannot mask each other."""

        key = id(ws)
        existing = self._ws_close_tasks.get(key)
        if existing is not None and existing[0] is ws:
            existing_task = existing[1]
            if not existing_task.done():
                return existing_task
            try:
                existing_task.result()
            except BaseException:
                # A later lifecycle attempt may retry the exact failed close.
                self._ws_close_tasks.pop(key, None)
            else:
                self._ws_close_tasks.pop(key, None)
                return existing_task

        task = asyncio.create_task(_run_onebot_child(ws.close))
        self._ws_close_tasks[key] = (ws, task)

        def observe_close_result(done: asyncio.Task[Any]) -> None:
            entry = self._ws_close_tasks.get(key)
            if entry is None or entry[0] is not ws or entry[1] is not done:
                return
            try:
                done.result()
            except asyncio.CancelledError:
                logger.warning("OneBot WebSocket close task was cancelled before convergence")
            except BaseException as exc:
                # Retrieve the failure immediately to avoid an orphaned task,
                # but retain ownership so stop or a later rotation can retry.
                logger.warning("OneBot WebSocket close failed: %s", exc)
            else:
                self._ws_close_tasks.pop(key, None)
                with self._auth_state_lock:
                    if self._ws is ws:
                        self._ws = None
                        self._connected_auth_generation = None

        task.add_done_callback(observe_close_result)
        return task

    def _reap_successful_ws_close_tasks(self) -> None:
        """Remove completed successful closes while retaining failures for retry."""

        for key, (ws, task) in tuple(self._ws_close_tasks.items()):
            if not task.done():
                continue
            try:
                task.result()
            except BaseException:
                continue
            entry = self._ws_close_tasks.get(key)
            if entry is not None and entry[0] is ws and entry[1] is task:
                self._ws_close_tasks.pop(key, None)

    def _prepare_ws_closes_for_stop(self) -> None:
        """Retry completed failed closes and start closing the current socket."""

        for owned_ws, close_task in tuple(self._ws_close_tasks.values()):
            if close_task.done():
                self._schedule_ws_close(owned_ws)
        ws = self._ws
        if ws is not None:
            self._schedule_ws_close(ws)

    def _track_connection_attempt(self, task: asyncio.Task[Any]) -> None:
        self._connection_attempt_tasks.add(task)

        def observe_attempt(done: asyncio.Task[Any]) -> None:
            self._connection_attempt_tasks.discard(done)
            was_quarantined = done in self._quarantined_connection_attempt_tasks
            self._quarantined_connection_attempt_tasks.discard(done)
            try:
                error = done.exception()
            except asyncio.CancelledError:
                pass
            else:
                if was_quarantined and error is not None:
                    logger.warning(
                        "Quarantined OneBot connection attempt ended with %s",
                        type(error).__name__,
                    )

        task.add_done_callback(observe_attempt)

    async def _cancel_connection_attempts(
        self,
        timeout: float,
    ) -> tuple[set[asyncio.Task[Any]], list[BaseException]]:
        tasks = set(self._connection_attempt_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if not tasks:
            return set(), []
        done, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout))
        errors: list[BaseException] = []
        for task in done:
            self._connection_attempt_tasks.discard(task)
            self._quarantined_connection_attempt_tasks.discard(task)
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                errors.append(exc)
        return pending, errors

    async def _await_connection_attempt(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
        reconnect_wakeup: asyncio.Event | None,
    ) -> Any:
        """Race one connection generation against auth/endpoint rotation."""

        attempt_task = asyncio.create_task(self._connect_once(handler))
        self._track_connection_attempt(attempt_task)
        if reconnect_wakeup is None:
            return await attempt_task

        wake_task = asyncio.create_task(reconnect_wakeup.wait())
        try:
            done, _ = await asyncio.wait(
                {attempt_task, wake_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if attempt_task in done:
                return attempt_task.result()

            # A revoked connection may have a close implementation that fails
            # or ignores cancellation.  Give it a short grace period, retain
            # ownership if it remains stuck, and let the new generation start.
            attempt_task.cancel()
            attempt_done, _ = await asyncio.wait(
                {attempt_task},
                timeout=_ONEBOT_WS_ROTATION_CANCEL_GRACE_SECONDS,
            )
            if attempt_done:
                try:
                    attempt_task.result()
                except asyncio.CancelledError:
                    pass
                except BaseException as exc:
                    logger.warning(
                        "Revoked OneBot connection attempt ended with %s",
                        type(exc).__name__,
                    )
            else:
                self._quarantined_connection_attempt_tasks.add(attempt_task)
                logger.error(
                    "Revoked OneBot connection attempt ignored cancellation; "
                    "it remains quarantined for bounded shutdown"
                )
            return _ConnectionAttemptResult(0.0)
        except asyncio.CancelledError:
            if not attempt_task.done():
                attempt_task.cancel()
            raise
        finally:
            if not wake_task.done():
                wake_task.cancel()
            await asyncio.gather(wake_task, return_exceptions=True)

    def update(
        self,
        ws_uri: str,
        auth_token: str,
        *,
        credentials_trusted: bool = True,
    ) -> None:
        """更新配置

        Args:
            ws_uri: WebSocket URI
            auth_token: 认证 token（用于 Bearer 认证）

        Note:
            Token 验证在服务器端进行，客户端负责正确携带
        """
        with self._auth_state_lock:
            previous_state = self._endpoint_auth
            if (ws_uri, auth_token, credentials_trusted) == (
                previous_state.endpoint,
                previous_state.token,
                previous_state.credentials_trusted,
            ):
                return
            ws = self._ws
            if ws is not None and self._connected_auth_generation is None:
                self._connected_auth_generation = previous_state.generation
            self._endpoint_auth = _EndpointAuthState(
                ws_uri,
                auth_token,
                previous_state.generation + 1,
                credentials_trusted,
            )
        self._request_auth_rotation(previous_state, ws)

    def _request_auth_rotation(
        self,
        previous_state: _EndpointAuthState,
        ws: Any | None,
    ) -> None:
        loop = self._event_loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
        if loop.is_closed():
            return

        def invalidate_previous_generation() -> None:
            self._fail_pending_action_futures(
                "WebSocket authorization changed",
                auth_state=previous_state,
            )
            reconnect_wakeup = self._reconnect_wakeup
            if reconnect_wakeup is not None:
                reconnect_wakeup.set()
            if ws is not None:
                self._schedule_ws_close(ws)

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            invalidate_previous_generation()
        else:
            try:
                loop.call_soon_threadsafe(invalidate_previous_generation)
            except RuntimeError:
                # The loop can close after ``is_closed`` above.  The atomic
                # generation swap already rejects the old connection; clear
                # its local publication as the only thread-safe convergence
                # available once its owning loop can no longer run callbacks.
                with self._auth_state_lock:
                    if self._ws is ws and self._endpoint_auth is not previous_state:
                        self._ws = None
                        self._connected_auth_generation = None
                logger.debug("OneBot auth rotation raced with event-loop shutdown")

    def _connected_target(self) -> tuple[Any, _EndpointAuthState] | None:
        """Snapshot an internally current connection, then inspect it without the auth lock."""

        with self._auth_state_lock:
            ws = self._ws
            auth_state = self._endpoint_auth
            connected_auth_generation = self._connected_auth_generation
        if ws is None:
            return None
        if not auth_state.credentials_trusted:
            return None
        if (
            connected_auth_generation is not None
            and connected_auth_generation != auth_state.generation
        ):
            return None
        try:
            if getattr(ws, "closed", False) is True:
                return None
            close_code = getattr(ws, "close_code", None)
            if isinstance(close_code, int):
                return None
            state = getattr(ws, "state", None)
            state_name = getattr(state, "name", "")
            if isinstance(state_name, str) and state_name.upper() in {"CLOSING", "CLOSED"}:
                return None
        except Exception as exc:
            logger.debug("Unable to inspect OneBot WebSocket state: %s", exc)
            return None
        return ws, auth_state

    def connected(self) -> bool:
        """是否已连接"""

        return self._connected_target() is not None

    async def request_action(self, action: dict[str, Any]) -> dict[str, Any] | None:
        """Send an action and return the response matched by its ``echo``."""

        current_loop = asyncio.get_running_loop()
        normalized_action = _normalize_action_for_onebot(action)
        action_name = str(normalized_action.get("action", "unknown") or "unknown")
        echo = f"xiaoqing-{uuid.uuid4().hex}"
        request = dict(normalized_action)
        request["echo"] = echo
        payload = json.dumps(request, ensure_ascii=False)
        connected_target = self._connected_target()
        if connected_target is None:
            return None
        candidate_ws, candidate_auth_state = connected_target
        response_future: asyncio.Future[dict[str, Any]] = current_loop.create_future()
        commit: _OneBotActionCommit | None = None
        try:
            # Token creation is the action's linearization point.  No external
            # WebSocket code runs while this threading lock is held: rotation
            # before this point rejects the claim; rotation afterwards cannot
            # make a possibly-sent action safe to retry on another transport.
            with self._auth_state_lock:
                if self._event_loop is None:
                    self._event_loop = current_loop
                if (
                    self._ws is not candidate_ws
                    or self._endpoint_auth is not candidate_auth_state
                    or self._connected_auth_generation
                    not in (None, candidate_auth_state.generation)
                ):
                    commit = None
                else:
                    self._pending_action_futures[echo] = response_future
                    self._pending_action_auth_states[echo] = candidate_auth_state
                    commit = _OneBotActionCommit(
                        auth_state=candidate_auth_state,
                        ws=candidate_ws,
                        echo=echo,
                    )
            if commit is None:
                return None

            await commit.ws.send(payload)
            response = await asyncio.wait_for(
                asyncio.shield(response_future),
                timeout=self._action_response_timeout_seconds,
            )
            return response if isinstance(response, dict) else None
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            logger.warning(
                "[WS] Timed out waiting for OneBot response to %s",
                action_name,
            )
            raise OneBotActionOutcomeUnknown(action_name) from exc
        except Exception as exc:
            logger.warning("[WS] Send failed: %s", exc)
            with self._auth_state_lock:
                if commit is not None and self._ws is commit.ws:
                    self._ws = None
            self._fail_pending_action_futures(
                "WebSocket send failed",
                exclude=response_future,
                auth_state=commit.auth_state if commit is not None else None,
            )
            if commit is None:
                return None
            raise OneBotActionOutcomeUnknown(action_name) from exc
        finally:
            if self._pending_action_futures.get(echo) is response_future:
                self._pending_action_futures.pop(echo, None)
                self._pending_action_auth_states.pop(echo, None)
            if not response_future.done():
                response_future.cancel()
            elif not response_future.cancelled():
                # A same-loop re-entrant rotation can fail the future before
                # this coroutine starts waiting for it.  Retrieve that error so
                # the loop does not report an orphaned Future exception.
                try:
                    response_future.exception()
                except BaseException:
                    pass

    async def send_action(self, action: dict[str, Any]) -> bool:
        """Send an action and return whether the matched response accepted it."""

        response = await self.request_action(action)
        return _finalize_onebot_action("WS", action, response)

    @staticmethod
    def _is_action_response(event: dict[str, Any]) -> bool:
        return "echo" in event and ("status" in event or "retcode" in event)

    def _resolve_action_response(
        self,
        event: dict[str, Any],
        *,
        auth_state: _EndpointAuthState | None = None,
    ) -> bool:
        """Resolve a pending action by echo; action responses never reach plugins."""
        if not self._is_action_response(event):
            return False
        echo = event.get("echo")
        if not isinstance(echo, str):
            return True
        if auth_state is None:
            # An action envelope without its admitting connection generation is
            # still consumed, but must never resolve a pending privileged call.
            return True
        # Serialize the generation check with ``update``.  Either this response
        # linearizes before a credential rotation, or the old connection is
        # already stale and cannot complete a pending action from that point on.
        with self._auth_state_lock:
            if auth_state is not self._endpoint_auth:
                return True
            future = self._pending_action_futures.get(echo)
            pending_state = self._pending_action_auth_states.get(echo)
            if future is not None and not future.done() and pending_state is auth_state:
                future.set_result(event)
        return True

    def _fail_pending_action_futures(
        self,
        reason: str,
        *,
        exclude: asyncio.Future[dict[str, Any]] | None = None,
        auth_state: _EndpointAuthState | None = None,
    ) -> None:
        pending: list[asyncio.Future[dict[str, Any]]] = []
        for echo, future in tuple(self._pending_action_futures.items()):
            pending_state = self._pending_action_auth_states.get(echo)
            if auth_state is not None and pending_state not in (None, auth_state):
                continue
            if self._pending_action_futures.get(echo) is future:
                self._pending_action_futures.pop(echo, None)
                self._pending_action_auth_states.pop(echo, None)
                pending.append(future)
        for future in pending:
            if future is not exclude and not future.done():
                future.set_exception(ConnectionError(reason))

    async def _wait_for_reconnect(self, delay: float) -> bool:
        """Wait for the backoff timer or a hot-reload wakeup.

        Returns ``True`` when an endpoint/auth generation change should retry
        immediately.  Both owned child tasks are always cancelled and drained
        before this method returns or propagates cancellation.
        """

        reconnect_wakeup = self._reconnect_wakeup
        if reconnect_wakeup is None:
            await self._reconnect_sleep(delay)
            return False
        sleep_task = asyncio.create_task(self._reconnect_sleep(delay))
        wake_task = asyncio.create_task(reconnect_wakeup.wait())
        try:
            done, _ = await asyncio.wait(
                {sleep_task, wake_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if sleep_task in done:
                sleep_task.result()
            return wake_task in done and wake_task.result()
        finally:
            for task in (sleep_task, wake_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleep_task, wake_task, return_exceptions=True)

    def _begin_listener(self) -> tuple[asyncio.Task[Any], asyncio.Task[Any]]:
        """核验上一生命周期已经收敛，并发布本轮监听器所有权。"""

        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("connect_and_listen requires an asyncio task")
        # Validate the installed transport before publishing lifecycle state or
        # starting cleanup work.  ``_connect_once`` repeats this check for a
        # token introduced later by hot reload.
        endpoint_auth = self._endpoint_auth
        if endpoint_auth.credentials_trusted:
            websockets_module = _load_websockets_module()
            try:
                _select_connect_header_parameter(websockets_module, endpoint_auth.token)
            except _UnsupportedWebSocketAuthentication as exc:
                logger.error("OneBot WebSocket client refused unsafe authentication: %s", exc)
                raise
        self._reap_successful_ws_close_tasks()
        previous_cleanup = self._cleanup_task
        if previous_cleanup is not None and previous_cleanup.done():
            try:
                previous_cleanup.result()
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                self._cleanup_task = None
                raise RuntimeError("previous OneBot queue cleanup failed") from exc
            else:
                self._cleanup_task = None
        if (
            self._main_task is not None
            and self._main_task is not current_task
            and not self._main_task.done()
        ):
            raise RuntimeError("OneBot WS client is already running")
        if (
            any(not task.done() for task in self._queue_tasks.values())
            or (self._cleanup_task is not None and not self._cleanup_task.done())
            or bool(self._connection_attempt_tasks)
            or bool(self._ws_close_tasks)
        ):
            raise RuntimeError("OneBot WS client has an incomplete previous stop")
        self._main_task = current_task
        self._event_loop = asyncio.get_running_loop()
        self._reconnect_wakeup = asyncio.Event()
        self._running = True
        self._accepting_events = True

        cleanup_task = asyncio.create_task(self._cleanup_inactive_queues_loop())
        self._cleanup_task = cleanup_task
        return current_task, cleanup_task

    async def _run_reconnect_iteration(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
        retry_base: float,
    ) -> float:
        """执行一次连接/退避周期并返回下一轮退避基数。"""

        reconnect_wakeup = self._reconnect_wakeup
        if reconnect_wakeup is not None:
            reconnect_wakeup.clear()
        if not self.credentials_trusted:
            await self._wait_for_reconnect(_ONEBOT_WS_RECONNECT_MAX_SECONDS)
            return _ONEBOT_WS_RECONNECT_INITIAL_SECONDS
        if not self.ws_uri:
            await self._wait_for_reconnect(_ONEBOT_WS_RECONNECT_INITIAL_SECONDS)
            return retry_base

        connected_duration: float | None = None
        failure: Exception | None = None
        try:
            attempt = await self._await_connection_attempt(handler, reconnect_wakeup)
            if isinstance(attempt, _ConnectionAttemptResult):
                connected_duration = attempt.connected_seconds
                failure = attempt.error
            elif isinstance(attempt, (int, float)) and not isinstance(attempt, bool):
                # 兼容仍返回连接时长的私有测试适配器和第三方子类。
                connected_duration = max(0.0, float(attempt))
            else:
                raise TypeError("OneBot connection attempt returned an invalid outcome")
        except _RevokedWebSocketAuthentication:
            return _ONEBOT_WS_RECONNECT_INITIAL_SECONDS
        except _UnsupportedWebSocketAuthentication as exc:
            # 连接器不兼容时重试不会变得安全，也绝不能降级为匿名连接。
            logger.error("OneBot WebSocket client refused unsafe authentication: %s", exc)
            raise
        except Exception as exc:
            failure = exc

        if not self._running:
            return retry_base
        if reconnect_wakeup is not None and reconnect_wakeup.is_set():
            return _ONEBOT_WS_RECONNECT_INITIAL_SECONDS

        if (
            connected_duration is not None
            and connected_duration >= _ONEBOT_WS_RECONNECT_STABLE_SECONDS
        ):
            retry_base = _ONEBOT_WS_RECONNECT_INITIAL_SECONDS
        delay = _jittered_reconnect_delay(retry_base, self._reconnect_random())
        if failure is None:
            logger.info(
                "OneBot WS disconnected after %.3fs; reconnecting in %.2fs",
                connected_duration or 0.0,
                delay,
            )
        else:
            logger.warning("OneBot WS error: %s; reconnecting in %.2fs", failure, delay)
        with self._auth_state_lock:
            self._ws = None
            self._connected_auth_generation = None
        if await self._wait_for_reconnect(delay):
            return _ONEBOT_WS_RECONNECT_INITIAL_SECONDS
        return min(retry_base * 2.0, _ONEBOT_WS_RECONNECT_MAX_SECONDS)

    async def _finish_listener(
        self,
        current_task: asyncio.Task[Any],
        cleanup_task: asyncio.Task[Any],
    ) -> None:
        """收敛连接尝试和队列清理任务，最后撤销监听器所有权。"""

        self._running = False
        try:
            try:
                attempt_pending, attempt_errors = await self._cancel_connection_attempts(
                    _ONEBOT_WS_ROTATION_CANCEL_GRACE_SECONDS
                )
                if attempt_errors:
                    logger.warning(
                        "%d OneBot connection attempt(s) failed while listener exited",
                        len(attempt_errors),
                    )
                if attempt_pending:
                    logger.error(
                        "%d OneBot connection attempt(s) remain quarantined after listener exit",
                        len(attempt_pending),
                    )
            finally:
                # 重复取消可能打断连接尝试的排空；队列清理所有权仍必须先收敛。
                if self._cleanup_task is cleanup_task:
                    if not cleanup_task.done():
                        cleanup_task.cancel()
                    try:
                        await cleanup_task
                    except asyncio.CancelledError:
                        pass
                    except BaseException as exc:
                        logger.error(
                            "OneBot queue cleanup failed while the listener exited: %s",
                            exc,
                        )
                    finally:
                        if self._cleanup_task is cleanup_task:
                            self._cleanup_task = None
        finally:
            # 此层没有 await，第二次取消也不能遗留监听器身份或 loop wakeup。
            if self._main_task is current_task:
                self._main_task = None
                self._event_loop = None
                self._reconnect_wakeup = None

    async def connect_and_listen(
        self, handler: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None:
        """连接并监听消息。"""

        current_task, cleanup_task = self._begin_listener()

        try:
            retry_base = _ONEBOT_WS_RECONNECT_INITIAL_SECONDS
            while self._running:
                retry_base = await self._run_reconnect_iteration(handler, retry_base)
        finally:
            await self._finish_listener(current_task, cleanup_task)

    async def _connect_once(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> _ConnectionAttemptResult:
        """Run one connection until it closes and return its observable outcome."""

        endpoint_auth = self._endpoint_auth
        if not endpoint_auth.credentials_trusted:
            raise _RevokedWebSocketAuthentication(
                "OneBot credential source is unavailable; outbound connection is disabled"
            )
        websockets = _load_websockets_module()
        headers = {"Authorization": f"Bearer {endpoint_auth.token}"} if endpoint_auth.token else {}
        header_parameter = _select_connect_header_parameter(websockets, endpoint_auth.token)
        connect_kwargs = {header_parameter: headers} if header_parameter is not None else {}
        try:
            async with websockets.connect(endpoint_auth.endpoint, **connect_kwargs) as ws:
                return await self._listen(ws, handler, auth_state=endpoint_auth)
        except Exception as exc:
            logger.error("WebSocket connection failed (%s)", type(exc).__name__)
            raise

    async def _listen(
        self,
        ws,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        auth_generation: int | None = None,
        auth_state: _EndpointAuthState | None = None,
    ) -> _ConnectionAttemptResult:
        """Listen until disconnection and retain abnormal close information."""
        connected_at = time.monotonic()
        if auth_state is None:
            auth_state = self._endpoint_auth
            if auth_generation is not None and auth_generation != auth_state.generation:
                await ws.close()
                return _ConnectionAttemptResult(0.0)
        with self._auth_state_lock:
            if auth_state is not self._endpoint_auth:
                stale_auth = True
            else:
                stale_auth = False
                self._ws = ws
                self._connected_auth_generation = auth_state.generation
        if stale_auth:
            await ws.close()
            return _ConnectionAttemptResult(0.0)
        logger.info("Connected to OneBot WS: %s", redact_url_for_log(self.ws_uri))

        # 触发连接成功回调
        if self._on_connect:
            try:
                await self._on_connect()
            except Exception as exc:
                logger.warning("on_connect callback error: %s", exc)

        from websockets.exceptions import ConnectionClosed, ConnectionClosedOK

        disconnect_error: Exception | None = None
        try:
            async for raw in ws:
                if auth_state is not self._endpoint_auth:
                    await ws.close()
                    break
                try:
                    raw_len = len(raw) if hasattr(raw, "__len__") else None
                    logger.debug("[WS] Raw frame type=%s size=%s", type(raw).__name__, raw_len)
                    event = json.loads(raw)
                    logger.debug("[WS] Event received: %s", _summarize_event(event))
                    if self._resolve_action_response(event, auth_state=auth_state):
                        continue
                    await self._dispatch_event(handler, event, auth_state=auth_state)
                except json.JSONDecodeError:
                    logger.debug("[WS] Non-JSON frame received")
                    continue
                except Exception as exc:
                    logger.exception("Event parse error: %s", exc)
        except ConnectionClosedOK as exc:
            logger.debug("WebSocket connection closed normally: %s", exc)
        except ConnectionClosed as exc:
            disconnect_error = exc
        except Exception as exc:
            disconnect_error = exc
        finally:
            self._fail_pending_action_futures(
                "WebSocket connection closed",
                auth_state=auth_state,
            )
            with self._auth_state_lock:
                if self._ws is ws:
                    self._ws = None
                    self._connected_auth_generation = None
        return _ConnectionAttemptResult(
            max(0.0, time.monotonic() - connected_at),
            disconnect_error,
        )

    async def _handle_event_safely(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
        event: dict[str, Any],
        *,
        auth_state: _EndpointAuthState,
    ) -> None:
        """安全地处理事件（捕获异常，避免影响其他消息）"""
        try:
            # The frozen token is the admission/rotation linearization point.
            # The plugin factory is external code and must run after releasing
            # the threading lock, otherwise an update-thread join can deadlock.
            commit: _OneBotEventCommit | None = None
            with self._auth_state_lock:
                if auth_state is self._endpoint_auth:
                    commit = _OneBotEventCommit(auth_state=auth_state)
            if commit is None:
                logger.debug("Dropping OneBot event from a revoked auth generation")
                return
            handler_awaitable = handler(event)
            await handler_awaitable
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Event handler error: %s", exc)
        except BaseException as exc:
            logger.critical(
                "Fatal OneBot event handler failure was isolated: %s: %s",
                type(exc).__name__,
                exc,
            )

    def _get_queue_key(self, event: dict[str, Any]) -> str | None:
        user_id = event.get("user_id")
        if user_id is None:
            return None
        group_id = event.get("group_id")
        if group_id is None:
            return f"user:{user_id}"
        return f"group:{group_id}:user:{user_id}"

    async def _dispatch_event(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
        event: dict[str, Any],
        *,
        auth_state: _EndpointAuthState | None = None,
    ) -> None:
        if not self._accepting_events:
            logger.debug("Dropping OneBot event while client is stopping")
            return
        if auth_state is None:
            with self._auth_state_lock:
                auth_state = self._endpoint_auth
        key = self._get_queue_key(event)
        if not key:
            async with self._pending_semaphore:
                await self._handle_event_safely(handler, event, auth_state=auth_state)
            return

        queue = self._message_queues.get(key)
        if queue is None:
            queue = asyncio.Queue(maxsize=self._queue_size)
            self._message_queues[key] = queue

        self._queue_last_activity[key] = time.time()
        # 如果队列已满，丢弃最旧的消息
        if self._queue_size > 0 and queue.full():
            try:
                queue.get_nowait()  # 丢弃最旧的消息
                logger.warning("Queue full for %s, dropped oldest event", key)
            except asyncio.QueueEmpty:
                pass
        try:
            await asyncio.wait_for(
                queue.put(_QueuedOneBotEvent(event=event, auth_state=auth_state)),
                timeout=1.0,
            )
        except asyncio.TimeoutError:
            logger.warning("Queue put timeout for %s, dropping event", key)
        task = self._queue_tasks.get(key)
        if task is None or task.done():
            self._queue_tasks[key] = asyncio.create_task(self._drain_queue(key, handler))

    async def _drain_queue(
        self,
        key: str,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        queue = self._message_queues.get(key)
        if not queue:
            return
        # 使用带超时的 get 来避免忙等待
        # 如果 1 秒内没有新事件，则退出循环
        try:
            while True:
                # 等待事件，最多等待 1 秒
                queued = await asyncio.wait_for(queue.get(), timeout=1.0)
                if not isinstance(queued, _QueuedOneBotEvent):
                    logger.warning("Dropping OneBot queue item without an auth generation")
                    self._queue_last_activity[key] = time.time()
                    continue
                event = queued.event
                auth_state = queued.auth_state
                async with self._pending_semaphore:
                    await self._handle_event_safely(
                        handler,
                        event,
                        auth_state=auth_state,
                    )
                # 标记任务为活跃
                self._queue_last_activity[key] = time.time()
        except asyncio.TimeoutError:
            # 超时表示队列为空，正常退出
            pass
        finally:
            current_task = asyncio.current_task()
            if self._queue_tasks.get(key) is current_task:
                if queue.empty():
                    self._queue_tasks.pop(key, None)
                elif self._accepting_events:
                    self._queue_tasks[key] = asyncio.create_task(self._drain_queue(key, handler))
                else:
                    self._queue_tasks.pop(key, None)

    async def stop(self) -> None:
        """Stop every owned task with bounded, retryable convergence."""
        async with self._stop_lock.get():
            await self._stop_locked()

    async def _stop_queue_workers(
        self,
        remaining: Callable[[], float],
        errors: list[tuple[str, BaseException]],
    ) -> None:
        """取消所有按会话串行化的消息队列 worker。"""

        all_queue_tasks = set(self._queue_tasks.values())
        queue_tasks = {task for task in all_queue_tasks if not task.done()}
        for task in queue_tasks:
            task.cancel()
        if queue_tasks:
            queue_done, queue_pending = await asyncio.wait(
                queue_tasks,
                timeout=remaining(),
            )
        else:
            queue_done, queue_pending = set(), set()
        queue_done.update(task for task in all_queue_tasks if task.done())
        for task in queue_done:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                errors.append(("queue worker", exc))
        for key, task in tuple(self._queue_tasks.items()):
            if task.done():
                self._queue_tasks.pop(key, None)
        if queue_pending:
            errors.append(
                (
                    "queue workers",
                    RuntimeError(
                        f"{len(queue_pending)} task(s) ignored cancellation within "
                        f"{self._shutdown_timeout_seconds:.3f}s"
                    ),
                )
            )
        if not any(not task.done() for task in self._queue_tasks.values()):
            self._queue_tasks.clear()
            self._message_queues.clear()
            self._queue_last_activity.clear()

    async def _stop_queue_cleanup(
        self,
        remaining: Callable[[], float],
        errors: list[tuple[str, BaseException]],
    ) -> None:
        """停止周期性队列清理任务，并保留未收敛所有权供下次重试。"""

        cleanup_task = self._cleanup_task
        if cleanup_task is None:
            return
        if not cleanup_task.done():
            cleanup_task.cancel()
            cleanup_done, cleanup_pending = await asyncio.wait(
                {cleanup_task},
                timeout=remaining(),
            )
        else:
            cleanup_done, cleanup_pending = {cleanup_task}, set()
        if cleanup_done:
            try:
                cleanup_task.result()
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                errors.append(("queue cleanup", exc))
            if self._cleanup_task is cleanup_task:
                self._cleanup_task = None
        if cleanup_pending:
            errors.append(
                (
                    "queue cleanup",
                    RuntimeError(
                        "cleanup task ignored cancellation within "
                        f"{self._shutdown_timeout_seconds:.3f}s"
                    ),
                )
            )

    async def _stop_connection_attempts(
        self,
        remaining: Callable[[], float],
        errors: list[tuple[str, BaseException]],
    ) -> None:
        """停止握手任务，并把异常和超时统一汇入关停报告。"""

        attempt_pending, attempt_errors = await self._cancel_connection_attempts(remaining())
        for exc in attempt_errors:
            errors.append(("connection attempt", exc))
        if attempt_pending:
            errors.append(
                (
                    "connection attempts",
                    RuntimeError(
                        f"{len(attempt_pending)} task(s) ignored cancellation within "
                        f"{self._shutdown_timeout_seconds:.3f}s"
                    ),
                )
            )

    async def _stop_websocket_closes(
        self,
        remaining: Callable[[], float],
        errors: list[tuple[str, BaseException]],
    ) -> None:
        """等待所有 WebSocket close；成功项撤销所有权，超时项保留诊断。"""

        close_entries = tuple(self._ws_close_tasks.values())
        close_tasks = {task for _, task in close_entries}
        if not close_tasks:
            return
        close_done, close_pending = await asyncio.wait(close_tasks, timeout=remaining())
        for owned_ws, close_task in close_entries:
            if close_task not in close_done:
                continue
            try:
                close_task.result()
            except asyncio.CancelledError as exc:
                errors.append(("WebSocket close", exc))
            except BaseException as exc:
                errors.append(("WebSocket close", exc))
            else:
                key = id(owned_ws)
                entry = self._ws_close_tasks.get(key)
                if entry is not None and entry[0] is owned_ws and entry[1] is close_task:
                    self._ws_close_tasks.pop(key, None)
                if self._ws is owned_ws:
                    self._ws = None
                    self._connected_auth_generation = None
        for close_task in close_pending:
            close_task.cancel()
        if close_pending:
            errors.append(
                (
                    "WebSocket close",
                    RuntimeError(
                        f"{len(close_pending)} close task(s) exceeded "
                        f"{self._shutdown_timeout_seconds:.3f}s"
                    ),
                )
            )

    async def _stop_main_listener(
        self,
        remaining: Callable[[], float],
        errors: list[tuple[str, BaseException]],
    ) -> None:
        """取消主监听任务，并仅在所有权确实释放后清理 loop 状态。"""

        main_task = self._main_task
        current_task = asyncio.current_task()
        if main_task and main_task is not current_task and not main_task.done():
            main_task.cancel()
            main_done, main_pending = await asyncio.wait(
                {main_task},
                timeout=remaining(),
            )
            if main_done:
                try:
                    main_task.result()
                except asyncio.CancelledError:
                    pass
                except BaseException as exc:
                    errors.append(("main listener", exc))
                if self._main_task is main_task:
                    self._main_task = None
            if main_pending:
                errors.append(
                    (
                        "main listener",
                        RuntimeError(
                            "listener task ignored cancellation within "
                            f"{self._shutdown_timeout_seconds:.3f}s"
                        ),
                    )
                )
        elif main_task is not None and main_task.done():
            try:
                main_task.result()
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                errors.append(("main listener", exc))
            if self._main_task is main_task:
                self._main_task = None

        if self._main_task is None:
            # A listener cancelled during its own cleanup may already be done
            # before stop() observes it.  Stop is the final ownership backstop
            # for loop-bound wakeup fields in that case.
            self._event_loop = None
            self._reconnect_wakeup = None

    async def _stop_locked(self) -> None:
        self._running = False
        self._accepting_events = False
        self._fail_pending_action_futures("WebSocket client stopped")
        errors: list[tuple[str, BaseException]] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._shutdown_timeout_seconds

        def remaining() -> float:
            return max(0.0, deadline - loop.time())

        # close 最可能解除忽略取消的 recv；先启动所有 close，再共享同一绝对截止时间。
        self._prepare_ws_closes_for_stop()
        await self._stop_queue_workers(remaining, errors)
        await self._stop_queue_cleanup(remaining, errors)
        await self._stop_connection_attempts(remaining, errors)

        # 握手可能在第一次准备后才发布 socket，同时重试早期失败的 close。
        self._prepare_ws_closes_for_stop()
        await self._stop_websocket_closes(remaining, errors)
        await self._stop_main_listener(remaining, errors)

        if errors:
            summary = "; ".join(f"{name}: {type(exc).__name__}: {exc}" for name, exc in errors)
            raise RuntimeError(f"OneBot WS client cleanup failed: {summary}") from errors[0][1]

    async def _cleanup_inactive_queues_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._queue_cleanup_interval)
            self._cleanup_inactive_queues()

    def _cleanup_inactive_queues(self) -> None:
        now = time.time()
        inactive_keys: list[str] = []
        for key, queue in self._message_queues.items():
            last_active = self._queue_last_activity.get(key, 0)
            task = self._queue_tasks.get(key)
            if queue.empty() and (not task or task.done()):
                if now - last_active > self._queue_ttl_seconds:
                    inactive_keys.append(key)

        for key in inactive_keys:
            self._message_queues.pop(key, None)
            self._queue_tasks.pop(key, None)
            self._queue_last_activity.pop(key, None)
            logger.debug("Cleaned up inactive queue: %s", key)
