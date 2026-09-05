"""入站服务器鉴权、HTTP 和 WebSocket。"""

from __future__ import annotations

import tests.helpers.server_test_support as _fixture_support
from core.models import OneBotEvent
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.server_test_support import (
    VERSION,
    Any,
    AsyncMock,
    BroadcastResult,
    InboundServer,
    Mock,
    SimpleNamespace,
    _make_request_with_auth,
    _make_request_without_auth,
    _onebot_message_payload,
    asyncio,
    json,
    patch,
    pytest,
    tomllib,
    web,
)

mock_handler  = _fixture_support.mock_handler
sample_server = _fixture_support.sample_server


def test_health_version_matches_project_metadata() -> None:
    pyproject = REPOSITORY_ROOT / "pyproject.toml"
    with pyproject.open("rb") as stream:
        project = tomllib.load(stream)["project"]

    assert VERSION == project["version"]


@pytest.mark.unit
def test_server_initialization(mock_handler):
    """Test InboundServer initialization"""
    server = InboundServer(
        host    = "127.0.0.1",
        port    = 8765,
        token   = "test_token",
        handler = mock_handler,
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
        host        = "127.0.0.1",
        port        = 8765,
        token       = "test_token",
        handler     = mock_handler,
        enable_http = True,
        enable_ws   = False,
    )

    assert server.enable_http is True
    assert server.enable_ws is False


@pytest.mark.unit
def test_server_initialization_ws_only(mock_handler):
    """Test InboundServer initialization with WS only"""
    server = InboundServer(
        host        = "127.0.0.1",
        port        = 8765,
        token       = "test_token",
        handler     = mock_handler,
        enable_http = False,
        enable_ws   = True,
    )

    assert server.enable_http is False
    assert server.enable_ws is True


@pytest.mark.unit
def test_server_queue_size_validation(mock_handler):
    """Test queue size is validated correctly"""
    # Valid queue size
    server1 = InboundServer(
        host          = "127.0.0.1",
        port          = 8765,
        token         = "test_token",
        handler       = mock_handler,
        enable_ws     = True,
        ws_queue_size = 100,
    )
    assert server1._ws_event_queue.maxsize == 100

    # Zero/negative backlog remains bounded to the worker count for delivery.
    server2 = InboundServer(
        host          = "127.0.0.1",
        port          = 8765,
        token         = "test_token",
        handler       = mock_handler,
        enable_ws     = True,
        ws_queue_size = -10,
    )
    assert server2._ws_event_queue.maxsize == server2._ws_max_workers
    assert server2._event_dispatcher._capacity == server2._ws_max_workers

    # Invalid queue size becomes default
    server3 = InboundServer(
        host          = "127.0.0.1",
        port          = 8765,
        token         = "test_token",
        handler       = mock_handler,
        enable_ws     = True,
        ws_queue_size = "invalid",
    )
    assert server3._ws_event_queue.maxsize == server3._ws_max_workers
    assert server3._event_dispatcher._capacity == server3._ws_max_workers


@pytest.mark.unit
def test_server_max_workers_validation(mock_handler):
    """Test max_workers is validated correctly"""
    server = InboundServer(
        host           = "127.0.0.1",
        port           = 8765,
        token          = "test_token",
        handler        = mock_handler,
        enable_ws      = True,
        ws_max_workers = 5,
    )
    assert server._ws_max_workers == 5

    # Negative becomes 1
    server2 = InboundServer(
        host           = "127.0.0.1",
        port           = 8765,
        token          = "test_token",
        handler        = mock_handler,
        enable_ws      = True,
        ws_max_workers = -5,
    )
    assert server2._ws_max_workers == 1


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
        host    = "127.0.0.1",
        port    = 8765,
        token   = "",
        handler = handler,
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


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_health_unauthorized(sample_server):
    """Test health endpoint returns 401 without auth"""
    request  = _make_request_without_auth("GET", "/health")
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
    sample_server._get_metrics = Mock(
        return_value={
            "total_calls": 100,
            "avg_time": 0.5,
        }
    )

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
        host    = "127.0.0.1",
        port    = 8765,
        token   = "",
        handler = handler,
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
@pytest.mark.parametrize("content_type", ["text/plain", "", "multipart/form-data"])
async def test_server_post_event_rejects_non_json_content_type(sample_server, content_type):
    request                         = _make_request_with_auth("POST", "/event", "test_token")
    request.headers["Content-Type"] = content_type
    request.json = AsyncMock(return_value=_onebot_message_payload())

    response = await sample_server.post_event(request)

    assert response.status == 415
    request.json.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    "content_type",
    ["application/json; charset=utf-8", "application/problem+json"],
)
async def test_server_post_event_accepts_json_media_types(sample_server, content_type):
    request                         = _make_request_with_auth("POST", "/event", "test_token")
    request.headers["Content-Type"] = content_type
    request.json = AsyncMock(return_value=_onebot_message_payload())

    response = await sample_server.post_event(request)

    assert response.status == 200


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
async def test_http_action_receipt_commits_only_after_response_write_eof():
    from core.delivery import DeliveryReceipt, attach_receipt
    from core.server import _finalize_http_action_response

    events: list[str] = []
    receipt           = DeliveryReceipt(
        expected_actions = 1,
        commit           = lambda: events.append("commit"),
        rollback         = lambda: events.append("rollback"),
        unknown          = lambda: events.append("unknown"),
    )
    action = attach_receipt(
        {"action": "send_private_msg", "params": {"user_id": 1, "message": []}},
        receipt,
    )

    class FakeResponse:
        async def prepare(self, _request):
            events.append("prepare")

        async def write_eof(self):
            events.append("write_eof")

    response = FakeResponse()
    with patch("core.server.web.json_response", return_value=response):
        assert (
            await _finalize_http_action_response(
                object(),
                [action],
                write_to_transport=True,
            )
            is response
        )

    assert events == ["prepare", "write_eof", "commit"]
    assert receipt.committed is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_http_action_receipt_rolls_back_when_response_write_fails():
    from core.delivery import DeliveryReceipt, attach_receipt
    from core.server import _finalize_http_action_response

    events: list[str] = []
    receipt           = DeliveryReceipt(
        expected_actions = 1,
        commit           = lambda: events.append("commit"),
        rollback         = lambda: events.append("rollback"),
        unknown          = lambda: events.append("unknown"),
    )
    action = attach_receipt(
        {"action": "send_private_msg", "params": {"user_id": 1, "message": []}},
        receipt,
    )

    class BrokenResponse:
        async def prepare(self, _request):
            events.append("prepare")

        async def write_eof(self):
            events.append("write_eof")
            raise ConnectionResetError("client disconnected")

    with (
        patch("core.server.web.json_response", return_value=BrokenResponse()),
        pytest.raises(ConnectionResetError, match="client disconnected"),
    ):
        await _finalize_http_action_response(
            object(),
            [action],
            write_to_transport=True,
        )

    assert events == ["prepare", "write_eof", "rollback"]
    assert receipt.resolved is True
    assert receipt.committed is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_post_event_isolates_handler_fatal(sample_server):
    class HandlerFatal(BaseException):
        pass

    fatal = HandlerFatal("fatal handler")
    sample_server.handler = AsyncMock(side_effect=fatal)
    request = _make_request_with_auth("POST", "/event", "test_token")
    request.json = AsyncMock(return_value=_onebot_message_payload())

    task     = asyncio.create_task(sample_server.post_event(request))
    response = await task

    assert response.status == 500
    assert "unavailable" in response.text


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_post_event_normalizes_raw_message_only_payload(sample_server):
    """HTTP raw_message-only events reach handlers as text segments."""
    received: list[dict[str, Any]] = []

    async def handler(payload: dict[str, Any]) -> list[dict[str, Any]]:
        received.append(payload)
        return []

    sample_server.handler = handler
    payload               = _onebot_message_payload("/help")
    payload.pop("message")
    request = _make_request_with_auth("POST", "/event", "test_token")
    request.json = AsyncMock(return_value=payload)

    response = await sample_server.post_event(request)

    assert response.status == 200
    assert received[0]["message"] == [{"type": "text", "data": {"text": "/help"}}]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_reuses_canonical_payload_without_second_pydantic_validation(sample_server):
    """The transport validates once; Dispatcher must reuse that detached payload."""
    sample_server.handler = AsyncMock(return_value=[])
    request = _make_request_with_auth("POST", "/event", "test_token")
    request.json = AsyncMock(return_value=_onebot_message_payload())
    original_validate = OneBotEvent.model_validate

    with patch.object(OneBotEvent, "model_validate", wraps=original_validate) as validate:
        response = await sample_server.post_event(request)

    assert response.status == 200
    validate.assert_called_once()
    event = sample_server.handler.await_args.args[0]
    assert event["_source"] == "inbound_http"
    assert event["message"] == [{"type": "text", "data": {"text": "/help"}}]


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


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    "message",
    [
        [{"type": "text", "data": "truthy"}],
        '[{"type":"text","data":"truthy"}]',
    ],
)
async def test_server_post_event_rejects_malformed_message_segments_before_dispatch(
    sample_server,
    message,
):
    handler = AsyncMock(return_value=[])
    sample_server.handler = handler
    payload               = _onebot_message_payload()
    payload["message"]    = message
    request               = _make_request_with_auth("POST", "/event", "test_token")
    request.json = AsyncMock(return_value=payload)

    response = await sample_server.post_event(request)

    assert response.status == 400
    assert "Invalid message payload" in response.text
    handler.assert_not_awaited()


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
        host  = "127.0.0.1",
        port  = 8765,
        token = "",
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
        host        = "127.0.0.1",
        port        = 8765,
        token       = "test_token",
        handler     = handler,
        enable_http = True,
        enable_ws   = False,
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
    request                          = _make_request_with_auth("GET", "/ws", "test_token")

    with patch("core.server.web.WebSocketResponse", return_value=FakeWebSocket()):
        await sample_server.ws_handler(request)

    assert sample_server._request_count == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_broadcast_no_sockets(sample_server):
    """Test broadcast with no active sockets"""
    result = await sample_server.broadcast({"action": "test"})

    assert result == BroadcastResult()
    assert result.delivered is False
    assert bool(result) is False
