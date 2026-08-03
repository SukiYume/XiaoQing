"""OneBot WebSocket 客户端。"""

from __future__ import annotations

import tests.helpers.onebot_test_support as _fixture_support
from tests.helpers.onebot_test_support import (
    _CONNECT_SIGNATURE_CACHE,
    Any,
    AsyncMock,
    MagicMock,
    OneBotActionOutcomeUnknown,
    OneBotWsClient,
    _ConnectionAttemptResult,
    _drain_owned_ws_closes,
    _get_connect_signature,
    _jittered_reconnect_delay,
    _normalize_action_for_onebot,
    _QueuedOneBotEvent,
    _UnsupportedWebSocketAuthentication,
    _wait_for_ws_action_request,
    asyncio,
    cancellation_resistant_callback,
    inspect,
    json,
    patch,
    pytest,
    threading,
)

bounded_transport_adapter = _fixture_support.bounded_transport_adapter


@pytest.mark.asyncio
async def test_connected_log_redacts_websocket_query_and_fragment(caplog):
    client = OneBotWsClient(
        "ws://example.test/socket?access_token=super-secret#private-fragment",
        "",
    )

    class EmptySocket:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def close(self):
            return None

    with caplog.at_level("INFO"):
        await client._listen(EmptySocket(), AsyncMock())

    assert "ws://example.test/socket?<redacted>#<redacted>" in caplog.text
    assert "super-secret" not in caplog.text
    assert "private-fragment" not in caplog.text


class TestOneBotWsClient:
    """OneBotWsClient 测试"""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"max_pending_events": 0},
            {"max_pending_events": True},
            {"queue_size": -1},
            {"queue_size": 1.5},
            {"queue_ttl_seconds": float("nan")},
            {"queue_cleanup_interval": 0},
            {"action_response_timeout_seconds": float("inf")},
            {"credentials_trusted": 1},
        ],
    )
    def test_initialization_rejects_invalid_runtime_limits(self, kwargs):
        with pytest.raises((TypeError, ValueError)):
            OneBotWsClient("ws://localhost:3000", "", **kwargs)

    def test_initialization(self):
        """测试初始化"""
        client = OneBotWsClient(
            ws_uri="ws://localhost:3000",
            auth_token="test_token",
        )
        assert client.ws_uri == "ws://localhost:3000"
        assert client.auth_token == "test_token"
        assert client.connected() is False

    def test_set_on_connect(self):
        """测试设置连接回调"""
        client = OneBotWsClient("ws://localhost:3000", "")

        async def callback():
            pass

        client.set_on_connect(callback)
        assert client._on_connect is callback

    def test_update(self):
        """测试更新配置"""
        client = OneBotWsClient("ws://old:3000", "old_token")
        client.update("ws://new:4000", "new_token")
        assert client.ws_uri == "ws://new:4000"
        assert client.auth_token == "new_token"

    @pytest.mark.asyncio
    async def test_update_immediately_invalidates_the_connected_auth_generation(self):
        client = OneBotWsClient("ws://old:3000", "old_token")
        ws = MagicMock(close=AsyncMock())
        client._ws = ws
        client._event_loop = asyncio.get_running_loop()
        client._connected_auth_generation = client._endpoint_auth.generation
        pending = asyncio.get_running_loop().create_future()
        client._pending_action_futures["pending"] = pending

        client.update("ws://new:4000", "new_token")

        assert (client.ws_uri, client.auth_token) == ("ws://new:4000", "new_token")
        assert client.connected() is False
        with pytest.raises(ConnectionError, match="authorization changed"):
            await pending
        assert len(client._ws_close_tasks) == 1
        await _drain_owned_ws_closes(client)
        ws.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_revoked_connection_event_is_dropped_by_the_final_queue_consumer(self):
        """A frame parsed during cross-thread rotation cannot reach a plugin."""

        client = OneBotWsClient("ws://old:3000", "old-token")
        client._event_loop = asyncio.get_running_loop()
        old_state = client._endpoint_auth
        handler = AsyncMock()

        worker = threading.Thread(
            target=client.update,
            args=("ws://new:4000", "new-token"),
        )
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()

        # The owning-loop invalidation callback is still queued here.  This is
        # the same window in which _listen can finish parsing an old frame.
        await client._dispatch_event(
            handler,
            {"post_type": "message", "user_id": 1},
            auth_state=old_state,
        )
        await asyncio.sleep(0)

        handler.assert_not_awaited()
        await client.stop()

    @pytest.mark.asyncio
    async def test_revoked_connection_cannot_resolve_an_old_action_response(self):
        client = OneBotWsClient("ws://old:3000", "old-token")
        client._event_loop = asyncio.get_running_loop()
        old_state = client._endpoint_auth
        future = asyncio.get_running_loop().create_future()
        client._pending_action_futures["old-echo"] = future
        client._pending_action_auth_states["old-echo"] = old_state

        worker = threading.Thread(
            target=client.update,
            args=("ws://new:4000", "new-token"),
        )
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()

        assert client._resolve_action_response(
            {"echo": "old-echo", "status": "ok", "retcode": 0},
            auth_state=old_state,
        )
        assert not future.done()
        await asyncio.sleep(0)

        with pytest.raises(ConnectionError, match="authorization changed"):
            await future

    @pytest.mark.asyncio
    async def test_handler_commit_runs_external_factory_without_update_join_deadlock(self):
        """A post-commit rotation cannot deadlock or revoke the admitted handler."""

        client = OneBotWsClient("ws://old:3000", "old-token")
        old_state = client._endpoint_auth
        handled_generations: list[int] = []

        async def handler_body() -> None:
            handled_generations.append(old_state.generation)

        def rotating_handler(_event: dict[str, Any]):
            worker = threading.Thread(
                target=client.update,
                args=("ws://new:4000", "new-token"),
            )
            worker.start()
            worker.join(timeout=2)
            assert not worker.is_alive()
            return handler_body()

        await client._handle_event_safely(
            rotating_handler,
            {"post_type": "message", "user_id": 1},
            auth_state=old_state,
        )

        assert client._endpoint_auth is not old_state
        assert handled_generations == [old_state.generation]

    @pytest.mark.asyncio
    async def test_request_action_commit_reports_unknown_after_lock_free_rotation(self):
        """A committed send continues, while rotation makes its response explicitly unknown."""

        client = OneBotWsClient("ws://old:3000", "old-token")
        old_state = client._endpoint_auth
        send_body_started = False
        committed_states = []

        async def send_body() -> None:
            nonlocal send_body_started
            send_body_started = True

        class RotatingWebSocket:
            closed = False
            close_code = None

            def __init__(self) -> None:
                self.send_called = False
                self.close_called = False

            def send(self, payload: str):
                self.send_called = True
                echo = json.loads(payload)["echo"]
                committed_states.append(client._pending_action_auth_states[echo])
                worker = threading.Thread(
                    target=client.update,
                    args=("ws://new:4000", "new-token"),
                )
                worker.start()
                worker.join(timeout=2)
                assert not worker.is_alive()
                return send_body()

            async def close(self) -> None:
                self.close_called = True

        ws = RotatingWebSocket()
        client._event_loop = asyncio.get_running_loop()
        client._ws = ws
        client._connected_auth_generation = old_state.generation

        with pytest.raises(OneBotActionOutcomeUnknown, match="outcome is unknown"):
            await client.request_action({"action": "test", "params": {}})
        await _drain_owned_ws_closes(client)

        assert ws.send_called is True
        assert send_body_started is True
        assert committed_states == [old_state]
        assert ws.close_called is True
        assert client._pending_action_futures == {}
        assert client._pending_action_auth_states == {}

    @pytest.mark.asyncio
    async def test_request_action_rotation_before_commit_is_uncommitted(self):
        """Rotation during lock-free transport inspection prevents the later commit."""

        client = OneBotWsClient("ws://old:3000", "old-token")

        class RotatingBeforeCommitWebSocket:
            close_code = None

            def __init__(self) -> None:
                self.rotated = False
                self.send_called = False

            @property
            def closed(self) -> bool:
                if not self.rotated:
                    self.rotated = True
                    worker = threading.Thread(
                        target=client.update,
                        args=("ws://new:4000", "new-token"),
                    )
                    worker.start()
                    worker.join(timeout=2)
                    assert not worker.is_alive()
                return False

            async def send(self, _payload: str) -> None:
                self.send_called = True

            async def close(self) -> None:
                return None

        ws = RotatingBeforeCommitWebSocket()
        client._event_loop = asyncio.get_running_loop()
        client._ws = ws
        client._connected_auth_generation = client._endpoint_auth.generation

        assert await client.request_action({"action": "test", "params": {}}) is None
        await asyncio.sleep(0)
        await _drain_owned_ws_closes(client)

        assert ws.send_called is False
        assert client._pending_action_futures == {}

    @pytest.mark.asyncio
    async def test_action_response_without_auth_generation_is_consumed_not_resolved(self):
        client = OneBotWsClient("ws://localhost:3000", "token")
        future = asyncio.get_running_loop().create_future()
        client._pending_action_futures["missing-generation"] = future
        client._pending_action_auth_states["missing-generation"] = client._endpoint_auth

        assert client._resolve_action_response(
            {
                "echo": "missing-generation",
                "status": "ok",
                "retcode": 0,
            }
        )
        assert not future.done()

        client._pending_action_futures.clear()
        client._pending_action_auth_states.clear()
        future.cancel()

    def test_rotation_tolerates_loop_closing_after_is_closed_check(self):
        client = OneBotWsClient("ws://old:3000", "old-token")
        ws = MagicMock()

        class ClosingLoop:
            @staticmethod
            def is_closed() -> bool:
                return False

            @staticmethod
            def call_soon_threadsafe(*_args) -> None:
                raise RuntimeError("Event loop is closed")

        client._event_loop = ClosingLoop()
        client._ws = ws
        client._connected_auth_generation = client._endpoint_auth.generation

        client.update("ws://new:4000", "new-token")

        assert (client.ws_uri, client.auth_token) == ("ws://new:4000", "new-token")
        assert client._ws is None
        assert client.connected() is False

    def test_connected(self):
        """测试连接状态"""
        client = OneBotWsClient("ws://localhost:3000", "")
        assert client.connected() is False

        # 模拟设置 WebSocket
        client._ws = MagicMock()
        assert client.connected() is True

    def test_connected_rejects_closed_websocket(self):
        """测试 closed/close_code/state 会被识别为未连接"""
        client = OneBotWsClient("ws://localhost:3000", "")

        ws = MagicMock()
        ws.closed = True
        client._ws = ws
        assert client.connected() is False

        ws = MagicMock()
        ws.closed = False
        ws.close_code = 1000
        client._ws = ws
        assert client.connected() is False

        ws = MagicMock()
        ws.closed = False
        ws.close_code = None
        ws.state.name = "CLOSED"
        client._ws = ws
        assert client.connected() is False

    @pytest.mark.asyncio
    async def test_send_action_when_connected(self):
        """测试连接时只在匹配回执确认成功"""
        client = OneBotWsClient("ws://localhost:3000", "")
        mock_ws = AsyncMock()
        client._ws = mock_ws

        action = {
            "action": "send_group_msg",
            "params": {"group_id": 12345, "message": []},
        }

        with patch(
            "core.onebot._normalize_action_for_onebot",
            wraps=_normalize_action_for_onebot,
        ) as normalize:
            pending = asyncio.create_task(client.send_action(action))
            sent_payload = await _wait_for_ws_action_request(mock_ws)
            assert "echo" in sent_payload
            assert client._resolve_action_response(
                {"echo": sent_payload["echo"], "status": "ok", "retcode": 0},
                auth_state=client._endpoint_auth,
            )
            result = await pending

        assert normalize.call_count == 1

        mock_ws.send.assert_called_once()
        assert result is True
        assert client._pending_action_futures == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "response",
        [
            {"status": "ok", "retcode": 0, "data": {"file": "image.png"}},
            {"status": "failed", "retcode": 100, "data": {}},
        ],
    )
    async def test_request_action_returns_echo_matched_envelope(self, response):
        client = OneBotWsClient("ws://localhost:3000", "")
        mock_ws = AsyncMock()
        client._ws = mock_ws
        action = {"action": "get_image", "params": {"file_id": "abc"}}

        pending = asyncio.create_task(client.request_action(action))
        sent_payload = await _wait_for_ws_action_request(mock_ws)
        envelope = {"echo": sent_payload["echo"], **response}
        assert client._resolve_action_response(envelope, auth_state=client._endpoint_auth) is True

        assert await pending == envelope
        assert action == {"action": "get_image", "params": {"file_id": "abc"}}
        assert client._pending_action_futures == {}

    @pytest.mark.asyncio
    async def test_send_action_when_connected_normalizes_emoji_segment(self):
        """测试 WebSocket 发送时会归一化 emoji 段"""
        client = OneBotWsClient("ws://localhost:3000", "")
        mock_ws = AsyncMock()
        client._ws = mock_ws

        action = {
            "action": "send_group_msg",
            "params": {
                "group_id": 12345,
                "message": [{"type": "emoji", "data": {"file": "emoji.png"}}],
            },
        }

        pending = asyncio.create_task(client.send_action(action))
        sent_payload = await _wait_for_ws_action_request(mock_ws)
        client._resolve_action_response(
            {"echo": sent_payload["echo"], "status": "ok", "retcode": 0},
            auth_state=client._endpoint_auth,
        )
        result = await pending

        assert sent_payload["params"]["message"] == [
            {"type": "image", "data": {"file": "emoji.png", "sub_type": "emoji"}}
        ]
        assert result is True

    @pytest.mark.asyncio
    async def test_send_action_normalizes_scalar_text_before_ws_commit(self):
        client = OneBotWsClient("ws://localhost:3000", "")
        mock_ws = AsyncMock()
        client._ws = mock_ws
        action = {
            "action": "send_group_msg",
            "params": {
                "group_id": 12345,
                "message": [{"type": "text", "data": {"text": 123}}],
            },
        }

        pending = asyncio.create_task(client.send_action(action))
        sent_payload = await _wait_for_ws_action_request(mock_ws)
        assert sent_payload["params"]["message"] == [{"type": "text", "data": {"text": "123"}}]
        client._resolve_action_response(
            {"echo": sent_payload["echo"], "status": "ok", "retcode": 0},
            auth_state=client._endpoint_auth,
        )
        assert await pending is True

    @pytest.mark.asyncio
    async def test_send_action_when_not_connected(self):
        """测试未连接时不发送"""
        client = OneBotWsClient("ws://localhost:3000", "")

        action = {"action": "test", "params": {}}

        result = await client.send_action(action)
        assert result is False

    @pytest.mark.asyncio
    async def test_send_action_failure_clears_ws(self):
        """测试发送失败时会清理失效连接"""
        client = OneBotWsClient("ws://localhost:3000", "")
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock(side_effect=RuntimeError("boom"))
        client._ws = mock_ws

        with pytest.raises(OneBotActionOutcomeUnknown):
            await client.send_action({"action": "test", "params": {}})

        assert client._ws is None

    @pytest.mark.asyncio
    async def test_send_action_rejects_nonzero_retcode(self):
        client = OneBotWsClient("ws://localhost:3000", "")
        mock_ws = AsyncMock()
        client._ws = mock_ws

        pending = asyncio.create_task(client.send_action({"action": "test", "params": {}}))
        sent_payload = await _wait_for_ws_action_request(mock_ws)
        client._resolve_action_response(
            {"echo": sent_payload["echo"], "status": "failed", "retcode": 100},
            auth_state=client._endpoint_auth,
        )

        assert await pending is False
        assert client._pending_action_futures == {}

    @pytest.mark.asyncio
    async def test_send_action_times_out_for_wrong_echo(self):
        client = OneBotWsClient("ws://localhost:3000", "", action_response_timeout_seconds=0.01)
        mock_ws = AsyncMock()
        client._ws = mock_ws

        pending = asyncio.create_task(client.send_action({"action": "test", "params": {}}))
        sent_payload = await _wait_for_ws_action_request(mock_ws)
        assert client._resolve_action_response(
            {"echo": f"wrong-{sent_payload['echo']}", "status": "ok", "retcode": 0},
            auth_state=client._endpoint_auth,
        )

        with pytest.raises(OneBotActionOutcomeUnknown):
            await pending
        assert client._pending_action_futures == {}

    @pytest.mark.asyncio
    async def test_stop_fails_pending_ws_action_response(self):
        client = OneBotWsClient("ws://localhost:3000", "")
        mock_ws = AsyncMock()
        mock_ws.close = AsyncMock()
        client._ws = mock_ws

        pending = asyncio.create_task(client.send_action({"action": "test", "params": {}}))
        await _wait_for_ws_action_request(mock_ws)
        await client.stop()

        with pytest.raises(OneBotActionOutcomeUnknown):
            await pending
        assert client._pending_action_futures == {}

    @pytest.mark.asyncio
    async def test_duplicate_ws_action_response_does_not_change_completed_result(self):
        client = OneBotWsClient("ws://localhost:3000", "")
        mock_ws = AsyncMock()
        client._ws = mock_ws

        pending = asyncio.create_task(client.send_action({"action": "test", "params": {}}))
        sent_payload = await _wait_for_ws_action_request(mock_ws)
        response = {"echo": sent_payload["echo"], "status": "ok", "retcode": 0}
        client._resolve_action_response(response, auth_state=client._endpoint_auth)
        assert await pending is True

        assert client._resolve_action_response(response, auth_state=client._endpoint_auth) is True
        assert client._pending_action_futures == {}

    @pytest.mark.asyncio
    async def test_listen_routes_action_response_without_dispatching_it(self):
        client = OneBotWsClient("ws://localhost:3000", "")
        response_future = asyncio.get_running_loop().create_future()
        client._pending_action_futures["request-echo"] = response_future
        client._pending_action_auth_states["request-echo"] = client._endpoint_auth
        handler = AsyncMock()

        class ResponseOnlyWebSocket:
            def __init__(self):
                self._messages = iter(
                    [json.dumps({"echo": "request-echo", "status": "ok", "retcode": 0})]
                )

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self._messages)
                except StopIteration:
                    raise StopAsyncIteration from None

        await client._listen(ResponseOnlyWebSocket(), handler)

        assert response_future.result() == {
            "echo": "request-echo",
            "status": "ok",
            "retcode": 0,
        }
        handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop(self):
        """测试停止客户端"""
        client = OneBotWsClient("ws://localhost:3000", "")
        mock_ws = AsyncMock()
        mock_ws.close = AsyncMock()
        client._ws = mock_ws

        await client.stop()

        assert client._running is False
        mock_ws.close.assert_awaited_once()
        assert client._ws is None

    @pytest.mark.asyncio
    async def test_stop_awaits_cleanup_task_cancellation(self):
        """测试 stop 会等待 cleanup task 完成取消清理"""
        client = OneBotWsClient("ws://localhost:3000", "")
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
        client = OneBotWsClient("ws://localhost:3000", "")
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
        client = OneBotWsClient("ws://localhost:3000", "")
        client._shutdown_timeout_seconds = 0.01
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()

        resistant_worker = cancellation_resistant_callback(entered, cancelled, release)

        task = asyncio.create_task(resistant_worker())
        client._queue_tasks["user:1"] = task
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
        client = OneBotWsClient("ws://old.example/ws", "old-token")
        client._event_loop = asyncio.get_running_loop()
        prior_started = asyncio.Event()
        prior_release = asyncio.Event()

        class PriorSocket:
            async def close(self) -> None:
                prior_started.set()
                await prior_release.wait()

        prior_socket = PriorSocket()
        prior_task = client._schedule_ws_close(prior_socket)
        await prior_started.wait()

        current_socket = MagicMock(close=AsyncMock())
        client._ws = current_socket
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
        client = OneBotWsClient("ws://old.example/ws", "old-token")
        first_entered = asyncio.Event()
        first_cancelled = asyncio.Event()
        first_release = asyncio.Event()
        close_entered = asyncio.Event()
        close_release = asyncio.Event()
        second_attempted = asyncio.Event()
        attempts = 0

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
                client._ws = socket
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
        client = OneBotWsClient("ws://localhost:3000", "")
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
        calls = 0

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
        client = OneBotWsClient("ws://localhost:3000", "")
        attempt_entered = asyncio.Event()
        second_attempt_cancel = asyncio.Event()
        release_attempt = asyncio.Event()
        cancel_count = 0

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
        client = OneBotWsClient("ws://localhost:3000", "")
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
        client._reconnect_sleep = record_sleep
        client._reconnect_random = lambda: 0.5

        await client.connect_and_listen(AsyncMock())
        await client.stop()

        assert delays == [5.0, 10.0, 20.0, 40.0, 54.0, 54.0]

    @pytest.mark.asyncio
    async def test_stable_connection_resets_reconnect_backoff(self, monkeypatch):
        client = OneBotWsClient("ws://localhost:3000", "")
        durations = iter((0.01, 0.02, 31.0, 0.03))
        delays: list[float] = []

        async def connect_once(_handler):
            return next(durations)

        async def record_sleep(delay: float) -> None:
            delays.append(delay)
            if len(delays) == 4:
                client._running = False

        monkeypatch.setattr(client, "_connect_once", connect_once)
        client._reconnect_sleep = record_sleep
        client._reconnect_random = lambda: 0.5

        await client.connect_and_listen(AsyncMock())
        await client.stop()

        assert delays == [5.0, 10.0, 5.0, 10.0]

    @pytest.mark.asyncio
    async def test_cross_thread_auth_rotation_wakes_current_backoff(self, monkeypatch):
        client = OneBotWsClient("ws://old.example/ws", "")
        backoff_entered = asyncio.Event()
        backoff_cancelled = asyncio.Event()

        async def blocked_sleep(_delay: float) -> None:
            backoff_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                backoff_cancelled.set()

        connect_once = AsyncMock(return_value=0.0)
        monkeypatch.setattr(client, "_connect_once", connect_once)
        client._reconnect_sleep = blocked_sleep
        client._reconnect_random = lambda: 0.5

        listener = asyncio.create_task(client.connect_and_listen(AsyncMock()))
        await asyncio.wait_for(backoff_entered.wait(), timeout=1)

        worker = threading.Thread(
            target=client.update,
            args=("ws://new.example/ws", "new-token"),
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
        client = OneBotWsClient("ws://old.example/ws", "")
        delays: list[float] = []
        calls = 0

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
        client = OneBotWsClient("ws://localhost:3000", "")
        samples = iter((0.0, 1.0, 1.0, 1.0, 1.0))
        delays: list[float] = []

        async def record_sleep(delay: float) -> None:
            delays.append(delay)
            if len(delays) == 5:
                client._running = False

        monkeypatch.setattr(client, "_connect_once", AsyncMock(return_value=0.0))
        client._reconnect_sleep = record_sleep
        client._reconnect_random = lambda: next(samples)

        await client.connect_and_listen(AsyncMock())
        await client.stop()

        assert delays == [4.0, 12.0, 24.0, 48.0, 60.0]

    def test_max_backoff_jitter_remains_continuous_instead_of_collapsing_at_cap(self):
        samples = (0.0, 0.25, 0.5, 0.75, 1.0)
        values = [_jittered_reconnect_delay(60.0, sample) for sample in samples]

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

        normal = ConnectionClosedOK(Close(1000, "normal"), Close(1000, "normal"), True)
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

    @pytest.mark.asyncio
    @pytest.mark.parametrize("header_parameter", ["additional_headers", "extra_headers"])
    async def test_connect_once_sends_token_with_supported_header_parameter(
        self,
        monkeypatch,
        header_parameter,
    ):
        client = OneBotWsClient("ws://localhost:3000", "secret-token")
        captured: dict[str, Any] = {}
        ws = object()

        class ConnectContext:
            async def __aenter__(self):
                return ws

            async def __aexit__(self, *_args):
                return None

        class DummyWebsockets:
            @staticmethod
            def connect(uri, **kwargs):
                captured.update(uri=uri, kwargs=kwargs)
                return ConnectContext()

        monkeypatch.setattr(client, "_listen", AsyncMock(return_value=1.25))
        with (
            patch.dict("sys.modules", {"websockets": DummyWebsockets()}),
            patch("core.onebot._get_connect_signature", return_value={header_parameter}),
        ):
            assert await client._connect_once(AsyncMock()) == 1.25

        assert captured == {
            "uri": "ws://localhost:3000",
            "kwargs": {
                header_parameter: {"Authorization": "Bearer secret-token"},
            },
        }

    @pytest.mark.asyncio
    async def test_configured_token_fails_closed_when_header_parameter_is_unknown(self):
        client = OneBotWsClient("ws://localhost:3000", "secret-token")

        class DummyWebsockets:
            connect = MagicMock()

        with (
            patch.dict("sys.modules", {"websockets": DummyWebsockets()}),
            patch("core.onebot._get_connect_signature", return_value=set()),
        ):
            with pytest.raises(RuntimeError, match="cannot be sent"):
                await client._connect_once(AsyncMock())

        DummyWebsockets.connect.assert_not_called()

    @pytest.mark.asyncio
    async def test_revoked_credentials_never_call_websocket_connect(self):
        client = OneBotWsClient(
            "ws://localhost:3000",
            "",
            credentials_trusted=False,
        )

        with patch("websockets.connect") as connect:
            with pytest.raises(RuntimeError, match="credential source is unavailable"):
                await client._connect_once(AsyncMock())

        connect.assert_not_called()
        assert client.connected() is False

    @pytest.mark.asyncio
    async def test_revoked_connect_loop_waits_until_credentials_recover(self, monkeypatch):
        client = OneBotWsClient(
            "ws://localhost:3000",
            "",
            credentials_trusted=False,
        )
        waiting = asyncio.Event()
        blocked = asyncio.Event()

        async def wait_for_recovery(_delay: float) -> None:
            waiting.set()
            await blocked.wait()

        connect_once = AsyncMock(return_value=0.0)
        monkeypatch.setattr(client, "_connect_once", connect_once)
        client._reconnect_sleep = wait_for_recovery

        listener = asyncio.create_task(client.connect_and_listen(AsyncMock()))
        await waiting.wait()
        connect_once.assert_not_awaited()

        client.update(client.ws_uri, "", credentials_trusted=True)
        for _ in range(20):
            if connect_once.await_count:
                break
            await asyncio.sleep(0)

        connect_once.assert_awaited()
        await client.stop()
        assert listener.cancelled()

    @pytest.mark.asyncio
    async def test_connect_loop_rejects_unsupported_token_before_starting_owned_tasks(self):
        client = OneBotWsClient("ws://localhost:3000", "secret-token")

        class DummyWebsockets:
            connect = MagicMock()

        with (
            patch.dict("sys.modules", {"websockets": DummyWebsockets()}),
            patch("core.onebot._get_connect_signature", return_value=set()),
        ):
            with pytest.raises(RuntimeError, match="cannot be sent"):
                await client.connect_and_listen(AsyncMock())

        assert client._running is False
        assert client._main_task is None
        assert client._cleanup_task is None
        DummyWebsockets.connect.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_token_connects_without_unproven_header_keyword(self, monkeypatch):
        client = OneBotWsClient("ws://localhost:3000", "")
        captured: dict[str, Any] = {}
        ws = object()

        class ConnectContext:
            async def __aenter__(self):
                return ws

            async def __aexit__(self, *_args):
                return None

        class DummyWebsockets:
            @staticmethod
            def connect(uri):
                captured["uri"] = uri
                return ConnectContext()

        monkeypatch.setattr(client, "_listen", AsyncMock(return_value=0.5))
        with patch.dict("sys.modules", {"websockets": DummyWebsockets()}):
            assert await client._connect_once(AsyncMock()) == 0.5

        assert captured == {"uri": "ws://localhost:3000"}

    @pytest.mark.asyncio
    async def test_connect_once_rejects_cross_thread_rotated_auth_snapshot(self, monkeypatch):
        """An old endpoint/token snapshot can never inherit the new generation."""

        snapshot_read = threading.Event()
        rotation_finished = threading.Event()

        class SnapshotRaceClient(OneBotWsClient):
            def __init__(self) -> None:
                self._race_armed = False
                self._race_state = None
                super().__init__("ws://old.example/ws", "old-token")

            @property
            def _endpoint_auth(self):
                state = self._race_state
                if self._race_armed:
                    self._race_armed = False
                    snapshot_read.set()
                    if not rotation_finished.wait(timeout=2):
                        raise AssertionError("token rotation thread did not finish")
                return state

            @_endpoint_auth.setter
            def _endpoint_auth(self, state) -> None:
                self._race_state = state

        client = SnapshotRaceClient()
        client._event_loop = asyncio.get_running_loop()
        client._race_armed = True
        ws = MagicMock(close=AsyncMock())
        captured: dict[str, Any] = {}

        class ConnectContext:
            async def __aenter__(self):
                return ws

            async def __aexit__(self, *_args):
                return None

        def connect(uri, additional_headers=None):
            captured.update(uri=uri, headers=additional_headers)
            return ConnectContext()

        monkeypatch.setattr("websockets.connect", connect)

        errors: list[BaseException] = []

        def rotate() -> None:
            try:
                if not snapshot_read.wait(timeout=2):
                    raise AssertionError("connect did not capture its auth snapshot")
                client.update("ws://new.example/ws", "new-token")
            except BaseException as exc:
                errors.append(exc)
            finally:
                rotation_finished.set()

        worker = threading.Thread(target=rotate)
        worker.start()
        await client._connect_once(AsyncMock())
        worker.join(timeout=2)

        assert errors == []
        assert not worker.is_alive()
        assert captured == {
            "uri": "ws://old.example/ws",
            "headers": {"Authorization": "Bearer old-token"},
        }
        assert (client.ws_uri, client.auth_token) == (
            "ws://new.example/ws",
            "new-token",
        )
        ws.close.assert_awaited_once()
        assert client._ws is None

    @pytest.mark.asyncio
    async def test_cross_thread_rotation_invalidates_only_old_pending_generation(self):
        client = OneBotWsClient("ws://old.example/ws", "old-token")
        loop = asyncio.get_running_loop()
        client._event_loop = loop
        old_state = client._endpoint_auth
        old_future = loop.create_future()
        client._pending_action_futures["old"] = old_future
        client._pending_action_auth_states["old"] = old_state

        worker = threading.Thread(
            target=client.update,
            args=("ws://new.example/ws", "new-token"),
        )
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()

        # The owning-loop callback is queued but has not run while this
        # coroutine is still executing.  A request from the new generation
        # must survive that delayed callback.
        new_future = loop.create_future()
        client._pending_action_futures["new"] = new_future
        client._pending_action_auth_states["new"] = client._endpoint_auth
        await asyncio.sleep(0)

        with pytest.raises(ConnectionError, match="authorization changed"):
            await old_future
        assert not new_future.done()
        new_future.cancel()
        client._pending_action_futures.clear()
        client._pending_action_auth_states.clear()

    def test_get_connect_signature_is_cached(self):
        """测试 connect signature 只 inspect 一次"""

        class DummyWebsockets:
            @staticmethod
            def connect(uri, additional_headers=None):
                return None

        _CONNECT_SIGNATURE_CACHE.clear()
        with patch("inspect.signature", wraps=inspect.signature) as mock_signature:
            assert "additional_headers" in _get_connect_signature(DummyWebsockets)
            assert "additional_headers" in _get_connect_signature(DummyWebsockets)

        assert mock_signature.call_count == 1

    def test_get_connect_signature_fails_closed_and_caches_inspection_failure(self):
        class DummyWebsockets:
            connect = MagicMock()

        _CONNECT_SIGNATURE_CACHE.clear()
        with patch("inspect.signature", side_effect=ValueError("opaque")) as mock_signature:
            assert _get_connect_signature(DummyWebsockets) == frozenset()
            assert _get_connect_signature(DummyWebsockets) == frozenset()

        assert mock_signature.call_count == 1

    @pytest.mark.parametrize(
        "parameter",
        [
            inspect.Parameter(
                "additional_headers",
                inspect.Parameter.POSITIONAL_ONLY,
            ),
            inspect.Parameter(
                "additional_headers",
                inspect.Parameter.VAR_KEYWORD,
            ),
        ],
    )
    def test_connect_signature_rejects_header_names_that_cannot_be_keyword_arguments(
        self,
        parameter,
    ):
        class DummyWebsockets:
            connect = MagicMock()

        uri_kind = (
            inspect.Parameter.POSITIONAL_ONLY
            if parameter.kind is inspect.Parameter.POSITIONAL_ONLY
            else inspect.Parameter.POSITIONAL_OR_KEYWORD
        )
        signature = inspect.Signature([inspect.Parameter("uri", uri_kind), parameter])
        _CONNECT_SIGNATURE_CACHE.clear()
        with patch("inspect.signature", return_value=signature):
            assert "additional_headers" not in _get_connect_signature(DummyWebsockets)

    def test_bound_connect_method_uses_one_signature_cache_entry(self):
        class DummyWebsockets:
            def connect(self, uri, *, extra_headers=None):
                return uri, extra_headers

        module = DummyWebsockets()
        _CONNECT_SIGNATURE_CACHE.clear()
        with patch("inspect.signature", wraps=inspect.signature) as mock_signature:
            assert "extra_headers" in _get_connect_signature(module)
            assert "extra_headers" in _get_connect_signature(module)

        assert mock_signature.call_count == 1
        assert len(_CONNECT_SIGNATURE_CACHE) == 1

    def test_get_queue_key(self):
        """测试获取队列键"""
        client = OneBotWsClient("ws://localhost:3000", "")

        # 私聊事件
        private_event = {"user_id": 12345, "group_id": None}
        key = client._get_queue_key(private_event)
        assert key == "user:12345"

        # 群聊事件
        group_event = {"user_id": 12345, "group_id": 67890}
        key = client._get_queue_key(group_event)
        assert key == "group:67890:user:12345"

        # 无 user_id
        no_user_event = {"group_id": 67890}
        key = client._get_queue_key(no_user_event)
        assert key is None

    @pytest.mark.asyncio
    async def test_dispatch_event_respects_pending_semaphore_across_queues(self):
        """测试 max_pending_events 真正限制 handler 执行并发"""
        client = OneBotWsClient("ws://localhost:3000", "", max_pending_events=1)
        started = asyncio.Event()
        release = asyncio.Event()
        current = 0
        max_seen = 0

        async def handler(event: dict[str, Any]) -> None:
            nonlocal current, max_seen
            current += 1
            max_seen = max(max_seen, current)
            started.set()
            await release.wait()
            current -= 1

        await asyncio.gather(
            client._dispatch_event(handler, {"user_id": 1}),
            client._dispatch_event(handler, {"user_id": 2}),
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.gather(*client._queue_tasks.values())

        assert max_seen == 1

    @pytest.mark.asyncio
    async def test_drain_queue_restarts_when_event_arrives_during_timeout_exit(self):
        """测试 drain 超时退出窗口内入队的事件不会滞留"""
        client = OneBotWsClient("ws://localhost:3000", "")
        key = "user:1"
        queue: asyncio.Queue[_QueuedOneBotEvent] = asyncio.Queue()
        client._message_queues[key] = queue
        handled: list[dict[str, Any]] = []
        real_wait_for = asyncio.wait_for
        wait_calls = 0

        async def handler(event: dict[str, Any]) -> None:
            handled.append(event)

        async def fake_wait_for(awaitable, timeout):
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                if hasattr(awaitable, "close"):
                    awaitable.close()
                queue.put_nowait(
                    _QueuedOneBotEvent(
                        event={"user_id": 1},
                        auth_state=client._endpoint_auth,
                    )
                )
                raise asyncio.TimeoutError()
            return await real_wait_for(awaitable, timeout)

        task = asyncio.create_task(client._drain_queue(key, handler))
        client._queue_tasks[key] = task

        with patch("core.onebot.asyncio.wait_for", side_effect=fake_wait_for):
            await task
            restarted = client._queue_tasks[key]
            await real_wait_for(restarted, timeout=2.0)

        assert handled == [{"user_id": 1}]

    @pytest.mark.asyncio
    async def test_drain_queue_does_not_suppress_an_unexpected_handler_failure(self, monkeypatch):
        client = OneBotWsClient("ws://localhost:3000", "")
        key = "user:unhandled-error"
        queue: asyncio.Queue[_QueuedOneBotEvent] = asyncio.Queue()
        queue.put_nowait(
            _QueuedOneBotEvent(
                event={"user_id": 1},
                auth_state=client._endpoint_auth,
            )
        )
        client._message_queues[key] = queue

        async def fail_handler(*_args, **_kwargs):
            raise RuntimeError("handler exploded")

        monkeypatch.setattr(client, "_handle_event_safely", fail_handler)

        with pytest.raises(RuntimeError, match="handler exploded"):
            await client._drain_queue(key, AsyncMock())

    @pytest.mark.asyncio
    async def test_drain_queue_drops_raw_event_without_auth_generation(self):
        client = OneBotWsClient("ws://localhost:3000", "")
        key = "user:raw-event"
        queue: asyncio.Queue[Any] = asyncio.Queue()
        queue.put_nowait({"user_id": 1})
        client._message_queues[key] = queue
        handler = AsyncMock()

        task = asyncio.create_task(client._drain_queue(key, handler))
        for _ in range(10):
            if queue.empty():
                break
            await asyncio.sleep(0)
        assert queue.empty()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        handler.assert_not_awaited()
