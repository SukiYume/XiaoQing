"""OneBot 测试共享 fixture、导入和私有 helper。"""

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.auth import verify_bearer_token
from core.bounded_http import HttpStatusError
from core.onebot import OneBotActionOutcomeUnknown, OneBotHttpSender, OneBotWsClient
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


# 重连等待测试把后台清理循环的间隔设成这个值：循环的睡眠照常等待，不和重连等待混在一起。
CLEANUP_INTERVAL_SECONDS = 3600.0


def record_reconnect_waits(
    monkeypatch: pytest.MonkeyPatch,
    on_wait: Callable[[float], Awaitable[None]] | None = None,
) -> list[float]:
    """在标准库边界替换 ``asyncio.sleep``：重连等待只记录时长，不真的等。

    返回按顺序记录的等待时长。``on_wait`` 在每次记录后运行，可以改变客户端状态或一直阻塞。
    时长为 0 的让出和 ``CLEANUP_INTERVAL_SECONDS`` 的清理循环睡眠照常等待。
    """

    delays: list[float] = []
    real_sleep          = asyncio.sleep

    async def fake_sleep(delay: float, result: Any = None) -> Any:
        if delay == 0 or delay == CLEANUP_INTERVAL_SECONDS:
            return await real_sleep(delay, result)
        delays.append(delay)
        if on_wait is not None:
            await on_wait(delay)
        return await real_sleep(0, result)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return delays


async def wait_for_ws_action_request(mock_ws: AsyncMock) -> dict[str, Any]:
    for _ in range(10):
        if mock_ws.send.await_count:
            return json.loads(mock_ws.send.await_args.args[0])
        await asyncio.sleep(0)
    raise AssertionError("WebSocket action was not sent")


async def drain_owned_ws_closes(client: OneBotWsClient) -> None:
    tasks = {task for _, task in tuple(client._ws_close_tasks.values())}
    if tasks:
        await asyncio.gather(*tasks)


class EndingSocket:
    """连上后读不到任何帧、直接正常结束的连接；结束前先执行 ``on_end``。"""

    def __init__(self, on_end: Callable[[], None] | None = None) -> None:
        self._on_end = on_end

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._on_end is not None:
            self._on_end()
        raise StopAsyncIteration

    async def close(self) -> None:
        return None


class ConnectContext:
    """``websockets.connect(...)`` 返回的异步上下文：进入后得到 ``socket``。"""

    def __init__(self, socket) -> None:
        self._socket = socket

    async def __aenter__(self):
        return self._socket

    async def __aexit__(self, *_args):
        return None


__all__ = (
    "CLEANUP_INTERVAL_SECONDS",
    "Any",
    "AsyncMock",
    "ConnectContext",
    "EndingSocket",
    "HttpStatusError",
    "OneBotActionOutcomeUnknown",
    "OneBotHttpSender",
    "OneBotWsClient",
    "drain_owned_ws_closes",
    "wait_for_ws_action_request",
    "asyncio",
    "bounded_transport_adapter",
    "cancellation_resistant_callback",
    "inspect",
    "json",
    "pytest",
    "record_reconnect_waits",
    "verify_bearer_token",
)
