"""OneBot WebSocket 动作发送、响应匹配与鉴权提交。"""

from __future__ import annotations

import tests.helpers.onebot_test_support as _fixture_support
from tests.helpers.onebot_test_support import (
    Any,
    AsyncMock,
    MagicMock,
    OneBotActionOutcomeUnknown,
    OneBotWsClient,
    _drain_owned_ws_closes,
    _normalize_action_for_onebot,
    _wait_for_ws_action_request,
    asyncio,
    json,
    patch,
    pytest,
    threading,
)

bounded_transport_adapter = _fixture_support.bounded_transport_adapter


class TestOneBotWebSocketActions:
    """按单一传输职责组织的 OneBot WebSocket 测试。"""

    @pytest.mark.asyncio
    async def test_update_immediately_invalidates_the_connected_auth_generation(self):
        client = OneBotWsClient("ws://old:3000", "old_token")
        ws = MagicMock(close=AsyncMock())
        client._ws                                = ws
        client._event_loop                        = asyncio.get_running_loop()
        client._connected_auth_generation         = client._endpoint_auth.generation
        pending                                   = asyncio.get_running_loop().create_future()
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

        client             = OneBotWsClient("ws://old:3000", "old-token")
        client._event_loop = asyncio.get_running_loop()
        old_state          = client._endpoint_auth
        handler            = AsyncMock()

        worker = threading.Thread(
            target = client.update,
            args   = ("ws://new:4000", "new-token"),
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
            target = client.update,
            args   = ("ws://new:4000", "new-token"),
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

        client                         = OneBotWsClient("ws://old:3000", "old-token")
        old_state                      = client._endpoint_auth
        handled_generations: list[int] = []

        async def handler_body() -> None:
            handled_generations.append(old_state.generation)

        def rotating_handler(_event: dict[str, Any]):
            worker = threading.Thread(
                target = client.update,
                args   = ("ws://new:4000", "new-token"),
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

        client            = OneBotWsClient("ws://old:3000", "old-token")
        old_state         = client._endpoint_auth
        send_body_started = False
        committed_states  = []

        async def send_body() -> None:
            nonlocal send_body_started
            send_body_started = True

        class RotatingWebSocket:
            closed     = False
            close_code = None

            def __init__(self) -> None:
                self.send_called  = False
                self.close_called = False

            def send(self, payload: str):
                self.send_called = True
                echo             = json.loads(payload)["echo"]
                committed_states.append(client._pending_action_auth_states[echo])
                worker = threading.Thread(
                    target = client.update,
                    args   = ("ws://new:4000", "new-token"),
                )
                worker.start()
                worker.join(timeout=2)
                assert not worker.is_alive()
                return send_body()

            async def close(self) -> None:
                self.close_called = True

        ws                                = RotatingWebSocket()
        client._event_loop                = asyncio.get_running_loop()
        client._ws                        = ws
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
                self.rotated     = False
                self.send_called = False

            @property
            def closed(self) -> bool:
                if not self.rotated:
                    self.rotated = True
                    worker       = threading.Thread(
                        target = client.update,
                        args   = ("ws://new:4000", "new-token"),
                    )
                    worker.start()
                    worker.join(timeout=2)
                    assert not worker.is_alive()
                return False

            async def send(self, _payload: str) -> None:
                self.send_called = True

            async def close(self) -> None:
                return None

        ws                                = RotatingBeforeCommitWebSocket()
        client._event_loop                = asyncio.get_running_loop()
        client._ws                        = ws
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

    @pytest.mark.asyncio
    async def test_send_action_when_connected(self):
        """测试连接时只在匹配回执确认成功"""
        client     = OneBotWsClient("ws://localhost:3000", "")
        mock_ws    = AsyncMock()
        client._ws = mock_ws

        action = {
            "action": "send_group_msg",
            "params": {"group_id": 12345, "message": []},
        }

        with patch(
            "core.onebot._normalize_action_for_onebot",
            wraps=_normalize_action_for_onebot,
        ) as normalize:
            pending      = asyncio.create_task(client.send_action(action))
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
        client     = OneBotWsClient("ws://localhost:3000", "")
        mock_ws    = AsyncMock()
        client._ws = mock_ws
        action     = {"action": "get_image", "params": {"file_id": "abc"}}

        pending      = asyncio.create_task(client.request_action(action))
        sent_payload = await _wait_for_ws_action_request(mock_ws)
        envelope     = {"echo": sent_payload["echo"], **response}
        assert client._resolve_action_response(envelope, auth_state=client._endpoint_auth) is True

        assert await pending == envelope
        assert action == {"action": "get_image", "params": {"file_id": "abc"}}
        assert client._pending_action_futures == {}

    @pytest.mark.asyncio
    async def test_send_action_when_connected_normalizes_emoji_segment(self):
        """测试 WebSocket 发送时会归一化 emoji 段"""
        client     = OneBotWsClient("ws://localhost:3000", "")
        mock_ws    = AsyncMock()
        client._ws = mock_ws

        action = {
            "action": "send_group_msg",
            "params": {
                "group_id": 12345,
                "message": [{"type": "emoji", "data": {"file": "emoji.png"}}],
            },
        }

        pending      = asyncio.create_task(client.send_action(action))
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
        client     = OneBotWsClient("ws://localhost:3000", "")
        mock_ws    = AsyncMock()
        client._ws = mock_ws
        action     = {
            "action": "send_group_msg",
            "params": {
                "group_id": 12345,
                "message": [{"type": "text", "data": {"text": 123}}],
            },
        }

        pending      = asyncio.create_task(client.send_action(action))
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
        client  = OneBotWsClient("ws://localhost:3000", "")
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock(side_effect=RuntimeError("boom"))
        client._ws = mock_ws

        with pytest.raises(OneBotActionOutcomeUnknown):
            await client.send_action({"action": "test", "params": {}})

        assert client._ws is None

    @pytest.mark.asyncio
    async def test_send_action_rejects_nonzero_retcode(self):
        client     = OneBotWsClient("ws://localhost:3000", "")
        mock_ws    = AsyncMock()
        client._ws = mock_ws

        pending      = asyncio.create_task(client.send_action({"action": "test", "params": {}}))
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
        mock_ws    = AsyncMock()
        client._ws = mock_ws

        pending      = asyncio.create_task(client.send_action({"action": "test", "params": {}}))
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
        client        = OneBotWsClient("ws://localhost:3000", "")
        mock_ws       = AsyncMock()
        mock_ws.close = AsyncMock()
        client._ws    = mock_ws

        pending = asyncio.create_task(client.send_action({"action": "test", "params": {}}))
        await _wait_for_ws_action_request(mock_ws)
        await client.stop()

        with pytest.raises(OneBotActionOutcomeUnknown):
            await pending
        assert client._pending_action_futures == {}

    @pytest.mark.asyncio
    async def test_duplicate_ws_action_response_does_not_change_completed_result(self):
        client     = OneBotWsClient("ws://localhost:3000", "")
        mock_ws    = AsyncMock()
        client._ws = mock_ws

        pending      = asyncio.create_task(client.send_action({"action": "test", "params": {}}))
        sent_payload = await _wait_for_ws_action_request(mock_ws)
        response     = {"echo": sent_payload["echo"], "status": "ok", "retcode": 0}
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
