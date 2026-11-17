"""入站服务器鉴权、HTTP 和 WebSocket。"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientError, ClientSession

import tests.helpers.app_test_support as _app_support
import tests.helpers.server_test_support as _fixture_support
from core.constants import MAX_MESSAGE_TEXT_LENGTH
from core.delivery import DeliveryReceipt, DeliverySegments, attach_receipt
from core.log import LogEvent
from core.router import CommandSpec, build_command_catalog_node
from tests.helpers.app_test_support import LoggedLines, XiaoQingApp
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.server_test_support import (
    VERSION,
    BroadcastResult,
    StandaloneInboundServer,
    WSServerHandshakeError,
    auth_headers,
    http_request,
    make_server,
    onebot_message_payload,
    post_event,
    serving,
    tomllib,
    wait_until,
    web,
)

mock_handler    = _fixture_support.mock_handler
temp_app_root   = _app_support.temp_app_root
sample_server   = _fixture_support.sample_server
restore_logging = _app_support.restore_logging
logged_lines    = _app_support.logged_lines

probe_log = logging.getLogger("plugins.probe")


def test_health_version_matches_project_metadata() -> None:
    pyproject = REPOSITORY_ROOT / "pyproject.toml"
    with pyproject.open("rb") as stream:
        project = tomllib.load(stream)["project"]

    assert VERSION == project["version"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("options", "enable_http", "enable_ws"),
    [
        pytest.param({}, True, True, id="default"),
        pytest.param({"enable_http": True, "enable_ws": False}, True, False, id="http-only"),
        pytest.param({"enable_http": False, "enable_ws": True}, False, True, id="ws-only"),
    ],
)
def test_server_initialization(mock_handler, options, enable_http, enable_ws):
    server = StandaloneInboundServer(
        host    = "127.0.0.1",
        port    = 8765,
        token   = "test_token",
        handler = mock_handler,
        **options,
    )

    assert server.host == "127.0.0.1"
    assert server.port == 8765
    assert server.token == "test_token"
    assert server.enable_http is enable_http
    assert server.enable_ws is enable_ws
    assert server.ws_path == "/ws"


@pytest.mark.unit
def test_server_queue_size_validation(mock_handler):
    """Test queue size is validated correctly"""
    # Valid queue size
    server1 = StandaloneInboundServer(
        host          = "127.0.0.1",
        port          = 8765,
        token         = "test_token",
        handler       = mock_handler,
        enable_ws     = True,
        ws_queue_size = 100,
    )
    assert server1._ws_event_queue.maxsize == 100

    # 零和负数都收成 1：队列长度只有正整数一种含义。
    server2 = StandaloneInboundServer(
        host          = "127.0.0.1",
        port          = 8765,
        token         = "test_token",
        handler       = mock_handler,
        enable_ws     = True,
        ws_queue_size = -10,
    )
    assert server2._ws_event_queue.maxsize == 1
    assert server2._event_dispatcher._capacity == server2._ws_max_workers + 1

    # Invalid queue size becomes default
    server3 = StandaloneInboundServer(
        host          = "127.0.0.1",
        port          = 8765,
        token         = "test_token",
        handler       = mock_handler,
        enable_ws     = True,
        ws_queue_size = "invalid",
    )
    assert server3._ws_event_queue.maxsize == 200
    assert server3._event_dispatcher._capacity == server3._ws_max_workers + 200


@pytest.mark.unit
def test_server_max_workers_validation(mock_handler):
    """Test max_workers is validated correctly"""
    server = StandaloneInboundServer(
        host           = "127.0.0.1",
        port           = 8765,
        token          = "test_token",
        handler        = mock_handler,
        enable_ws      = True,
        ws_max_workers = 5,
    )
    assert server._ws_max_workers == 5

    # Negative becomes 1
    server2 = StandaloneInboundServer(
        host           = "127.0.0.1",
        port           = 8765,
        token          = "test_token",
        handler        = mock_handler,
        enable_ws      = True,
        ws_max_workers = -5,
    )
    assert server2._ws_max_workers == 1


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    ("token", "expected_status"),
    [
        pytest.param("test_token", 200, id="valid-token"),
        pytest.param("wrong_token", 401, id="wrong-token"),
        pytest.param(None, 401, id="missing-header"),
    ],
)
async def test_server_http_checks_bearer_token(sample_server, token, expected_status):
    async with serving(sample_server) as session:
        response = await http_request(session, "GET", "/health", token=token)

    assert response.status == expected_status


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_rejects_every_request_once_the_token_is_cleared(unused_tcp_port):
    """令牌被清空后服务器对一切请求闭锁，处理器不会被调用。"""

    handler = AsyncMock(return_value=[])
    server = make_server(handler, unused_tcp_port)
    async with serving(server) as session:
        server.update_token("")

        health = await http_request(session, "GET", "/health", token="anything")
        event = await post_event(session, onebot_message_payload(), token="anything")
        no_header_event = await post_event(session, onebot_message_payload(), token=None)
        with pytest.raises(WSServerHandshakeError) as exc_info:
            await session.ws_connect("/ws", headers=auth_headers("anything"))

    assert (health.status, event.status, no_header_event.status) == (401, 401, 401)
    assert exc_info.value.status == 401
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    ("method", "path"),
    [
        pytest.param("GET", "/health", id="health"),
        pytest.param("GET", "/metrics", id="metrics"),
        pytest.param("POST", "/event", id="event"),
    ],
)
async def test_server_http_endpoints_return_401_without_auth(sample_server, method, path):
    async with serving(sample_server) as session:
        response = await http_request(session, method, path, token=None)

    assert response.status == 401


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_health_authorized(sample_server):
    """Test health endpoint returns status"""
    async with serving(sample_server) as session:
        response = await http_request(session, "GET", "/health")

    assert response.status == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["version"] == VERSION


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_health_with_status_providers(sample_server):
    """Test health endpoint with custom status providers"""
    sample_server.set_status_providers(
        plugins_count=Mock(return_value=5),
        sessions_count=Mock(return_value=10),
        pending_jobs=Mock(return_value=2),
    )

    async with serving(sample_server) as session:
        response = await http_request(session, "GET", "/health")

    assert response.status == 200
    body = response.json()
    assert body["plugins_loaded"] == 5
    assert body["active_sessions"] == 10
    assert body["pending_jobs"] == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_health_reports_uptime(sample_server):
    """health 响应包含运行时间字段"""
    async with serving(sample_server) as session:
        response = await http_request(session, "GET", "/health")

    assert response.status == 200
    body = response.json()
    assert body["uptime_seconds"] >= 0
    assert body["uptime_human"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_metrics_not_configured(sample_server):
    """Test metrics endpoint when not configured"""
    async with serving(sample_server) as session:
        response = await http_request(session, "GET", "/metrics")

    assert response.status == 501
    assert response.json() == {"error": "Metrics not configured"}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_metrics_with_provider(sample_server):
    """Test metrics endpoint with provider"""
    metrics = Mock(
        return_value={
            "total_calls": 100,
            "avg_time"   : 0.5,
        }
    )
    sample_server.set_status_providers(metrics=metrics)

    async with serving(sample_server) as session:
        response = await http_request(session, "GET", "/metrics")

    assert response.status == 200
    assert response.json() == {"total_calls": 100, "avg_time": 0.5}
    metrics.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_metrics_provider_error(sample_server):
    """Test metrics endpoint handles provider errors"""
    sample_server.set_status_providers(metrics=Mock(side_effect=Exception("Test error")))

    async with serving(sample_server) as session:
        response = await http_request(session, "GET", "/metrics")

    assert response.status == 500


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_post_event_invalid_json(sample_server):
    """Test POST event with invalid JSON"""
    async with serving(sample_server) as session:
        response = await http_request(
            session,
            "POST",
            "/event",
            headers = {"Content-Type": "application/json"},
            data    = "{bad json",
        )

    assert response.status == 400


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("content_type", ["text/plain", "", "multipart/form-data"])
async def test_server_post_event_rejects_non_json_content_type(unused_tcp_port, content_type):
    handler = AsyncMock(return_value=[])
    server = make_server(handler, unused_tcp_port)
    async with serving(server) as session:
        response = await http_request(
            session,
            "POST",
            "/event",
            headers = {"Content-Type": content_type},
            data    = json.dumps(onebot_message_payload()),
        )

    assert response.status == 415
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "content_type",
    ["application/json; charset=utf-8", "application/problem+json"],
)
async def test_server_post_event_accepts_json_media_types(sample_server, content_type):
    async with serving(sample_server) as session:
        response = await http_request(
            session,
            "POST",
            "/event",
            headers = {"Content-Type": content_type},
            data    = json.dumps(onebot_message_payload()),
        )

    assert response.status == 200


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_post_event_success(sample_server):
    """Test POST event with valid payload"""
    async with serving(sample_server) as session:
        response = await post_event(session, onebot_message_payload())

    assert response.status == 200
    assert response.json() == {"actions": [{"action": "test", "params": {}}]}


def _receipt_action_handler(events: list[str]):
    """返回一个动作带投递回执的处理器；回执的三个回调把各自的名字记进 ``events``。

    处理器记一行 ``probe.handled``，回执结算时记一行 ``probe.settled``。
    """

    def settle(outcome: str):
        def record() -> None:
            events.append(outcome)
            probe_log.info(LogEvent("probe.settled", outcome=outcome))

        return record

    receipt = DeliveryReceipt(
        expected_actions = 1,
        commit           = settle("commit"),
        rollback         = settle("rollback"),
        unknown          = settle("unknown"),
    )

    async def handler(_event: dict[str, Any]) -> list[dict[str, Any]]:
        probe_log.info(LogEvent("probe.handled"))
        return [
            attach_receipt(
                {"action": "send_private_msg", "params": {"user_id": 1, "message": []}},
                receipt,
            )
        ]

    return handler, receipt


@pytest.mark.asyncio
@pytest.mark.integration
async def test_http_action_receipt_commits_only_after_response_write_eof(
    unused_tcp_port,
    monkeypatch,
    logged_lines: LoggedLines,
):
    events: list[str] = []
    handler, receipt = _receipt_action_handler(events)
    real_write_eof = web.Response.write_eof

    # aiohttp 的写边界：只记录 write_eof 发生的时刻，然后照常写完。
    async def recording_write_eof(self, *args, **kwargs):
        events.append("write_eof")
        return await real_write_eof(self, *args, **kwargs)

    monkeypatch.setattr(web.Response, "write_eof", recording_write_eof)
    async with serving(make_server(handler, unused_tcp_port)) as session:
        response = await post_event(session, onebot_message_payload())

    assert response.status == 200
    assert response.json() == {
        "actions": [{"action": "send_private_msg", "params": {"user_id": 1, "message": []}}]
    }
    # aiohttp 在处理器返回后会再调一次 write_eof（已写完，不做事），所以只看前两项的先后。
    assert events[:2] == ["write_eof", "commit"]
    assert "rollback" not in events
    assert receipt.committed is True
    handled_id = logged_lines.request_id_of("probe.handled")
    assert logged_lines.request_id_of("probe.settled") == handled_id != "-"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_http_action_receipt_rolls_back_when_response_write_fails(
    unused_tcp_port,
    monkeypatch,
    logged_lines: LoggedLines,
):
    events: list[str] = []
    handler, receipt = _receipt_action_handler(events)

    # 客户端在响应写完前断开：aiohttp 的 write_eof 抛出连接重置。
    async def broken_write_eof(self, *args, **kwargs):
        events.append("write_eof")
        raise ConnectionResetError("client disconnected")

    monkeypatch.setattr(web.Response, "write_eof", broken_write_eof)
    async with serving(make_server(handler, unused_tcp_port)) as session:
        with pytest.raises(ClientError):
            await post_event(session, onebot_message_payload())
        await wait_until(lambda: receipt.resolved)

    # aiohttp 处理写失败时还会再写一次错误响应（同样失败），所以只看前两项的先后。
    assert events[:2] == ["write_eof", "rollback"]
    assert "commit" not in events
    assert receipt.committed is False
    handled_id = logged_lines.request_id_of("probe.handled")
    assert logged_lines.request_id_of("probe.settled") == handled_id != "-"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_post_event_isolates_handler_fatal(unused_tcp_port):
    class HandlerFatal(BaseException):
        pass

    server = make_server(AsyncMock(side_effect=HandlerFatal("fatal handler")), unused_tcp_port)
    async with serving(server) as session:
        response = await post_event(session, onebot_message_payload())

    assert response.status == 500
    assert "unavailable" in response.text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_post_event_normalizes_raw_message_only_payload(unused_tcp_port):
    """HTTP raw_message-only events reach handlers as text segments."""
    received: list[dict[str, Any]] = []

    async def handler(payload: dict[str, Any]) -> list[dict[str, Any]]:
        received.append(payload)
        return []

    payload = onebot_message_payload("/help")
    payload.pop("message")
    async with serving(make_server(handler, unused_tcp_port)) as session:
        response = await post_event(session, payload)

    assert response.status == 200
    assert received[0]["message"] == [{"type": "text", "data": {"text": "/help"}}]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_post_event_hands_canonical_event_to_handler(unused_tcp_port):
    """The handler receives the validated payload tagged with its inbound source."""
    handler = AsyncMock(return_value=[])
    async with serving(make_server(handler, unused_tcp_port)) as session:
        response = await post_event(session, onebot_message_payload())

    assert response.status == 200
    event = handler.await_args.args[0]
    assert event["_source"] == "inbound_http"
    assert event["message"] == [{"type": "text", "data": {"text": "/help"}}]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_post_event_rejects_non_object_payload(sample_server):
    """Test POST event rejects non-object JSON payloads."""
    async with serving(sample_server) as session:
        response = await post_event(session, ["not", "an", "object"])

    assert response.status == 400
    assert "Payload must be a JSON object" in response.text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_post_event_rejects_missing_post_type(sample_server):
    """Test POST event rejects payloads without OneBot post_type."""
    async with serving(sample_server) as session:
        response = await post_event(session, {"message": "/help", "user_id": 10001})

    assert response.status == 400
    assert "post_type" in response.text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_post_event_rejects_message_without_user_id(sample_server):
    """Test message events require a sender user_id."""
    payload = onebot_message_payload()
    payload.pop("user_id")
    async with serving(sample_server) as session:
        response = await post_event(session, payload)

    assert response.status == 400
    assert "user_id" in response.text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_post_event_rejects_message_without_message_body(sample_server):
    """Test message events require message or raw_message content."""
    payload = onebot_message_payload()
    payload.pop("message")
    payload.pop("raw_message")
    async with serving(sample_server) as session:
        response = await post_event(session, payload)

    assert response.status == 400
    assert "message" in response.text


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "message",
    [
        [{"type": "text", "data": "truthy"}],
        '[{"type":"text","data":"truthy"}]',
    ],
)
async def test_server_post_event_rejects_malformed_message_segments_before_dispatch(
    unused_tcp_port,
    message,
):
    handler = AsyncMock(return_value=[])
    payload            = onebot_message_payload()
    payload["message"] = message
    async with serving(make_server(handler, unused_tcp_port)) as session:
        response = await post_event(session, payload)

    assert response.status == 400
    assert "Invalid message payload" in response.text
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_ws_handler_unauthorized(sample_server):
    """Test WebSocket handler returns 401 without auth"""
    async with serving(sample_server) as session:
        with pytest.raises(WSServerHandshakeError) as exc_info:
            await session.ws_connect("/ws")

    assert exc_info.value.status == 401


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_ws_handler_disabled(mock_handler, unused_tcp_port):
    """WebSocket 关闭时服务器不提供 /ws。"""
    server = make_server(mock_handler, unused_tcp_port, enable_ws=False)
    async with serving(server) as session:
        with pytest.raises(WSServerHandshakeError) as exc_info:
            await session.ws_connect("/ws", headers=auth_headers())

    assert exc_info.value.status == 404


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_ws_handler_counts_invalid_json_frame(sample_server):
    """Test WebSocket request_count increments before JSON parsing."""
    async with serving(sample_server) as session:
        ws = await session.ws_connect("/ws", headers=auth_headers())
        await wait_until(lambda: sample_server._get_ws_connections() == 1)
        await ws.send_str("{bad json")
        await ws.close()
        # 服务器按顺序处理完这个连接上的帧，连接计数回到 0 时坏帧一定已经处理过。
        await wait_until(lambda: sample_server._get_ws_connections() == 0)

    assert sample_server._get_ws_connections() == 0
    assert sample_server._request_count == 1


_QUERY_CANARY   = "QUERY-CANARY-3f9a"
_AGENT_CANARY   = "AGENT-CANARY-3f9a"
_REFERER_CANARY = "REFERER-CANARY-3f9a"
_METHOD_CANARY  = "METHODCANARY"
_CANARY_HEADERS = {
    "User-Agent": _AGENT_CANARY,
    "Referer"   : f"https://example.invalid/?key={_REFERER_CANARY}",
}


async def _send_canary_request(session: ClientSession, port: int, kind: str) -> int:
    """发一次查询串、Referer 和 User-Agent 都带探针串的 ``kind`` 请求，返回响应状态码。"""

    query = f"?token={_QUERY_CANARY}"
    if kind == "websocket":
        ws = await session.ws_connect(f"/ws{query}", headers={**_CANARY_HEADERS, **auth_headers()})
        await ws.close()
        return 101
    if kind == "malformed-request-line":
        # 有一行请求头没有冒号：aiohttp 解析不了这个请求，不做路由直接回 400。
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            f"GET /event{query} HTTP/1.1\r\nHost: x\r\nUser-Agent: {_AGENT_CANARY}\r\n"
            "header-without-colon\r\n\r\n".encode()
        )
        await writer.drain()
        status_line = await reader.readline()
        writer.close()
        await writer.wait_closed()
        return int(status_line.split()[1])
    json_body = {"json": onebot_message_payload()}
    method, path, token, extra = {
        "event"         : ("POST", "/event", "test_token", json_body),
        "unauthorized"  : ("POST", "/event", None, json_body),
        "unknown-route" : ("POST", "/missing", "test_token", json_body),
        "malformed-body": (
            "POST",
            "/event",
            "test_token",
            {"data": "{bad json", "headers": {"Content-Type": "application/json"}},
        ),
        "unknown-method": (_METHOD_CANARY, "/health", "test_token", {}),
    }[kind]
    headers  = {**_CANARY_HEADERS, **extra.pop("headers", {})}
    response = await http_request(
        session, method, f"{path}{query}", token=token, headers=headers, **extra
    )
    return response.status


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    ("kind", "expected_status", "method", "route", "same_request_event"),
    [
        pytest.param("event", 200, "POST", "/event", "probe.handled", id="success"),
        pytest.param("unauthorized", 401, "POST", "/event", None, id="rejected-auth"),
        pytest.param("unknown-route", 404, "POST", None, None, id="unknown-route"),
        pytest.param(
            "malformed-body",
            400,
            "POST",
            "/event",
            "inbound.http.invalid_json",
            id="malformed-body",
        ),
        pytest.param("websocket", 101, "GET", "/ws", None, id="websocket-handshake"),
        pytest.param("malformed-request-line", 400, None, None, None, id="malformed-request"),
        # 解析器认不认这个方法名因实现而异（回 400 或 405），两种都不能把它原样写进日志。
        pytest.param("unknown-method", None, None, None, None, id="unknown-method"),
    ],
)
async def test_access_log_records_only_method_status_route_and_time(
    unused_tcp_port: int,
    logged_lines: LoggedLines,
    kind: str,
    expected_status: int | None,
    method: str | None,
    route: str | None,
    same_request_event: str | None,
):
    # 成功请求的访问记录在 DEBUG：只把 core.server 放到 DEBUG，其余仍是默认的 INFO。
    logging.getLogger("core.server").setLevel(logging.DEBUG)

    async def handler(_event: dict[str, Any]) -> list[dict[str, Any]]:
        probe_log.info(LogEvent("probe.handled"))
        return []

    async with serving(make_server(handler, unused_tcp_port)) as session:
        status = await _send_canary_request(session, unused_tcp_port, kind)

    if expected_status is None:
        assert status >= 400
    else:
        assert status == expected_status
    canaries = (_QUERY_CANARY, _AGENT_CANARY, _REFERER_CANARY, _METHOD_CANARY)
    assert [line for line in logged_lines.lines() if any(c in line for c in canaries)] == []

    access = logged_lines.line_with("inbound.http.access")
    fields = dict(part.split("=", 1) for part in access.group("message").split()[1:])
    assert fields.pop("status") == str(status)
    assert fields.pop("route", None) == route
    if method is not None:
        assert fields["method"] == method
    assert set(fields) == {"method", "elapsed_s"}
    assert access.group("level") == ("WARNING" if status >= 400 else "DEBUG")
    if same_request_event is not None:
        # 访问记录在处理器的作用域退出后才写，仍带这次请求的 id。
        assert access.group("request_id") == logged_lines.request_id_of(same_request_event)
        assert access.group("request_id") != "-"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_broadcast_no_sockets(sample_server):
    """Test broadcast with no active sockets"""
    result = await sample_server.broadcast({"action": "test"})

    assert result == BroadcastResult()
    assert result.delivered is False
    assert bool(result) is False


def _http_reply_app(temp_app_root: Path, reply, sent: bool) -> tuple[XiaoQingApp, SimpleNamespace]:
    """真实的应用：命令 `/echo` 回 `reply`；OneBot HTTP 发送器（边界）对每个动作返回 `sent`。"""

    app = XiaoQingApp(temp_app_root)
    (app.plugins_dir / "echo").mkdir(parents=True, exist_ok=True)

    async def echo(_command, _args, _event, _context):
        return reply

    catalog = build_command_catalog_node(
        "echo",
        {
            "name"      : "echo",
            "triggers"  : ["echo"],
            "help"      : "测试命令",
            "permission": "public",
            "contexts"  : ["private", "group"],
        },
        root=True,
    )
    app.router.register(
        CommandSpec(plugin="echo", name="echo", triggers=["echo"], handler=echo, catalog=catalog)
    )
    sender = SimpleNamespace(
        http_base           = "http://127.0.0.1:1",
        credentials_trusted = True,
        send_action=AsyncMock(return_value=sent),
    )
    app.http_sender = sender
    return app, sender


def _standard_http_echo_event() -> dict[str, Any]:
    return {
        "post_type"            : "message",
        "message_type"         : "private",
        "user_id"              : 12345,
        "message"              : "/echo",
        "_source"              : "inbound_http",
        "_http_action_delivery": True,
    }


@pytest.mark.asyncio
async def test_http_standard_mode_executes_actions_before_returning(temp_app_root: Path):
    image = {"type": "image", "data": {"file": "review.png"}}
    reply = [{"type": "text", "data": {"text": "A" * (MAX_MESSAGE_TEXT_LENGTH + 17)}}, image]
    app, sender = _http_reply_app(temp_app_root, reply, sent=True)

    result = await app._handle_inbound_event(_standard_http_echo_event())

    assert result == []
    assert sender.send_action.await_count == 2
    posted = [call.args[0]["params"]["message"] for call in sender.send_action.await_args_list]
    assert posted[-1][-1] == image


@pytest.mark.asyncio
async def test_http_standard_mode_exposes_delivery_failure(temp_app_root: Path):
    reply = [{"type": "text", "data": {"text": "hello"}}]
    app, sender = _http_reply_app(temp_app_root, reply, sent=False)

    with pytest.raises(RuntimeError, match="not acknowledged"):
        await app._handle_inbound_event(_standard_http_echo_event())

    sender.send_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_http_failure_rolls_back_unattempted_reply_receipts(temp_app_root: Path):
    """前一段回复投递失败后，后面还没发的片段不再发送，这条回复的业务预留回滚。"""
    callbacks = []
    receipt   = DeliveryReceipt(
        expected_actions = 1,
        commit           = lambda: callbacks.append("commit"),
        rollback         = lambda: callbacks.append("rollback"),
        unknown          = lambda: callbacks.append("unknown"),
    )
    reply = DeliverySegments(
        [{"type": "text", "data": {"text": "A" * (MAX_MESSAGE_TEXT_LENGTH + 17)}}],
        receipt,
    )
    app, sender = _http_reply_app(temp_app_root, reply, sent=False)

    with pytest.raises(RuntimeError, match="not acknowledged"):
        await app._handle_inbound_event(_standard_http_echo_event())

    assert sender.send_action.await_count == 1
    assert callbacks == ["rollback"]
    assert receipt.resolved is True
    assert receipt.committed is False
