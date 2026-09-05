"""OneBot WebSocket 配置、鉴权代次和连接签名。"""

from __future__ import annotations

import tests.helpers.onebot_test_support as _fixture_support
from tests.helpers.onebot_test_support import (
    _CONNECT_SIGNATURE_CACHE,
    Any,
    AsyncMock,
    MagicMock,
    OneBotWsClient,
    _get_connect_signature,
    asyncio,
    inspect,
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


class TestOneBotWebSocketConfiguration:
    """按单一传输职责组织的 OneBot WebSocket 测试。"""

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
            ws_uri     = "ws://localhost:3000",
            auth_token = "test_token",
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

    def test_rotation_tolerates_loop_closing_after_is_closed_check(self):
        client = OneBotWsClient("ws://old:3000", "old-token")
        ws     = MagicMock()

        class ClosingLoop:
            @staticmethod
            def is_closed() -> bool:
                return False

            @staticmethod
            def call_soon_threadsafe(*_args) -> None:
                raise RuntimeError("Event loop is closed")

        client._event_loop                = ClosingLoop()
        client._ws                        = ws
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

        ws         = MagicMock()
        ws.closed  = True
        client._ws = ws
        assert client.connected() is False

        ws            = MagicMock()
        ws.closed     = False
        ws.close_code = 1000
        client._ws    = ws
        assert client.connected() is False

        ws            = MagicMock()
        ws.closed     = False
        ws.close_code = None
        ws.state.name = "CLOSED"
        client._ws    = ws
        assert client.connected() is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("header_parameter", ["additional_headers", "extra_headers"])
    async def test_connect_once_sends_token_with_supported_header_parameter(
        self,
        monkeypatch,
        header_parameter,
    ):
        client                   = OneBotWsClient("ws://localhost:3000", "secret-token")
        captured: dict[str, Any] = {}
        ws                       = object()

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
        client                   = OneBotWsClient("ws://localhost:3000", "")
        captured: dict[str, Any] = {}
        ws                       = object()

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

        snapshot_read     = threading.Event()
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

        client             = SnapshotRaceClient()
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
            target = client.update,
            args   = ("ws://new.example/ws", "new-token"),
        )
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()

        # The owning-loop callback is queued but has not run while this
        # coroutine is still executing.  A request from the new generation
        # must survive that delayed callback.
        new_future                                = loop.create_future()
        client._pending_action_futures["new"]     = new_future
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
