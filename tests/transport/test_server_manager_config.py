"""入站管理器配置和广播。"""

from __future__ import annotations

import tests.helpers.server_test_support as _fixture_support
from tests.helpers.server_test_support import (
    Any,
    AsyncMock,
    BroadcastResult,
    InboundManager,
    InboundServer,
    MagicMock,
    Mock,
    _parse_http_base,
    asyncio,
    patch,
    pytest,
)

mock_handler  = _fixture_support.mock_handler
sample_server = _fixture_support.sample_server


@pytest.mark.unit
def test_inbound_manager_from_config_disabled():
    """Test InboundManager.from_config when disabled"""
    config = {"enable_inbound_server": False}

    async def handler(event):
        return []

    manager = InboundManager.from_config(
        config  = config,
        token   = "test_token",
        handler = handler,
    )

    assert manager is None


@pytest.mark.unit
def test_inbound_manager_from_config_no_urls():
    """Test InboundManager.from_config with no URLs configured"""
    config = {
        "enable_inbound_server": True,
        "inbound_http_base": "",
        "inbound_ws_uri": "",
    }

    async def handler(event):
        return []

    manager = InboundManager.from_config(
        config  = config,
        token   = "test_token",
        handler = handler,
    )

    assert manager is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("http_base", "ws_uri"),
    [
        ("http://127.0.0.1:8080", ""),
        ("", "ws://127.0.0.1:8080/ws"),
    ],
)
def test_configured_loopback_listener_requires_non_empty_token(http_base, ws_uri):
    with pytest.raises(ValueError, match="require a non-empty inbound token"):
        InboundManager.from_config(
            config={
                "enable_inbound_server": True,
                "inbound_http_base": http_base,
                "inbound_ws_uri": ws_uri,
            },
            token="",
            handler=AsyncMock(return_value=[]),
        )


@pytest.mark.unit
def test_inbound_manager_from_config_http_only():
    """Test InboundManager.from_config with HTTP only"""
    from core.config import ConfigSnapshot

    config = ConfigSnapshot(
        config={
            "enable_inbound_server": True,
            "inbound_http_base": "http://localhost:8080",
            "inbound_ws_uri": "",
        },
        secrets={},
    ).config

    async def handler(event):
        return []

    manager = InboundManager.from_config(
        config  = config,
        token   = "test_token",
        handler = handler,
    )

    assert manager is not None
    assert manager._inbound_http_base == "http://localhost:8080"


@pytest.mark.unit
def test_inbound_manager_from_config_ws_only():
    """Test InboundManager.from_config with WS only"""
    config = {
        "enable_inbound_server": True,
        "inbound_http_base": "",
        "inbound_ws_uri": "ws://localhost:8080/ws",
    }

    async def handler(event):
        return []

    manager = InboundManager.from_config(
        config  = config,
        token   = "test_token",
        handler = handler,
    )

    assert manager is not None
    assert manager._inbound_ws_uri == "ws://localhost:8080/ws"


@pytest.mark.unit
def test_inbound_manager_from_config_applies_broadcast_timeout():
    manager = InboundManager.from_config(
        config={
            "enable_inbound_server": True,
            "inbound_http_base": "",
            "inbound_ws_uri": "ws://localhost:8080/ws",
            "inbound_ws_broadcast_timeout_seconds": 2.25,
        },
        token="test_token",
        handler=AsyncMock(return_value=[]),
    )

    assert manager is not None
    assert manager._ws_broadcast_timeout_seconds == 2.25
    assert manager.config_key[-2] == 2.25


@pytest.mark.unit
@pytest.mark.parametrize(
    "config",
    [
        {
            "enable_inbound_server": True,
            "inbound_http_base": "https://127.0.0.1:8443",
            "inbound_ws_uri": "",
        },
        {
            "enable_inbound_server": True,
            "inbound_http_base": "",
            "inbound_ws_uri": "wss://127.0.0.1:8443/ws",
        },
    ],
)
def test_inbound_manager_rejects_tls_schemes_it_does_not_terminate(config):
    with pytest.raises(ValueError, match="does not terminate TLS"):
        InboundManager.from_config(
            config = config,
            token  = "test_token",
            handler=AsyncMock(return_value=[]),
        )


@pytest.mark.unit
def test_inbound_manager_rejects_plaintext_non_loopback_without_proxy_acknowledgement():
    with pytest.raises(ValueError, match="non-loopback.*plaintext"):
        InboundManager(
            inbound_http_base = "http://0.0.0.0:8080",
            inbound_ws_uri    = "",
            token             = "test_token",
            handler=AsyncMock(return_value=[]),
        )


@pytest.mark.unit
def test_inbound_manager_accepts_non_loopback_with_trusted_tls_proxy_acknowledgement():
    manager = InboundManager.from_config(
        config={
            "enable_inbound_server": True,
            "inbound_http_base": "http://0.0.0.0:8080",
            "inbound_ws_uri": "ws://0.0.0.0:8080/ws",
            "inbound_trusted_tls_proxy": True,
        },
        token="test_token",
        handler=AsyncMock(return_value=[]),
    )

    assert manager is not None
    assert manager._trusted_tls_proxy is True
    assert manager.config_key[-1] is True


@pytest.mark.unit
def test_inbound_manager_requires_token_for_non_loopback_proxy_listener():
    with pytest.raises(ValueError, match="require a non-empty inbound token"):
        InboundManager(
            inbound_http_base = "http://0.0.0.0:8080",
            inbound_ws_uri    = "",
            token             = "",
            handler=AsyncMock(return_value=[]),
            trusted_tls_proxy=True,
        )


@pytest.mark.unit
@pytest.mark.parametrize("invalid_flag", ["true", "false", "yes", 1, 0, None])
def test_inbound_runtime_rejects_non_boolean_proxy_flags(invalid_flag):
    with pytest.raises(TypeError, match="must be a boolean"):
        InboundManager(
            inbound_http_base = "http://127.0.0.1:8080",
            inbound_ws_uri    = "",
            token             = "test_token",
            handler=AsyncMock(return_value=[]),
            trusted_tls_proxy=invalid_flag,
        )
    with pytest.raises(TypeError, match="must be a boolean"):
        InboundServer(
            "127.0.0.1",
            8080,
            "test_token",
            AsyncMock(return_value=[]),
            trusted_tls_proxy=invalid_flag,
        )
    with pytest.raises(ValueError, match="must be a boolean"):
        InboundManager.from_config(
            config={
                "enable_inbound_server": True,
                "inbound_http_base": "http://127.0.0.1:8080",
                "inbound_trusted_tls_proxy": invalid_flag,
            },
            token="test_token",
            handler=AsyncMock(return_value=[]),
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("http_base", "ws_uri"),
    [
        ("http://0.0.0.0:8080", ""),
        ("http://[::]:8080", ""),
        ("http://192.168.1.20:8080", ""),
        ("http://bot.internal:8080", ""),
        ("", "ws://0.0.0.0:8080/ws"),
        ("http://127.0.0.1:8080", "ws://192.168.1.20:8081/ws"),
    ],
)
def test_inbound_manager_rejects_all_non_loopback_plaintext_without_proxy(
    http_base,
    ws_uri,
):
    with pytest.raises(ValueError, match="non-loopback.*plaintext"):
        InboundManager(
            inbound_http_base = http_base,
            inbound_ws_uri    = ws_uri,
            token             = "test_token",
            handler=AsyncMock(return_value=[]),
        )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_direct_inbound_server_rejects_non_loopback_before_runner_setup():
    server = InboundServer(
        "0.0.0.0",
        8080,
        "test_token",
        AsyncMock(return_value=[]),
    )

    with patch("core.server.web.AppRunner") as app_runner:
        with pytest.raises(ValueError, match="non-loopback.*plaintext"):
            await server.start()

    app_runner.assert_not_called()
    assert server._runner is None
    assert server._site is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_direct_inbound_server_rejects_empty_token_before_runner_setup():
    server = InboundServer(
        "127.0.0.1",
        8080,
        "",
        AsyncMock(return_value=[]),
    )

    with patch("core.server.web.AppRunner") as app_runner:
        with pytest.raises(ValueError, match="non-empty inbound token"):
            await server.start()

    app_runner.assert_not_called()
    assert server._runner is None
    assert server._site is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inbound_manager_rejects_empty_token_before_starting_servers():
    manager = InboundManager(
        inbound_http_base = "http://127.0.0.1:8080",
        inbound_ws_uri    = "",
        token             = "",
        handler=AsyncMock(return_value=[]),
    )
    manager._start_servers = AsyncMock()

    with pytest.raises(ValueError, match="non-empty inbound token"):
        await manager.start()

    manager._start_servers.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_direct_proxy_server_enters_explicit_plaintext_tcpsite():
    server = InboundServer(
        "0.0.0.0",
        8080,
        "test_token",
        AsyncMock(return_value=[]),
        trusted_tls_proxy=True,
    )
    runner       = MagicMock()
    runner.setup = AsyncMock()
    site         = MagicMock()
    site.start   = AsyncMock()

    with (
        patch("core.server.web.AppRunner", return_value=runner),
        patch("core.server.web.TCPSite", return_value=site) as tcp_site,
    ):
        await server.start()

    runner.setup.assert_awaited_once()
    tcp_site.assert_called_once_with(runner, "0.0.0.0", 8080, ssl_context=None)
    site.start.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    ("http_base", "ws_uri", "expected_servers"),
    [
        ("http://0.0.0.0:8080", "ws://0.0.0.0:8080/ws", 1),
        ("http://0.0.0.0:8080", "", 1),
        ("", "ws://0.0.0.0:8080/ws", 1),
        ("http://0.0.0.0:8080", "ws://0.0.0.0:8081/ws", 2),
    ],
)
async def test_inbound_manager_propagates_proxy_acknowledgement_to_every_server(
    http_base,
    ws_uri,
    expected_servers,
):
    manager = InboundManager(
        inbound_http_base = http_base,
        inbound_ws_uri    = ws_uri,
        token             = "test_token",
        handler=AsyncMock(return_value=[]),
        ws_broadcast_timeout_seconds = 2.5,
        trusted_tls_proxy            = True,
    )
    created = []

    def build_server(*args, **kwargs):
        server       = MagicMock()
        server.start = AsyncMock()
        created.append(server)
        return server

    with patch("core.server.InboundServer", side_effect=build_server) as server_cls:
        await manager.start()

    assert len(created) == expected_servers
    assert server_cls.call_count == expected_servers
    assert all(call.kwargs["trusted_tls_proxy"] is True for call in server_cls.call_args_list)
    assert all(
        call.kwargs["ws_broadcast_timeout_seconds"] == 2.5 for call in server_cls.call_args_list
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inbound_manager_broadcast():
    """Test InboundManager broadcast"""

    async def handler(event):
        return []

    manager = InboundManager(
        inbound_http_base = "",
        inbound_ws_uri    = "",
        token             = "test_token",
        handler           = handler,
    )

    # Mock servers
    manager.http_server                                    = MagicMock()
    manager.http_server.active_ws_connections.return_value = 2
    manager.http_server.broadcast                          = AsyncMock(
        return_value=BroadcastResult(target_count=2, success_count=1, failure_count=1)
    )

    manager.ws_server                                    = MagicMock()
    manager.ws_server.active_ws_connections.return_value = 1
    manager.ws_server.broadcast                          = AsyncMock(
        return_value=BroadcastResult(target_count=1, timeout_count=1)
    )

    result = await manager.broadcast({"action": "test"})

    # Both servers should have broadcast called
    manager.http_server.broadcast.assert_called_once()
    manager.ws_server.broadcast.assert_called_once()
    assert result == BroadcastResult(
        target_count  = 3,
        success_count = 1,
        failure_count = 1,
        timeout_count = 1,
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inbound_manager_broadcast_same_server():
    """Test InboundManager broadcast when both servers are the same"""

    async def handler(event):
        return []

    manager = InboundManager(
        inbound_http_base = "",
        inbound_ws_uri    = "",
        token             = "test_token",
        handler           = handler,
    )

    # Same server instance
    mock_server                                    = MagicMock()
    mock_server.active_ws_connections.return_value = 1
    mock_server.broadcast = AsyncMock(return_value=BroadcastResult(target_count=1, success_count=1))
    manager.http_server = mock_server
    manager.ws_server   = mock_server

    result = await manager.broadcast({"action": "test"})

    # Should only call once
    mock_server.broadcast.assert_called_once()
    assert result == BroadcastResult(target_count=1, success_count=1)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inbound_manager_broadcast_without_servers_returns_zero_result():
    manager = InboundManager(
        inbound_http_base = "",
        inbound_ws_uri    = "",
        token             = "test_token",
        handler=AsyncMock(return_value=[]),
    )

    assert await manager.broadcast({"action": "test"}) == BroadcastResult()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inbound_manager_broadcast_isolates_one_server_failure():
    manager = InboundManager(
        inbound_http_base = "",
        inbound_ws_uri    = "",
        token             = "test_token",
        handler=AsyncMock(return_value=[]),
    )
    failed                                    = MagicMock()
    failed.active_ws_connections.return_value = 2
    failed.broadcast = AsyncMock(side_effect=ConnectionError("server failed"))
    healthy                                    = MagicMock()
    healthy.active_ws_connections.return_value = 1
    healthy.broadcast = AsyncMock(return_value=BroadcastResult(target_count=1, success_count=1))
    manager.http_server = failed
    manager.ws_server   = healthy

    result = await manager.broadcast({"action": "test"})

    assert result == BroadcastResult(target_count=3, success_count=1, failure_count=2)
    failed.broadcast.assert_awaited_once()
    healthy.broadcast.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inbound_manager_broadcast_preserves_cancellation():
    manager = InboundManager(
        inbound_http_base = "",
        inbound_ws_uri    = "",
        token             = "test_token",
        handler=AsyncMock(return_value=[]),
    )
    server                                    = MagicMock()
    server.active_ws_connections.return_value = 1
    server.broadcast = AsyncMock(side_effect=asyncio.CancelledError)
    manager.ws_server = server

    with pytest.raises(asyncio.CancelledError):
        await manager.broadcast({"action": "test"})


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("fatal_kind", ["cancelled", "fatal"])
async def test_inbound_manager_child_fatal_cancels_and_drains_sibling(fatal_kind: str):
    class ChildFatal(BaseException):
        pass

    manager = InboundManager(
        inbound_http_base = "",
        inbound_ws_uri    = "",
        token             = "test_token",
        handler=AsyncMock(return_value=[]),
    )
    sibling_entered   = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    blocker           = asyncio.Event()

    async def slow_broadcast(_action: dict[str, Any]) -> BroadcastResult:
        sibling_entered.set()
        try:
            await blocker.wait()
        finally:
            sibling_cancelled.set()
        return BroadcastResult(target_count=1, success_count=1)

    async def fatal_broadcast(_action: dict[str, Any]) -> BroadcastResult:
        await sibling_entered.wait()
        if fatal_kind == "cancelled":
            raise asyncio.CancelledError
        raise ChildFatal("fatal server")

    slow_server                                     = MagicMock()
    slow_server.active_ws_connections.return_value  = 1
    slow_server.broadcast                           = slow_broadcast
    fatal_server                                    = MagicMock()
    fatal_server.active_ws_connections.return_value = 1
    fatal_server.broadcast                          = fatal_broadcast
    manager.http_server                             = slow_server
    manager.ws_server                               = fatal_server

    expected = asyncio.CancelledError if fatal_kind == "cancelled" else ChildFatal
    with pytest.raises(expected):
        await manager.broadcast({"action": "test"})

    assert sibling_cancelled.is_set()


@pytest.mark.unit
def test_inbound_manager_update_token():
    """Test InboundManager update_token"""

    async def handler(event):
        return []

    manager = InboundManager(
        inbound_http_base = "",
        inbound_ws_uri    = "",
        token             = "old_token",
        handler           = handler,
    )

    # Create mock servers
    manager.http_server = MagicMock()
    manager.ws_server   = MagicMock()

    manager.update_token("new_token")

    assert manager._token == "new_token"
    manager.http_server.update_token.assert_called_once_with("new_token")
    manager.ws_server.update_token.assert_called_once_with("new_token")


@pytest.mark.unit
def test_inbound_manager_update_token_deduplicates_shared_server_and_same_value():
    async def handler(event):
        return []

    manager = InboundManager(
        inbound_http_base = "",
        inbound_ws_uri    = "",
        token             = "old_token",
        handler           = handler,
    )
    shared_server       = MagicMock()
    manager.http_server = shared_server
    manager.ws_server   = shared_server

    manager.update_token("new_token")
    manager.update_token("new_token")

    assert manager._token == "new_token"
    shared_server.update_token.assert_called_once_with("new_token")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inbound_manager_factory_reentrant_rotation_cannot_publish_old_token():
    async def handler(event):
        return []

    manager = InboundManager(
        inbound_http_base = "http://127.0.0.1:8765",
        inbound_ws_uri    = "",
        token             = "old-token",
        handler           = handler,
    )
    server = MagicMock(start=AsyncMock())

    def build_server(_host, _port, token, _handler, **_kwargs):
        assert token == "old-token"
        manager.update_token("new-token")
        return server

    with patch("core.server.InboundServer", side_effect=build_server):
        await manager._start_servers()

    assert manager._token == "new-token"
    assert manager.http_server is server
    server.update_token.assert_called_once_with("new-token")
    server.start.assert_awaited_once()


@pytest.mark.unit
def test_inbound_manager_set_status_providers_updates_existing_servers():
    """Test InboundManager forwards status providers to active servers."""

    async def handler(event):
        return []

    manager = InboundManager(
        inbound_http_base = "",
        inbound_ws_uri    = "",
        token             = "test_token",
        handler           = handler,
    )
    manager.http_server = MagicMock()
    manager.ws_server   = MagicMock()
    plugins_count = Mock(return_value=5)
    sessions_count = Mock(return_value=2)
    pending_jobs = Mock(return_value=1)
    metrics = Mock(return_value={"ok": True})

    manager.set_status_providers(
        plugins_count  = plugins_count,
        sessions_count = sessions_count,
        pending_jobs   = pending_jobs,
        metrics        = metrics,
    )

    manager.http_server.set_status_providers.assert_called_once_with(
        plugins_count  = plugins_count,
        sessions_count = sessions_count,
        pending_jobs   = pending_jobs,
        metrics        = metrics,
    )
    manager.ws_server.set_status_providers.assert_called_once_with(
        plugins_count  = plugins_count,
        sessions_count = sessions_count,
        pending_jobs   = pending_jobs,
        metrics        = metrics,
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inbound_manager_applies_status_providers_when_servers_start():
    """Test providers bound before start are applied to created servers."""

    async def handler(event):
        return []

    created = []

    class FakeInboundServer:
        def __init__(self, *args, **kwargs):
            self.set_status_providers = Mock()
            self.commit_admission     = Mock()
            self.kwargs               = kwargs
            created.append(self)

        async def start(self, *, accept_events: bool = True):
            assert accept_events is False
            return None

        async def stop(self):
            return None

    manager = InboundManager(
        inbound_http_base = "http://localhost:8080",
        inbound_ws_uri    = "",
        token             = "test_token",
        handler           = handler,
    )
    plugins_count = Mock(return_value=5)

    manager.set_status_providers(plugins_count=plugins_count)

    with patch("core.server.InboundServer", FakeInboundServer):
        await manager.start()

    assert len(created) == 1
    created[0].set_status_providers.assert_called_once_with(
        plugins_count  = plugins_count,
        sessions_count = None,
        pending_jobs   = None,
        metrics        = None,
    )
    created[0].commit_admission.assert_called_once_with()
    await manager.stop()


@pytest.mark.unit
def test_parse_http_base_valid():
    """Test _parse_http_base with valid URLs"""
    assert _parse_http_base("http://localhost:8080") == ("localhost", 8080)
    assert _parse_http_base("http://127.0.0.1:3000") == ("127.0.0.1", 3000)
