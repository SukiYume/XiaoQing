# 验证关闭期间停止接纳新工作并等待已接纳任务。
from __future__ import annotations

import asyncio
import contextlib

import pytest
from aiohttp import ClientConnectionError, ClientSession

from tests.helpers.server_test_support import (
    StandaloneInboundServer,
    onebot_message_payload,
    post_event,
)


@pytest.mark.asyncio
async def test_inbound_stop_refuses_new_requests_and_drains_the_running_one(
    unused_tcp_port,
) -> None:
    entered            = asyncio.Event()
    release            = asyncio.Event()
    handled: list[str] = []

    async def handler(payload):
        handled.append(str(payload.get("message_id")))
        entered.set()
        await release.wait()
        return []

    server = StandaloneInboundServer("127.0.0.1", unused_tcp_port, "token", handler)
    await server.start()
    async with ClientSession(base_url=f"http://127.0.0.1:{unused_tcp_port}") as session:
        running = asyncio.create_task(
            post_event(session, {**onebot_message_payload(), "message_id": 1}, token="token")
        )
        await asyncio.wait_for(entered.wait(), 5)

        stopping = asyncio.create_task(server.stop())
        await asyncio.sleep(0.05)
        # 停止开始后新请求不再交给处理器：拿到 503，或者监听已经关了连不上。
        with contextlib.suppress(ClientConnectionError):
            refused = await post_event(
                session, {**onebot_message_payload(), "message_id": 2}, token="token"
            )
            assert refused.status == 503

        release.set()
        await asyncio.wait_for(stopping, 5)
        with contextlib.suppress(ClientConnectionError):
            await running

    assert handled == ["1"]


@pytest.mark.asyncio
async def test_inbound_stop_cancels_a_request_that_outlives_the_drain_deadline(
    unused_tcp_port,
) -> None:
    entered   = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(_payload):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return []

    server = StandaloneInboundServer("127.0.0.1", unused_tcp_port, "token", handler)
    # 调短排空期限，测试不真的等。
    server._handler_drain_timeout_seconds = 0.01
    await server.start()
    async with ClientSession(base_url=f"http://127.0.0.1:{unused_tcp_port}") as session:
        running = asyncio.create_task(post_event(session, onebot_message_payload(), token="token"))
        await asyncio.wait_for(entered.wait(), 5)

        await asyncio.wait_for(server.stop(), 5)

        assert cancelled.is_set()
        with contextlib.suppress(ClientConnectionError):
            await running
