"""入站服务器测试共享 fixture、导入和私有 helper。"""

import asyncio
import contextlib
import json
import tomllib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from aiohttp import ClientSession, WSServerHandshakeError, web

from core.server import (
    VERSION,
    BroadcastResult,
    InboundManager,
    InboundServer,
    _InboundEventDispatcher,
    _parse_positive_int,
)
from tests.helpers.asyncio_tools import (
    BlockingConcurrencyProbe,
    cancellation_resistant_callback,
)


class StandaloneInboundServer(InboundServer):
    """测试里单独起的入站服务器，自己带一个事件分发器并管它的启停。

    生产上分发器由 ``InboundManager`` 建好交给服务器；这里替测试做同一件事。事件处理函数
    在执行时才从 ``server.handler`` 取，测试中途换掉它也生效。
    """

    def __init__(self, host: str, port: int, token: str, handler: Any, **options: Any) -> None:
        dispatcher = _InboundEventDispatcher(
            self._call_current_handler,
            max_workers=max(1, _parse_positive_int(options.get("ws_max_workers", 8), default=1)),
            queue_size=_parse_positive_int(options.get("ws_queue_size", 200), default=200),
        )
        super().__init__(host, port, token, handler, event_dispatcher=dispatcher, **options)

    async def _call_current_handler(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        return await self.handler(payload)

    async def start(self, *, accept_events: bool = True) -> None:
        await super().start(accept_events=accept_events)
        await self._event_dispatcher.start()

    async def stop(self) -> None:
        try:
            await super().stop()
        finally:
            self._event_dispatcher._drain_timeout_seconds = self._handler_drain_timeout_seconds
            await self._event_dispatcher.stop()


def auth_headers(token: str | None = "test_token") -> dict[str, str]:
    """HTTP 请求头：带令牌时为 Bearer 鉴权，``None`` 表示不带鉴权。"""

    return {} if token is None else {"Authorization": f"Bearer {token}"}


@dataclass(frozen=True)
class HttpResult:
    """读完整个响应之后的状态码和正文。"""

    status: int
    text  : str

    def json(self) -> Any:
        return json.loads(self.text)


async def http_request(
    session: ClientSession,
    method: str,
    path: str,
    *,
    token: str | None              = "test_token",
    headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> HttpResult:
    """对真实启动的服务器发一次请求；``token`` 为 ``None`` 时不带鉴权头。"""

    request_headers = {**auth_headers(token), **(headers or {})}
    async with session.request(method, path, headers=request_headers, **kwargs) as response:
        return HttpResult(response.status, await response.text())


async def post_event(
    session: ClientSession,
    payload: Any,
    *,
    token: str | None = "test_token",
    **kwargs: Any,
) -> HttpResult:
    """POST /event；响应模式取 ``actions``，正文带回处理器返回的动作列表。"""

    headers = {"X-XiaoQing-Response-Mode": "actions", **kwargs.pop("headers", {})}
    return await http_request(
        session, "POST", "/event", token=token, headers=headers, json=payload, **kwargs
    )


@contextlib.asynccontextmanager
async def serving(server: InboundServer) -> AsyncIterator[ClientSession]:
    """在 ``server.port`` 上真实启动服务器，交出指向它的 HTTP 客户端；退出时一定停掉。"""

    await server.start()
    try:
        async with ClientSession(base_url=f"http://{server.host}:{server.port}") as session:
            yield session
    finally:
        await server.stop()


async def wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """轮询到 ``condition()`` 成立；真实网络上的状态变化没有可等待的事件时使用。"""

    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not reached in time")
        await asyncio.sleep(0.005)


def onebot_message_payload(text: str = "/help") -> dict[str, Any]:
    return {
        "post_type"   : "message",
        "message_type": "private",
        "user_id"     : 10001,
        "raw_message" : text,
        "message"     : [{"type": "text", "data": {"text": text}}],
    }


def pending_for_key(dispatcher: _InboundEventDispatcher, key: str) -> int:
    """读取测试关注的通道占用数，不把测试探针暴露为生产接口。"""

    lane = dispatcher._lanes.get(key)
    if lane is None:
        return 0
    return len(lane.pending) + int(lane.running is not None)


def inflight_count(dispatcher: _InboundEventDispatcher) -> int:
    """读取测试关注的已准入事件数。"""

    return dispatcher._inflight


def lane_count(dispatcher: _InboundEventDispatcher) -> int:
    """读取测试关注的当前通道数。"""

    return len(dispatcher._lanes)


@pytest.fixture
def mock_handler():
    """Mock event handler"""

    async def handler(event):
        return [{"action": "test", "params": {}}]

    return handler


@pytest.fixture
def sample_server(mock_handler, unused_tcp_port):
    """空闲端口上的 InboundServer：构造后未启动，用 ``serving`` 启动。"""
    return make_server(mock_handler, unused_tcp_port)


def make_server(
    mock_handler,
    port: int,
    token: str = "test_token",
    **options: Any,
) -> InboundServer:
    """Create a server instance for tests that need to bind a real socket."""
    return StandaloneInboundServer(
        host    = "127.0.0.1",
        port    = port,
        token   = token,
        handler = mock_handler,
        **{
            "enable_http"   : True,
            "enable_ws"     : True,
            "ws_path"       : "/ws",
            "ws_max_workers": 2,
            "ws_queue_size" : 10,
            **options,
        },
    )


__all__ = (
    "Any",
    "BlockingConcurrencyProbe",
    "BroadcastResult",
    "ClientSession",
    "InboundManager",
    "InboundServer",
    "StandaloneInboundServer",
    "VERSION",
    "WSServerHandshakeError",
    "_InboundEventDispatcher",
    "HttpResult",
    "inflight_count",
    "lane_count",
    "auth_headers",
    "http_request",
    "make_server",
    "onebot_message_payload",
    "pending_for_key",
    "post_event",
    "serving",
    "wait_until",
    "_parse_positive_int",
    "asyncio",
    "cancellation_resistant_callback",
    "json",
    "mock_handler",
    "pytest",
    "sample_server",
    "tomllib",
    "web",
)
