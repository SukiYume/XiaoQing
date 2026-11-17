"""分发器容量、公平性和故障收敛。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from aiohttp import WSCloseCode, WSMsgType

import tests.helpers.app_test_support as _app_support
import tests.helpers.server_test_support as _fixture_support
from core.delivery import DeliveryReceipt, attach_receipt
from core.log import LogEvent, current_request_id, log_scope
from core.server import _InboundEventDispatcher
from tests.helpers.app_test_support import LoggedLines
from tests.helpers.server_test_support import (
    ClientSession,
    InboundManager,
    auth_headers,
    inflight_count,
    lane_count,
    make_server,
    onebot_message_payload,
    pending_for_key,
    post_event,
    serving,
    wait_until,
    web,
)

mock_handler    = _fixture_support.mock_handler
sample_server   = _fixture_support.sample_server
restore_logging = _app_support.restore_logging
logged_lines    = _app_support.logged_lines


async def _post_marker(session: ClientSession, marker: str, *, token: str = "token"):
    """向真实服务器 POST 一条带 ``marker`` 的私聊事件（用户 10001）。"""

    event           = onebot_message_payload(marker)
    event["marker"] = marker
    return await post_event(session, event, token=token)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_http_admission_capacity_is_bounded_and_released(unused_tcp_port):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(event: dict[str, Any]) -> list[dict[str, Any]]:
        if event["marker"] == "first":
            entered.set()
            await release.wait()
        return []

    server = make_server(
        handler,
        unused_tcp_port,
        token          = "token",
        ws_max_workers = 1,
        ws_queue_size  = 1,
    )
    dispatcher = server._event_dispatcher
    async with serving(server) as session:
        try:
            first = asyncio.create_task(_post_marker(session, "first"))
            await asyncio.wait_for(entered.wait(), 5)
            queued = asyncio.create_task(_post_marker(session, "queued"))
            await wait_until(lambda: pending_for_key(dispatcher, "user:10001") == 2)

            overloaded = await _post_marker(session, "N+1")
            assert overloaded.status == 503
            assert "overloaded" in overloaded.text
            assert inflight_count(dispatcher) == 2
        finally:
            release.set()

        assert (await first).status == 200
        assert (await queued).status == 200
        await wait_until(lambda: inflight_count(dispatcher) == 0)
        assert lane_count(dispatcher) == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_queued_cancellation_storm_returns_dispatcher_to_exact_baseline():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(event: dict[str, Any]) -> list[dict[str, Any]]:
        if event["marker"] == "running":
            entered.set()
            await release.wait()
        return []

    dispatcher = _InboundEventDispatcher(handler, max_workers=1, queue_size=64)
    await dispatcher.start()
    running = asyncio.create_task(dispatcher.dispatch({"user_id": 1, "marker": "running"}))
    await asyncio.wait_for(entered.wait(), 5)
    queued = [
        asyncio.create_task(dispatcher.dispatch({"user_id": 1, "marker": f"queued-{index}"}))
        for index in range(50)
    ]
    for _ in range(20):
        if inflight_count(dispatcher) == 51:
            break
        await asyncio.sleep(0)
    assert inflight_count(dispatcher) == 51

    for task in queued:
        task.cancel()
    await asyncio.gather(*queued, return_exceptions=True)
    await asyncio.sleep(0)
    assert inflight_count(dispatcher) == 1
    assert pending_for_key(dispatcher, "user:1") == 1

    release.set()
    await running
    assert inflight_count(dispatcher) == 0
    assert lane_count(dispatcher) == 0
    assert all(task.done() for task in queued)
    await dispatcher.stop()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_queued_and_running_old_auth_events_fail_closed_without_poisoning_lane(
    unused_tcp_port,
):
    entered         = asyncio.Event()
    release         = asyncio.Event()
    seen: list[str] = []

    async def handler(event: dict[str, Any]) -> list[dict[str, Any]]:
        marker = str(event["marker"])
        seen.append(marker)
        if marker == "running":
            entered.set()
            await release.wait()
        return [{"marker": marker}]

    server = make_server(handler, unused_tcp_port, token="old", ws_max_workers=1)
    dispatcher = server._event_dispatcher
    async with serving(server) as session:
        try:
            running = asyncio.create_task(_post_marker(session, "running", token="old"))
            await asyncio.wait_for(entered.wait(), 5)
            queued = asyncio.create_task(_post_marker(session, "queued", token="old"))
            await wait_until(lambda: inflight_count(dispatcher) == 2)

            server.update_token("new")
        finally:
            release.set()
        running_response, queued_response = await asyncio.gather(running, queued)
        await wait_until(lambda: inflight_count(dispatcher) == 0)

    assert running_response.status == 401
    assert queued_response.status == 401
    assert seen == ["running"]
    assert lane_count(dispatcher) == 0


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("failure_kind", ["fatal", "sync", "invalid"])
async def test_handler_failures_are_task_safe_and_next_same_key_runs(
    unused_tcp_port,
    failure_kind: str,
):
    async def fatal_handler(_event: dict[str, Any]) -> list[dict[str, Any]]:
        raise SystemExit("fatal")

    def sync_handler(_event: dict[str, Any]):
        raise ValueError("sync failure")

    async def invalid_handler(_event: dict[str, Any]):
        return {"not": "a list"}

    async def healthy(_event: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"ok": True}]

    current = {
        "fatal"  : fatal_handler,
        "sync"   : sync_handler,
        "invalid": invalid_handler,
    }[failure_kind]

    def handler(event: dict[str, Any]):
        return current(event)

    server = make_server(handler, unused_tcp_port, token="token")
    async with serving(server) as session:
        failed = await post_event(session, onebot_message_payload("bad"), token="token")
        assert failed.status == 500

        current   = healthy
        succeeded = await asyncio.wait_for(
            post_event(session, onebot_message_payload("good"), token="token"),
            timeout=1,
        )
        assert succeeded.status == 200
        assert inflight_count(server._event_dispatcher) == 0
        assert lane_count(server._event_dispatcher) == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_hot_lane_yields_to_cold_lane_without_breaking_same_key_fifo():
    first_entered    = asyncio.Event()
    release          = asyncio.Event()
    calls: list[str] = []

    async def handler(event: dict[str, Any]) -> list[dict[str, Any]]:
        marker = str(event["marker"])
        calls.append(marker)
        if marker == "hot-0":
            first_entered.set()
            await release.wait()
        return []

    dispatcher = _InboundEventDispatcher(handler, max_workers=1, queue_size=10)
    await dispatcher.start()

    def event(marker: str, user_id: int) -> dict[str, Any]:
        return {"marker": marker, "user_id": user_id}

    first   = dispatcher.admit(event("hot-0", 1))
    waiters = [asyncio.create_task(dispatcher.wait(first))]
    await first_entered.wait()
    for index in range(1, 5):
        waiters.append(
            asyncio.create_task(dispatcher.wait(dispatcher.admit(event(f"hot-{index}", 1))))
        )
    waiters.append(asyncio.create_task(dispatcher.wait(dispatcher.admit(event("cold", 2)))))

    release.set()
    await asyncio.gather(*waiters)
    assert calls == ["hot-0", "cold", "hot-1", "hot-2", "hot-3", "hot-4"]
    assert inflight_count(dispatcher) == 0
    assert lane_count(dispatcher) == 0
    await dispatcher.stop()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_long_same_key_lane_does_not_spawn_worker_per_event():
    calls = 0

    async def handler(_event: dict[str, Any]) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return []

    dispatcher = _InboundEventDispatcher(handler, max_workers=4, queue_size=1_000)
    await dispatcher.start()
    tickets = [dispatcher.admit({"user_id": 1, "index": index}) for index in range(1_000)]
    await asyncio.gather(*(dispatcher.wait(ticket) for ticket in tickets))

    assert calls == 1_000
    assert dispatcher._worker_starts == 1
    assert inflight_count(dispatcher) == 0
    assert lane_count(dispatcher) == 0
    await dispatcher.stop()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_one_worker_runs_each_ticket_and_rolls_back_dropped_results_under_its_id(
    logged_lines: LoggedLines,
):
    probe_log                        = logging.getLogger("plugins.probe")
    late_entered                     = asyncio.Event()
    guard_ids: dict[str, str | None] = {}

    async def handler(event: dict[str, Any]) -> list[dict[str, Any]]:
        label = event["label"]
        if label == "late":
            late_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass  # 吞掉取消、照样交回回复：结果已被取消，没有人会发送它。

        def rolled_back() -> None:
            probe_log.info(LogEvent("probe.rolled_back", label=label))

        receipt = DeliveryReceipt(
            expected_actions=1, commit=lambda: None, rollback=rolled_back, unknown=rolled_back
        )
        return [attach_receipt({"action": "send_private_msg", "params": {}}, receipt)]

    def guard(label: str) -> Callable[[], bool]:
        def authorized() -> bool:
            guard_ids[label] = current_request_id()
            return True

        return authorized

    labels = ("first", "late", "done")
    dispatcher = _InboundEventDispatcher(handler, max_workers=1, queue_size=4)
    await dispatcher.start()
    # 三条请求在各自的作用域里登记，由第一条登记时启动的同一个 worker 依次执行。
    tickets = {}
    for user_id, label in enumerate(labels, start=1):
        with log_scope(f"request-{label}"):
            tickets[label] = dispatcher.admit(
                {"user_id": user_id, "label": label}, auth_guard=guard(label)
            )
    await dispatcher.wait(tickets["first"])
    await asyncio.wait_for(late_entered.wait(), 5)
    dispatcher.cancel(tickets["late"])
    await wait_until(tickets["done"].result.done)
    # 另一条请求丢弃一个已经完成的结果。
    with log_scope("request-other"):
        dispatcher.discard(tickets["done"])
    await dispatcher.stop()

    assert dispatcher._worker_starts == 1
    assert guard_ids == {label: f"request-{label}" for label in labels}
    assert logged_lines.request_id_of("probe.rolled_back label=late") == "request-late"
    assert logged_lines.request_id_of("probe.rolled_back label=done") == "request-done"


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("send_failure", ["timeout", "fatal"])
async def test_ws_send_failure_is_bounded_and_does_not_poison_same_key(
    unused_tcp_port,
    monkeypatch,
    send_failure: str,
):
    calls: list[int] = []

    async def handler(event: dict[str, Any]) -> list[dict[str, Any]]:
        calls.append(int(event["user_id"]))
        return [{"ok": True}]

    never         = asyncio.Event()
    real_send_str = web.WebSocketResponse.send_str
    failed_once   = False

    # aiohttp 的写边界：第一次回复发送失败（卡住直到超时，或抛出致命异常），之后照常发送。
    async def failing_first_send_str(self, data, compress=None):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            if send_failure == "timeout":
                await never.wait()
            raise SystemExit("send fatal")
        return await real_send_str(self, data, compress)

    monkeypatch.setattr(web.WebSocketResponse, "send_str", failing_first_send_str)
    event            = onebot_message_payload()
    event["user_id"] = 7
    server = make_server(handler, unused_tcp_port, ws_broadcast_timeout_seconds=0.01)
    dispatcher = server._event_dispatcher
    async with serving(server) as session:
        failed_ws = await session.ws_connect("/ws", headers=auth_headers())
        await failed_ws.send_json(event)
        await wait_until(lambda: failed_once and inflight_count(dispatcher) == 0)
        assert lane_count(dispatcher) == 0
        closed = await failed_ws.receive(timeout=2)
        assert closed.type in {web.WSMsgType.CLOSE, web.WSMsgType.CLOSED}

        healthy_ws = await session.ws_connect("/ws", headers=auth_headers())
        await healthy_ws.send_json(event)
        assert await healthy_ws.receive_json(timeout=2) == {"ok": True}

    assert calls == [7, 7]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ws_frame_after_stop_gate_never_reaches_dispatcher(unused_tcp_port, monkeypatch):
    frame_read = asyncio.Event()
    frame_gate = asyncio.Event()
    handler = AsyncMock(return_value=[])
    server = make_server(handler, unused_tcp_port)

    # 屏障：要让“帧已经读到、但 stop 的关闸先发生”这个确切时刻稳定出现，
    # 就在 aiohttp 读到文本帧之后拦住它，放行后照常返回这一帧。
    real_receive = web.WebSocketResponse.receive

    async def gated_receive(self, timeout=None):
        msg = await real_receive(self, timeout)
        if msg.type == web.WSMsgType.TEXT:
            frame_read.set()
            await frame_gate.wait()
        return msg

    monkeypatch.setattr(web.WebSocketResponse, "receive", gated_receive)
    async with serving(server) as session:
        ws = await session.ws_connect("/ws", headers=auth_headers())
        try:
            await ws.send_json(onebot_message_payload("late"))
            await asyncio.wait_for(frame_read.wait(), 5)
            stopping = asyncio.create_task(server.stop())
            await wait_until(lambda: not server._accepting_events)
        finally:
            frame_gate.set()
        # 服务器收到停止后关闭这条连接：客户端读到关闭帧并应答，stop 才会立即收尾。
        closed = await ws.receive(timeout=2)
        await stopping
        assert closed.type in {web.WSMsgType.CLOSE, web.WSMsgType.CLOSED, web.WSMsgType.CLOSING}

    handler.assert_not_awaited()
    assert inflight_count(server._event_dispatcher) == 0
    assert lane_count(server._event_dispatcher) == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_stop_leaves_no_queued_ws_work_behind(unused_tcp_port):
    entered         = asyncio.Event()
    release         = asyncio.Event()
    seen: list[str] = []

    async def handler(event: dict[str, Any]) -> list[dict[str, Any]]:
        marker = str(event["marker"])
        seen.append(marker)
        if marker == "running":
            entered.set()
            await release.wait()
        return []

    server = make_server(handler, unused_tcp_port, ws_max_workers=1)
    server._handler_drain_timeout_seconds = 0.05
    async with serving(server) as session:
        try:
            ws = await session.ws_connect("/ws", headers=auth_headers())
            await ws.send_json({**onebot_message_payload(), "user_id": 42, "marker": "running"})
            await asyncio.wait_for(entered.wait(), 5)
            # 只有一个 worker 且它在忙：第二个用户的事件停在 WS 队列里。
            await ws.send_json({**onebot_message_payload(), "user_id": 43, "marker": "queued"})
            queue = server._ws_event_queue
            assert queue is not None
            await wait_until(lambda: queue.qsize() == 1)

            # 客户端还连着就停服务器：在途的那件按排空期限收尾，排队的那件不再开始。
            await asyncio.wait_for(server.stop(), 5)
            closing = await asyncio.wait_for(ws.receive(), 5)
        finally:
            release.set()

    assert seen == ["running"]
    assert closing.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING}
    assert inflight_count(server._event_dispatcher) == 0
    assert lane_count(server._event_dispatcher) == 0
    assert queue.empty()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_stop_closes_idle_websocket_clients_promptly(unused_tcp_port):
    server = make_server(AsyncMock(return_value=[]), unused_tcp_port)
    async with serving(server) as session:
        ws = await session.ws_connect("/ws", headers=auth_headers())

        # 空闲的客户端不发消息：停止要主动关掉它，而不是等 aiohttp 的关闭超时。
        await asyncio.wait_for(server.stop(), 5)
        closing = await asyncio.wait_for(ws.receive(), 5)

    assert closing.type == WSMsgType.CLOSE
    assert closing.data == WSCloseCode.GOING_AWAY
    assert server.active_ws_connections() == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_dispatcher_stop_timeout_and_retry_leave_no_event_wait_tasks():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def resistant(_event: dict[str, Any]) -> list[dict[str, Any]]:
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        return []

    dispatcher = _InboundEventDispatcher(
        resistant,
        max_workers           = 1,
        queue_size            = 1,
        drain_timeout_seconds = 0.01,
    )
    await dispatcher.start()
    waiter = asyncio.create_task(dispatcher.dispatch({"user_id": 1}))
    try:
        await asyncio.wait_for(entered.wait(), 5)

        with pytest.raises(RuntimeError, match="ignored cancellation"):
            await dispatcher.stop()
        assert not [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and getattr(task.get_coro(), "__qualname__", "") == "Event.wait"
        ]
    finally:
        release.set()
        await asyncio.gather(waiter, return_exceptions=True)
        for _ in range(20):
            if inflight_count(dispatcher) == 0:
                break
            await asyncio.sleep(0)
        await dispatcher.stop()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_cancelled_dispatcher_stop_leaves_no_anonymous_drain_waiter():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(_event: dict[str, Any]) -> list[dict[str, Any]]:
        entered.set()
        await release.wait()
        return []

    dispatcher = _InboundEventDispatcher(
        handler,
        max_workers           = 1,
        queue_size            = 1,
        drain_timeout_seconds = 1,
    )
    await dispatcher.start()
    event_task = asyncio.create_task(dispatcher.dispatch({"user_id": 1}))
    await asyncio.wait_for(entered.wait(), 5)
    stop_task = asyncio.create_task(dispatcher.stop())
    await asyncio.sleep(0)
    stop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop_task
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and getattr(task.get_coro(), "__qualname__", "") == "Event.wait"
    ]

    release.set()
    await event_task
    await dispatcher.stop()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manager_retains_shared_dispatcher_until_failed_child_stop_retries():
    manager = InboundManager(
        inbound_http_base = "http://127.0.0.1:18080",
        inbound_ws_uri    = "ws://127.0.0.1:18081/ws",
        token             = "token",
        handler=AsyncMock(return_value=[]),
    )
    first = MagicMock(
        start=AsyncMock(),
        stop=AsyncMock(side_effect=[RuntimeError("first stop failed"), None]),
        commit_admission=Mock(),
    )
    second = MagicMock(start=AsyncMock(), stop=AsyncMock(), commit_admission=Mock())
    with patch("core.server.InboundServer", side_effect=[first, second]):
        await manager.start()

    dispatcher = manager._event_dispatcher
    assert dispatcher is not None
    with pytest.raises(RuntimeError, match="first stop failed"):
        await manager.stop()
    assert manager._event_dispatcher is dispatcher
    assert manager.http_server is first

    await manager.stop()
    assert manager._event_dispatcher is None
    assert manager.http_server is None
    assert first.stop.await_count == 2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manager_partial_stop_restores_latest_cross_thread_token():
    manager = InboundManager(
        inbound_http_base = "http://127.0.0.1:18080",
        inbound_ws_uri    = "ws://127.0.0.1:18080/ws",
        token             = "old",
        handler=AsyncMock(return_value=[]),
    )
    stop_entered = asyncio.Event()
    stop_release = asyncio.Event()
    attempts     = 0

    async def stop_server() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            stop_entered.set()
            await stop_release.wait()
            raise RuntimeError("partial stop")

    server = MagicMock(stop=AsyncMock(side_effect=stop_server), update_token=Mock())
    manager.http_server = server
    manager.ws_server   = server
    manager._running    = True

    stopping = asyncio.create_task(manager.stop())
    await stop_entered.wait()
    await asyncio.to_thread(manager.update_token, "new")
    stop_release.set()
    with pytest.raises(RuntimeError, match="partial stop"):
        await stopping

    assert manager.http_server is server
    assert manager.ws_server is server
    server.update_token.assert_called_once_with("new")

    await manager.stop()
    assert manager.http_server is None
    assert manager.ws_server is None
