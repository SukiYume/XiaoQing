"""OneBot 测试共享 fixture、导入和私有 helper。"""

import asyncio
import inspect
import json
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.auth import verify_bearer_token
from core.bounded_http import HttpStatusError
from core.onebot import (
    _CONNECT_SIGNATURE_CACHE,
    OneBotActionOutcomeUnknown,
    OneBotHttpSender,
    OneBotWsClient,
    _ConnectionAttemptResult,
    _extract_message_preview,
    _get_connect_signature,
    _jittered_reconnect_delay,
    _mask_sensitive_text,
    _normalize_action_for_onebot,
    _onebot_action_succeeded,
    _QueuedOneBotEvent,
    _summarize_event,
    _UnsupportedWebSocketAuthentication,
)
from tests.helpers.asyncio_tools import cancellation_resistant_callback


@pytest.fixture(autouse=True)
def bounded_transport_adapter(monkeypatch):
    """Adapt the historical OneBot response mocks to bounded JSON bytes."""

    async def request(session, method, url, **kwargs):
        request_kwargs = dict(kwargs.get("request_kwargs") or {})
        response_cm    = session.post(
            url,
            headers=kwargs.get("headers"),
            **request_kwargs,
        )
        async with response_cm as response:
            status = int(response.status)
            if status not in kwargs.get("success_statuses", {200}):
                raise HttpStatusError(status)
            return json.dumps(await response.json()).encode("utf-8")

    monkeypatch.setattr("core.onebot.aiohttp_request_bounded", request)


async def _wait_for_ws_action_request(mock_ws: AsyncMock) -> dict[str, Any]:
    for _ in range(10):
        if mock_ws.send.await_count:
            return json.loads(mock_ws.send.await_args.args[0])
        await asyncio.sleep(0)
    raise AssertionError("WebSocket action was not sent")


async def _drain_owned_ws_closes(client: OneBotWsClient) -> None:
    tasks = {task for _, task in tuple(client._ws_close_tasks.values())}
    if tasks:
        await asyncio.gather(*tasks)


__all__ = (
    "Any",
    "AsyncMock",
    "HttpStatusError",
    "MagicMock",
    "OneBotActionOutcomeUnknown",
    "OneBotHttpSender",
    "OneBotWsClient",
    "_CONNECT_SIGNATURE_CACHE",
    "_ConnectionAttemptResult",
    "_QueuedOneBotEvent",
    "_UnsupportedWebSocketAuthentication",
    "_drain_owned_ws_closes",
    "_extract_message_preview",
    "_get_connect_signature",
    "_jittered_reconnect_delay",
    "_mask_sensitive_text",
    "_normalize_action_for_onebot",
    "_onebot_action_succeeded",
    "_summarize_event",
    "_wait_for_ws_action_request",
    "asyncio",
    "bounded_transport_adapter",
    "cancellation_resistant_callback",
    "inspect",
    "json",
    "patch",
    "pytest",
    "threading",
    "verify_bearer_token",
)
