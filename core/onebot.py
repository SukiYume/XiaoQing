"""
OneBot 协议支持

提供 HTTP 发送器和 WebSocket 客户端。
"""

import asyncio
import hmac
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable

import aiohttp

from .constants import MAX_SHORT_TEXT_LENGTH

logger = logging.getLogger(__name__)

_SENSITIVE_KEYS = {"token", "appid", "api_key", "secret", "password", "authorization"}

# 预编译敏感信息匹配模式（避免每次调用都重新编译）
_SENSITIVE_PATTERNS = [
    re.compile(rf"({key}\s*[:=]\s*)([^\s,;]+)", re.IGNORECASE)
    for key in _SENSITIVE_KEYS
]

_token_warning_shown = False

def _verify_token_auth(auth_header: str, expected_token: str) -> bool:
    """
    时序安全的 token 验证（防止时序攻击）

    Args:
        auth_header: Authorization header 值
        expected_token: 期望的 token

    Returns:
        验证是否成功
    """
    global _token_warning_shown
    if not expected_token:
        if not _token_warning_shown:
            logger.warning("Security: no token configured, all requests accepted")
            _token_warning_shown = True
        return True  # 没有配置 token 时跳过验证
    expected = f"Bearer {expected_token}"
    # 使用 hmac.compare_digest 防止时序攻击（不预先比较长度，避免泄露长度信息）
    return hmac.compare_digest(auth_header.encode(), expected.encode())

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
            elif seg_type == "image":
                parts.append("[图片]")
            elif seg_type == "at":
                parts.append(f"[@{seg.get('data', {}).get('qq', '')}]")
            else:
                parts.append(f"[{seg_type}]")
    
    text = "".join(parts)
    if len(text) > max_len:
        text = text[:max_len] + "..."
    return text

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
    try:
        sig = inspect.signature(websockets_module.connect)
        return set(sig.parameters.keys())
    except Exception:
        # 降级：尝试导入并检查
        return {"additional_headers", "extra_headers"}

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

    async def send_action(self, action: dict[str, Any]) -> None:
        """发送 OneBot action"""
        if not self.http_base:
            return

        url = f"{self.http_base}/{action['action']}"
        # 构建请求头（token 已在客户端配置时验证）
        headers = {"Authorization": f"Bearer {self.auth_token}"} if self.auth_token else {}

        # 提取消息内容用于日志
        params = action.get("params", {})
        target = params.get("group_id") or params.get("user_id")
        message = params.get("message", [])
        msg_preview = _extract_message_preview(message)
        
        try:
            async with self.session.post(url, json=params, headers=headers) as resp:
                if resp.status == 200:
                    logger.info(
                        "[HTTP] Sent %s to %s: %s",
                        action['action'], target, msg_preview
                    )
                else:
                    logger.warning(
                        "[HTTP] Send failed (status=%s) to %s: %s",
                        resp.status, target, msg_preview
                    )
        except Exception as exc:
            logger.warning("[HTTP] Send failed: %s", exc)

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
    ) -> None:
        self.ws_uri = ws_uri
        self.auth_token = auth_token
        self._ws: Any | None = None
        self._running = False
        self._on_connect: Callable[[], Awaitable[None]] | None = None
        self._message_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._queue_tasks: dict[str, asyncio.Task[None]] = {}
        self._queue_last_activity: dict[str, float] = {}
        self._queue_size = max(0, int(queue_size))
        self._queue_ttl_seconds = queue_ttl_seconds
        self._queue_cleanup_interval = queue_cleanup_interval
        self._pending_semaphore = asyncio.Semaphore(max_pending_events)
        self._cleanup_task: asyncio.Task[None] | None = None

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
        return self._ws is not None

    async def send_action(self, action: dict[str, Any]) -> None:
        """发送 OneBot action"""
        if not self._ws:
            return
        
        # 提取消息内容用于日志
        params = action.get("params", {})
        target = params.get("group_id") or params.get("user_id")
        message = params.get("message", [])
        msg_preview = _extract_message_preview(message)
        
        try:
            await self._ws.send(json.dumps(action, ensure_ascii=False))
            logger.info(
                "[WS] Sent %s to %s: %s",
                action.get('action', 'unknown'), target, msg_preview
            )
        except Exception as exc:
            logger.warning("[WS] Send failed: %s", exc)

    async def connect_and_listen(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """连接并监听消息"""
        self._running = True
        retry_delay = 5

        if not self._cleanup_task or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_inactive_queues_loop())

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
                    async with self._pending_semaphore:
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
        key = self._get_queue_key(event)
        if not key:
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
                await self._handle_event_safely(handler, event)
                # 标记任务为活跃
                self._queue_last_activity[key] = time.time()
        except asyncio.TimeoutError:
            # 超时表示队列为空，正常退出
            pass
        finally:
            # 清理已完成或取消的任务
            if self._queue_tasks.get(key) and self._queue_tasks[key].done():
                self._queue_tasks.pop(key, None)

    async def stop(self) -> None:
        """停止客户端"""
        self._running = False
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
        if self._ws:
            await self._ws.close()

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
