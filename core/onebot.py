"""
OneBot 协议支持

提供 HTTP 发送器和 WebSocket 客户端。
"""

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any, Awaitable, Callable

import aiohttp

from .auth import verify_bearer_token
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

logger = logging.getLogger(__name__)

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
    re.compile(rf"({key}\s*[:=]\s*)([^\s,;]+)", re.IGNORECASE)
    for key in _SENSITIVE_KEYS
]
_CONNECT_SIGNATURE_CACHE: dict[int, set[str]] = {}

def _verify_token_auth(auth_header: str, expected_token: str) -> bool:
    """
    时序安全的 token 验证（防止时序攻击）

    Args:
        auth_header: Authorization header 值
        expected_token: 期望的 token

    Returns:
        验证是否成功
    """
    return verify_bearer_token(auth_header, expected_token)

def _mask_sensitive_text(text: str) -> str:
    masked = text
    for pattern in _SENSITIVE_PATTERNS:
        masked = pattern.sub(r"\1********", masked)
    return masked

def _extract_message_preview(message: list[dict[str, Any]], max_len: int = MAX_SHORT_TEXT_LENGTH) -> str:
    """从消息段中提取预览文本（供日志使用）"""
    if not message:
        return "(empty)"
    
    parts = []
    for seg in message:
        if isinstance(seg, dict):
            seg_type = seg.get("type", "")
            if seg_type == "text":
                raw = seg.get("data", {}).get("text", "")
                parts.append(_mask_sensitive_text(raw))
            elif seg_type == "emoji":
                parts.append("[表情包]")
            elif seg_type == "image":
                data = seg.get("data", {}) or {}
                subtype = str(data.get("sub_type", "") or "").strip().lower()
                parts.append("[表情包]" if subtype == "emoji" else "[图片]")
            elif seg_type == "at":
                parts.append(f"[@{seg.get('data', {}).get('qq', '')}]")
            else:
                parts.append(f"[{seg_type}]")
    
    text = "".join(parts)
    if len(text) > max_len:
        text = text[:max_len] + "..."
    return text


def _normalize_segment_for_onebot(seg: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(seg, dict):
        return seg
    seg_type = str(seg.get("type", "") or "").strip()
    if seg_type != "emoji":
        return seg

    data = dict(seg.get("data", {}) or {})
    normalized_data = {"sub_type": "emoji", **data}
    return {"type": "image", "data": normalized_data}


def _normalize_action_for_onebot(action: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(action, dict):
        return action
    params = dict(action.get("params", {}) or {})
    message = params.get("message")
    if isinstance(message, list):
        params["message"] = [
            _normalize_segment_for_onebot(segment) if isinstance(segment, dict) else segment
            for segment in message
        ]
    normalized = dict(action)
    normalized["params"] = params
    return normalized


def _onebot_action_succeeded(response: Any) -> bool:
    """Return whether an OneBot action response confirms business success."""
    return (
        isinstance(response, dict)
        and response.get("status") == "ok"
        and response.get("retcode") == 0
    )

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

def _get_connect_signature(websockets_module) -> set[str]:
    """获取 websockets.connect 函数支持的参数名"""
    import inspect
    connect_func = websockets_module.connect
    cache_key = id(connect_func)
    if cache_key in _CONNECT_SIGNATURE_CACHE:
        return _CONNECT_SIGNATURE_CACHE[cache_key]

    try:
        sig = inspect.signature(connect_func)
        result = set(sig.parameters.keys())
    except Exception:
        # 降级：尝试导入并检查
        result = {"additional_headers", "extra_headers"}
    _CONNECT_SIGNATURE_CACHE[cache_key] = result
    return result

class OneBotHttpSender:
    """OneBot HTTP 发送器"""

    def __init__(self, http_base: str, auth_token: str, session: aiohttp.ClientSession) -> None:
        self.http_base = http_base.rstrip("/")
        self.auth_token = auth_token
        self.session = session

    def update(self, http_base: str, auth_token: str) -> None:
        """更新配置"""
        self.http_base = http_base.rstrip("/")
        self.auth_token = auth_token

    async def request_action(self, action: dict[str, Any]) -> dict[str, Any] | None:
        """Send an action and return its parsed OneBot response envelope."""

        if not self.http_base:
            return None

        normalized_action = _normalize_action_for_onebot(action)
        url = f"{self.http_base}/{normalized_action['action']}"
        headers = {"Authorization": f"Bearer {self.auth_token}"} if self.auth_token else {}
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

    async def send_action(self, action: dict[str, Any]) -> bool:
        """Send an OneBot action and return whether OneBot accepted it."""

        response = await self.request_action(action)
        normalized_action = _normalize_action_for_onebot(action)
        params = normalized_action.get("params", {})
        target = params.get("group_id") or params.get("user_id")
        message = action.get("params", {}).get("message", [])
        msg_preview = _extract_message_preview(message)
        if _onebot_action_succeeded(response):
            data = response.get("data") if isinstance(response, dict) else None
            message_id = data.get("message_id") if isinstance(data, dict) else None
            if message_id not in (None, ""):
                action["_result_message_id"] = message_id
            logger.info(
                "[HTTP] Sent %s to %s: %s",
                normalized_action.get("action", "unknown"),
                target,
                msg_preview,
            )
            return True
        if response is not None:
            logger.warning(
                "[HTTP] OneBot rejected %s to %s (status=%r retcode=%r)",
                normalized_action.get("action", "unknown"),
                target,
                response.get("status"),
                response.get("retcode"),
            )
        return False

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
    ) -> None:
        self.ws_uri = ws_uri
        self.auth_token = auth_token
        self._ws: Any | None = None
        self._running = False
        self._accepting_events = True
        self._on_connect: Callable[[], Awaitable[None]] | None = None
        self._message_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._queue_tasks: dict[str, asyncio.Task[None]] = {}
        self._queue_last_activity: dict[str, float] = {}
        self._queue_size = max(0, int(queue_size))
        self._queue_ttl_seconds = queue_ttl_seconds
        self._queue_cleanup_interval = queue_cleanup_interval
        self._pending_semaphore = asyncio.Semaphore(max_pending_events)
        self._cleanup_task: asyncio.Task[None] | None = None
        self._main_task: asyncio.Task[None] | None = None
        self._pending_action_futures: dict[str, asyncio.Future[dict[str, Any]]] = {}
        try:
            self._action_response_timeout_seconds = max(0.1, float(action_response_timeout_seconds))
        except (TypeError, ValueError):
            self._action_response_timeout_seconds = DEFAULT_ONEBOT_WS_ACTION_TIMEOUT_SECONDS

    def set_on_connect(self, callback: Callable[[], Awaitable[None]]) -> None:
        """设置连接成功回调"""
        self._on_connect = callback

    def update(self, ws_uri: str, auth_token: str) -> None:
        """更新配置

        Args:
            ws_uri: WebSocket URI
            auth_token: 认证 token（用于 Bearer 认证）

        Note:
            Token 验证在服务器端进行，客户端负责正确携带
        """
        self.ws_uri = ws_uri
        self.auth_token = auth_token

    def connected(self) -> bool:
        """是否已连接"""
        ws = self._ws
        if ws is None:
            return False
        if getattr(ws, "closed", False) is True:
            return False
        close_code = getattr(ws, "close_code", None)
        if isinstance(close_code, int):
            return False
        state = getattr(ws, "state", None)
        state_name = getattr(state, "name", "")
        if isinstance(state_name, str) and state_name.upper() in {"CLOSING", "CLOSED"}:
            return False
        return True

    async def request_action(self, action: dict[str, Any]) -> dict[str, Any] | None:
        """Send an action and return the response matched by its ``echo``."""

        ws = self._ws
        if not self.connected() or ws is None:
            return None

        normalized_action = _normalize_action_for_onebot(action)
        echo = f"xiaoqing-{uuid.uuid4().hex}"
        request = dict(normalized_action)
        request["echo"] = echo
        response_future = asyncio.get_running_loop().create_future()
        self._pending_action_futures[echo] = response_future
        try:
            await ws.send(json.dumps(request, ensure_ascii=False))
            response = await asyncio.wait_for(
                asyncio.shield(response_future),
                timeout=self._action_response_timeout_seconds,
            )
            return response if isinstance(response, dict) else None
        except asyncio.TimeoutError:
            logger.warning(
                "[WS] Timed out waiting for OneBot response to %s",
                normalized_action.get("action", "unknown"),
            )
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[WS] Send failed: %s", exc)
            if self._ws is ws:
                self._ws = None
            self._fail_pending_action_futures(
                "WebSocket send failed",
                exclude=response_future,
            )
            return None
        finally:
            if self._pending_action_futures.get(echo) is response_future:
                self._pending_action_futures.pop(echo, None)
            if not response_future.done():
                response_future.cancel()

    async def send_action(self, action: dict[str, Any]) -> bool:
        """Send an action and return whether the matched response accepted it."""

        response = await self.request_action(action)
        normalized_action = _normalize_action_for_onebot(action)
        params = normalized_action.get("params", {})
        target = params.get("group_id") or params.get("user_id")
        message = action.get("params", {}).get("message", [])
        msg_preview = _extract_message_preview(message)
        if _onebot_action_succeeded(response):
            data = response.get("data") if isinstance(response, dict) else None
            message_id = data.get("message_id") if isinstance(data, dict) else None
            if message_id not in (None, ""):
                action["_result_message_id"] = message_id
            logger.info(
                "[WS] Sent %s to %s: %s",
                normalized_action.get("action", "unknown"),
                target,
                msg_preview,
            )
            return True
        if response is not None:
            logger.warning(
                "[WS] OneBot rejected %s to %s (status=%r retcode=%r)",
                normalized_action.get("action", "unknown"),
                target,
                response.get("status"),
                response.get("retcode"),
            )
        return False

    @staticmethod
    def _is_action_response(event: dict[str, Any]) -> bool:
        return "echo" in event and ("status" in event or "retcode" in event)

    def _resolve_action_response(self, event: dict[str, Any]) -> bool:
        """Resolve a pending action by echo; action responses never reach plugins."""
        if not self._is_action_response(event):
            return False
        echo = event.get("echo")
        if not isinstance(echo, str):
            return True
        future = self._pending_action_futures.get(echo)
        if future is not None and not future.done():
            future.set_result(event)
        return True

    def _fail_pending_action_futures(
        self,
        reason: str,
        *,
        exclude: asyncio.Future[dict[str, Any]] | None = None,
    ) -> None:
        pending = list(self._pending_action_futures.values())
        self._pending_action_futures.clear()
        for future in pending:
            if future is not exclude and not future.done():
                future.set_exception(ConnectionError(reason))

    async def connect_and_listen(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """连接并监听消息"""
        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("connect_and_listen requires an asyncio task")
        if self._main_task is not None and self._main_task is not current_task and not self._main_task.done():
            raise RuntimeError("OneBot WS client is already running")
        self._main_task = current_task
        self._running = True
        self._accepting_events = True
        retry_delay = 5

        if not self._cleanup_task or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_inactive_queues_loop())

        try:
            while self._running:
                if not self.ws_uri:
                    await asyncio.sleep(5)
                    continue

                try:
                    await self._connect_once(handler)
                    retry_delay = 5
                except Exception as exc:
                    logger.warning(
                        "OneBot WS error: %s, reconnecting in %ds...",
                        exc,
                        retry_delay,
                    )
                    self._ws = None
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)
        finally:
            self._running = False
            if self._main_task is current_task:
                self._main_task = None

    async def _connect_once(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """单次连接"""
        try:
            import websockets
        except ImportError:
            logger.error("websockets module not installed")
            await asyncio.sleep(60)
            return

        headers = {"Authorization": f"Bearer {self.auth_token}"} if self.auth_token else {}

        # 检测 websockets 版本支持的参数名
        connect_sig = _get_connect_signature(websockets)
        try:
            if "additional_headers" in connect_sig:
                async with websockets.connect(self.ws_uri, additional_headers=headers) as ws:
                    await self._listen(ws, handler)
            elif "extra_headers" in connect_sig:
                async with websockets.connect(self.ws_uri, extra_headers=headers) as ws:
                    await self._listen(ws, handler)
            else:
                # 不支持 headers 参数
                if self.auth_token:
                    logger.warning("WS client does not support headers, sending without token")
                async with websockets.connect(self.ws_uri) as ws:
                    await self._listen(ws, handler)
        except Exception as exc:
            logger.error("WebSocket connection failed: %s", exc)
            raise


    async def _listen(self, ws, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """监听消息"""
        self._ws = ws
        logger.info("Connected to OneBot WS: %s", self.ws_uri)

        # 触发连接成功回调
        if self._on_connect:
            try:
                await self._on_connect()
            except Exception as exc:
                logger.warning("on_connect callback error: %s", exc)

        from websockets.exceptions import ConnectionClosed

        try:
            async for raw in ws:
                try:
                    raw_len = len(raw) if hasattr(raw, "__len__") else None
                    logger.debug("[WS] Raw frame type=%s size=%s", type(raw).__name__, raw_len)
                    event = json.loads(raw)
                    logger.debug("[WS] Event received: %s", _summarize_event(event))
                    if self._resolve_action_response(event):
                        continue
                    await self._dispatch_event(handler, event)
                except json.JSONDecodeError:
                    logger.debug("[WS] Non-JSON frame received")
                    continue
                except Exception as exc:
                    logger.exception("Event parse error: %s", exc)
        except ConnectionClosed as exc:
            logger.info("WebSocket connection closed: %s", exc)
        except Exception as exc:
            logger.error("WebSocket listen loop error: %s", exc)
        finally:
            self._fail_pending_action_futures("WebSocket connection closed")
            self._ws = None

    async def _handle_event_safely(self, handler: Callable[[dict[str, Any]], Awaitable[None]], event: dict[str, Any]) -> None:
        """安全地处理事件（捕获异常，避免影响其他消息）"""
        try:
            await handler(event)
        except Exception as exc:
            logger.exception("Event handler error: %s", exc)

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
    ) -> None:
        if not self._accepting_events:
            logger.debug("Dropping OneBot event while client is stopping")
            return
        key = self._get_queue_key(event)
        if not key:
            async with self._pending_semaphore:
                await self._handle_event_safely(handler, event)
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
            await asyncio.wait_for(queue.put(event), timeout=1.0)
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
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
                async with self._pending_semaphore:
                    await self._handle_event_safely(handler, event)
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
        """停止客户端"""
        self._running = False
        self._accepting_events = False
        self._fail_pending_action_futures("WebSocket client stopped")

        queue_tasks = [task for task in self._queue_tasks.values() if not task.done()]
        for task in queue_tasks:
            task.cancel()
        if queue_tasks:
            await asyncio.gather(*queue_tasks, return_exceptions=True)
        self._queue_tasks.clear()
        self._message_queues.clear()
        self._queue_last_activity.clear()

        cleanup_task = self._cleanup_task
        if cleanup_task and not cleanup_task.done():
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
        self._cleanup_task = None
        if self._ws:
            await self._ws.close()
            self._ws = None
        main_task = self._main_task
        current_task = asyncio.current_task()
        if main_task and main_task is not current_task and not main_task.done():
            main_task.cancel()
            try:
                await main_task
            except asyncio.CancelledError:
                pass

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
