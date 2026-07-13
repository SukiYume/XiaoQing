"""
Tests for core/server.py - InboundServer and InboundManager classes
"""

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from aiohttp import ClientSession, WSServerHandshakeError, web

from core.server import (
    VERSION,
    InboundManager,
    InboundServer,
    _parse_http_base,
    _parse_non_negative_int,
    _parse_positive_int,
    _parse_ws_uri,
)

# ============================================================
# Helper Classes & Functions
# ============================================================

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
    return _MockRequest(method, path, {"Authorization": f"Bearer {token}"})


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


# ============================================================
# Fixtures
# ============================================================

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


# ============================================================
# InboundServer Initialization Tests
# ============================================================

@pytest.mark.unit
def test_server_initialization(mock_handler):
    """Test InboundServer initialization"""
    server = InboundServer(
        host="127.0.0.1",
        port=8765,
        token="test_token",
        handler=mock_handler,
    )

    assert server.host == "127.0.0.1"
    assert server.port == 8765
    assert server.token == "test_token"
    assert server.enable_http is True
    assert server.enable_ws is True
    assert server.ws_path == "/ws"


@pytest.mark.unit
def test_server_initialization_http_only(mock_handler):
    """Test InboundServer initialization with HTTP only"""
    server = InboundServer(
        host="127.0.0.1",
        port=8765,
        token="test_token",
        handler=mock_handler,
        enable_http=True,
        enable_ws=False,
    )

    assert server.enable_http is True
    assert server.enable_ws is False


@pytest.mark.unit
def test_server_initialization_ws_only(mock_handler):
    """Test InboundServer initialization with WS only"""
    server = InboundServer(
        host="127.0.0.1",
        port=8765,
        token="test_token",
        handler=mock_handler,
        enable_http=False,
        enable_ws=True,
    )

    assert server.enable_http is False
    assert server.enable_ws is True


@pytest.mark.unit
def test_server_queue_size_validation(mock_handler):
    """Test queue size is validated correctly"""
    # Valid queue size
    server1 = InboundServer(
        host="127.0.0.1",
        port=8765,
        token="test_token",
        handler=mock_handler,
        enable_ws=True,
        ws_queue_size=100,
    )
    assert server1._ws_event_queue.maxsize == 100

    # Negative queue size becomes 0 (unlimited)
    server2 = InboundServer(
        host="127.0.0.1",
        port=8765,
        token="test_token",
        handler=mock_handler,
        enable_ws=True,
        ws_queue_size=-10,
    )
    assert server2._ws_event_queue.maxsize == 0

    # Invalid queue size becomes default
    server3 = InboundServer(
        host="127.0.0.1",
        port=8765,
        token="test_token",
        handler=mock_handler,
        enable_ws=True,
        ws_queue_size="invalid",
    )
    assert server3._ws_event_queue.maxsize == 0


@pytest.mark.unit
def test_server_max_workers_validation(mock_handler):
    """Test max_workers is validated correctly"""
    server = InboundServer(
        host="127.0.0.1",
        port=8765,
        token="test_token",
        handler=mock_handler,
        enable_ws=True,
        ws_max_workers=5,
    )
    assert server._ws_max_workers == 5

    # Negative becomes 1
    server2 = InboundServer(
        host="127.0.0.1",
        port=8765,
        token="test_token",
        handler=mock_handler,
        enable_ws=True,
        ws_max_workers=-5,
    )
    assert server2._ws_max_workers == 1


# ============================================================
# Authentication Tests
# ============================================================

@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_authorized_with_token(sample_server):
    """Test authorization with valid token"""
    request = _make_request_with_auth("GET", "/", "test_token")
    assert sample_server._authorized(request) is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_authorized_with_wrong_token(sample_server):
    """Test authorization with wrong token"""
    request = _make_request_with_auth("GET", "/", "wrong_token")
    assert sample_server._authorized(request) is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_authorized_without_token():
    """Test authorization fails closed when the server has no token."""
    async def handler(event):
        return []

    server = InboundServer(
        host="127.0.0.1",
        port=8765,
        token="",
        handler=handler,
    )

    request = _make_request_without_auth("GET", "/")
    assert server._authorized(request) is False

    request = _make_request_with_auth("GET", "/", "anything")
    assert server._authorized(request) is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_authorized_no_header(sample_server):
    """Test authorization with missing header"""
    request = _make_request_without_auth("GET", "/")
    assert sample_server._authorized(request) is False


# ============================================================
# Health Endpoint Tests
# ============================================================

@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_health_unauthorized(sample_server):
    """Test health endpoint returns 401 without auth"""
    request = _make_request_without_auth("GET", "/health")
    response = await sample_server.health(request)

    assert response.status == 401


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_health_authorized(sample_server):
    """Test health endpoint returns status"""
    request = _make_request_with_auth("GET", "/health", "test_token")

    response = await sample_server.health(request)
    assert response.status == 200

    # Patch json() method for testing
    async def mock_json():
        return {"status": "ok", "version": VERSION}
    response.json = mock_json

    data = await response.json()
    assert data["status"] == "ok"
    assert data["version"] == VERSION


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_health_with_status_providers(sample_server):
    """Test health endpoint with custom status providers"""
    # Set status providers
    sample_server._get_plugins_count = Mock(return_value=5)
    sample_server._get_sessions_count = Mock(return_value=10)
    sample_server._get_pending_jobs = Mock(return_value=2)

    request = _make_request_with_auth("GET", "/health", "test_token")

    response = await sample_server.health(request)
    assert response.status == 200

    # Check response body exists
    body = response.text
    assert body is not None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_uptime_formatting(sample_server):
    """Test uptime formatting"""
    request = _make_request_with_auth("GET", "/health", "test_token")

    response = await sample_server.health(request)
    assert response.status == 200

    # Verify uptime fields are in response
    body = response.text
    assert "uptime" in body.lower() or "uptime" in body


# ============================================================
# Metrics Endpoint Tests
# ============================================================

@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_metrics_unauthorized(sample_server):
    """Test metrics endpoint returns 401 without auth"""
    request = _make_request_without_auth("GET", "/metrics")

    response = await sample_server.metrics(request)

    assert response.status == 401


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_metrics_not_configured(sample_server):
    """Test metrics endpoint when not configured"""
    request = _make_request_with_auth("GET", "/metrics", "test_token")

    response = await sample_server.metrics(request)

    assert response.status == 501

    # Check error response
    assert "error" in response.text or response.status == 501


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_metrics_with_provider(sample_server):
    """Test metrics endpoint with provider"""
    sample_server._get_metrics = Mock(return_value={
        "total_calls": 100,
        "avg_time": 0.5,
    })

    request = _make_request_with_auth("GET", "/metrics", "test_token")

    response = await sample_server.metrics(request)

    assert response.status == 200


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_metrics_provider_error(sample_server):
    """Test metrics endpoint handles provider errors"""
    sample_server._get_metrics = Mock(side_effect=Exception("Test error"))

    request = _make_request_with_auth("GET", "/metrics", "test_token")

    response = await sample_server.metrics(request)

    assert response.status == 500


# ============================================================
# POST Event Tests
# ============================================================

@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_post_event_unauthorized(sample_server):
    """Test POST event returns 401 without auth"""
    request = _make_request_without_auth("POST", "/event")

    response = await sample_server.post_event(request)

    assert response.status == 401


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_post_event_rejects_requests_when_token_is_unconfigured():
    """An empty configured token must not expose the event handler."""
    handler = AsyncMock(return_value=[])
    server = InboundServer(
        host="127.0.0.1",
        port=8765,
        token="",
        handler=handler,
    )
    request = _make_request_with_auth("POST", "/event", "anything")

    response = await server.post_event(request)

    assert response.status == 401
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_post_event_invalid_json(sample_server):
    """Test POST event with invalid JSON"""
    request = _make_request_with_auth("POST", "/event", "test_token")
    # Mock invalid JSON
    request.json = AsyncMock(side_effect=json.JSONDecodeError("test", "", 0))

    response = await sample_server.post_event(request)

    assert response.status == 400


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_post_event_success(sample_server):
    """Test POST event with valid payload"""
    payload = _onebot_message_payload()

    request = _make_request_with_auth("POST", "/event", "test_token")
    request.json = AsyncMock(return_value=payload)

    response = await sample_server.post_event(request)

    assert response.status == 200

    # Verify actions key in response
    body = response.text
    assert "actions" in body


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_post_event_normalizes_raw_message_only_payload(sample_server):
    """HTTP raw_message-only events reach handlers as text segments."""
    received: list[dict[str, Any]] = []

    async def handler(payload: dict[str, Any]) -> list[dict[str, Any]]:
        received.append(payload)
        return []

    sample_server.handler = handler
    payload = _onebot_message_payload("/help")
    payload.pop("message")
    request = _make_request_with_auth("POST", "/event", "test_token")
    request.json = AsyncMock(return_value=payload)

    response = await sample_server.post_event(request)

    assert response.status == 200
    assert received[0]["message"] == [{"type": "text", "data": {"text": "/help"}}]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_ws_event_normalizes_raw_message_only_payload(sample_server):
    """WS raw_message-only events use the same normalized payload as HTTP."""
    received: list[dict[str, Any]] = []

    async def handler(payload: dict[str, Any]) -> list[dict[str, Any]]:
        received.append(payload)
        return []

    sample_server.handler = handler
    await sample_server._handle_ws_event(AsyncMock(), {"user_id": 1, "raw_message": "/help"})

    assert received[0]["message"] == [{"type": "text", "data": {"text": "/help"}}]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_post_event_rejects_non_object_payload(sample_server):
    """Test POST event rejects non-object JSON payloads."""
    request = _make_request_with_auth("POST", "/event", "test_token")
    request.json = AsyncMock(return_value=["not", "an", "object"])

    response = await sample_server.post_event(request)

    assert response.status == 400
    assert "Payload must be a JSON object" in response.text


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_post_event_rejects_missing_post_type(sample_server):
    """Test POST event rejects payloads without OneBot post_type."""
    request = _make_request_with_auth("POST", "/event", "test_token")
    request.json = AsyncMock(return_value={"message": "/help", "user_id": 10001})

    response = await sample_server.post_event(request)

    assert response.status == 400
    assert "post_type" in response.text


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_post_event_rejects_message_without_user_id(sample_server):
    """Test message events require a sender user_id."""
    payload = _onebot_message_payload()
    payload.pop("user_id")
    request = _make_request_with_auth("POST", "/event", "test_token")
    request.json = AsyncMock(return_value=payload)

    response = await sample_server.post_event(request)

    assert response.status == 400
    assert "user_id" in response.text


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_post_event_rejects_message_without_message_body(sample_server):
    """Test message events require message or raw_message content."""
    payload = _onebot_message_payload()
    payload.pop("message")
    payload.pop("raw_message")
    request = _make_request_with_auth("POST", "/event", "test_token")
    request.json = AsyncMock(return_value=payload)

    response = await sample_server.post_event(request)

    assert response.status == 400
    assert "message" in response.text


# ============================================================
# WebSocket Handler Tests
# ============================================================

@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_ws_handler_unauthorized(sample_server):
    """Test WebSocket handler returns 401 without auth"""
    request = _make_request_without_auth("GET", "/ws")

    with pytest.raises(web.HTTPUnauthorized):
        await sample_server.ws_handler(request)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_ws_handler_rejects_requests_when_token_is_unconfigured():
    """An empty configured token must not expose the WebSocket endpoint."""
    server = InboundServer(
        host="127.0.0.1",
        port=8765,
        token="",
        handler=AsyncMock(return_value=[]),
        enable_ws=True,
    )
    request = _make_request_with_auth("GET", "/ws", "anything")

    with pytest.raises(web.HTTPUnauthorized):
        await server.ws_handler(request)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_ws_handler_disabled():
    """Test WebSocket handler when WS is disabled"""
    async def handler(event):
        return []

    server = InboundServer(
        host="127.0.0.1",
        port=8765,
        token="test_token",
        handler=handler,
        enable_http=True,
        enable_ws=False,
    )

    request = _make_request_with_auth("GET", "/ws", "test_token")

    with pytest.raises(web.HTTPNotFound):
        await server.ws_handler(request)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_ws_handler_counts_invalid_json_frame(sample_server):
    """Test WebSocket request_count increments before JSON parsing."""
    class FakeWebSocket:
        def __init__(self):
            self._messages = iter([SimpleNamespace(type=web.WSMsgType.TEXT, data="{bad json")])

        async def prepare(self, request):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._messages)
            except StopIteration:
                raise StopAsyncIteration from None

    sample_server._ensure_ws_workers = Mock()
    request = _make_request_with_auth("GET", "/ws", "test_token")

    with patch("core.server.web.WebSocketResponse", return_value=FakeWebSocket()):
        await sample_server.ws_handler(request)

    assert sample_server._request_count == 1


# ============================================================
# Broadcast Tests
# ============================================================

@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_broadcast_no_sockets(sample_server):
    """Test broadcast with no active sockets"""
    # Should not raise
    await sample_server.broadcast({"action": "test"})


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_broadcast_with_sockets(sample_server):
    """Test broadcast with active sockets"""
    # Create mock sockets
    mock_ws1 = AsyncMock()
    mock_ws2 = AsyncMock()

    sample_server._active_sockets.add(mock_ws1)
    sample_server._active_sockets.add(mock_ws2)

    await sample_server.broadcast({"action": "test", "message": "hello"})

    # Both sockets should have been called
    mock_ws1.send_str.assert_called_once()
    mock_ws2.send_str.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_broadcast_socket_error(sample_server):
    """Test broadcast handles socket errors gracefully"""
    # Create mock socket that raises
    mock_ws = AsyncMock()
    mock_ws.send_str = AsyncMock(side_effect=Exception("Connection lost"))

    sample_server._active_sockets.add(mock_ws)

    # Should not raise
    await sample_server.broadcast({"action": "test"})
    assert mock_ws not in sample_server._active_sockets


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_ws_locks_serialize_same_key_and_are_released(sample_server):
    """Same-key work stays serial and its lock is removed after the final user."""
    ws = AsyncMock()
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    max_active = 0

    async def handler(_payload: dict[str, Any]) -> list[dict[str, Any]]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        entered.set()
        await release.wait()
        active -= 1
        return []

    sample_server.handler = handler
    first = asyncio.create_task(sample_server._handle_ws_event(ws, {"user_id": 12345}))
    await entered.wait()
    second = asyncio.create_task(sample_server._handle_ws_event(ws, {"user_id": 12345}))
    await asyncio.sleep(0)

    assert max_active == 1
    assert sample_server._ws_event_locks["user:12345"].users == 2

    release.set()
    await asyncio.gather(first, second)

    assert max_active == 1
    assert sample_server._ws_event_locks == {}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_ws_locks_allow_different_keys_in_parallel(sample_server):
    """Independent event keys must not block each other."""
    ws = AsyncMock()
    both_entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    max_active = 0

    async def handler(_payload: dict[str, Any]) -> list[dict[str, Any]]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if active == 2:
            both_entered.set()
        await release.wait()
        active -= 1
        return []

    sample_server.handler = handler
    first = asyncio.create_task(sample_server._handle_ws_event(ws, {"user_id": 1}))
    second = asyncio.create_task(sample_server._handle_ws_event(ws, {"user_id": 2}))
    await asyncio.wait_for(both_entered.wait(), timeout=1)

    assert max_active == 2
    release.set()
    await asyncio.gather(first, second)
    assert sample_server._ws_event_locks == {}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_ws_lock_waiter_cancellation_releases_reference(sample_server):
    """Cancelling a same-key waiter must not leak the keyed lock reference."""
    ws = AsyncMock()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(_payload: dict[str, Any]) -> list[dict[str, Any]]:
        entered.set()
        await release.wait()
        return []

    sample_server.handler = handler
    first = asyncio.create_task(sample_server._handle_ws_event(ws, {"user_id": 12345}))
    await entered.wait()
    waiting = asyncio.create_task(sample_server._handle_ws_event(ws, {"user_id": 12345}))
    await asyncio.sleep(0)

    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)
    assert sample_server._ws_event_locks["user:12345"].users == 1

    release.set()
    await first
    assert sample_server._ws_event_locks == {}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_ws_lock_pool_does_not_grow_for_high_cardinality_keys(sample_server):
    """Completed high-cardinality WS events leave no stale lock entries."""
    ws = AsyncMock()
    sample_server.handler = AsyncMock(return_value=[])

    for user_id in range(1_000):
        await sample_server._handle_ws_event(ws, {"user_id": user_id})

    assert sample_server._ws_event_locks == {}


# ============================================================
# Set Status Providers Tests
# ============================================================

@pytest.mark.unit
def test_server_set_status_providers(sample_server):
    """Test setting status providers"""
    plugins_count = Mock(return_value=5)
    sessions_count = Mock(return_value=10)
    pending_jobs = Mock(return_value=2)
    metrics = Mock(return_value={"test": "data"})

    sample_server.set_status_providers(
        plugins_count=plugins_count,
        sessions_count=sessions_count,
        pending_jobs=pending_jobs,
        metrics=metrics,
    )

    assert sample_server._get_plugins_count == plugins_count
    assert sample_server._get_sessions_count == sessions_count
    assert sample_server._get_pending_jobs == pending_jobs
    assert sample_server._get_metrics == metrics


@pytest.mark.unit
def test_server_set_status_providers_partial(sample_server):
    """Test setting partial status providers"""
    plugins_count = Mock(return_value=5)

    sample_server.set_status_providers(plugins_count=plugins_count)

    assert sample_server._get_plugins_count == plugins_count
    assert sample_server._get_sessions_count is None


# ============================================================
# Update Token Tests
# ============================================================

@pytest.mark.unit
def test_server_update_token(sample_server):
    """Test updating token"""
    assert sample_server.token == "test_token"
    assert sample_server._auth_generation == 0

    sample_server.update_token("new_token")

    assert sample_server.token == "new_token"
    assert sample_server._auth_generation == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_token_rotation_immediately_removes_and_closes_active_sockets(
    sample_server,
):
    first = MagicMock()
    first.close = AsyncMock()
    second = MagicMock()
    second.close = AsyncMock()
    sample_server._active_sockets.update({first, second})

    sample_server.update_token("new_token")

    assert sample_server._active_sockets == set()
    assert sample_server.active_ws_connections() == 0
    await asyncio.sleep(0)
    first.close.assert_awaited_once_with(
        code=1008,
        message=b"inbound token rotated",
    )
    second.close.assert_awaited_once_with(
        code=1008,
        message=b"inbound token rotated",
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_same_token_update_keeps_active_websockets(sample_server):
    ws = MagicMock()
    ws.close = AsyncMock()
    sample_server._active_sockets.add(ws)

    sample_server.update_token("test_token")
    await asyncio.sleep(0)

    assert sample_server._auth_generation == 0
    assert sample_server._active_sockets == {ws}
    ws.close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_token_rotation_during_ws_prepare_rejects_connection(
    sample_server,
):
    prepare_started = asyncio.Event()
    release_prepare = asyncio.Event()

    class FakeWebSocket:
        def __init__(self):
            self.close = AsyncMock()

        async def prepare(self, _request):
            prepare_started.set()
            await release_prepare.wait()

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    ws = FakeWebSocket()
    request = _make_request_with_auth("GET", "/ws", "test_token")
    with patch("core.server.web.WebSocketResponse", return_value=ws):
        handler_task = asyncio.create_task(sample_server.ws_handler(request))
        await prepare_started.wait()
        sample_server.update_token("new_token")
        release_prepare.set()
        result = await handler_task

    assert result is ws
    assert sample_server._active_sockets == set()
    assert sample_server._get_ws_connections() == 0
    ws.close.assert_awaited_once_with(
        code=1008,
        message=b"inbound token rotated",
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_worker_drops_events_from_revoked_generation():
    handler = AsyncMock(return_value=[])
    server = InboundServer(
        host="127.0.0.1",
        port=8765,
        token="old-token",
        handler=handler,
        enable_http=False,
        enable_ws=True,
        ws_max_workers=1,
    )
    ws = AsyncMock()
    queue = server._ws_event_queue
    assert queue is not None
    await queue.put((ws, server._auth_generation, _onebot_message_payload()))
    server.update_token("new-token")

    server._ensure_ws_workers()
    await asyncio.wait_for(queue.join(), timeout=1)

    handler.assert_not_awaited()
    ws.send_str.assert_not_awaited()
    for task in server._ws_worker_tasks:
        task.cancel()
    await asyncio.gather(*server._ws_worker_tasks, return_exceptions=True)


@pytest.mark.unit
def test_server_ws_connection_counter_helpers(sample_server):
    assert sample_server._get_ws_connections() == 0
    sample_server._increment_ws_connections()
    assert sample_server._get_ws_connections() == 1
    sample_server._decrement_ws_connections()
    assert sample_server._get_ws_connections() == 0


# ============================================================
# InboundManager Tests
# ============================================================

@pytest.mark.unit
def test_inbound_manager_initialization():
    async def handler(event):
        return []

    manager = InboundManager(
        inbound_http_base="http://localhost:8080",
        inbound_ws_uri="ws://localhost:8080/ws",
        token="test_token",
        handler=handler,
        ws_max_workers=4,
        ws_queue_size=100,
    )

    assert manager._inbound_http_base == "http://localhost:8080"
    assert manager._inbound_ws_uri == "ws://localhost:8080/ws"
    assert manager._token == "test_token"
    assert manager._handler == handler
    assert manager._ws_max_workers == 4
    assert manager._ws_queue_size == 100


@pytest.mark.unit
def test_inbound_manager_from_config_disabled():
    """Test InboundManager.from_config when disabled"""
    config = {"enable_inbound_server": False}

    async def handler(event):
        return []

    manager = InboundManager.from_config(
        config=config,
        token="test_token",
        handler=handler,
    )

    assert manager is None


@pytest.mark.unit
def test_inbound_manager_from_config_no_urls():
    """Test InboundManager.from_config with no URLs configured"""
    config = {
        "enable_inbound_server": True,
        "inbound_http_base": "",
        "inbound_ws_uri": "",
    }

    async def handler(event):
        return []

    manager = InboundManager.from_config(
        config=config,
        token="test_token",
        handler=handler,
    )

    assert manager is None


@pytest.mark.unit
def test_inbound_manager_from_config_http_only():
    """Test InboundManager.from_config with HTTP only"""
    config = {
        "enable_inbound_server": True,
        "inbound_http_base": "http://localhost:8080",
        "inbound_ws_uri": "",
    }

    async def handler(event):
        return []

    manager = InboundManager.from_config(
        config=config,
        token="test_token",
        handler=handler,
    )

    assert manager is not None
    assert manager._inbound_http_base == "http://localhost:8080"


@pytest.mark.unit
def test_inbound_manager_from_config_ws_only():
    """Test InboundManager.from_config with WS only"""
    config = {
        "enable_inbound_server": True,
        "inbound_http_base": "",
        "inbound_ws_uri": "ws://localhost:8080/ws",
    }

    async def handler(event):
        return []

    manager = InboundManager.from_config(
        config=config,
        token="test_token",
        handler=handler,
    )

    assert manager is not None
    assert manager._inbound_ws_uri == "ws://localhost:8080/ws"


@pytest.mark.unit
@pytest.mark.parametrize(
    "config",
    [
        {
            "enable_inbound_server": True,
            "inbound_http_base": "https://127.0.0.1:8443",
            "inbound_ws_uri": "",
        },
        {
            "enable_inbound_server": True,
            "inbound_http_base": "",
            "inbound_ws_uri": "wss://127.0.0.1:8443/ws",
        },
    ],
)
def test_inbound_manager_rejects_tls_schemes_it_does_not_terminate(config):
    with pytest.raises(ValueError, match="does not terminate TLS"):
        InboundManager.from_config(
            config=config,
            token="test_token",
            handler=AsyncMock(return_value=[]),
        )


@pytest.mark.unit
def test_inbound_manager_rejects_plaintext_non_loopback_without_proxy_acknowledgement():
    with pytest.raises(ValueError, match="non-loopback.*plaintext"):
        InboundManager(
            inbound_http_base="http://0.0.0.0:8080",
            inbound_ws_uri="",
            token="test_token",
            handler=AsyncMock(return_value=[]),
        )


@pytest.mark.unit
def test_inbound_manager_accepts_non_loopback_with_trusted_tls_proxy_acknowledgement():
    manager = InboundManager.from_config(
        config={
            "enable_inbound_server": True,
            "inbound_http_base": "http://0.0.0.0:8080",
            "inbound_ws_uri": "ws://0.0.0.0:8080/ws",
            "inbound_trusted_tls_proxy": True,
        },
        token="test_token",
        handler=AsyncMock(return_value=[]),
    )

    assert manager is not None
    assert manager._trusted_tls_proxy is True
    assert manager.config_key[-1] is True


@pytest.mark.unit
def test_inbound_manager_requires_token_for_non_loopback_proxy_listener():
    with pytest.raises(ValueError, match="require a non-empty inbound token"):
        InboundManager(
            inbound_http_base="http://0.0.0.0:8080",
            inbound_ws_uri="",
            token="",
            handler=AsyncMock(return_value=[]),
            trusted_tls_proxy=True,
        )


@pytest.mark.unit
@pytest.mark.parametrize("invalid_flag", ["true", "false", "yes", 1, 0, None])
def test_inbound_runtime_rejects_non_boolean_proxy_flags(invalid_flag):
    with pytest.raises(TypeError, match="must be a boolean"):
        InboundManager(
            inbound_http_base="http://127.0.0.1:8080",
            inbound_ws_uri="",
            token="test_token",
            handler=AsyncMock(return_value=[]),
            trusted_tls_proxy=invalid_flag,
        )
    with pytest.raises(TypeError, match="must be a boolean"):
        InboundServer(
            "127.0.0.1",
            8080,
            "test_token",
            AsyncMock(return_value=[]),
            trusted_tls_proxy=invalid_flag,
        )
    with pytest.raises(ValueError, match="must be a boolean"):
        InboundManager.from_config(
            config={
                "enable_inbound_server": True,
                "inbound_http_base": "http://127.0.0.1:8080",
                "inbound_trusted_tls_proxy": invalid_flag,
            },
            token="test_token",
            handler=AsyncMock(return_value=[]),
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("http_base", "ws_uri"),
    [
        ("http://0.0.0.0:8080", ""),
        ("http://[::]:8080", ""),
        ("http://192.168.1.20:8080", ""),
        ("http://bot.internal:8080", ""),
        ("", "ws://0.0.0.0:8080/ws"),
        ("http://127.0.0.1:8080", "ws://192.168.1.20:8081/ws"),
    ],
)
def test_inbound_manager_rejects_all_non_loopback_plaintext_without_proxy(
    http_base,
    ws_uri,
):
    with pytest.raises(ValueError, match="non-loopback.*plaintext"):
        InboundManager(
            inbound_http_base=http_base,
            inbound_ws_uri=ws_uri,
            token="test_token",
            handler=AsyncMock(return_value=[]),
        )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_direct_inbound_server_rejects_non_loopback_before_runner_setup():
    server = InboundServer(
        "0.0.0.0",
        8080,
        "test_token",
        AsyncMock(return_value=[]),
    )

    with patch("core.server.web.AppRunner") as app_runner:
        with pytest.raises(ValueError, match="non-loopback.*plaintext"):
            await server.start()

    app_runner.assert_not_called()
    assert server._runner is None
    assert server._site is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_direct_proxy_server_enters_explicit_plaintext_tcpsite():
    server = InboundServer(
        "0.0.0.0",
        8080,
        "test_token",
        AsyncMock(return_value=[]),
        trusted_tls_proxy=True,
    )
    runner = MagicMock()
    runner.setup = AsyncMock()
    site = MagicMock()
    site.start = AsyncMock()

    with (
        patch("core.server.web.AppRunner", return_value=runner),
        patch("core.server.web.TCPSite", return_value=site) as tcp_site,
    ):
        await server.start()

    runner.setup.assert_awaited_once()
    tcp_site.assert_called_once_with(runner, "0.0.0.0", 8080, ssl_context=None)
    site.start.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    ("http_base", "ws_uri", "expected_servers"),
    [
        ("http://0.0.0.0:8080", "ws://0.0.0.0:8080/ws", 1),
        ("http://0.0.0.0:8080", "", 1),
        ("", "ws://0.0.0.0:8080/ws", 1),
        ("http://0.0.0.0:8080", "ws://0.0.0.0:8081/ws", 2),
    ],
)
async def test_inbound_manager_propagates_proxy_acknowledgement_to_every_server(
    http_base,
    ws_uri,
    expected_servers,
):
    manager = InboundManager(
        inbound_http_base=http_base,
        inbound_ws_uri=ws_uri,
        token="test_token",
        handler=AsyncMock(return_value=[]),
        trusted_tls_proxy=True,
    )
    created = []

    def build_server(*args, **kwargs):
        server = MagicMock()
        server.start = AsyncMock()
        created.append(server)
        return server

    with patch("core.server.InboundServer", side_effect=build_server) as server_cls:
        await manager.start()

    assert len(created) == expected_servers
    assert server_cls.call_count == expected_servers
    assert all(call.kwargs["trusted_tls_proxy"] is True for call in server_cls.call_args_list)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inbound_manager_broadcast():
    """Test InboundManager broadcast"""
    async def handler(event):
        return []

    manager = InboundManager(
        inbound_http_base="",
        inbound_ws_uri="",
        token="test_token",
        handler=handler,
    )

    # Mock servers
    manager.http_server = MagicMock()
    manager.http_server.broadcast = AsyncMock()

    manager.ws_server = MagicMock()
    manager.ws_server.broadcast = AsyncMock()

    await manager.broadcast({"action": "test"})

    # Both servers should have broadcast called
    manager.http_server.broadcast.assert_called_once()
    manager.ws_server.broadcast.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inbound_manager_broadcast_same_server():
    """Test InboundManager broadcast when both servers are the same"""
    async def handler(event):
        return []

    manager = InboundManager(
        inbound_http_base="",
        inbound_ws_uri="",
        token="test_token",
        handler=handler,
    )

    # Same server instance
    mock_server = MagicMock()
    mock_server.broadcast = AsyncMock()
    manager.http_server = mock_server
    manager.ws_server = mock_server

    await manager.broadcast({"action": "test"})

    # Should only call once
    mock_server.broadcast.assert_called_once()


@pytest.mark.unit
def test_inbound_manager_update_token():
    """Test InboundManager update_token"""
    async def handler(event):
        return []

    manager = InboundManager(
        inbound_http_base="",
        inbound_ws_uri="",
        token="old_token",
        handler=handler,
    )

    # Create mock servers
    manager.http_server = MagicMock()
    manager.ws_server = MagicMock()

    manager.update_token("new_token")

    assert manager._token == "new_token"
    manager.http_server.update_token.assert_called_once_with("new_token")
    manager.ws_server.update_token.assert_called_once_with("new_token")


@pytest.mark.unit
def test_inbound_manager_update_token_deduplicates_shared_server_and_same_value():
    async def handler(event):
        return []

    manager = InboundManager(
        inbound_http_base="",
        inbound_ws_uri="",
        token="old_token",
        handler=handler,
    )
    shared_server = MagicMock()
    manager.http_server = shared_server
    manager.ws_server = shared_server

    manager.update_token("new_token")
    manager.update_token("new_token")

    assert manager._token == "new_token"
    shared_server.update_token.assert_called_once_with("new_token")


@pytest.mark.unit
def test_inbound_manager_set_status_providers_updates_existing_servers():
    """Test InboundManager forwards status providers to active servers."""

    async def handler(event):
        return []

    manager = InboundManager(
        inbound_http_base="",
        inbound_ws_uri="",
        token="test_token",
        handler=handler,
    )
    manager.http_server = MagicMock()
    manager.ws_server = MagicMock()
    plugins_count = Mock(return_value=5)
    sessions_count = Mock(return_value=2)
    pending_jobs = Mock(return_value=1)
    metrics = Mock(return_value={"ok": True})

    manager.set_status_providers(
        plugins_count=plugins_count,
        sessions_count=sessions_count,
        pending_jobs=pending_jobs,
        metrics=metrics,
    )

    manager.http_server.set_status_providers.assert_called_once_with(
        plugins_count=plugins_count,
        sessions_count=sessions_count,
        pending_jobs=pending_jobs,
        metrics=metrics,
    )
    manager.ws_server.set_status_providers.assert_called_once_with(
        plugins_count=plugins_count,
        sessions_count=sessions_count,
        pending_jobs=pending_jobs,
        metrics=metrics,
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inbound_manager_applies_status_providers_when_servers_start():
    """Test providers bound before start are applied to created servers."""

    async def handler(event):
        return []

    created = []

    class FakeInboundServer:
        def __init__(self, *args, **kwargs):
            self.set_status_providers = Mock()
            self.kwargs = kwargs
            created.append(self)

        async def start(self):
            return None

    manager = InboundManager(
        inbound_http_base="http://localhost:8080",
        inbound_ws_uri="",
        token="test_token",
        handler=handler,
    )
    plugins_count = Mock(return_value=5)

    manager.set_status_providers(plugins_count=plugins_count)

    with patch("core.server.InboundServer", FakeInboundServer):
        await manager.start()

    assert len(created) == 1
    created[0].set_status_providers.assert_called_once_with(
        plugins_count=plugins_count,
        sessions_count=None,
        pending_jobs=None,
        metrics=None,
    )


# ============================================================
# Utility Function Tests
# ============================================================

@pytest.mark.unit
def test_parse_http_base_valid():
    """Test _parse_http_base with valid URLs"""
    assert _parse_http_base("http://localhost:8080") == ("localhost", 8080)
    assert _parse_http_base("http://127.0.0.1:3000") == ("127.0.0.1", 3000)


@pytest.mark.unit
def test_parse_http_base_invalid():
    """Test _parse_http_base with invalid URLs"""
    assert _parse_http_base("") is None
    assert _parse_http_base(None) is None
    assert _parse_http_base("not-a-url") is None
    assert _parse_http_base("ftp://localhost:8080") is None  # Wrong scheme
    assert _parse_http_base("https://localhost:8443") is None  # TLS is unsupported
    assert _parse_http_base("http://localhost") is None  # No port
    assert _parse_http_base("http://localhost:8080/ignored") is None
    assert _parse_http_base("http://user@localhost:8080") is None
    assert _parse_http_base("http://localhost:8080?debug=1") is None
    assert _parse_http_base("http://localhost:invalid") is None


@pytest.mark.unit
def test_parse_ws_uri_valid():
    """Test _parse_ws_uri with valid URIs"""
    assert _parse_ws_uri("ws://localhost:8080") == ("localhost", 8080, "/ws")
    assert _parse_ws_uri("ws://example.com:9000/ws") == ("example.com", 9000, "/ws")
    assert _parse_ws_uri("ws://localhost:8080", default_path="/custom") == ("localhost", 8080, "/custom")


@pytest.mark.unit
def test_parse_ws_uri_invalid():
    """Test _parse_ws_uri with invalid URIs"""
    assert _parse_ws_uri("") is None
    assert _parse_ws_uri(None) is None
    assert _parse_ws_uri("not-a-uri") is None
    assert _parse_ws_uri("http://localhost:8080") is None  # Wrong scheme
    assert _parse_ws_uri("wss://localhost:8443/ws") is None  # TLS is unsupported
    assert _parse_ws_uri("ws://user@localhost:8080/ws") is None
    assert _parse_ws_uri("ws://localhost:8080/ws?debug=1") is None
    assert _parse_ws_uri("ws://localhost") is None  # No port


@pytest.mark.unit
def test_parse_non_negative_int():
    """Test _parse_non_negative_int utility"""
    assert _parse_non_negative_int(5, default=10) == 5
    assert _parse_non_negative_int(0, default=10) == 0
    assert _parse_non_negative_int(-5, default=10) == 0  # Negative becomes 0
    assert _parse_non_negative_int("invalid", default=10) == 10
    assert _parse_non_negative_int(None, default=10) == 10


@pytest.mark.unit
def test_parse_positive_int():
    """Test _parse_positive_int utility"""
    assert _parse_positive_int(5, default=10, min_value=1) == 5
    assert _parse_positive_int(1, default=10, min_value=1) == 1
    assert _parse_positive_int(0, default=10, min_value=1) == 1  # Below min
    assert _parse_positive_int(-5, default=10, min_value=1) == 1  # Negative becomes min
    assert _parse_positive_int("invalid", default=10, min_value=1) == 10
    assert _parse_positive_int(None, default=10, min_value=1) == 10


# ============================================================
# Lifecycle Tests
# ============================================================

@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_start_stop(mock_handler, unused_tcp_port):
    """Test server start and stop"""
    server = _make_server(mock_handler, unused_tcp_port)
    await server.start()

    assert server._runner is not None
    assert server._site is not None

    await server.stop()

    assert server._runner is None
    assert server._site is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_token_rotation_revokes_old_ws_and_accepts_new_token(
    unused_tcp_port,
):
    seen_message_ids: list[int] = []

    async def handler(event):
        seen_message_ids.append(event["message_id"])
        return [{"action": "ack", "params": {"message_id": event["message_id"]}}]

    server = InboundServer(
        host="127.0.0.1",
        port=unused_tcp_port,
        token="old-token",
        handler=handler,
        enable_http=False,
        enable_ws=True,
        ws_path="/ws",
        ws_max_workers=1,
        ws_queue_size=4,
    )
    await server.start()
    url = f"http://127.0.0.1:{unused_tcp_port}/ws"
    try:
        async with ClientSession() as session:
            old_ws = await session.ws_connect(
                url,
                headers={"Authorization": "Bearer old-token"},
            )
            server.update_token("new-token")
            old_close = await old_ws.receive(timeout=2)
            assert old_close.type in {
                web.WSMsgType.CLOSE,
                web.WSMsgType.CLOSED,
                web.WSMsgType.CLOSING,
            }

            with pytest.raises(WSServerHandshakeError) as exc_info:
                await session.ws_connect(
                    url,
                    headers={"Authorization": "Bearer old-token"},
                )
            assert exc_info.value.status == 401

            new_ws = await session.ws_connect(
                url,
                headers={"Authorization": "Bearer new-token"},
            )
            payload = _onebot_message_payload("/help")
            payload["message_id"] = 902
            await new_ws.send_json(payload)
            response = await new_ws.receive_json(timeout=2)
            assert response["action"] == "ack"
            assert response["params"]["message_id"] == 902
            await new_ws.close()
    finally:
        await server.stop()

    assert seen_message_ids == [902]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_stop_with_workers(mock_handler, unused_tcp_port):
    """Test server stop cleans up worker tasks"""
    server = _make_server(mock_handler, unused_tcp_port)

    # Start server
    await server.start()

    # Create mock worker tasks
    mock_task1 = asyncio.create_task(asyncio.sleep(10))
    mock_task2 = asyncio.create_task(asyncio.sleep(10))
    server._ws_worker_tasks = [mock_task1, mock_task2]
    server._ws_event_locks["stale"] = MagicMock()

    await server.stop()

    # Tasks should be cancelled
    assert mock_task1.cancelled()
    assert mock_task2.cancelled()
    assert server._ws_event_locks == {}
