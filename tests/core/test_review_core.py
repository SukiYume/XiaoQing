"""审查回归：真实投递边界、暂存收据和连接通知生命周期。"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from core.app_delivery import AppDeliveryMixin
from core.app_support import current_action_sink
from core.delivery import DeliveryReceipt, send_with_receipt
from core.onebot import OneBotHttpSender, OneBotWsClient
from core.plugin_base import split_message_segments
from core.server import InboundServer


@pytest.mark.asyncio
async def test_captured_receipt_waits_for_transport_failure():
    callbacks = []
    captured  = []

    async def sink(action):
        captured.append(action)

    receipt = DeliveryReceipt(
        expected_actions = 1,
        commit           = lambda: callbacks.append("commit"),
        rollback         = lambda: callbacks.append("rollback"),
        unknown          = lambda: callbacks.append("unknown"),
    )
    token = current_action_sink.set(sink)
    try:
        await send_with_receipt(
            AppDeliveryMixin()._send_action,
            {"action": "send_private_msg", "params": {"user_id": 1, "message": "review"}},
            receipt,
        )
    finally:
        current_action_sink.reset(token)
    assert len(captured) == 1
    assert callbacks == []
    await receipt.record(False)
    assert callbacks == ["rollback"]


@pytest.mark.asyncio
async def test_receipt_helper_does_not_count_core_partial_success_twice():
    callbacks = []
    app       = AppDeliveryMixin()
    app._send_single_action = AsyncMock(return_value=True)
    app._notify_outgoing_action_observers = AsyncMock()
    receipt                               = DeliveryReceipt(
        expected_actions = 2,
        commit           = lambda: callbacks.append("commit"),
        rollback         = lambda: None,
        unknown          = lambda: None,
    )
    action = {"action": "send_private_msg", "params": {"user_id": 1, "message": "review"}}
    await send_with_receipt(app._send_action, action, receipt)
    assert callbacks == []
    await send_with_receipt(app._send_action, action, receipt)
    assert callbacks == ["commit"]


@pytest.mark.parametrize("length", [1, 10, 3000])
def test_newline_split_preserves_text_and_length(length):
    text = "a" * length + "\nb"
    chunks = split_message_segments([{"type": "text", "data": {"text": text}}], max_length=length)
    parts = ["".join(segment["data"]["text"] for segment in chunk) for chunk in chunks]
    assert max(map(len, parts)) <= length
    assert "".join(parts) == text


@pytest.mark.asyncio
async def test_http_standard_mode_executes_actions_before_returning():
    app     = AppDeliveryMixin()
    actions = [
        {"action": "send_private_msg", "params": {"user_id": 1, "message": message}}
        for message in ["chunk1", [{"type": "image", "data": {"file": "review.png"}}]]
    ]
    app._collect_actions_for_event = AsyncMock(return_value=actions)
    app._send_action = AsyncMock(return_value=True)
    result = await app._handle_inbound_event(
        {"_source": "inbound_http", "_http_action_delivery": True}
    )
    assert result == []
    assert app._send_action.await_count == 2


@pytest.mark.asyncio
async def test_http_standard_mode_exposes_delivery_failure():
    app = AppDeliveryMixin()
    app._collect_actions_for_event = AsyncMock(return_value=[{"action": "send_private_msg"}])
    app._send_action = AsyncMock(return_value=False)
    with pytest.raises(RuntimeError, match="not acknowledged"):
        await app._handle_inbound_event({"_source": "inbound_http", "_http_action_delivery": True})


@pytest.mark.asyncio
async def test_http_failure_rolls_back_unattempted_reply_receipts():
    """前一条回复失败后，后续尚未投递的业务预留必须回滚。"""
    callbacks = []
    receipt   = DeliveryReceipt(
        expected_actions = 1,
        commit           = lambda: callbacks.append("commit"),
        rollback         = lambda: callbacks.append("rollback"),
        unknown          = lambda: callbacks.append("unknown"),
    )
    app                            = AppDeliveryMixin()
    app._collect_actions_for_event = AsyncMock(
        return_value=[
            {"action": "send_private_msg"},
            {"action": "send_private_msg", "_delivery_receipt": receipt},
        ]
    )
    app._send_action = AsyncMock(return_value=False)
    with pytest.raises(RuntimeError, match="not acknowledged"):
        await app._handle_inbound_event({"_source": "inbound_http", "_http_action_delivery": True})
    assert app._send_action.await_count == 1
    assert callbacks == ["rollback"]


@pytest.mark.asyncio
async def test_connect_callback_receives_echo_while_listener_runs():
    frames = asyncio.Queue()
    client = OneBotWsClient("ws://unused", "", action_response_timeout_seconds=0.5)
    results = []

    class Socket:
        async def send(self, payload):
            request = json.loads(payload)
            await frames.put(json.dumps({"echo": request["echo"], "status": "ok", "retcode": 0}))

        def __aiter__(self):
            return self

        async def __anext__(self):
            frame = await frames.get()
            if frame is None:
                raise StopAsyncIteration
            return frame

    async def connected():
        results.append(
            await client.send_action(
                {"action": "send_group_msg", "params": {"group_id": 1, "message": "review"}}
            )
        )
        await frames.put(None)

    client.set_on_connect(connected)
    await asyncio.wait_for(client._listen(Socket(), AsyncMock()), timeout=2)
    assert results == [True]
    assert not any(task.get_name() == "onebot-on-connect" for task in asyncio.all_tasks())


@pytest.mark.asyncio
@pytest.mark.parametrize("retcode, expected_status", [(0, 200), (100, 500)])
async def test_standard_http_event_reaches_real_action_api(retcode, expected_status):
    """本机双服务贯通上报、Action POST 和收据，验证协议边界。"""
    received  = []
    callbacks = []

    async def action_api(request):
        received.append(await request.json())
        assert request.headers["Authorization"] == "Bearer outgoing"
        return web.json_response(
            {
                "status": "ok" if retcode == 0 else "failed",
                "retcode": retcode,
                "data": {"message_id": 42},
            }
        )

    api_app = web.Application()
    api_app.router.add_post("/send_private_msg", action_api)
    async with TestServer(api_app) as upstream, ClientSession() as session:
        app = AppDeliveryMixin()
        app.ws_client = None
        app.inbound_manager = None
        app.http_sender = OneBotHttpSender(str(upstream.make_url("")), "outgoing", session)
        app._http_transport_is_trusted = lambda _: True
        app._claim_inbound_event = AsyncMock(return_value=True)
        app._notify_outgoing_action_observers = AsyncMock()
        receipt                               = DeliveryReceipt(
            expected_actions = 1,
            commit           = lambda: callbacks.append("commit"),
            rollback         = lambda: callbacks.append("rollback"),
            unknown          = lambda: callbacks.append("unknown"),
        )
        app._process_event = AsyncMock(
            return_value={
                "action": "send_private_msg",
                "params": {
                    "user_id": 123,
                    "message": [{"type": "text", "data": {"text": "review"}}],
                },
                "_delivery_receipt": receipt,
            }
        )
        inbound = InboundServer("127.0.0.1", 0, "incoming", app._handle_inbound_event)
        try:
            async with TestClient(TestServer(inbound.app)) as client:
                response = await client.post(
                    "/event",
                    headers = {"Authorization": "Bearer incoming"},
                    json    = {
                        "post_type": "message",
                        "message_type": "private",
                        "user_id": 123,
                        "message": "/echo review",
                        "message_id": 1,
                    },
                )
                assert response.status == expected_status
                if retcode == 0:
                    assert await response.json() == {}
        finally:
            await inbound.stop()
    assert len(received) == 1
    assert received[0]["message"][0]["data"]["text"] == "review"
    assert callbacks == ["commit" if retcode == 0 else "rollback"]
