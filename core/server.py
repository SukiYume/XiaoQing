"""
Inbound Server 模块

提供 HTTP/WebSocket 入站服务器，接收来自外部的事件推送。
"""

import asyncio
import hmac
import json
import logging
import threading
import time
from urllib.parse import urlsplit
from typing import Any, Awaitable, Callable

from aiohttp import ContentTypeError, web

from .constants import DEFAULT_INBOUND_WS_MAX_WORKERS, DEFAULT_INBOUND_WS_QUEUE_SIZE, SECONDS_PER_HOUR, SECONDS_PER_DAY

logger = logging.getLogger(__name__)

# 版本号
VERSION = "1.0.0"

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
    ) -> None:
        self.host = host
        self.port = port
        self.ws_path = ws_path
        self.token = token
        self.handler = handler
        self.enable_http = bool(enable_http)
        self.enable_ws = bool(enable_ws)
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
        self._ws_connections_lock = threading.Lock()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

        self._ws_event_queue: asyncio.Queue[tuple[web.WebSocketResponse, dict[str, Any]]] | None = None
        self._ws_worker_tasks: list[asyncio.Task[None]] = []
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
        """更新认证 token"""
        self.token = token

    def _increment_ws_connections(self) -> None:
        with self._ws_connections_lock:
            self._ws_connections += 1

    def _decrement_ws_connections(self) -> None:
        with self._ws_connections_lock:
            self._ws_connections -= 1

    def _get_ws_connections(self) -> int:
        with self._ws_connections_lock:
            return self._ws_connections
    
    @staticmethod
    def _unauthorized_response() -> web.Response:
        return web.json_response({"status": "unauthorized"}, status=401)

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
        if not self.token:
            return True
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {self.token}"
        # 使用 hmac.compare_digest 防止时序攻击（不预先比较长度，避免泄露长度信息）
        return hmac.compare_digest(auth.encode(), expected.encode())

    async def post_event(self, request: web.Request) -> web.Response:
        """处理 HTTP POST 事件"""
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
        actions = await self.handler(payload)
        return web.json_response({"actions": actions}, dumps=lambda obj: json.dumps(obj, ensure_ascii=False))

    async def ws_handler(self, request: web.Request) -> web.StreamResponse:
        """处理 WebSocket 连接"""
        if not self._authorized(request):
            raise web.HTTPUnauthorized()
        if not self.enable_ws:
            raise web.HTTPNotFound()
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        self._ensure_ws_workers()
        
        self._increment_ws_connections()
        self._active_sockets.add(ws)
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data)
                        self._request_count += 1
                    except json.JSONDecodeError:
                        continue
                    queue = self._ws_event_queue
                    if not queue:
                        continue
                    await queue.put((ws, payload))
        finally:
            self._active_sockets.discard(ws)
            self._decrement_ws_connections()
        return ws

    async def broadcast(self, action: dict[str, Any]) -> None:
        """向所有连接的 WebSocket 广播 Action"""
        if not self._active_sockets:
            return
        
        text = json.dumps(action, ensure_ascii=False)
        # 复制集合以防迭代时变更
        for ws in list(self._active_sockets):
            try:
                await ws.send_str(text)
            except Exception as exc:
                logger.warning("Broadcast failed for one client: %s", exc)

    async def _handle_ws_event(self, ws: web.WebSocketResponse, payload: dict[str, Any]) -> None:
        """处理 WebSocket 事件（非阻塞）"""
        try:
            payload = dict(payload)
            payload["_source"] = "inbound_ws"
            actions = await self.handler(payload)
            for action in actions:
                await ws.send_str(json.dumps(action, ensure_ascii=False))
        except Exception as exc:
            logger.exception("WebSocket event handler error: %s", exc)

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
                ws, payload = await queue.get()
            except asyncio.CancelledError:
                break
            try:
                await self._handle_ws_event(ws, payload)
            finally:
                queue = self._ws_event_queue
                if queue:
                    queue.task_done()

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        logger.info(
            "Inbound server listening on %s:%s (http=%s ws=%s)",
            self.host,
            self.port,
            self.enable_http,
            self.enable_ws,
        )

    async def stop(self) -> None:
        """停止入站服务器并释放资源"""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

        if self._ws_worker_tasks:
            for task in self._ws_worker_tasks:
                task.cancel()
            await asyncio.gather(*self._ws_worker_tasks, return_exceptions=True)
            self._ws_worker_tasks.clear()

def _parse_http_base(value: Any) -> tuple[str, int] | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    if not parts.scheme or not parts.hostname or not parts.port:
        return None
    if parts.scheme not in {"http", "https"}:
        return None
    return parts.hostname, int(parts.port)

def _parse_ws_uri(value: Any, *, default_path: str = "/ws") -> tuple[str, int, str] | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    if not parts.scheme or not parts.hostname or not parts.port:
        return None
    if parts.scheme not in {"ws", "wss"}:
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
    ) -> None:
        self._inbound_http_base = inbound_http_base
        self._inbound_ws_uri = inbound_ws_uri
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
        return cls(
            inbound_http_base=inbound_http_base,
            inbound_ws_uri=inbound_ws_uri,
            token=token,
            handler=handler,
            ws_max_workers=ws_max_workers,
            ws_queue_size=ws_queue_size,
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

    def update_token(self, token: str) -> None:
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
            )
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
            )
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
            )
            await server.start()
            self.ws_server = server

    async def stop(self) -> None:
        for server in {self.http_server, self.ws_server}:
            if server:
                await server.stop()
        self.http_server = None
        self.ws_server = None
