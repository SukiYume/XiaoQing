"""分发器容量、公平性和故障收敛。"""

from __future__ import annotations

import tests.helpers.server_test_support as _fixture_support
from tests.helpers.server_test_support import (
    Any,
    AsyncMock,
    InboundManager,
    InboundServer,
    MagicMock,
    Mock,
    SimpleNamespace,
    _InboundEventDispatcher,
    _inflight_count,
    _lane_count,
    _make_request_with_auth,
    _MockRequest,
    _onebot_message_payload,
    _pending_for_key,
    asyncio,
    json,
    patch,
    pytest,
    web,
)

mock_handler = _fixture_support.mock_handler
sample_server = _fixture_support.sample_server


@pytest.mark.asyncio
@pytest.mark.unit
async def test_http_admission_capacity_and_queued_cancellation_are_bounded():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(event: dict[str, Any]) -> list[dict[str, Any]]:
        if event["marker"] == "first":
            entered.set()
            await release.wait()
        return []

    server = InboundServer(
        "127.0.0.1",
        8765,
        "token",
        handler,
        ws_max_workers=1,
        ws_queue_size=1,
    )

    def request(marker: str) -> _MockRequest:
        event = _onebot_message_payload(marker)
        event["marker"] = marker
        result = _make_request_with_auth("POST", "/event", "token")
        result.json = AsyncMock(return_value=event)
        return result

    first = asyncio.create_task(server.post_event(request("first")))
    await entered.wait()
    queued = asyncio.create_task(server.post_event(request("queued")))
    for _ in range(20):
        if _pending_for_key(server._event_dispatcher, "user:10001") == 2:
            break
        await asyncio.sleep(0)

    overloaded = await server.post_event(request("N+1"))
    assert overloaded.status == 503
    assert "overloaded" in overloaded.text
    assert _inflight_count(server._event_dispatcher) == 2

    queued.cancel()
    await asyncio.gather(queued, return_exceptions=True)
    await asyncio.sleep(0)
    assert _pending_for_key(server._event_dispatcher, "user:10001") == 1
    assert _inflight_count(server._event_dispatcher) == 1

    release.set()
    assert (await first).status == 200
    assert _inflight_count(server._event_dispatcher) == 0
    assert _lane_count(server._event_dispatcher) == 0
    await server.stop()


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
    await entered.wait()
    queued = [
        asyncio.create_task(dispatcher.dispatch({"user_id": 1, "marker": f"queued-{index}"}))
        for index in range(50)
    ]
    for _ in range(20):
        if _inflight_count(dispatcher) == 51:
            break
        await asyncio.sleep(0)
    assert _inflight_count(dispatcher) == 51

    for task in queued:
        task.cancel()
    await asyncio.gather(*queued, return_exceptions=True)
    await asyncio.sleep(0)
    assert _inflight_count(dispatcher) == 1
    assert _pending_for_key(dispatcher, "user:1") == 1

    release.set()
    await running
    assert _inflight_count(dispatcher) == 0
    assert _lane_count(dispatcher) == 0
    assert all(task.done() for task in queued)
    await dispatcher.stop()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_queued_and_running_old_auth_events_fail_closed_without_poisoning_lane():
    entered = asyncio.Event()
    release = asyncio.Event()
    seen: list[str] = []

    async def handler(event: dict[str, Any]) -> list[dict[str, Any]]:
        marker = str(event["marker"])
        seen.append(marker)
        if marker == "running":
            entered.set()
            await release.wait()
        return [{"marker": marker}]

    server = InboundServer("127.0.0.1", 8765, "old", handler, ws_max_workers=1)

    def request(marker: str) -> _MockRequest:
        event = _onebot_message_payload(marker)
        event["marker"] = marker
        result = _make_request_with_auth("POST", "/event", "old")
        result.json = AsyncMock(return_value=event)
        return result

    running = asyncio.create_task(server.post_event(request("running")))
    await entered.wait()
    queued = asyncio.create_task(server.post_event(request("queued")))
    for _ in range(20):
        if _inflight_count(server._event_dispatcher) == 2:
            break
        await asyncio.sleep(0)

    server.update_token("new")
    release.set()
    running_response, queued_response = await asyncio.gather(running, queued)

    assert running_response.status == 401
    assert queued_response.status == 401
    assert seen == ["running"]
    assert _inflight_count(server._event_dispatcher) == 0
    assert _lane_count(server._event_dispatcher) == 0
    await server.stop()


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("failure_kind", ["fatal", "sync", "invalid"])
async def test_handler_failures_are_task_safe_and_next_same_key_runs(failure_kind: str):
    async def fatal_handler(_event: dict[str, Any]) -> list[dict[str, Any]]:
        raise SystemExit("fatal")

    def sync_handler(_event: dict[str, Any]):
        raise ValueError("sync failure")

    async def invalid_handler(_event: dict[str, Any]):
        return {"not": "a list"}

    handlers = {
        "fatal": fatal_handler,
        "sync": sync_handler,
        "invalid": invalid_handler,
    }
    server = InboundServer("127.0.0.1", 8765, "token", handlers[failure_kind])
    event = _onebot_message_payload("bad")
    request = _make_request_with_auth("POST", "/event", "token")
    request.json = AsyncMock(return_value=event)

    failed = await server.post_event(request)
    assert failed.status == 500

    async def healthy(_event: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"ok": True}]

    server.handler = healthy
    healthy_request = _make_request_with_auth("POST", "/event", "token")
    healthy_request.json = AsyncMock(return_value=_onebot_message_payload("good"))
    succeeded = await asyncio.wait_for(server.post_event(healthy_request), timeout=1)
    assert succeeded.status == 200
    assert _inflight_count(server._event_dispatcher) == 0
    assert _lane_count(server._event_dispatcher) == 0
    await server.stop()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_hot_lane_yields_to_cold_lane_without_breaking_same_key_fifo():
    first_entered = asyncio.Event()
    release = asyncio.Event()
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

    first = dispatcher.admit(event("hot-0", 1))
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
    assert _inflight_count(dispatcher) == 0
    assert _lane_count(dispatcher) == 0
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
    assert _inflight_count(dispatcher) == 0
    assert _lane_count(dispatcher) == 0
    await dispatcher.stop()


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("send_failure", ["timeout", "fatal"])
async def test_ws_send_failure_is_bounded_and_does_not_poison_same_key(send_failure: str):
    calls: list[int] = []

    async def handler(event: dict[str, Any]) -> list[dict[str, Any]]:
        calls.append(int(event["user_id"]))
        return [{"ok": True}]

    server = InboundServer("127.0.0.1", 8765, "token", handler)
    server._ws_broadcast_timeout_seconds = 0.01
    never = asyncio.Event()

    async def hung_send(_text: str) -> None:
        await never.wait()

    if send_failure == "timeout":
        failed_ws = MagicMock(send_str=AsyncMock(side_effect=hung_send), close=AsyncMock())
    else:
        failed_ws = MagicMock(
            send_str=AsyncMock(side_effect=SystemExit("send fatal")),
            close=AsyncMock(),
        )
    await asyncio.wait_for(server._handle_ws_event(failed_ws, {"user_id": 7}), timeout=1)
    assert _inflight_count(server._event_dispatcher) == 0
    assert _lane_count(server._event_dispatcher) == 0

    healthy_ws = MagicMock(send_str=AsyncMock(), close=AsyncMock())
    await asyncio.wait_for(server._handle_ws_event(healthy_ws, {"user_id": 7}), timeout=1)
    healthy_ws.send_str.assert_awaited_once()
    assert calls == [7, 7]
    if server._ws_close_tasks:
        await asyncio.gather(*tuple(server._ws_close_tasks), return_exceptions=True)
    await server.stop()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ws_frame_after_stop_gate_never_reaches_dispatcher():
    frame_ready = asyncio.Event()
    prepared = asyncio.Event()
    handler = AsyncMock(return_value=[])

    class DelayedWebSocket:
        def __init__(self) -> None:
            self.sent = False

        async def prepare(self, _request) -> None:
            prepared.set()

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.sent:
                raise StopAsyncIteration
            await frame_ready.wait()
            self.sent = True
            return SimpleNamespace(
                type=web.WSMsgType.TEXT,
                data=json.dumps(_onebot_message_payload("late")),
            )

        async def close(self, **_kwargs) -> None:
            return None

    server = InboundServer("127.0.0.1", 8765, "token", handler)
    ws = DelayedWebSocket()
    request = _make_request_with_auth("GET", "/ws", "token")
    with patch("core.server.web.WebSocketResponse", return_value=ws):
        task = asyncio.create_task(server.ws_handler(request))
        await prepared.wait()
        server._accepting_events = False
        frame_ready.set()
        assert await task is ws

    handler.assert_not_awaited()
    assert _inflight_count(server._event_dispatcher) == 0
    assert _lane_count(server._event_dispatcher) == 0
    await server.stop()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_stop_discards_ws_queue_ticket_without_orphan_handler():
    handler = AsyncMock(return_value=[])
    server = InboundServer("127.0.0.1", 8765, "token", handler, ws_max_workers=1)
    queue = server._ws_event_queue
    assert queue is not None
    ticket = server._event_dispatcher.admit({"user_id": 42})
    queue.put_nowait((MagicMock(), server._auth_state, ticket))

    await server.stop()

    handler.assert_not_awaited()
    assert ticket.result.cancelled()
    assert _inflight_count(server._event_dispatcher) == 0
    assert _lane_count(server._event_dispatcher) == 0
    assert queue.empty()


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
        max_workers=1,
        queue_size=1,
        drain_timeout_seconds=0.01,
    )
    await dispatcher.start()
    waiter = asyncio.create_task(dispatcher.dispatch({"user_id": 1}))
    await entered.wait()

    with pytest.raises(RuntimeError, match="ignored cancellation"):
        await dispatcher.stop()
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and getattr(task.get_coro(), "__qualname__", "") == "Event.wait"
    ]

    release.set()
    await asyncio.gather(waiter, return_exceptions=True)
    for _ in range(20):
        if _inflight_count(dispatcher) == 0:
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
        max_workers=1,
        queue_size=1,
        drain_timeout_seconds=1,
    )
    await dispatcher.start()
    event_task = asyncio.create_task(dispatcher.dispatch({"user_id": 1}))
    await entered.wait()
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
@pytest.mark.integration
async def test_standalone_direct_call_can_bind_then_restart_cleanly(unused_tcp_port):
    handler = AsyncMock(return_value=[])
    server = InboundServer("127.0.0.1", unused_tcp_port, "token", handler)
    request = _make_request_with_auth("POST", "/event", "token")
    request.json = AsyncMock(return_value=_onebot_message_payload("direct"))

    assert (await server.post_event(request)).status == 200
    lazy_dispatcher = server._event_dispatcher
    assert lazy_dispatcher.is_quiescent

    await server.start()
    assert server._event_dispatcher is lazy_dispatcher
    await server.stop()
    assert lazy_dispatcher.is_stopped

    await server.start()
    assert server._event_dispatcher is not lazy_dispatcher
    await server.stop()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manager_retains_shared_dispatcher_until_failed_child_stop_retries():
    manager = InboundManager(
        inbound_http_base="http://127.0.0.1:18080",
        inbound_ws_uri="ws://127.0.0.1:18081/ws",
        token="token",
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
        inbound_http_base="http://127.0.0.1:18080",
        inbound_ws_uri="ws://127.0.0.1:18080/ws",
        token="old",
        handler=AsyncMock(return_value=[]),
    )
    stop_entered = asyncio.Event()
    stop_release = asyncio.Event()
    attempts = 0

    async def stop_server() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            stop_entered.set()
            await stop_release.wait()
            raise RuntimeError("partial stop")

    server = MagicMock(stop=AsyncMock(side_effect=stop_server), update_token=Mock())
    manager.http_server = server
    manager.ws_server = server
    manager._running = True

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
