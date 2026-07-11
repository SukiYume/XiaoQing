"""
Inbound Server 模块

提供 HTTP/WebSocket 入站服务器，接收来自外部的事件推送。
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from aiohttp import ContentTypeError, web

from .auth import verify_bearer_token
from .constants import (
    DEFAULT_INBOUND_WS_MAX_WORKERS,
    DEFAULT_INBOUND_WS_QUEUE_SIZE,
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
)
from .inbound_policy import is_loopback_host, parse_inbound_listener, validate_inbound_listener
from .message import normalize_inbound_message

logger = logging.getLogger(__name__)

# 版本号
VERSION = "1.0.0"


@dataclass
class _KeyedEventLock:
    """A per-event-key lock together with its active user count."""

    lock: asyncio.Lock
    users: int = 0


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
        trusted_tls_proxy: bool = False,
    ) -> None:
        if type(trusted_tls_proxy) is not bool:
            raise TypeError("trusted_tls_proxy must be a boolean")
        self.host = host
        self.port = port
        self.ws_path = ws_path
        self.token = token
        self.handler = handler
        self.enable_http = bool(enable_http)
        self.enable_ws = bool(enable_ws)
        self.trusted_tls_proxy = trusted_tls_proxy
        self.app = web.Application()
        routes = []
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
        self._start_time = time.time()
        self._request_count = 0
        self._ws_connections = 0
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

        self._ws_event_queue: asyncio.Queue[
            tuple[web.WebSocketResponse, int, dict[str, Any]]
        ] | None = None
        self._ws_worker_tasks: list[asyncio.Task[None]] = []
        self._auth_generation = 0
        self._ws_close_tasks: set[asyncio.Task[None]] = set()
        # Entries exist only while a worker holds or waits for the key.  The
        # number of retained locks is therefore bounded by live WS work, not
        # by the number of users that have ever connected.
        self._ws_event_locks: dict[str, _KeyedEventLock] = {}
        self._ws_max_workers = 0
        if self.enable_ws:
            try:
                max_queue = int(ws_queue_size)
            except (TypeError, ValueError):
                max_queue = 0
            if max_queue < 0:
                max_queue = 0
            self._ws_event_queue = asyncio.Queue(maxsize=max_queue)
            self._ws_max_workers = max(1, ws_max_workers)

        # 可选：外部注入的状态获取函数
        self._get_plugins_count: Callable[[], int] | None = None
        self._get_sessions_count: Callable[[], int] | None = None
        self._get_pending_jobs: Callable[[], int] | None = None
        self._get_metrics: Callable[[], dict[str, Any]] | None = None

        # 活跃的 WebSocket 连接集合
        self._active_sockets: set[web.WebSocketResponse] = set()
        # A constructed server may be invoked directly by embedders/tests;
        # network admission is still impossible until ``start`` binds a site.
        self._accepting_events = True
        self._active_handler_tasks: set[asyncio.Task[Any]] = set()
        self._handler_drain_timeout_seconds = 5.0

    def set_status_providers(
        self,
        plugins_count: Callable[[], int] | None = None,
        sessions_count: Callable[[], int] | None = None,
        pending_jobs: Callable[[], int] | None = None,
        metrics: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        """设置状态提供函数"""
        self._get_plugins_count = plugins_count
        self._get_sessions_count = sessions_count
        self._get_pending_jobs = pending_jobs
        self._get_metrics = metrics

    def update_token(self, token: str) -> None:
        """Replace the inbound token and revoke older WebSocket sessions."""
        if token == self.token:
            return
        self.token = token
        self._auth_generation += 1

        # Logical revocation is synchronous: callers must stop treating these
        # sockets as delivery targets before their asynchronous close runs.
        sockets = tuple(self._active_sockets)
        self._active_sockets.clear()
        for ws in sockets:
            self._schedule_ws_close(ws, reason=b"inbound token rotated")

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
            except Exception as exc:
                logger.debug("Closing revoked WebSocket failed: %s", exc)

        task = loop.create_task(close_socket())
        self._ws_close_tasks.add(task)

        def discard(done: asyncio.Task[None]) -> None:
            self._ws_close_tasks.discard(done)
            try:
                done.result()
            except (asyncio.CancelledError, Exception):
                pass

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

    @staticmethod
    def _payload_validation_error(payload: dict[str, Any]) -> tuple[str, int] | None:
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

        message = payload.get("message")
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

        if self._get_sessions_count:
            try:
                response["active_sessions"] = self._get_sessions_count()
            except Exception as exc:
                logger.warning("Sessions count unavailable: %s", exc)

        if self._get_pending_jobs:
            try:
                response["pending_jobs"] = self._get_pending_jobs()
            except Exception as exc:
                logger.warning("Pending jobs count unavailable: %s", exc)

        return web.json_response(response)

    async def metrics(self, request: web.Request) -> web.Response:
        """
        性能指标端点

        返回详细的性能指标数据。需要配置 metrics 提供函数。
        """
        if not self._authorized(request):
            return self._unauthorized_response()

        if not self._get_metrics:
            return web.json_response(
                {"error": "Metrics not configured"},
                status=501
            )

        try:
            metrics_data = self._get_metrics()
            return web.json_response(metrics_data)
        except Exception as exc:
            logger.exception("Failed to get metrics: %s", exc)
            return web.json_response(
                {"error": "Metrics unavailable"},
                status=500
            )

    def _format_uptime(self, seconds: float) -> str:
        """格式化运行时间为人类可读格式"""
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < SECONDS_PER_HOUR:
            minutes = int(seconds / 60)
            secs = int(seconds % 60)
            return f"{minutes}m {secs}s"
        elif seconds < SECONDS_PER_DAY:
            hours = int(seconds / SECONDS_PER_HOUR)
            minutes = int((seconds % SECONDS_PER_HOUR) / 60)
            return f"{hours}h {minutes}m"
        else:
            days = int(seconds / SECONDS_PER_DAY)
            hours = int((seconds % SECONDS_PER_DAY) / SECONDS_PER_HOUR)
            return f"{days}d {hours}h"

    def _authorized(self, request: web.Request) -> bool:
        auth = request.headers.get("Authorization", "")
        return verify_bearer_token(auth, self.token)

    async def post_event(self, request: web.Request) -> web.Response:
        """处理 HTTP POST 事件"""
        if not self._accepting_events:
            return web.json_response({"status": "shutting_down"}, status=503)
        if not self._authorized(request):
            return self._unauthorized_response()

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
        validation_error = self._payload_validation_error(payload)
        if validation_error is not None:
            message, status = validation_error
            return web.json_response({"error": message}, status=status)
        actions = await self._invoke_handler(normalize_inbound_message(payload))
        return web.json_response({"actions": actions}, dumps=lambda obj: json.dumps(obj, ensure_ascii=False))

    async def ws_handler(self, request: web.Request) -> web.StreamResponse:
        """处理 WebSocket 连接"""
        connection_generation = self._auth_generation
        if not self._authorized(request):
            raise web.HTTPUnauthorized()
        if not self.enable_ws:
            raise web.HTTPNotFound()
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if connection_generation != self._auth_generation:
            await ws.close(code=1008, message=b"inbound token rotated")
            return ws

        self._ensure_ws_workers()

        self._increment_ws_connections()
        self._active_sockets.add(ws)
        try:
            async for msg in ws:
                if connection_generation != self._auth_generation:
                    await ws.close(code=1008, message=b"inbound token rotated")
                    break
                if msg.type == web.WSMsgType.TEXT:
                    self._request_count += 1
                    try:
                        payload = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        await ws.send_str(
                            json.dumps({"error": "Payload must be a JSON object"}, ensure_ascii=False)
                        )
                        continue
                    validation_error = self._payload_validation_error(payload)
                    if validation_error is not None:
                        message, _status = validation_error
                        await ws.send_str(json.dumps({"error": message}, ensure_ascii=False))
                        continue
                    queue = self._ws_event_queue
                    if not queue:
                        continue
                    await queue.put(
                        (ws, connection_generation, normalize_inbound_message(payload))
                    )
        finally:
            self._active_sockets.discard(ws)
            self._decrement_ws_connections()
        return ws

    async def broadcast(self, action: dict[str, Any]) -> None:
        """向所有连接的 WebSocket 广播 Action"""
        if not self._active_sockets:
            return

        text = json.dumps(action, ensure_ascii=False)
        sockets = list(self._active_sockets)

        async def send_one(ws: web.WebSocketResponse) -> web.WebSocketResponse | None:
            try:
                await ws.send_str(text)
                return None
            except Exception as exc:
                logger.warning("Broadcast failed for one client: %s", exc)
                return ws

        failed_sockets = await asyncio.gather(*(send_one(ws) for ws in sockets))
        for ws in failed_sockets:
            if ws is not None:
                self._active_sockets.discard(ws)

    def active_ws_connections(self) -> int:
        return len(self._active_sockets)

    def has_active_ws_connections(self) -> bool:
        return bool(self._active_sockets)

    @staticmethod
    def _get_event_key(payload: dict[str, Any]) -> str | None:
        user_id = payload.get("user_id")
        if user_id is None:
            return None
        group_id = payload.get("group_id")
        if group_id is None:
            return f"user:{user_id}"
        return f"group:{group_id}:user:{user_id}"

    async def _handle_ws_event(
        self,
        ws: web.WebSocketResponse,
        payload: dict[str, Any],
        auth_generation: int | None = None,
    ) -> None:
        if not self._accepting_events:
            return
        if auth_generation is not None and auth_generation != self._auth_generation:
            return
        """处理 WebSocket 事件（非阻塞）"""
        try:
            payload = normalize_inbound_message(payload)
            payload["_source"] = "inbound_ws"
            key = self._get_event_key(payload)
            if key:
                async with self._ws_event_lock(key):
                    actions = await self._invoke_handler(payload)
            else:
                actions = await self._invoke_handler(payload)
            if auth_generation is not None and auth_generation != self._auth_generation:
                return
            for action in actions:
                await ws.send_str(json.dumps(action, ensure_ascii=False))
        except Exception as exc:
            logger.exception("WebSocket event handler error: %s", exc)

    @asynccontextmanager
    async def _ws_event_lock(self, key: str) -> AsyncIterator[None]:
        """Acquire a keyed lock and remove it once no worker references it."""
        entry = self._ws_event_locks.get(key)
        if entry is None:
            entry = _KeyedEventLock(lock=asyncio.Lock())
            self._ws_event_locks[key] = entry
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0 and self._ws_event_locks.get(key) is entry:
                del self._ws_event_locks[key]

    def _ensure_ws_workers(self) -> None:
        if not self.enable_ws or not self._ws_event_queue:
            return
        alive = [task for task in self._ws_worker_tasks if not task.done()]
        needed = self._ws_max_workers - len(alive)
        for _ in range(needed):
            alive.append(asyncio.create_task(self._ws_worker_loop()))
        self._ws_worker_tasks = alive

    async def _ws_worker_loop(self) -> None:
        while True:
            try:
                queue = self._ws_event_queue
                if not queue:
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

    async def start(self) -> None:
        if not is_loopback_host(self.host):
            if self.trusted_tls_proxy is not True:
                raise ValueError(
                    "non-loopback inbound server bind is plaintext and requires "
                    "trusted_tls_proxy=true"
                )
            if not self.token.strip():
                raise ValueError("non-loopback inbound server bind requires a non-empty token")
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port, ssl_context=None)
        await self._site.start()
        self._accepting_events = True
        logger.info(
            "Inbound server listening on %s:%s (http=%s ws=%s)",
            self.host,
            self.port,
            self.enable_http,
            self.enable_ws,
        )

    async def stop(self) -> None:
        """停止入站服务器并释放资源"""
        # Close admission before touching plugins or shared clients.  Existing
        # handlers get a bounded grace period and are then cancelled.
        self._accepting_events = False
        if self._site:
            await self._site.stop()
            self._site = None

        active = [
            task for task in self._active_handler_tasks
            if task is not asyncio.current_task() and not task.done()
        ]
        if active:
            done, pending = await asyncio.wait(
                active,
                timeout=self._handler_drain_timeout_seconds,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

        close_tasks = [task for task in self._ws_close_tasks if not task.done()]
        if close_tasks:
            done, pending = await asyncio.wait(
                close_tasks,
                timeout=self._handler_drain_timeout_seconds,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._ws_close_tasks.clear()

        if self._ws_worker_tasks:
            for task in self._ws_worker_tasks:
                task.cancel()
            await asyncio.gather(*self._ws_worker_tasks, return_exceptions=True)
            self._ws_worker_tasks.clear()
        self._ws_event_locks.clear()

    async def _invoke_handler(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        if not self._accepting_events:
            raise RuntimeError("inbound server is shutting down")
        task = asyncio.current_task()
        if task is not None:
            self._active_handler_tasks.add(task)
        try:
            return await self.handler(payload)
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

class InboundManager:
    def __init__(
        self,
        *,
        inbound_http_base: str,
        inbound_ws_uri: str,
        token: str,
        handler: Callable[[dict[str, Any]], Awaitable[list[dict[str, Any]]]],
        ws_max_workers: int = DEFAULT_INBOUND_WS_MAX_WORKERS,
        ws_queue_size: int = DEFAULT_INBOUND_WS_QUEUE_SIZE,
        trusted_tls_proxy: bool = False,
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
        self._inbound_ws_uri = inbound_ws_uri
        self._trusted_tls_proxy = trusted_tls_proxy
        self._token = token
        self._handler = handler
        self._ws_max_workers = _parse_positive_int(
            ws_max_workers,
            default=DEFAULT_INBOUND_WS_MAX_WORKERS,
            min_value=1,
        )
        self._ws_queue_size = _parse_non_negative_int(
            ws_queue_size,
            default=DEFAULT_INBOUND_WS_QUEUE_SIZE,
        )

        self.http_server: InboundServer | None = None
        self.ws_server: InboundServer | None = None
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

    @classmethod
    def from_config(
        cls,
        *,
        config: dict[str, Any],
        token: str,
        handler: Callable[[dict[str, Any]], Awaitable[list[dict[str, Any]]]],
        default_ws_max_workers: int = DEFAULT_INBOUND_WS_MAX_WORKERS,
        default_ws_queue_size: int = DEFAULT_INBOUND_WS_QUEUE_SIZE,
    ) -> "InboundManager | None":
        enabled = bool(config.get("enable_inbound_server", True))
        if not enabled:
            logger.info("Inbound server disabled")
            return None

        inbound_http_base = str(config.get("inbound_http_base", "") or "").strip()
        inbound_ws_uri = str(config.get("inbound_ws_uri", "") or "").strip()
        if not inbound_http_base and not inbound_ws_uri:
            logger.info("Inbound server disabled (inbound_http_base/inbound_ws_uri are empty)")
            return None

        ws_max_workers = _parse_positive_int(
            config.get("inbound_ws_max_workers", default_ws_max_workers),
            default=default_ws_max_workers,
            min_value=1,
        )
        ws_queue_size = _parse_non_negative_int(
            config.get("ws_queue_size", default_ws_queue_size),
            default=default_ws_queue_size,
        )
        raw_trusted_tls_proxy = config.get("inbound_trusted_tls_proxy", False)
        if type(raw_trusted_tls_proxy) is not bool:
            raise ValueError("inbound_trusted_tls_proxy must be a boolean")
        trusted_tls_proxy = raw_trusted_tls_proxy
        return cls(
            inbound_http_base=inbound_http_base,
            inbound_ws_uri=inbound_ws_uri,
            token=token,
            handler=handler,
            ws_max_workers=ws_max_workers,
            ws_queue_size=ws_queue_size,
            trusted_tls_proxy=trusted_tls_proxy,
        )

    async def broadcast(self, action: dict[str, Any]) -> None:
        """广播 Action 到所有 Inbound WebSocket 客户端"""
        tasks = []
        if self.http_server:
            # http server 同时也可能包含 ws (enable_ws=True)
            tasks.append(self.http_server.broadcast(action))

        # 如果 ws_server 是独立实例且不相同
        if self.ws_server and self.ws_server is not self.http_server:
            tasks.append(self.ws_server.broadcast(action))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def active_ws_connections(self) -> int:
        total = 0
        for server in {self.http_server, self.ws_server}:
            if server:
                total += server.active_ws_connections()
        return total

    def has_active_ws_clients(self) -> bool:
        return self.active_ws_connections() > 0

    @property
    def config_key(self) -> tuple[str, str, int, int, bool]:
        return (
            self._inbound_http_base,
            self._inbound_ws_uri,
            self._ws_max_workers,
            self._ws_queue_size,
            self._trusted_tls_proxy,
        )

    def set_status_providers(
        self,
        plugins_count: Callable[[], int] | None = None,
        sessions_count: Callable[[], int] | None = None,
        pending_jobs: Callable[[], int] | None = None,
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
        if token == self._token:
            return
        self._token = token
        for server in {self.http_server, self.ws_server}:
            if server:
                server.update_token(token)

    async def start(self) -> None:
        http_parsed = _parse_http_base(self._inbound_http_base)
        ws_parsed = _parse_ws_uri(self._inbound_ws_uri)

        if not http_parsed and not ws_parsed:
            return

        if http_parsed and ws_parsed and http_parsed == (ws_parsed[0], ws_parsed[1]):
            host, port = http_parsed
            _, _, path = ws_parsed
            server = InboundServer(
                host,
                port,
                self._token,
                self._handler,
                enable_http=True,
                enable_ws=True,
                ws_path=path,
                ws_max_workers=self._ws_max_workers,
                ws_queue_size=self._ws_queue_size,
                trusted_tls_proxy=self._trusted_tls_proxy,
            )
            self._apply_status_providers(server)
            await server.start()
            self.http_server = server
            self.ws_server = server
            return

        if http_parsed:
            host, port = http_parsed
            server = InboundServer(
                host,
                port,
                self._token,
                self._handler,
                enable_http=True,
                enable_ws=False,
                trusted_tls_proxy=self._trusted_tls_proxy,
            )
            self._apply_status_providers(server)
            await server.start()
            self.http_server = server

        if ws_parsed:
            host, port, path = ws_parsed
            server = InboundServer(
                host,
                port,
                self._token,
                self._handler,
                enable_http=False,
                enable_ws=True,
                ws_path=path,
                ws_max_workers=self._ws_max_workers,
                ws_queue_size=self._ws_queue_size,
                trusted_tls_proxy=self._trusted_tls_proxy,
            )
            self._apply_status_providers(server)
            await server.start()
            self.ws_server = server

    async def stop(self) -> None:
        for server in {self.http_server, self.ws_server}:
            if server:
                await server.stop()
        self.http_server = None
        self.ws_server = None
