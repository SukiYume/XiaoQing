"""事件处理和消息投递。"""

from __future__ import annotations

import logging

import tests.helpers.app_test_support as _fixture_support
from core.constants import MAX_MESSAGE_TEXT_LENGTH
from tests.helpers.app_test_support import (
    AsyncMock,
    BroadcastResult,
    MagicMock,
    Mock,
    OneBotActionOutcomeUnknown,
    Path,
    SimpleNamespace,
    XiaoQingApp,
    asyncio,
    pytest,
)

mock_dependencies = _fixture_support.mock_dependencies
temp_app_root     = _fixture_support.temp_app_root


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inbound_event_uses_same_long_message_splitter_as_active_delivery(
    temp_app_root: Path,
):
    app                = XiaoQingApp(temp_app_root)
    image_segment      = {"type": "image", "data": {"file": "test.png"}}
    original_text      = "A" * (MAX_MESSAGE_TEXT_LENGTH + 17)
    app._process_event = AsyncMock(
        return_value={
            "action": "send_group_msg",
            "params": {
                "group_id": 67890,
                "message": [
                    {"type": "text", "data": {"text": original_text}},
                    image_segment,
                ],
            },
        }
    )

    actions = await app._handle_inbound_event(
        {
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 67890,
        }
    )

    assert len(actions) == 2
    messages = [action["params"]["message"] for action in actions]
    assert all(
        sum(len(seg["data"]["text"]) for seg in message if seg.get("type") == "text")
        <= MAX_MESSAGE_TEXT_LENGTH
        for message in messages
    )
    flattened = [segment for message in messages for segment in message]
    assert (
        "".join(segment["data"]["text"] for segment in flattened if segment.get("type") == "text")
        == original_text
    )
    assert [segment for segment in flattened if segment.get("type") == "image"] == [image_segment]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_send_single_action_logs_full_logical_message_length(
    temp_app_root: Path,
    caplog,
):
    app     = XiaoQingApp(temp_app_root)
    message = [
        {"type": "text", "data": {"text": "A" * 300}},
        *({"type": "text", "data": {"text": "B"}} for _ in range(20)),
    ]

    with caplog.at_level(logging.INFO, logger="core.app_delivery"):
        await app._send_single_action(
            {"action": "send_group_msg", "params": {"group_id": 1, "message": message}}
        )

    records = [record for record in caplog.records if record.name == "core.app_delivery"]
    assert any(record.getMessage().endswith("message_length=320") for record in records)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_process_event(temp_app_root: Path):
    """Test _process_event processes event through dispatcher"""
    app = XiaoQingApp(temp_app_root)

    # Mock dispatcher
    app.dispatcher.handle_event = AsyncMock(
        return_value=[{"type": "text", "data": {"text": "test"}}]
    )

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
    }

    result = await app._process_event(event)

    assert result is not None
    assert "action" in result
    assert result["action"] in ("send_group_msg", "send_private_msg")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_process_event_no_response(temp_app_root: Path):
    """Test _process_event with no response from dispatcher"""
    app = XiaoQingApp(temp_app_root)

    # Mock dispatcher to return empty
    app.dispatcher.handle_event = AsyncMock(return_value=[])

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
    }

    result = await app._process_event(event)

    assert result is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_handle_upstream_event(temp_app_root: Path):
    """Test _handle_upstream_event"""
    app = XiaoQingApp(temp_app_root)

    # Mock ws_client
    app.ws_client                     = MagicMock()
    app.ws_client.credentials_trusted = True
    app.ws_client.connected = Mock(return_value=True)
    app.ws_client.send_action = AsyncMock()

    # Mock dispatcher
    app.dispatcher.handle_event = AsyncMock(
        return_value=[{"type": "text", "data": {"text": "test"}}]
    )

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
    }

    await app._handle_upstream_event(event)

    # Verify action was sent
    app.ws_client.send_action.assert_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_handle_upstream_event_not_connected(temp_app_root: Path):
    """Test _handle_upstream_event falls back when WS is not connected"""
    app = XiaoQingApp(temp_app_root)

    # Mock ws_client as not connected
    app.ws_client = MagicMock()
    app.ws_client.connected = Mock(return_value=False)
    app.ws_client.send_action = AsyncMock()
    app.http_sender           = SimpleNamespace(
        http_base           = "http://onebot",
        credentials_trusted = True,
        send_action         = AsyncMock(),
    )

    # Mock dispatcher
    app.dispatcher.handle_event = AsyncMock(
        return_value=[{"type": "text", "data": {"text": "test"}}]
    )

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
    }

    await app._handle_upstream_event(event)

    # Verify fallback delivery was used
    app.ws_client.send_action.assert_not_called()
    app.http_sender.send_action.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_handle_inbound_event(temp_app_root: Path):
    """Test _handle_inbound_event returns actions"""
    app = XiaoQingApp(temp_app_root)

    # Mock dispatcher
    app.dispatcher.handle_event = AsyncMock(
        return_value=[{"type": "text", "data": {"text": "test"}}]
    )

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
    }

    result = await app._handle_inbound_event(event)

    assert isinstance(result, list)
    assert len(result) > 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_handle_inbound_event_with_source(temp_app_root: Path):
    """Test _handle_inbound_event sets source correctly"""
    app = XiaoQingApp(temp_app_root)

    received_events = []

    async def mock_handle(event):
        received_events.append(event)
        return []

    app.dispatcher.handle_event = mock_handle

    event = {"test": "data"}
    await app._handle_inbound_event(event)

    assert len(received_events) == 1
    assert received_events[0].get("_source") == "inbound_http"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_deduplicates_message_id_across_inbound_transports(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    app.dispatcher.handle_event = AsyncMock(return_value=[])
    event = {
        "post_type": "message",
        "message_type": "private",
        "self_id": 10000,
        "user_id": 12345,
        "message_id": 99,
        "message": [{"type": "text", "data": {"text": "hello"}}],
    }

    await app._handle_upstream_event(event)
    await app._handle_inbound_event(event)

    app.dispatcher.handle_event.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_does_not_deduplicate_events_without_message_id(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    app.dispatcher.handle_event = AsyncMock(return_value=[])
    event = {"post_type": "message", "message_type": "private", "user_id": 12345, "message": "same"}

    await app._handle_upstream_event(event)
    await app._handle_inbound_event(event)

    assert app.dispatcher.handle_event.await_count == 2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_dedupe_key_includes_group_and_user_scope(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    app.dispatcher.handle_event = AsyncMock(return_value=[])
    base = {
        "post_type": "message",
        "message_type": "group",
        "self_id": 10000,
        "user_id": 1,
        "group_id": 10,
        "message_id": 99,
        "message": "hello",
    }

    await app._handle_inbound_event(base)
    await app._handle_inbound_event({**base, "group_id": 20})
    await app._handle_inbound_event({**base, "user_id": 2})
    await app._handle_inbound_event(dict(base))

    assert app.dispatcher.handle_event.await_count == 3


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_dedupe_hot_path_never_scans_live_key_map(
    temp_app_root: Path,
    monkeypatch,
):
    class NoScanDict(dict):
        def __iter__(self):
            raise AssertionError("dedupe hot path must not iterate the live key map")

        def items(self):
            raise AssertionError("dedupe hot path must not scan every live key")

    app                   = XiaoQingApp(temp_app_root)
    app._recent_event_ids = NoScanDict()
    monkeypatch.setattr("core.app_delivery.MAX_INBOUND_EVENT_DEDUP_KEYS", 2)
    now = 100.0
    monkeypatch.setattr("core.app_delivery.time.monotonic", lambda: now)

    for message_id in (1, 2, 3):
        assert await app._claim_inbound_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 7,
                "message_id": message_id,
            }
        )

    assert len(app._recent_event_ids) == 2
    assert len(app._event_dedupe_expirations) == 2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_dedupe_heap_prunes_only_expired_entries(
    temp_app_root: Path,
    monkeypatch,
):
    app = XiaoQingApp(temp_app_root)
    now = 100.0
    monkeypatch.setattr("core.app_delivery.INBOUND_EVENT_DEDUP_TTL_SECONDS", 5.0)
    monkeypatch.setattr("core.app_delivery.time.monotonic", lambda: now)

    first  = {"message_id": 1, "user_id": 7, "message_type": "private"}
    second = {"message_id": 2, "user_id": 7, "message_type": "private"}
    assert await app._claim_inbound_event(first)
    now = 102.0
    assert await app._claim_inbound_event(second)
    now = 105.0

    assert await app._claim_inbound_event(first)
    assert await app._claim_inbound_event(second) is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_send_single_action_falls_back_to_http_when_inbound_has_no_ws_clients(
    temp_app_root: Path,
):
    """Test inbound manager does not swallow actions when no inbound WS clients are connected."""
    app                 = XiaoQingApp(temp_app_root)
    app.inbound_manager = MagicMock()
    app.inbound_manager.has_active_ws_clients = Mock(return_value=False)
    app.inbound_manager.broadcast       = AsyncMock()
    app.http_sender                     = MagicMock()
    app.http_sender.http_base           = "http://localhost:5700"
    app.http_sender.credentials_trusted = True
    app.http_sender.send_action         = AsyncMock()

    action = {"action": "send_group_msg", "params": {"group_id": 1, "message": []}}
    await app._send_single_action(action)

    app.inbound_manager.broadcast.assert_not_called()
    app.http_sender.send_action.assert_awaited_once_with(action)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_send_single_action_accepts_partial_inbound_broadcast_success(
    temp_app_root: Path,
):
    app                                                    = XiaoQingApp(temp_app_root)
    app.inbound_manager                                    = MagicMock()
    app.inbound_manager.has_active_ws_clients.return_value = True
    app.inbound_manager.broadcast                          = AsyncMock(
        return_value=BroadcastResult(
            target_count=3, success_count=1, failure_count=1, timeout_count=1
        )
    )
    app.http_sender                     = MagicMock()
    app.http_sender.http_base           = "http://localhost:5700"
    app.http_sender.credentials_trusted = True
    app.http_sender.send_action = AsyncMock(return_value=True)
    action = {"action": "send_group_msg", "params": {"group_id": 1, "message": []}}

    assert await app._send_single_action(action) is True

    app.inbound_manager.broadcast.assert_awaited_once_with(action)
    app.http_sender.send_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    "broadcast_result",
    [
        BroadcastResult(),
        BroadcastResult(target_count=2, failure_count=2),
    ],
)
async def test_app_send_single_action_falls_back_when_inbound_delivers_to_nobody(
    temp_app_root: Path,
    broadcast_result: BroadcastResult,
):
    app                                                    = XiaoQingApp(temp_app_root)
    app.inbound_manager                                    = MagicMock()
    app.inbound_manager.has_active_ws_clients.return_value = True
    app.inbound_manager.broadcast = AsyncMock(return_value=broadcast_result)
    app.http_sender                     = MagicMock()
    app.http_sender.http_base           = "http://localhost:5700"
    app.http_sender.credentials_trusted = True
    app.http_sender.send_action = AsyncMock(return_value=True)
    action = {"action": "send_group_msg", "params": {"group_id": 1, "message": []}}

    assert await app._send_single_action(action) is True

    app.inbound_manager.broadcast.assert_awaited_once_with(action)
    app.http_sender.send_action.assert_awaited_once_with(action)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_send_single_action_preserves_unknown_inbound_timeout(
    temp_app_root: Path,
):
    app                                                    = XiaoQingApp(temp_app_root)
    app.inbound_manager                                    = MagicMock()
    app.inbound_manager.has_active_ws_clients.return_value = True
    app.inbound_manager.broadcast                          = AsyncMock(
        return_value=BroadcastResult(target_count=2, timeout_count=2)
    )
    app.http_sender                     = MagicMock()
    app.http_sender.http_base           = "http://localhost:5700"
    app.http_sender.credentials_trusted = True
    app.http_sender.send_action = AsyncMock(return_value=True)
    action = {"action": "send_group_msg", "params": {"group_id": 1, "message": []}}

    assert await app._send_single_action(action) is None

    app.inbound_manager.broadcast.assert_awaited_once_with(action)
    app.http_sender.send_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_send_single_action_falls_back_after_inbound_broadcast_exception(
    temp_app_root: Path,
):
    app                                                    = XiaoQingApp(temp_app_root)
    app.inbound_manager                                    = MagicMock()
    app.inbound_manager.has_active_ws_clients.return_value = True
    app.inbound_manager.broadcast = AsyncMock(side_effect=ConnectionError("broadcast failed"))
    app.http_sender                     = MagicMock()
    app.http_sender.http_base           = "http://localhost:5700"
    app.http_sender.credentials_trusted = True
    app.http_sender.send_action = AsyncMock(return_value=True)
    action = {"action": "send_group_msg", "params": {"group_id": 1, "message": []}}

    assert await app._send_single_action(action) is True

    app.http_sender.send_action.assert_awaited_once_with(action)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_send_single_action_returns_false_when_zero_inbound_delivery_has_no_fallback(
    temp_app_root: Path,
):
    app                                                    = XiaoQingApp(temp_app_root)
    app.inbound_manager                                    = MagicMock()
    app.inbound_manager.has_active_ws_clients.return_value = True
    app.inbound_manager.broadcast = AsyncMock(return_value=BroadcastResult())
    app.http_sender = None
    action          = {"action": "send_group_msg", "params": {"group_id": 1, "message": []}}

    assert await app._send_single_action(action) is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_send_single_action_does_not_mutate_bypass_action(temp_app_root: Path):
    """Test internal _bypass_sink marker is stripped from delivery copy only."""
    app                 = XiaoQingApp(temp_app_root)
    app.inbound_manager = MagicMock()
    app.inbound_manager.has_active_ws_clients = Mock(return_value=False)
    app.http_sender                     = MagicMock()
    app.http_sender.http_base           = "http://localhost:5700"
    app.http_sender.credentials_trusted = True
    app.http_sender.send_action         = AsyncMock()

    action = {
        "action": "send_group_msg",
        "params": {"group_id": 1, "message": []},
        "_bypass_sink": True,
    }
    await app._send_single_action(action)

    assert action["_bypass_sink"] is True
    sent_action = app.http_sender.send_action.await_args.args[0]
    assert "_bypass_sink" not in sent_action


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_send_single_action_falls_back_to_http_when_ws_send_fails(temp_app_root: Path):
    """Test WS send failures fall through to HTTP sender."""
    app                               = XiaoQingApp(temp_app_root)
    app.ws_client                     = MagicMock()
    app.ws_client.credentials_trusted = True
    app.ws_client.connected = Mock(return_value=True)
    app.ws_client.send_action = AsyncMock(return_value=False)
    app.http_sender                     = MagicMock()
    app.http_sender.http_base           = "http://localhost:5700"
    app.http_sender.credentials_trusted = True
    app.http_sender.send_action         = AsyncMock()

    action = {"action": "send_group_msg", "params": {"group_id": 1, "message": []}}
    await app._send_single_action(action)

    app.ws_client.send_action.assert_awaited_once_with(action)
    app.http_sender.send_action.assert_awaited_once_with(action)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_send_does_not_fallback_after_committed_ws_outcome_unknown(
    temp_app_root: Path,
):
    app           = XiaoQingApp(temp_app_root)
    app.ws_client = SimpleNamespace(
        credentials_trusted = True,
        connected           = lambda: True,
        send_action=AsyncMock(side_effect=OneBotActionOutcomeUnknown("send_group_msg")),
    )
    app.inbound_manager = SimpleNamespace(
        has_active_ws_clients = lambda: True,
        broadcast             = AsyncMock(),
    )
    app.http_sender = SimpleNamespace(
        http_base="http://onebot",
        send_action=AsyncMock(return_value=True),
    )
    action = {"action": "send_group_msg", "params": {"group_id": 1, "message": []}}

    assert await app._send_single_action(action) is None

    app.ws_client.send_action.assert_awaited_once_with(action)
    app.inbound_manager.broadcast.assert_not_awaited()
    app.http_sender.send_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_wait_ws_does_not_retry_or_fallback_after_outcome_unknown(
    temp_app_root: Path,
):
    app           = XiaoQingApp(temp_app_root)
    app.ws_client = SimpleNamespace(
        credentials_trusted=True,
        connected=Mock(side_effect=[False, True]),
        send_action=AsyncMock(side_effect=OneBotActionOutcomeUnknown("send_group_msg")),
    )
    app.inbound_manager = None
    app.http_sender     = SimpleNamespace(
        http_base="http://onebot",
        send_action=AsyncMock(return_value=True),
    )
    action = {"action": "send_group_msg", "params": {"group_id": 1, "message": []}}

    assert await app._send_single_action(action, wait_ws_seconds=1.0) is None

    app.ws_client.send_action.assert_awaited_once_with(action)
    app.http_sender.send_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_wait_ws_follows_a_trusted_client_rotation(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    old_send = AsyncMock(return_value=True)
    new_send = AsyncMock(return_value=True)
    old_client = SimpleNamespace(
        credentials_trusted = True,
        connected           = lambda: False,
        send_action         = old_send,
    )
    new_client = SimpleNamespace(
        credentials_trusted = True,
        connected           = lambda: True,
        send_action         = new_send,
    )
    app.ws_client = old_client
    action        = {"action": "send_group_msg", "params": {"group_id": 1, "message": []}}

    sending = asyncio.create_task(app._send_single_action(action, wait_ws_seconds=0.3))
    await asyncio.sleep(0.03)
    app.ws_client = new_client

    assert await sending is True
    old_send.assert_not_awaited()
    new_send.assert_awaited_once_with(action)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_http_fallback_does_not_dereference_or_use_a_holder_detached_during_gate(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    send = AsyncMock(return_value=True)

    class DetachingHttpHolder:
        http_base   = "http://onebot"
        send_action = send

        @property
        def credentials_trusted(self) -> bool:
            app.http_sender = None
            return True

    app.http_sender = DetachingHttpHolder()  # type: ignore[assignment]
    action          = {"action": "send_group_msg", "params": {"group_id": 1, "message": []}}

    assert await app._send_single_action(action) is False
    send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_send_action_propagates_onebot_business_rejection(temp_app_root: Path):
    """Plugin callers can distinguish an acknowledged send from a OneBot rejection."""
    app                       = XiaoQingApp(temp_app_root)
    app.http_sender           = MagicMock()
    app.http_sender.http_base = "http://localhost:5700"
    app.http_sender.send_action = AsyncMock(return_value=False)
    app.plugin_manager.get = Mock(return_value=None)

    action = {"action": "send_group_msg", "params": {"group_id": 1, "message": []}}
    assert await app._send_action(action) is False

    context = app._build_plugin_context("test", Path("/test"), Path("/test"), {})
    assert await context.send_action(action) is False
