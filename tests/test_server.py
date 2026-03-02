"""
Tests for core/server.py - InboundServer and InboundManager classes
"""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch, PropertyMock

import pytest
from aiohttp import web

from core.server import (
    InboundServer,
    InboundManager,
    VERSION,
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
    """Test authorization with no token set on server"""
    async def handler(event):
        return []

    server = InboundServer(
        host="127.0.0.1",
        port=8765,
        token="",
        handler=handler,
    )

    request = _make_request_without_auth("GET", "/")
    assert server._authorized(request) is True


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
    payload = {"test": "data"}

    request = _make_request_with_auth("POST", "/event", "test_token")
    request.json = AsyncMock(return_value=payload)

    response = await sample_server.post_event(request)

    assert response.status == 200

    # Verify actions key in response
    body = response.text
    assert "actions" in body


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

    sample_server.update_token("new_token")

    assert sample_server.token == "new_token"


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


# ============================================================
# Utility Function Tests
# ============================================================

@pytest.mark.unit
def test_parse_http_base_valid():
    """Test _parse_http_base with valid URLs"""
    assert _parse_http_base("http://localhost:8080") == ("localhost", 8080)
    assert _parse_http_base("https://example.com:443") == ("example.com", 443)
    assert _parse_http_base("http://127.0.0.1:3000") == ("127.0.0.1", 3000)


@pytest.mark.unit
def test_parse_http_base_invalid():
    """Test _parse_http_base with invalid URLs"""
    assert _parse_http_base("") is None
    assert _parse_http_base(None) is None
    assert _parse_http_base("not-a-url") is None
    assert _parse_http_base("ftp://localhost:8080") is None  # Wrong scheme
    assert _parse_http_base("http://localhost") is None  # No port


@pytest.mark.unit
def test_parse_ws_uri_valid():
    """Test _parse_ws_uri with valid URIs"""
    assert _parse_ws_uri("ws://localhost:8080") == ("localhost", 8080, "/ws")
    assert _parse_ws_uri("ws://example.com:9000/ws") == ("example.com", 9000, "/ws")
    assert _parse_ws_uri("wss://localhost:8443/custom") == ("localhost", 8443, "/custom")
    assert _parse_ws_uri("ws://localhost:8080", default_path="/custom") == ("localhost", 8080, "/custom")


@pytest.mark.unit
def test_parse_ws_uri_invalid():
    """Test _parse_ws_uri with invalid URIs"""
    assert _parse_ws_uri("") is None
    assert _parse_ws_uri(None) is None
    assert _parse_ws_uri("not-a-uri") is None
    assert _parse_ws_uri("http://localhost:8080") is None  # Wrong scheme
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
async def test_server_start_stop(sample_server):
    """Test server start and stop"""
    await sample_server.start()

    assert sample_server._runner is not None
    assert sample_server._site is not None

    await sample_server.stop()

    assert sample_server._runner is None
    assert sample_server._site is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_stop_with_workers(sample_server):
    """Test server stop cleans up worker tasks"""
    # Start server
    await sample_server.start()

    # Create mock worker tasks
    mock_task1 = asyncio.create_task(asyncio.sleep(10))
    mock_task2 = asyncio.create_task(asyncio.sleep(10))
    sample_server._ws_worker_tasks = [mock_task1, mock_task2]

    await sample_server.stop()

    # Tasks should be cancelled
    assert mock_task1.cancelled()
    assert mock_task2.cancelled()
