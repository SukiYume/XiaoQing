# 验证关闭期间停止接纳新工作并等待已接纳任务。
from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest
from aiohttp import ClientConnectionError, ClientSession

import tests.helpers.app_test_support as _app_support
from core.delivery import DeliveryReceipt, attach_receipt
from core.log import LogEvent
from tests.helpers.app_test_support import LoggedLines
from tests.helpers.server_test_support import (
    StandaloneInboundServer,
    inflight_count,
    onebot_message_payload,
    post_event,
    wait_until,
)

restore_logging = _app_support.restore_logging
logged_lines    = _app_support.logged_lines


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
async def test_inbound_stop_cancels_requests_that_outlive_the_drain_deadline(
    unused_tcp_port, logged_lines: LoggedLines
) -> None:
    probe_log           = logging.getLogger("plugins.probe")
    release_warm        = asyncio.Event()
    entered  : set[str] = set()
    cancelled: set[str] = set()

    async def handler(payload):
        label = payload["label"]
        probe_log.info(LogEvent("probe.handled", label=label))
        entered.add(label)
        if label.startswith("warm"):
            await release_warm.wait()
            return []
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.add(label)
            if label == "slow-raises":
                raise

        # 吞掉取消、照样交回一条带回执的回复：结果已被丢弃，回执要回滚。
        def rolled_back() -> None:
            probe_log.info(LogEvent("probe.rolled_back", label=label))

        receipt = DeliveryReceipt(
            expected_actions = 1,
            commit           = lambda: None,
            rollback         = rolled_back,
            unknown          = lambda: None,
        )
        return [attach_receipt({"action": "send_private_msg", "params": {}}, receipt)]

    server = StandaloneInboundServer(
        "127.0.0.1", unused_tcp_port, "token", handler, ws_max_workers=2
    )
    # 调短排空期限，测试不真的等。
    server._handler_drain_timeout_seconds = 0.01
    await server.start()
    async with ClientSession(base_url=f"http://127.0.0.1:{unused_tcp_port}") as session:

        def post(label: str, user_id: int) -> asyncio.Task:
            event = {**onebot_message_payload(), "user_id": user_id, "label": label}
            return asyncio.create_task(post_event(session, event, token="token"))

        # 两个 worker 先各跑一条 warm 请求，之后才轮到排在后面的两条 slow 请求。
        requests = [post("warm-a", 1), post("warm-b", 2)]
        await wait_until(lambda: entered == {"warm-a", "warm-b"})
        requests += [post("slow-raises", 3), post("slow-replies", 4)]
        await wait_until(lambda: inflight_count(server._event_dispatcher) == 4)
        release_warm.set()
        await wait_until(lambda: {"slow-raises", "slow-replies"} <= entered)

        await asyncio.wait_for(server.stop(), 5)

        assert cancelled == {"slow-raises", "slow-replies"}
        with contextlib.suppress(ClientConnectionError):
            await asyncio.gather(*requests)

    rolled_back_id = logged_lines.request_id_of("probe.rolled_back label=slow-replies")
    assert rolled_back_id == logged_lines.request_id_of("probe.handled label=slow-replies")
    assert rolled_back_id != "-"
