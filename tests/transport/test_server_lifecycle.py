"""地址解析、启动和停机。"""

from __future__ import annotations

import tests.helpers.server_test_support as _fixture_support
from tests.helpers.server_test_support import (
    Any,
    AsyncMock,
    ClientSession,
    InboundManager,
    InboundServer,
    MagicMock,
    Mock,
    WSServerHandshakeError,
    _inflight_count,
    _lane_count,
    _make_server,
    _onebot_message_payload,
    _parse_http_base,
    _parse_non_negative_int,
    _parse_positive_int,
    _parse_ws_uri,
    asyncio,
    cancellation_resistant_callback,
    patch,
    pytest,
    web,
)

mock_handler = _fixture_support.mock_handler
sample_server = _fixture_support.sample_server


@pytest.mark.unit
def test_parse_http_base_invalid():
    """Test _parse_http_base with invalid URLs"""
    assert _parse_http_base("") is None
    assert _parse_http_base(None) is None
    assert _parse_http_base("not-a-url") is None
    assert _parse_http_base("ftp://localhost:8080") is None  # Wrong scheme
    assert _parse_http_base("https://localhost:8443") is None  # TLS is unsupported
    assert _parse_http_base("http://localhost") is None  # No port
    assert _parse_http_base("http://localhost:8080/ignored") is None
    assert _parse_http_base("http://user@localhost:8080") is None
    assert _parse_http_base("http://localhost:8080?debug=1") is None
    assert _parse_http_base("http://localhost:invalid") is None


@pytest.mark.unit
def test_parse_ws_uri_valid():
    """Test _parse_ws_uri with valid URIs"""
    assert _parse_ws_uri("ws://localhost:8080") == ("localhost", 8080, "/ws")
    assert _parse_ws_uri("ws://example.com:9000/ws") == ("example.com", 9000, "/ws")
    assert _parse_ws_uri("ws://localhost:8080", default_path="/custom") == (
        "localhost",
        8080,
        "/custom",
    )


@pytest.mark.unit
def test_parse_ws_uri_invalid():
    """Test _parse_ws_uri with invalid URIs"""
    assert _parse_ws_uri("") is None
    assert _parse_ws_uri(None) is None
    assert _parse_ws_uri("not-a-uri") is None
    assert _parse_ws_uri("http://localhost:8080") is None  # Wrong scheme
    assert _parse_ws_uri("wss://localhost:8443/ws") is None  # TLS is unsupported
    assert _parse_ws_uri("ws://user@localhost:8080/ws") is None
    assert _parse_ws_uri("ws://localhost:8080/ws?debug=1") is None
    assert _parse_ws_uri("ws://localhost") is None  # No port


@pytest.mark.unit
def test_parse_non_negative_int():
    """Test _parse_non_negative_int utility"""
    assert _parse_non_negative_int(5, default=10) == 5
    assert _parse_non_negative_int(0, default=10) == 0
    assert _parse_non_negative_int(-5, default=10) == 0  # Negative becomes 0
    assert _parse_non_negative_int("invalid", default=10) == 10
    assert _parse_non_negative_int(None, default=10) == 10


@pytest.mark.unit
def test_parse_positive_int():
    """Test _parse_positive_int utility"""
    assert _parse_positive_int(5, default=10, min_value=1) == 5
    assert _parse_positive_int(1, default=10, min_value=1) == 1
    assert _parse_positive_int(0, default=10, min_value=1) == 1  # Below min
    assert _parse_positive_int(-5, default=10, min_value=1) == 1  # Negative becomes min
    assert _parse_positive_int("invalid", default=10, min_value=1) == 10
    assert _parse_positive_int(None, default=10, min_value=1) == 10


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inbound_server_site_start_failure_cleans_runner(mock_handler):
    server = InboundServer(
        host="127.0.0.1",
        port=8765,
        token="test_token",
        handler=mock_handler,
    )
    runner = MagicMock()
    runner.setup = AsyncMock()
    runner.cleanup = AsyncMock()
    site = MagicMock()
    site.start = AsyncMock(side_effect=OSError("port occupied"))
    site.stop = AsyncMock()

    with (
        patch("core.server.web.AppRunner", return_value=runner),
        patch("core.server.web.TCPSite", return_value=site),
        pytest.raises(OSError, match="port occupied"),
    ):
        await server.start()

    runner.setup.assert_awaited_once()
    site.stop.assert_awaited_once()
    runner.cleanup.assert_awaited_once()
    assert server._site is None
    assert server._runner is None
    assert server._running is False
    assert server._accepting_events is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inbound_server_stop_waits_for_concurrent_start(mock_handler):
    server = InboundServer(
        host="127.0.0.1",
        port=8765,
        token="test_token",
        handler=mock_handler,
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_setup() -> None:
        entered.set()
        await release.wait()

    runner = MagicMock(setup=AsyncMock(side_effect=blocked_setup), cleanup=AsyncMock())
    site = MagicMock(start=AsyncMock(), stop=AsyncMock())

    with (
        patch("core.server.web.AppRunner", return_value=runner),
        patch("core.server.web.TCPSite", return_value=site),
    ):
        start_task = asyncio.create_task(server.start())
        await entered.wait()
        stop_task = asyncio.create_task(server.stop())
        await asyncio.sleep(0)
        assert not stop_task.done()
        release.set()
        await asyncio.gather(start_task, stop_task)

    site.start.assert_awaited_once()
    site.stop.assert_awaited_once()
    runner.cleanup.assert_awaited_once()
    assert server._running is False
    assert server._site is None
    assert server._runner is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inbound_server_concurrent_starts_bind_once(mock_handler):
    server = InboundServer(
        host="127.0.0.1",
        port=8765,
        token="test_token",
        handler=mock_handler,
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_setup() -> None:
        entered.set()
        await release.wait()

    runner = MagicMock(setup=AsyncMock(side_effect=blocked_setup), cleanup=AsyncMock())
    site = MagicMock(start=AsyncMock(), stop=AsyncMock())

    with (
        patch("core.server.web.AppRunner", return_value=runner) as runner_cls,
        patch("core.server.web.TCPSite", return_value=site) as site_cls,
    ):
        first = asyncio.create_task(server.start())
        await entered.wait()
        second = asyncio.create_task(server.start())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        await server.stop()

    runner_cls.assert_called_once()
    site_cls.assert_called_once()
    site.start.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inbound_manager_stop_waits_for_concurrent_start():
    manager = InboundManager(
        inbound_http_base="http://127.0.0.1:18080",
        inbound_ws_uri="",
        token="test_token",
        handler=AsyncMock(return_value=[]),
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_start(*, accept_events: bool = True) -> None:
        assert accept_events is False
        entered.set()
        await release.wait()

    server = MagicMock(
        start=AsyncMock(side_effect=blocked_start),
        stop=AsyncMock(),
        set_status_providers=Mock(),
        commit_admission=Mock(),
    )

    with patch("core.server.InboundServer", return_value=server):
        start_task = asyncio.create_task(manager.start())
        await entered.wait()
        stop_task = asyncio.create_task(manager.stop())
        await asyncio.sleep(0)
        assert not stop_task.done()
        release.set()
        await asyncio.gather(start_task, stop_task)

    server.stop.assert_awaited_once()
    assert manager._running is False
    assert manager.http_server is None
    assert manager.ws_server is None


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("task_kind", ["handler", "close", "worker"])
async def test_inbound_server_stop_bounds_cancellation_resistant_tasks(
    mock_handler,
    task_kind: str,
):
    server = InboundServer(
        host="127.0.0.1",
        port=8765,
        token="test_token",
        handler=mock_handler,
    )
    server._handler_drain_timeout_seconds = 0.01
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    resist_cancellation = cancellation_resistant_callback(entered, cancelled, release)

    if task_kind == "handler":

        async def resistant_handler(_payload: dict[str, Any]) -> list[dict[str, Any]]:
            await resist_cancellation()
            return []

        server.handler = resistant_handler
        task = asyncio.create_task(server._invoke_handler({}))
    else:
        task = asyncio.create_task(resist_cancellation())
        if task_kind == "close":
            server._ws_close_tasks.add(task)
        else:
            server._ws_worker_tasks = [task]

    await entered.wait()
    try:
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await asyncio.wait_for(server.stop(), timeout=0.5)
        assert cancelled.is_set()
        if task_kind == "handler":
            assert task.cancelled()
            assert _inflight_count(server._event_dispatcher) == 1
        elif task_kind == "close":
            assert not task.done()
            assert task in server._ws_close_tasks
        else:
            assert not task.done()
            assert task in server._ws_worker_tasks
        with pytest.raises(RuntimeError, match="incomplete previous start"):
            await server.start()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1)
        for _ in range(20):
            if _inflight_count(server._event_dispatcher) == 0:
                break
            await asyncio.sleep(0)
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_scheduled_websocket_close_isolates_fatal(mock_handler):
    class CloseFatal(BaseException):
        pass

    server = InboundServer(
        host="127.0.0.1",
        port=8765,
        token="old-token",
        handler=mock_handler,
    )
    socket = MagicMock(close=AsyncMock(side_effect=CloseFatal("close fatal")))
    server._active_sockets.add(socket)

    server.update_token("new-token")
    tasks = tuple(server._ws_close_tasks)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)

    assert tasks
    assert all(isinstance(result, Exception) for result in results)
    assert server._ws_close_tasks == set()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inbound_manager_second_listener_failure_stops_first():
    manager = InboundManager(
        inbound_http_base="http://127.0.0.1:18080",
        inbound_ws_uri="ws://127.0.0.1:18081/ws",
        token="test_token",
        handler=AsyncMock(return_value=[]),
    )
    first = MagicMock()
    first.start = AsyncMock()
    first.stop = AsyncMock()
    second = MagicMock()
    second.start = AsyncMock(side_effect=OSError("second bind failed"))
    second.stop = AsyncMock()

    with (
        patch("core.server.InboundServer", side_effect=[first, second]),
        pytest.raises(OSError, match="second bind failed"),
    ):
        await manager.start()

    first.start.assert_awaited_once()
    first.stop.assert_awaited_once()
    second.stop.assert_awaited_once()
    first.start.assert_awaited_once_with(accept_events=False)
    second.start.assert_awaited_once_with(accept_events=False)
    first.commit_admission.assert_not_called()
    assert manager.http_server is None
    assert manager.ws_server is None
    assert manager._running is False


@pytest.mark.unit
def test_inbound_manager_binding_ports_deduplicates_shared_port():
    shared = InboundManager(
        inbound_http_base="http://127.0.0.1:18080",
        inbound_ws_uri="ws://127.0.0.1:18080/ws",
        token="test_token",
        handler=AsyncMock(return_value=[]),
    )
    separate = InboundManager(
        inbound_http_base="http://127.0.0.1:18080",
        inbound_ws_uri="ws://127.0.0.1:18081/ws",
        token="test_token",
        handler=AsyncMock(return_value=[]),
    )

    assert shared.binding_ports == frozenset({18080})
    assert separate.binding_ports == frozenset({18080, 18081})


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_start_stop(mock_handler, unused_tcp_port):
    """Test server start and stop"""
    server = _make_server(mock_handler, unused_tcp_port)
    await server.start()

    assert server._runner is not None
    assert server._site is not None

    await server.stop()

    assert server._runner is None
    assert server._site is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_token_rotation_revokes_old_ws_and_accepts_new_token(
    unused_tcp_port,
):
    seen_message_ids: list[int] = []

    async def handler(event):
        seen_message_ids.append(event["message_id"])
        return [{"action": "ack", "params": {"message_id": event["message_id"]}}]

    server = InboundServer(
        host="127.0.0.1",
        port=unused_tcp_port,
        token="old-token",
        handler=handler,
        enable_http=False,
        enable_ws=True,
        ws_path="/ws",
        ws_max_workers=1,
        ws_queue_size=4,
    )
    await server.start()
    url = f"http://127.0.0.1:{unused_tcp_port}/ws"
    try:
        async with ClientSession() as session:
            old_ws = await session.ws_connect(
                url,
                headers={"Authorization": "Bearer old-token"},
            )
            server.update_token("new-token")
            old_close = await old_ws.receive(timeout=2)
            assert old_close.type in {
                web.WSMsgType.CLOSE,
                web.WSMsgType.CLOSED,
                web.WSMsgType.CLOSING,
            }

            with pytest.raises(WSServerHandshakeError) as exc_info:
                await session.ws_connect(
                    url,
                    headers={"Authorization": "Bearer old-token"},
                )
            assert exc_info.value.status == 401

            new_ws = await session.ws_connect(
                url,
                headers={"Authorization": "Bearer new-token"},
            )
            payload = _onebot_message_payload("/help")
            payload["message_id"] = 902
            await new_ws.send_json(payload)
            response = await new_ws.receive_json(timeout=2)
            assert response["action"] == "ack"
            assert response["params"]["message_id"] == 902
            await new_ws.close()
    finally:
        await server.stop()

    assert seen_message_ids == [902]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_server_stop_with_workers(mock_handler, unused_tcp_port):
    """Test server stop cleans up worker tasks"""
    server = _make_server(mock_handler, unused_tcp_port)

    # Start server
    await server.start()

    # Create mock worker tasks
    mock_task1 = asyncio.create_task(asyncio.sleep(10))
    mock_task2 = asyncio.create_task(asyncio.sleep(10))
    server._ws_worker_tasks = [mock_task1, mock_task2]

    await server.stop()

    # Tasks should be cancelled
    assert mock_task1.cancelled()
    assert mock_task2.cancelled()
    assert _lane_count(server._event_dispatcher) == 0


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("listener_layout", ["shared-port", "separate-ports"])
async def test_manager_http_ws_listeners_share_fifo_and_parallel_lanes(
    unused_tcp_port_factory,
    listener_layout: str,
):
    """Combined and split listeners linearize into one manager dispatcher."""

    http_port = unused_tcp_port_factory()
    ws_port = http_port if listener_layout == "shared-port" else unused_tcp_port_factory()
    same_entered = asyncio.Event()
    same_release = asyncio.Event()
    ws_first_entered = asyncio.Event()
    ws_first_release = asyncio.Event()
    other_entered = asyncio.Event()
    other_release = asyncio.Event()
    seen: list[str] = []

    async def handler(event: dict[str, Any]) -> list[dict[str, Any]]:
        marker = str(event["marker"])
        seen.append(marker)
        if marker == "http-same":
            same_entered.set()
            await same_release.wait()
        elif marker == "ws-first":
            ws_first_entered.set()
            await ws_first_release.wait()
        elif marker == "http-other":
            other_entered.set()
            await other_release.wait()
        return [{"marker": marker}]

    manager = InboundManager(
        inbound_http_base=f"http://127.0.0.1:{http_port}",
        inbound_ws_uri=f"ws://127.0.0.1:{ws_port}/ws",
        token="shared-token",
        handler=handler,
        ws_max_workers=2,
        ws_queue_size=8,
    )
    await manager.start()
    assert manager.http_server is not None
    assert manager.ws_server is not None
    if listener_layout == "shared-port":
        assert manager.http_server is manager.ws_server
    else:
        assert manager.http_server is not manager.ws_server
    assert manager.http_server._event_dispatcher is manager.ws_server._event_dispatcher
    assert manager._event_dispatcher is manager.http_server._event_dispatcher

    headers = {"Authorization": "Bearer shared-token"}

    def payload(marker: str, user_id: int) -> dict[str, Any]:
        event = _onebot_message_payload(marker)
        event.update(marker=marker, user_id=user_id)
        return event

    async def post(session: ClientSession, event: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        async with session.post(
            f"http://127.0.0.1:{http_port}/event",
            headers=headers,
            json=event,
        ) as response:
            return response.status, await response.json()

    try:
        async with ClientSession() as session:
            ws = await session.ws_connect(
                f"ws://127.0.0.1:{ws_port}/ws",
                headers=headers,
            )
            try:
                http_same = asyncio.create_task(post(session, payload("http-same", 1001)))
                await asyncio.wait_for(same_entered.wait(), timeout=1)
                await ws.send_json(payload("ws-same", 1001))
                await asyncio.sleep(0.02)
                assert seen == ["http-same"]

                same_release.set()
                status, body = await asyncio.wait_for(http_same, timeout=1)
                assert status == 200
                assert body["actions"] == [{"marker": "http-same"}]
                assert await ws.receive_json(timeout=1) == {"marker": "ws-same"}
                assert seen[:2] == ["http-same", "ws-same"]

                await ws.send_json(payload("ws-first", 1501))
                await asyncio.wait_for(ws_first_entered.wait(), timeout=1)
                before_reverse = len(seen)
                http_after_ws = asyncio.create_task(post(session, payload("http-after-ws", 1501)))
                await asyncio.sleep(0.02)
                assert seen[before_reverse:] == []
                ws_first_release.set()
                assert await ws.receive_json(timeout=1) == {"marker": "ws-first"}
                reverse_status, reverse_body = await asyncio.wait_for(http_after_ws, timeout=1)
                assert reverse_status == 200
                assert reverse_body["actions"] == [{"marker": "http-after-ws"}]
                assert seen[before_reverse - 1 : before_reverse + 1] == [
                    "ws-first",
                    "http-after-ws",
                ]

                http_other = asyncio.create_task(post(session, payload("http-other", 2001)))
                await asyncio.wait_for(other_entered.wait(), timeout=1)
                await ws.send_json(payload("ws-independent", 2002))
                assert await ws.receive_json(timeout=1) == {"marker": "ws-independent"}
                assert not http_other.done()
                other_release.set()
                assert (await asyncio.wait_for(http_other, timeout=1))[0] == 200
            finally:
                await ws.close()
    finally:
        same_release.set()
        ws_first_release.set()
        other_release.set()
        await manager.stop()

    assert manager._event_dispatcher is None
    rebound = [
        await asyncio.start_server(lambda _reader, writer: writer.close(), "127.0.0.1", port)
        for port in {http_port, ws_port}
    ]
    for listener in rebound:
        listener.close()
        await listener.wait_closed()
