"""入站服务器测试共享 fixture、导入和私有 helper。"""

import asyncio
import json
import threading
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from aiohttp import ClientSession, WSServerHandshakeError, web

from core.server import (
    VERSION,
    BroadcastResult,
    InboundManager,
    InboundServer,
    _InboundEventDispatcher,
    _parse_http_base,
    _parse_positive_int,
    _parse_ws_uri,
)
from tests.helpers.asyncio_tools import (
    BlockingConcurrencyProbe,
    cancellation_resistant_callback,
)


class _LazyEventDispatcher(_InboundEventDispatcher):
    """第一次收到事件时自己启动：测试可以不启动网络监听就直接调用服务器的处理函数。"""

    def admit(self, payload, *, auth_guard=None):
        if not self._accepting and not self._stopping and self._loop is None:
            self._start_on_loop(asyncio.get_running_loop())
        return super().admit(payload, auth_guard=auth_guard)


class StandaloneInboundServer(InboundServer):
    """测试里单独起的入站服务器，自己带一个事件分发器并管它的启停。

    生产上分发器由 ``InboundManager`` 建好交给服务器；这里替测试做同一件事。事件处理函数
    在执行时才从 ``server.handler`` 取，测试中途换掉它也生效。
    """

    def __init__(self, host: str, port: int, token: str, handler: Any, **options: Any) -> None:
        dispatcher = _LazyEventDispatcher(
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


class _MockRequest:
    """Mock request object with configurable headers"""

    def __init__(self, method: str, path: str, headers: dict | None = None):
        self.method     = method
        self.path       = path
        self.headers    = headers or {}
        self.query      = {}
        self.app        = None
        self.match_info = Mock()


def _make_request_with_auth(method: str, path: str, token: str) -> _MockRequest:
    """Create a mock request with Authorization header"""
    headers = {"Authorization": f"Bearer {token}"}
    if method.upper() == "POST":
        headers["Content-Type"]             = "application/json"
        headers["X-XiaoQing-Response-Mode"] = "actions"
    return _MockRequest(method, path, headers)


def _make_request_without_auth(method: str, path: str) -> _MockRequest:
    """Create a mock request without Authorization header"""
    return _MockRequest(method, path, {})


def _onebot_message_payload(text: str = "/help") -> dict[str, Any]:
    return {
        "post_type"   : "message",
        "message_type": "private",
        "user_id"     : 10001,
        "raw_message" : text,
        "message"     : [{"type": "text", "data": {"text": text}}],
    }


def _pending_for_key(dispatcher: _InboundEventDispatcher, key: str) -> int:
    """读取测试关注的通道占用数，不把测试探针暴露为生产接口。"""

    lane = dispatcher._lanes.get(key)
    if lane is None:
        return 0
    return len(lane.pending) + int(lane.running is not None)


def _inflight_count(dispatcher: _InboundEventDispatcher) -> int:
    """读取测试关注的已准入事件数。"""

    return dispatcher._inflight


def _lane_count(dispatcher: _InboundEventDispatcher) -> int:
    """读取测试关注的当前通道数。"""

    return len(dispatcher._lanes)


@pytest.fixture
def mock_handler():
    """Mock event handler"""

    async def handler(event):
        return [{"action": "test", "params": {}}]

    return handler


@pytest.fixture
def sample_server(mock_handler):
    """Create a sample InboundServer for testing"""
    return _make_server(mock_handler, 8765)


def _make_server(mock_handler, port: int) -> InboundServer:
    """Create a server instance for tests that need to bind a real socket."""
    return StandaloneInboundServer(
        host           = "127.0.0.1",
        port           = port,
        token          = "test_token",
        handler        = mock_handler,
        enable_http    = True,
        enable_ws      = True,
        ws_path        = "/ws",
        ws_max_workers = 2,
        ws_queue_size  = 10,
    )


__all__ = (
    "Any",
    "AsyncMock",
    "BlockingConcurrencyProbe",
    "BroadcastResult",
    "ClientSession",
    "InboundManager",
    "InboundServer",
    "StandaloneInboundServer",
    "MagicMock",
    "Mock",
    "Path",
    "SimpleNamespace",
    "VERSION",
    "WSServerHandshakeError",
    "_InboundEventDispatcher",
    "_MockRequest",
    "_inflight_count",
    "_lane_count",
    "_make_request_with_auth",
    "_make_request_without_auth",
    "_make_server",
    "_onebot_message_payload",
    "_pending_for_key",
    "_parse_http_base",
    "_parse_positive_int",
    "_parse_ws_uri",
    "asyncio",
    "cancellation_resistant_callback",
    "json",
    "mock_handler",
    "patch",
    "pytest",
    "sample_server",
    "threading",
    "tomllib",
    "web",
)
