"""入站服务器测试共享 fixture、导入和私有 helper。"""

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from aiohttp import ClientSession, WSServerHandshakeError, web

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on CPython 3.10
    import tomli as tomllib


from core.server import (
    VERSION,
    BroadcastResult,
    InboundManager,
    InboundServer,
    _InboundEventDispatcher,
    _parse_http_base,
    _parse_non_negative_int,
    _parse_positive_int,
    _parse_ws_uri,
)
from tests.helpers.asyncio_tools import (
    BlockingConcurrencyProbe,
    cancellation_resistant_callback,
)


class _MockRequest:
    """Mock request object with configurable headers"""

    def __init__(self, method: str, path: str, headers: dict | None = None):
        self.method = method
        self.path = path
        self.headers = headers or {}
        self.query = {}
        self.app = None
        self.match_info = Mock()


def _make_request_with_auth(method: str, path: str, token: str) -> _MockRequest:
    """Create a mock request with Authorization header"""
    headers = {"Authorization": f"Bearer {token}"}
    if method.upper() == "POST":
        headers["Content-Type"] = "application/json"
    return _MockRequest(method, path, headers)


def _make_request_without_auth(method: str, path: str) -> _MockRequest:
    """Create a mock request without Authorization header"""
    return _MockRequest(method, path, {})


def _onebot_message_payload(text: str = "/help") -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "private",
        "user_id": 10001,
        "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
    }


@pytest.fixture
def mock_handler():
    """Mock event handler"""

    async def handler(event):
        return [{"action": "test", "params": {}}]

    return handler


@pytest.fixture
def sample_server(mock_handler):
    """Create a sample InboundServer for testing"""
    return InboundServer(
        host="127.0.0.1",
        port=8765,
        token="test_token",
        handler=mock_handler,
        enable_http=True,
        enable_ws=True,
        ws_path="/ws",
        ws_max_workers=2,
        ws_queue_size=10,
    )


def _make_server(mock_handler, port: int) -> InboundServer:
    """Create a server instance for tests that need to bind a real socket."""
    return InboundServer(
        host="127.0.0.1",
        port=port,
        token="test_token",
        handler=mock_handler,
        enable_http=True,
        enable_ws=True,
        ws_path="/ws",
        ws_max_workers=2,
        ws_queue_size=10,
    )


__all__ = (
    "Any",
    "AsyncMock",
    "BlockingConcurrencyProbe",
    "BroadcastResult",
    "ClientSession",
    "InboundManager",
    "InboundServer",
    "MagicMock",
    "Mock",
    "Path",
    "SimpleNamespace",
    "VERSION",
    "WSServerHandshakeError",
    "_InboundEventDispatcher",
    "_MockRequest",
    "_make_request_with_auth",
    "_make_request_without_auth",
    "_make_server",
    "_onebot_message_payload",
    "_parse_http_base",
    "_parse_non_negative_int",
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
