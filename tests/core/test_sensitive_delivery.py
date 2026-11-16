"""敏感私聊（登录 Code、二维码提示）经各条传输发出时，正文不进日志，敏感标记不上线。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from core.app_delivery import AppDeliveryMixin
from core.delivery import SENSITIVE_ACTION_KEY, send_sensitive_private
from core.onebot import OneBotHttpSender, OneBotWsClient
from core.server import BroadcastResult
from tests.helpers.onebot_test_support import wait_for_ws_action_request

_CODE = "XQ-LOGIN-7F3K9Q"


def _bare_app() -> AppDeliveryMixin:
    app                                   = AppDeliveryMixin()
    app.ws_client                         = None
    app.inbound_manager                   = None
    app.http_sender                       = None
    app._http_transport_is_trusted        = lambda _: True
    app._ws_transport_is_trusted          = lambda _: True
    app._notify_outgoing_action_observers = AsyncMock()
    return app


class _AppContext:
    """插件拿到的 ``send_action`` 直通 Core 的单条投递。"""

    def __init__(self, app: AppDeliveryMixin) -> None:
        self._app = app

    async def send_action(self, action: dict[str, Any]) -> bool | None:
        return await self._app._send_single_action(action)


def _message() -> list[dict[str, Any]]:
    return [{"type": "text", "data": {"text": f"登录 Code：{_CODE}"}}]


def _logged(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_http_delivery_keeps_the_code_out_of_logs(caplog):
    received: list[dict[str, Any]] = []

    async def action_api(request: web.Request) -> web.Response:
        received.append(await request.json())
        return web.json_response({"status": "ok", "retcode": 0, "data": {"message_id": 7}})

    api_app = web.Application()
    api_app.router.add_post("/send_private_msg", action_api)
    async with TestServer(api_app) as upstream, ClientSession() as session:
        app             = _bare_app()
        app.http_sender = OneBotHttpSender(str(upstream.make_url("")), "", session)
        with caplog.at_level(logging.DEBUG):
            result = await send_sensitive_private(_AppContext(app), 123, _message())

    assert result is True
    assert received[0]["message"][0]["data"]["text"].endswith(_CODE)
    assert SENSITIVE_ACTION_KEY not in received[0]
    assert "Sent send_private_msg" in _logged(caplog)
    assert _CODE not in _logged(caplog)


@pytest.mark.asyncio
async def test_ws_delivery_keeps_the_code_out_of_logs_and_off_the_wire(caplog):
    client           = OneBotWsClient("ws://localhost:3000", "")
    client._ws       = AsyncMock()
    app              = _bare_app()
    app.ws_client    = client
    client.connected = lambda: True  # type: ignore[method-assign]

    with caplog.at_level(logging.DEBUG):
        pending = asyncio.create_task(send_sensitive_private(_AppContext(app), 123, _message()))
        frame   = await wait_for_ws_action_request(client._ws)
        assert client._resolve_action_response(
            {"echo": frame["echo"], "status": "ok", "retcode": 0, "data": {}},
            auth_state=client._endpoint_auth,
        )
        assert await pending is True

    assert SENSITIVE_ACTION_KEY not in frame
    assert frame["params"]["message"][0]["data"]["text"].endswith(_CODE)
    assert "Sent send_private_msg" in _logged(caplog)
    assert _CODE not in _logged(caplog)


@pytest.mark.asyncio
async def test_inbound_broadcast_receives_the_action_without_the_marker():
    broadcast: list[dict[str, Any]] = []

    class _Inbound:
        def has_active_ws_clients(self) -> bool:
            return True

        async def broadcast(self, action: dict[str, Any]) -> BroadcastResult:
            broadcast.append(json.loads(json.dumps(action, ensure_ascii=False)))
            return BroadcastResult(target_count=1, success_count=1)

    app                 = _bare_app()
    app.inbound_manager = _Inbound()

    assert await send_sensitive_private(_AppContext(app), 123, _message()) is True
    assert broadcast and SENSITIVE_ACTION_KEY not in broadcast[0]
