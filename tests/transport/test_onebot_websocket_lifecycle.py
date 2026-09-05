"""OneBot WebSocket 停止、轮换和重连退避生命周期。"""

from __future__ import annotations

import tests.helpers.onebot_test_support as _fixture_support
from tests.helpers.onebot_test_support import (
    AsyncMock,
    MagicMock,
    OneBotWsClient,
    _ConnectionAttemptResult,
    _jittered_reconnect_delay,
    _UnsupportedWebSocketAuthentication,
    asyncio,
    cancellation_resistant_callback,
    patch,
    pytest,
    threading,
)

bounded_transport_adapter = _fixture_support.bounded_transport_adapter


class TestOneBotWebSocketLifecycle:
    """按单一传输职责组织的 OneBot WebSocket 测试。"""

    @pytest.mark.asyncio
    async def test_stop(self):
        """测试停止客户端"""
        client        = OneBotWsClient("ws://localhost:3000", "")
        mock_ws       = AsyncMock()
        mock_ws.close = AsyncMock()
        client._ws    = mock_ws

        await client.stop()

        assert client._running is False
        mock_ws.close.assert_awaited_once()
        assert client._ws is None

    @pytest.mark.asyncio
    async def test_stop_awaits_cleanup_task_cancellation(self):
        """测试 stop 会等待 cleanup task 完成取消清理"""
        client           = OneBotWsClient("ws://localhost:3000", "")
        cleanup_finished = asyncio.Event()

        async def cleanup_loop():
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_finished.set()

        client._cleanup_task = asyncio.create_task(cleanup_loop())
        await asyncio.sleep(0)

        await client.stop()

        assert cleanup_finished.is_set()
        assert client._cleanup_task is None

    @pytest.mark.asyncio
    async def test_stop_cancels_all_keyed_drainers_and_clears_queues(self):
        client  = OneBotWsClient("ws://localhost:3000", "")
        entered = asyncio.Event()

        async def handler(_event):
            entered.set()
            await asyncio.Event().wait()

        await client._dispatch_event(handler, {"user_id": 1})
        await client._dispatch_event(handler, {"user_id": 2})
        await entered.wait()
        tasks = list(client._queue_tasks.values())

        await client.stop()

        assert tasks and all(task.cancelled() for task in tasks)
        assert client._queue_tasks == {}
        assert client._message_queues == {}
        assert client._queue_last_activity == {}
        await client._dispatch_event(handler, {"user_id": 3})
        assert client._queue_tasks == {}

    @pytest.mark.asyncio
    async def test_stop_bounds_and_retains_cancellation_resistant_queue_worker(self):
        client                           = OneBotWsClient("ws://localhost:3000", "")
        client._shutdown_timeout_seconds = 0.01
        entered                          = asyncio.Event()
        cancelled                        = asyncio.Event()
        release                          = asyncio.Event()

        resistant_worker = cancellation_resistant_callback(entered, cancelled, release)

        task                             = asyncio.create_task(resistant_worker())
        client._queue_tasks["user:1"]    = task
        client._message_queues["user:1"] = asyncio.Queue()
        await entered.wait()

        with pytest.raises(RuntimeError, match="ignored cancellation"):
            await asyncio.wait_for(client.stop(), timeout=0.5)

        assert cancelled.is_set()
        assert client._queue_tasks["user:1"] is task
        assert not task.done()

        release.set()
        await asyncio.wait_for(task, timeout=1)
        await client.stop()

        assert client._queue_tasks == {}
        assert client._message_queues == {}

    @pytest.mark.asyncio
    async def test_stop_isolates_websocket_close_fatal_and_allows_retry(self):
        class CloseFatal(BaseException):
            pass

        client = OneBotWsClient("ws://localhost:3000", "")
        socket = AsyncMock()
        socket.close = AsyncMock(side_effect=[CloseFatal("fatal close"), None])
        client._ws = socket

        with pytest.raises(RuntimeError, match="WebSocket close"):
            await client.stop()

        assert client._ws is socket
        await client.stop()
        assert client._ws is None
        assert socket.close.await_count == 2

    @pytest.mark.asyncio
    async def test_overlapping_rotations_close_each_exact_socket_independently(self):
        client             = OneBotWsClient("ws://old.example/ws", "old-token")
        client._event_loop = asyncio.get_running_loop()
        prior_started      = asyncio.Event()
        prior_release      = asyncio.Event()

        class PriorSocket:
            async def close(self) -> None:
                prior_started.set()
                await prior_release.wait()

        prior_socket = PriorSocket()
        prior_task   = client._schedule_ws_close(prior_socket)
        await prior_started.wait()

        current_socket = MagicMock(close=AsyncMock())
        client._ws                        = current_socket
        client._connected_auth_generation = client._endpoint_auth.generation

        client.update("ws://new.example/ws", "new-token")
        for _ in range(20):
            if current_socket.close.await_count:
                break
            await asyncio.sleep(0)

        current_socket.close.assert_awaited_once()
        assert not prior_task.done()
        assert any(owned_ws is prior_socket for owned_ws, _ in client._ws_close_tasks.values())

        prior_release.set()
        await prior_task
        await asyncio.sleep(0)
        await client.stop()

        assert client._ws_close_tasks == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("close_mode", ["raises", "hangs"])
    async def test_rotation_progresses_when_revoked_socket_close_cannot_converge(
        self,
        monkeypatch,
        close_mode: str,
    ):
        client           = OneBotWsClient("ws://old.example/ws", "old-token")
        first_entered    = asyncio.Event()
        first_cancelled  = asyncio.Event()
        first_release    = asyncio.Event()
        close_entered    = asyncio.Event()
        close_release    = asyncio.Event()
        second_attempted = asyncio.Event()
        attempts         = 0

        class RevokedSocket:
            def __init__(self) -> None:
                self.close_calls = 0

            async def close(self) -> None:
                self.close_calls += 1
                close_entered.set()
                if close_mode == "raises" and self.close_calls == 1:
                    raise RuntimeError("close failed")
                if close_mode == "hangs":
                    await close_release.wait()

        socket = RevokedSocket()

        async def connection_attempt(_handler):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                client._ws                        = socket
                client._connected_auth_generation = client._endpoint_auth.generation
                first_entered.set()
                while not first_release.is_set():
                    try:
                        await first_release.wait()
                    except asyncio.CancelledError:
                        first_cancelled.set()
                return _ConnectionAttemptResult(0.0)
            second_attempted.set()
            client._running = False
            return _ConnectionAttemptResult(0.0)

        monkeypatch.setattr(client, "_connect_once", connection_attempt)
        listener = asyncio.create_task(client.connect_and_listen(AsyncMock()))
        try:
            await first_entered.wait()

            client.update("ws://new.example/ws", "new-token")
            await asyncio.wait_for(close_entered.wait(), timeout=1)
            await asyncio.wait_for(second_attempted.wait(), timeout=1)
            for _ in range(20):
                if len(client._connection_attempt_tasks) == 1:
                    break
                await asyncio.sleep(0)

            assert first_cancelled.is_set()
            assert attempts == 2
            assert client._endpoint_auth.generation == 1
            assert len(client._connection_attempt_tasks) == 1
            assert len(client._quarantined_connection_attempt_tasks) == 1

            first_release.set()
            close_release.set()
            await asyncio.wait_for(listener, timeout=1)
            await client.stop()

            assert client._connection_attempt_tasks == set()
            assert client._quarantined_connection_attempt_tasks == set()
            assert client._ws_close_tasks == {}
        finally:
            first_release.set()
            close_release.set()
            if not listener.done():
                listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
            try:
                await client.stop()
            except RuntimeError:
                pass

    @pytest.mark.asyncio
    async def test_stop_cancels_and_joins_its_own_listen_task(self, monkeypatch):
        client  = OneBotWsClient("ws://localhost:3000", "")
        entered = asyncio.Event()

        async def block_connect(_handler):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(client, "_connect_once", block_connect)
        listen_task = asyncio.create_task(client.connect_and_listen(AsyncMock()))
        await entered.wait()

        await client.stop()

        assert listen_task.cancelled()
        assert client._main_task is None

    @pytest.mark.asyncio
    async def test_connect_once_reraises_connection_error_for_backoff(self):
        """测试 _connect_once 会重新抛出连接异常，让外层退避逻辑生效。"""
        client = OneBotWsClient("ws://localhost:3000", "token")

        class DummyWebsockets:
            def connect(self, *args, **kwargs):
                raise RuntimeError("boom")

        with (
            patch.dict("sys.modules", {"websockets": DummyWebsockets()}),
            patch(
                "core.onebot._get_connect_signature",
                return_value={"additional_headers"},
            ),
        ):
            with pytest.raises(RuntimeError, match="boom"):
                await client._connect_once(AsyncMock())

    @pytest.mark.asyncio
    async def test_fatal_listener_exit_cancels_its_cleanup_and_can_restart(self, monkeypatch):
        client = OneBotWsClient("ws://localhost:3000", "")
        calls  = 0

        async def fail_after_rotation(_handler):
            nonlocal calls
            calls += 1
            if calls == 1:
                client.update(client.ws_uri, "new-token")
                return 0.0
            raise _UnsupportedWebSocketAuthentication("opaque connector")

        monkeypatch.setattr(client, "_connect_once", fail_after_rotation)
        with pytest.raises(RuntimeError, match="opaque connector"):
            await client.connect_and_listen(AsyncMock())

        assert client._cleanup_task is None
        assert client._main_task is None

        async def finish_once(_handler):
            client._running = False
            return 0.0

        client.update(client.ws_uri, "", credentials_trusted=True)
        monkeypatch.setattr(client, "_connect_once", finish_once)
        await client.connect_and_listen(AsyncMock())
        await client.stop()

        assert client._cleanup_task is None

    @pytest.mark.asyncio
    async def test_repeated_listener_cancellation_cannot_skip_lifecycle_cleanup(
        self,
        monkeypatch,
    ):
        client                = OneBotWsClient("ws://localhost:3000", "")
        attempt_entered       = asyncio.Event()
        second_attempt_cancel = asyncio.Event()
        release_attempt       = asyncio.Event()
        cancel_count          = 0

        async def cancellation_resistant_attempt(_handler):
            nonlocal cancel_count
            attempt_entered.set()
            while not release_attempt.is_set():
                try:
                    await release_attempt.wait()
                except asyncio.CancelledError:
                    cancel_count += 1
                    if cancel_count >= 2:
                        second_attempt_cancel.set()
            return _ConnectionAttemptResult(0.0)

        monkeypatch.setattr(client, "_connect_once", cancellation_resistant_attempt)
        listener = asyncio.create_task(client.connect_and_listen(AsyncMock()))
        await attempt_entered.wait()

        listener.cancel()
        await asyncio.wait_for(second_attempt_cancel.wait(), timeout=1)
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)

        assert listener.cancelled()
        assert client._cleanup_task is None
        assert client._main_task is None
        assert client._event_loop is None
        assert client._reconnect_wakeup is None

        release_attempt.set()
        for _ in range(20):
            if not client._connection_attempt_tasks:
                break
            await asyncio.sleep(0)
        await client.stop()

        assert client._connection_attempt_tasks == set()
        assert client._quarantined_connection_attempt_tasks == set()
        assert client._cleanup_task is None
        assert client._main_task is None
        assert client._event_loop is None
        assert client._reconnect_wakeup is None

    @pytest.mark.asyncio
    async def test_normal_and_exception_disconnects_share_bounded_exponential_backoff(
        self,
        monkeypatch,
    ):
        client   = OneBotWsClient("ws://localhost:3000", "")
        attempts = iter((0.01, RuntimeError("short failure"), 0.02, 0.03, 0.04, 0.05))

        async def connect_once(_handler):
            outcome = next(attempts)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        delays: list[float] = []

        async def record_sleep(delay: float) -> None:
            delays.append(delay)
            if len(delays) == 6:
                client._running = False

        monkeypatch.setattr(client, "_connect_once", connect_once)
        client._reconnect_sleep  = record_sleep
        client._reconnect_random = lambda: 0.5

        await client.connect_and_listen(AsyncMock())
        await client.stop()

        assert delays == [5.0, 10.0, 20.0, 40.0, 54.0, 54.0]

    @pytest.mark.asyncio
    async def test_stable_connection_resets_reconnect_backoff(self, monkeypatch):
        client              = OneBotWsClient("ws://localhost:3000", "")
        durations           = iter((0.01, 0.02, 31.0, 0.03))
        delays: list[float] = []

        async def connect_once(_handler):
            return next(durations)

        async def record_sleep(delay: float) -> None:
            delays.append(delay)
            if len(delays) == 4:
                client._running = False

        monkeypatch.setattr(client, "_connect_once", connect_once)
        client._reconnect_sleep  = record_sleep
        client._reconnect_random = lambda: 0.5

        await client.connect_and_listen(AsyncMock())
        await client.stop()

        assert delays == [5.0, 10.0, 5.0, 10.0]

    @pytest.mark.asyncio
    async def test_cross_thread_auth_rotation_wakes_current_backoff(self, monkeypatch):
        client            = OneBotWsClient("ws://old.example/ws", "")
        backoff_entered   = asyncio.Event()
        backoff_cancelled = asyncio.Event()

        async def blocked_sleep(_delay: float) -> None:
            backoff_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                backoff_cancelled.set()

        connect_once = AsyncMock(return_value=0.0)
        monkeypatch.setattr(client, "_connect_once", connect_once)
        client._reconnect_sleep  = blocked_sleep
        client._reconnect_random = lambda: 0.5

        listener = asyncio.create_task(client.connect_and_listen(AsyncMock()))
        await asyncio.wait_for(backoff_entered.wait(), timeout=1)

        worker = threading.Thread(
            target = client.update,
            args   = ("ws://new.example/ws", "new-token"),
        )
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()

        for _ in range(20):
            if connect_once.await_count >= 2:
                break
            await asyncio.sleep(0)

        assert connect_once.await_count >= 2
        assert backoff_cancelled.is_set()
        assert (client.ws_uri, client.auth_token) == (
            "ws://new.example/ws",
            "new-token",
        )

        await client.stop()
        assert listener.cancelled()

    @pytest.mark.asyncio
    async def test_auth_rotation_between_attempt_and_wait_is_not_lost(self, monkeypatch):
        client              = OneBotWsClient("ws://old.example/ws", "")
        delays: list[float] = []
        calls               = 0

        async def connect_once(_handler):
            nonlocal calls
            calls += 1
            if calls == 1:
                client.update("ws://new.example/ws", "new-token")
            else:
                client._running = False
            return 0.0

        async def record_sleep(delay: float) -> None:
            delays.append(delay)

        monkeypatch.setattr(client, "_connect_once", connect_once)
        client._reconnect_sleep = record_sleep

        await client.connect_and_listen(AsyncMock())
        await client.stop()

        assert calls == 2
        assert delays == []
        assert (client.ws_uri, client.auth_token) == (
            "ws://new.example/ws",
            "new-token",
        )

    @pytest.mark.asyncio
    async def test_reconnect_backoff_applies_jitter_without_exceeding_cap(self, monkeypatch):
        client              = OneBotWsClient("ws://localhost:3000", "")
        samples             = iter((0.0, 1.0, 1.0, 1.0, 1.0))
        delays: list[float] = []

        async def record_sleep(delay: float) -> None:
            delays.append(delay)
            if len(delays) == 5:
                client._running = False

        monkeypatch.setattr(client, "_connect_once", AsyncMock(return_value=0.0))
        client._reconnect_sleep  = record_sleep
        client._reconnect_random = lambda: next(samples)

        await client.connect_and_listen(AsyncMock())
        await client.stop()

        assert delays == [4.0, 12.0, 24.0, 48.0, 60.0]

    def test_max_backoff_jitter_remains_continuous_instead_of_collapsing_at_cap(self):
        samples = (0.0, 0.25, 0.5, 0.75, 1.0)
        values  = [_jittered_reconnect_delay(60.0, sample) for sample in samples]

        assert values == sorted(values)
        assert len(set(values)) == len(values)
        assert values[0] == 48.0
        assert values[-1] == 60.0
        for base in (-10.0, 0.0, 5.0, 60.0, 100.0):
            for sample in samples:
                assert 0.0 <= _jittered_reconnect_delay(base, sample) <= 60.0

    @pytest.mark.asyncio
    async def test_listen_preserves_abnormal_close_reason_but_not_normal_close(self):
        from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
        from websockets.frames import Close

        class EndingSocket:
            def __init__(self, error: Exception | None) -> None:
                self.error = error

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.error is None:
                    raise StopAsyncIteration
                raise self.error

        normal   = ConnectionClosedOK(Close(1000, "normal"), Close(1000, "normal"), True)
        abnormal = ConnectionClosedError(Close(1008, "policy"), None, None)
        ordinary = RuntimeError("iterator failed")

        normal_result = await OneBotWsClient("ws://x", "")._listen(
            EndingSocket(normal), AsyncMock()
        )
        abnormal_result = await OneBotWsClient("ws://x", "")._listen(
            EndingSocket(abnormal), AsyncMock()
        )
        ordinary_result = await OneBotWsClient("ws://x", "")._listen(
            EndingSocket(ordinary), AsyncMock()
        )
        eof_result = await OneBotWsClient("ws://x", "")._listen(EndingSocket(None), AsyncMock())

        assert isinstance(normal_result, _ConnectionAttemptResult)
        assert normal_result.error is None
        assert abnormal_result.error is abnormal
        assert ordinary_result.error is ordinary
        assert eof_result.error is None
