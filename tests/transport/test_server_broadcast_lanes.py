"""广播、串行通道和令牌轮换。"""

from __future__ import annotations

import tests.helpers.server_test_support as _fixture_support
from tests.helpers.server_test_support import (
    Any,
    AsyncMock,
    BlockingConcurrencyProbe,
    BroadcastResult,
    InboundManager,
    InboundServer,
    MagicMock,
    Mock,
    _lane_count,
    _make_request_with_auth,
    _onebot_message_payload,
    _pending_for_key,
    asyncio,
    json,
    patch,
    pytest,
    threading,
)

mock_handler  = _fixture_support.mock_handler
sample_server = _fixture_support.sample_server


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_broadcast_with_sockets(sample_server):
    """Test broadcast with active sockets"""
    # Create mock sockets
    mock_ws1 = AsyncMock()
    mock_ws2 = AsyncMock()

    sample_server._active_sockets.add(mock_ws1)
    sample_server._active_sockets.add(mock_ws2)

    result = await sample_server.broadcast({"action": "test", "message": "hello"})

    # Both sockets should have been called
    mock_ws1.send_str.assert_called_once()
    mock_ws2.send_str.assert_called_once()
    assert result == BroadcastResult(target_count=2, success_count=2)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_broadcast_socket_error(sample_server):
    """Test broadcast handles socket errors gracefully"""
    # Create mock socket that raises
    mock_ws = AsyncMock()
    mock_ws.send_str = AsyncMock(side_effect=Exception("Connection lost"))

    sample_server._active_sockets.add(mock_ws)

    result = await sample_server.broadcast({"action": "test"})

    assert mock_ws not in sample_server._active_sockets
    assert result == BroadcastResult(target_count=1, failure_count=1)
    await asyncio.gather(*tuple(sample_server._ws_close_tasks))
    mock_ws.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_broadcast_reports_partial_failure_without_duplicate_encoding(sample_server):
    healthy                     = AsyncMock()
    failed                      = AsyncMock()
    failed.send_str.side_effect = ConnectionError("gone")
    sample_server._active_sockets.update({healthy, failed})

    with patch("core.server.json.dumps", wraps=json.dumps) as dumps:
        result = await sample_server.broadcast({"action": "test"})

    assert result == BroadcastResult(target_count=2, success_count=1, failure_count=1)
    assert result.delivered is True
    assert healthy in sample_server._active_sockets
    assert failed not in sample_server._active_sockets
    dumps.assert_called_once_with({"action": "test"}, ensure_ascii=False)
    await asyncio.gather(*tuple(sample_server._ws_close_tasks))


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_broadcast_times_out_hung_client_without_blocking_healthy_client(
    mock_handler,
):
    server = InboundServer(
        host                         = "127.0.0.1",
        port                         = 8765,
        token                        = "test_token",
        handler                      = mock_handler,
        ws_broadcast_timeout_seconds = 0.01,
    )
    never = asyncio.Event()
    hung  = AsyncMock()

    async def hung_send(_text: str) -> None:
        await never.wait()

    hung.send_str.side_effect = hung_send
    healthy                   = AsyncMock()
    server._active_sockets.update({hung, healthy})

    started_at = asyncio.get_running_loop().time()
    result     = await server.broadcast({"action": "test"})
    elapsed    = asyncio.get_running_loop().time() - started_at

    assert elapsed < 0.5
    assert result == BroadcastResult(target_count=2, success_count=1, timeout_count=1)
    healthy.send_str.assert_awaited_once()
    assert healthy in server._active_sockets
    assert hung not in server._active_sockets
    await asyncio.gather(*tuple(server._ws_close_tasks))


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_broadcast_uses_bounded_workers_for_many_sockets(
    mock_handler,
    monkeypatch,
):
    server = InboundServer(
        host                         = "127.0.0.1",
        port                         = 8765,
        token                        = "test_token",
        handler                      = mock_handler,
        ws_max_workers               = 3,
        ws_broadcast_timeout_seconds = 1,
    )
    release      = asyncio.Event()
    saturated    = asyncio.Event()
    active_sends = 0
    peak_sends   = 0

    async def slow_send(_text: str) -> None:
        nonlocal active_sends, peak_sends
        active_sends += 1
        peak_sends = max(peak_sends, active_sends)
        if active_sends == 3:
            saturated.set()
        try:
            await release.wait()
        finally:
            active_sends -= 1

    sockets = [MagicMock(send_str=slow_send) for _ in range(40)]
    server._active_sockets.update(sockets)
    real_create_task                      = asyncio.create_task
    worker_tasks: list[asyncio.Task[Any]] = []

    def track_worker(coro):
        task = real_create_task(coro)
        worker_tasks.append(task)
        return task

    monkeypatch.setattr("core.server.asyncio.create_task", track_worker)
    broadcast_task = real_create_task(server.broadcast({"action": "test"}))
    await asyncio.wait_for(saturated.wait(), timeout=1)

    assert peak_sends == 3
    assert len(worker_tasks) == 3

    release.set()
    result = await asyncio.wait_for(broadcast_task, timeout=1)

    assert result == BroadcastResult(target_count=40, success_count=40)
    assert peak_sends == 3


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_broadcast_preserves_caller_cancellation(mock_handler):
    server = InboundServer(
        host                         = "127.0.0.1",
        port                         = 8765,
        token                        = "test_token",
        handler                      = mock_handler,
        ws_broadcast_timeout_seconds = 30,
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send_str(_text: str) -> None:
        entered.set()
        await release.wait()

    socket          = MagicMock()
    socket.send_str = send_str
    server._active_sockets.add(socket)
    task = asyncio.create_task(server.broadcast({"action": "test"}))
    await entered.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert socket in server._active_sockets


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("fatal_kind", ["cancelled", "fatal"])
async def test_server_broadcast_child_fatal_cancels_and_drains_sibling(
    sample_server,
    fatal_kind: str,
):
    class ChildFatal(BaseException):
        pass

    sibling_entered   = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    blocker           = asyncio.Event()

    async def slow_send(_text: str) -> None:
        sibling_entered.set()
        try:
            await blocker.wait()
        finally:
            sibling_cancelled.set()

    async def fatal_send(_text: str) -> None:
        await sibling_entered.wait()
        if fatal_kind == "cancelled":
            raise asyncio.CancelledError
        raise ChildFatal("fatal send")

    slow_socket           = MagicMock()
    slow_socket.send_str  = slow_send
    fatal_socket          = MagicMock()
    fatal_socket.send_str = fatal_send
    sample_server._active_sockets.update({slow_socket, fatal_socket})

    expected = asyncio.CancelledError if fatal_kind == "cancelled" else ChildFatal
    with pytest.raises(expected):
        await sample_server.broadcast({"action": "test"})

    assert sibling_cancelled.is_set()
    assert slow_socket in sample_server._active_sockets
    assert fatal_socket not in sample_server._active_sockets
    await asyncio.gather(*tuple(sample_server._ws_close_tasks))


@pytest.mark.unit
def test_broadcast_result_enforces_accounting_and_aggregates():
    first = BroadcastResult(target_count=2, success_count=1, failure_count=1)
    second = BroadcastResult(target_count=1, timeout_count=1)

    assert first + second == BroadcastResult(
        target_count  = 3,
        success_count = 1,
        failure_count = 1,
        timeout_count = 1,
    )
    with pytest.raises(ValueError, match="sum"):
        BroadcastResult(target_count=2, success_count=1)
    with pytest.raises(ValueError, match="non-negative"):
        BroadcastResult(target_count=True, success_count=1)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inbound_lanes_serialize_same_key_and_are_released(sample_server):
    """Same-key work stays serial and its lane is removed after the final ticket."""
    ws      = AsyncMock()
    entered = asyncio.Event()
    release = asyncio.Event()
    probe   = BlockingConcurrencyProbe(entered, release)

    sample_server.handler = probe.run
    first = asyncio.create_task(sample_server._handle_ws_event(ws, {"user_id": 12345}))
    await entered.wait()
    second = asyncio.create_task(sample_server._handle_ws_event(ws, {"user_id": 12345}))
    await asyncio.sleep(0)

    assert probe.maximum_active == 1
    assert _pending_for_key(sample_server._event_dispatcher, "user:12345") == 2

    release.set()
    await asyncio.gather(first, second)

    assert probe.maximum_active == 1
    assert _lane_count(sample_server._event_dispatcher) == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inbound_lanes_allow_different_keys_in_parallel(sample_server):
    """Independent event keys must not block each other."""
    ws           = AsyncMock()
    both_entered = asyncio.Event()
    release      = asyncio.Event()
    active       = 0
    max_active   = 0

    async def handler(_payload: dict[str, Any]) -> list[dict[str, Any]]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if active == 2:
            both_entered.set()
        await release.wait()
        active -= 1
        return []

    sample_server.handler = handler
    first                 = asyncio.create_task(sample_server._handle_ws_event(ws, {"user_id": 1}))
    second                = asyncio.create_task(sample_server._handle_ws_event(ws, {"user_id": 2}))
    await asyncio.wait_for(both_entered.wait(), timeout=1)

    assert max_active == 2
    release.set()
    await asyncio.gather(first, second)
    assert _lane_count(sample_server._event_dispatcher) == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inbound_lane_ticket_cancellation_releases_capacity(sample_server):
    """Cancelling a same-key ticket must release lane capacity immediately."""
    ws      = AsyncMock()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(_payload: dict[str, Any]) -> list[dict[str, Any]]:
        entered.set()
        await release.wait()
        return []

    sample_server.handler = handler
    first = asyncio.create_task(sample_server._handle_ws_event(ws, {"user_id": 12345}))
    await entered.wait()
    waiting = asyncio.create_task(sample_server._handle_ws_event(ws, {"user_id": 12345}))
    await asyncio.sleep(0)

    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)
    assert _pending_for_key(sample_server._event_dispatcher, "user:12345") == 1

    release.set()
    await first
    assert _lane_count(sample_server._event_dispatcher) == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inbound_lane_pool_does_not_grow_for_high_cardinality_keys(sample_server):
    """Completed high-cardinality events leave no stale lane entries."""
    ws = AsyncMock()
    sample_server.handler = AsyncMock(return_value=[])

    for user_id in range(1_000):
        await sample_server._handle_ws_event(ws, {"user_id": user_id})

    assert _lane_count(sample_server._event_dispatcher) == 0


@pytest.mark.unit
def test_server_set_status_providers(sample_server):
    """Test setting status providers"""
    plugins_count = Mock(return_value=5)
    sessions_count = Mock(return_value=10)
    pending_jobs = Mock(return_value=2)
    metrics = Mock(return_value={"test": "data"})

    sample_server.set_status_providers(
        plugins_count  = plugins_count,
        sessions_count = sessions_count,
        pending_jobs   = pending_jobs,
        metrics        = metrics,
    )

    assert sample_server._get_plugins_count == plugins_count
    assert sample_server._get_sessions_count == sessions_count
    assert sample_server._get_pending_jobs == pending_jobs
    assert sample_server._get_metrics == metrics


@pytest.mark.unit
def test_server_set_status_providers_partial(sample_server):
    """Test setting partial status providers"""
    plugins_count = Mock(return_value=5)

    sample_server.set_status_providers(plugins_count=plugins_count)

    assert sample_server._get_plugins_count == plugins_count
    assert sample_server._get_sessions_count is None


@pytest.mark.unit
def test_server_update_token(sample_server):
    """Test updating token"""
    assert sample_server.token == "test_token"
    assert sample_server._auth_state.generation == 0

    sample_server.update_token("new_token")

    assert sample_server.token == "new_token"
    assert sample_server._auth_state.generation == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_token_rotation_immediately_removes_and_closes_active_sockets(
    sample_server,
):
    first        = MagicMock()
    first.close  = AsyncMock()
    second       = MagicMock()
    second.close = AsyncMock()
    sample_server._active_sockets.update({first, second})

    sample_server.update_token("new_token")

    assert sample_server._active_sockets == set()
    assert sample_server.active_ws_connections() == 0
    await asyncio.sleep(0)
    first.close.assert_awaited_once_with(
        code    = 1008,
        message = b"inbound token rotated",
    )
    second.close.assert_awaited_once_with(
        code    = 1008,
        message = b"inbound token rotated",
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_cross_thread_rotation_revokes_only_old_socket_generation(sample_server):
    loop                      = asyncio.get_running_loop()
    sample_server._event_loop = loop
    old_socket = MagicMock(close=AsyncMock())
    old_state = sample_server._auth_state
    sample_server._active_sockets.add(old_socket)
    sample_server._socket_auth_states[old_socket] = old_state

    worker = threading.Thread(target=sample_server.update_token, args=("new-token",))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()

    # The state swap is already visible although the owning-loop callback is
    # deliberately still queued.
    assert (sample_server.token, sample_server._auth_state.generation) == ("new-token", 1)
    assert old_socket in sample_server._active_sockets
    assert sample_server.active_ws_connections() == 0

    new_socket = MagicMock(close=AsyncMock())
    sample_server._active_sockets.add(new_socket)
    sample_server._socket_auth_states[new_socket] = sample_server._auth_state
    assert sample_server.active_ws_connections() == 1

    await asyncio.sleep(0)
    if sample_server._ws_close_tasks:
        await asyncio.gather(*sample_server._ws_close_tasks)

    assert old_socket not in sample_server._active_sockets
    assert new_socket in sample_server._active_sockets
    old_socket.close.assert_awaited_once_with(
        code    = 1008,
        message = b"inbound token rotated",
    )
    new_socket.close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_broadcast_rechecks_auth_after_cross_thread_rotation_captures_socket(
    sample_server,
):
    """A socket snapshot cannot outlive the token generation that admitted it."""

    loop                      = asyncio.get_running_loop()
    sample_server._event_loop = loop
    old_socket = MagicMock(send_str=AsyncMock(), close=AsyncMock())
    old_state                                     = sample_server._auth_state
    sample_server._socket_auth_states[old_socket] = old_state

    class RotatingSocketSet(set):
        rotated = False

        def __iter__(self):
            for socket in super().__iter__():
                yield socket
                if not self.rotated:
                    self.rotated = True
                    worker       = threading.Thread(
                        target = sample_server.update_token,
                        args   = ("new-token",),
                    )
                    worker.start()
                    worker.join(timeout=2)
                    assert not worker.is_alive()

    sample_server._active_sockets = RotatingSocketSet({old_socket})

    result = await sample_server.broadcast({"action": "send_group_msg", "params": {}})

    assert result == BroadcastResult(target_count=1, failure_count=1)
    old_socket.send_str.assert_not_awaited()
    await asyncio.sleep(0)
    if sample_server._ws_close_tasks:
        await asyncio.gather(*sample_server._ws_close_tasks)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_broadcast_commit_runs_send_factory_without_update_join_deadlock(sample_server):
    """A post-commit rotation cannot deadlock or revoke the admitted send."""

    sample_server._event_loop = asyncio.get_running_loop()
    old_state                 = sample_server._auth_state
    send_body_started         = False
    committed_states          = []

    async def send_body() -> None:
        nonlocal send_body_started
        send_body_started = True

    class RotatingSocket:
        def __init__(self) -> None:
            self.send_called  = False
            self.close_called = False

        def send_str(self, _text: str):
            self.send_called = True
            committed_states.append(sample_server._socket_auth_states[self])
            worker = threading.Thread(
                target = sample_server.update_token,
                args   = ("new-token",),
            )
            worker.start()
            worker.join(timeout=2)
            assert not worker.is_alive()
            return send_body()

        async def close(self, **_kwargs) -> None:
            self.close_called = True

    socket = RotatingSocket()
    sample_server._active_sockets.add(socket)
    sample_server._socket_auth_states[socket] = old_state

    result = await sample_server.broadcast({"action": "send_group_msg", "params": {}})
    if sample_server._ws_close_tasks:
        await asyncio.gather(*sample_server._ws_close_tasks)

    assert result == BroadcastResult(target_count=1, success_count=1)
    assert socket.send_called is True
    assert send_body_started is True
    assert committed_states == [old_state]
    assert socket.close_called is True
    assert sample_server.active_ws_connections() == 0


@pytest.mark.unit
def test_server_rotation_tolerates_loop_closing_after_is_closed_check(sample_server):
    old_state = sample_server._auth_state
    socket    = MagicMock()

    class ClosingLoop:
        @staticmethod
        def is_closed() -> bool:
            return False

        @staticmethod
        def call_soon_threadsafe(*_args) -> None:
            raise RuntimeError("Event loop is closed")

    sample_server._event_loop = ClosingLoop()
    sample_server._active_sockets.add(socket)
    sample_server._socket_auth_states[socket] = old_state

    sample_server.update_token("new-token")

    assert (sample_server.token, sample_server._auth_state.generation) == ("new-token", 1)
    assert sample_server.active_ws_connections() == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_same_token_update_keeps_active_websockets(sample_server):
    ws       = MagicMock()
    ws.close = AsyncMock()
    sample_server._active_sockets.add(ws)

    sample_server.update_token("test_token")
    await asyncio.sleep(0)

    assert sample_server._auth_state.generation == 0
    assert sample_server._active_sockets == {ws}
    ws.close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_token_rotation_during_ws_prepare_rejects_connection(
    sample_server,
):
    prepare_started = asyncio.Event()
    release_prepare = asyncio.Event()

    class FakeWebSocket:
        def __init__(self):
            self.close = AsyncMock()

        async def prepare(self, _request):
            prepare_started.set()
            await release_prepare.wait()

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    ws      = FakeWebSocket()
    request = _make_request_with_auth("GET", "/ws", "test_token")
    with patch("core.server.web.WebSocketResponse", return_value=ws):
        handler_task = asyncio.create_task(sample_server.ws_handler(request))
        await prepare_started.wait()
        sample_server.update_token("new_token")
        release_prepare.set()
        result = await handler_task

    assert result is ws
    assert sample_server._active_sockets == set()
    assert sample_server._get_ws_connections() == 0
    ws.close.assert_awaited_once_with(
        code    = 1008,
        message = b"inbound token rotated",
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_worker_drops_events_from_revoked_generation():
    handler = AsyncMock(return_value=[])
    server = InboundServer(
        host           = "127.0.0.1",
        port           = 8765,
        token          = "old-token",
        handler        = handler,
        enable_http    = False,
        enable_ws      = True,
        ws_max_workers = 1,
    )
    ws    = AsyncMock()
    queue = server._ws_event_queue
    assert queue is not None
    admitted_auth = server._auth_state
    await queue.put((ws, admitted_auth, _onebot_message_payload()))
    server.update_token("new-token")

    server._ensure_ws_workers()
    await asyncio.wait_for(queue.join(), timeout=1)

    handler.assert_not_awaited()
    ws.send_str.assert_not_awaited()
    for task in server._ws_worker_tasks:
        task.cancel()
    await asyncio.gather(*server._ws_worker_tasks, return_exceptions=True)


@pytest.mark.unit
def test_server_ws_connection_counter_helpers(sample_server):
    assert sample_server._get_ws_connections() == 0
    sample_server._increment_ws_connections()
    assert sample_server._get_ws_connections() == 1
    sample_server._decrement_ws_connections()
    assert sample_server._get_ws_connections() == 0


@pytest.mark.unit
def test_inbound_manager_initialization():
    async def handler(event):
        return []

    manager = InboundManager(
        inbound_http_base            = "http://localhost:8080",
        inbound_ws_uri               = "ws://localhost:8080/ws",
        token                        = "test_token",
        handler                      = handler,
        ws_max_workers               = 4,
        ws_queue_size                = 100,
        ws_broadcast_timeout_seconds = 2.5,
    )

    assert manager._inbound_http_base == "http://localhost:8080"
    assert manager._inbound_ws_uri == "ws://localhost:8080/ws"
    assert manager._token == "test_token"
    assert manager._handler == handler
    assert manager._ws_max_workers == 4
    assert manager._ws_queue_size == 100
    assert manager._ws_broadcast_timeout_seconds == 2.5
