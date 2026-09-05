"""带媒体回复和持久化。"""

from __future__ import annotations

import tests.helpers.xiaoqing_chat_media_test_support as _fixture_support
from tests.helpers.xiaoqing_chat_media_test_support import (
    AsyncMock,
    MagicMock,
    Mock,
    SimpleNamespace,
    _make_media_runtime,
    _reply_draft,
    _reply_draft_with_parts,
    _write_png,
    asyncio,
    message_parts_to_legacy,
    patch,
    pytest,
)

mock_context = _fixture_support.mock_context


@pytest.mark.asyncio
async def test_smalltalk_emoji_reply_returns_mixed_text_and_image_and_persists_marker(mock_context):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    runtime = _make_media_runtime(
        enable_inbound_media_context=False,
    )
    image_path = _write_png(mock_context.data_dir / "emoji_reply.png")

    state                               = MagicMock()
    state.get_mood_state.return_value   = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append             = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async    = AsyncMock()
    state.heartflow.on_no_reply_async     = AsyncMock()
    state.inc_stats                       = Mock()
    state.action_history.append           = Mock()

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
        "message_id": 1,
        "message": [{"type": "text", "data": {"text": "你好"}}],
        "_xc_command_forced": True,
    }
    hctx = SimpleNamespace(
        runtime  = runtime,
        state    = state,
        chat_id  = "g67890",
        bot_name = "小青",
        secrets  = {"api_base": "http://test", "api_key": "key", "model": "model"},
        data_dir = mock_context.data_dir,
    )
    emoji_marker = SimpleNamespace(
        kind="emoji",
        entry=SimpleNamespace(file_path=str(image_path), media_hash="hash-1"),
        marker    = "[表情包：无语]",
        reasoning = "emoji_tag:无语",
    )
    reply_draft = _reply_draft_with_parts(
        "懂了",
        (
            {"kind": "text", "text": "懂了"},
            {
                "kind": "emoji",
                "file_path": str(image_path),
                "media_hash": "hash-1",
                "marker": "[表情包：无语]",
            },
        ),
        media_marker=emoji_marker,
    )

    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=asyncio.Lock()),
        patch(
            "plugins.xiaoqing_chat.handlers.build_effective_user_text",
            new=AsyncMock(return_value="你好"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._generate_reply_draft",
            new=AsyncMock(return_value=reply_draft),
        ),
        patch("plugins.xiaoqing_chat.smalltalk_media_helpers.mark_emoji_used"),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("你好", event, mock_context)

    assert mock_context.send_action.await_count == 0
    assert [segment["type"] for segment in result] == ["text", "emoji"]
    assert result[0]["data"]["text"] == "懂了"
    await result.delivery_receipt.record(True)
    assistant_append = state.memory_store.append.call_args_list[-1]
    content, media_items = message_parts_to_legacy(assistant_append.kwargs["parts"])
    assert content == "懂了[[xc_media_1]]"
    assert media_items[0]["kind"] == "emoji"
    assert media_items[0]["media_hash"] == "hash-1"
    assert media_items[0]["marker"] == "[表情包：无语]"


@pytest.mark.asyncio
async def test_smalltalk_emoji_only_reply_returns_single_image_and_marker_memory(mock_context):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    runtime = _make_media_runtime(
        enable_inbound_media_context=False,
    )
    image_path = mock_context.data_dir / "media" / "library" / "emoji_only.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path = _write_png(image_path)

    state                               = MagicMock()
    state.get_mood_state.return_value   = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append             = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async    = AsyncMock()
    state.heartflow.on_no_reply_async     = AsyncMock()
    state.inc_stats                       = Mock()
    state.action_history.append           = Mock()

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
        "message_id": 1,
        "message": [{"type": "image", "data": {"url": "https://example.com/sticker.png"}}],
        "_xc_command_forced": True,
    }
    hctx = SimpleNamespace(
        runtime  = runtime,
        state    = state,
        chat_id  = "g67890",
        bot_name = "小青",
        secrets  = {"api_base": "http://test", "api_key": "key", "model": "model"},
        data_dir = mock_context.data_dir,
    )
    emoji_marker = SimpleNamespace(
        kind="emoji",
        entry=SimpleNamespace(file_path=str(image_path), media_hash="hash-1"),
        marker    = "[表情包：无语]",
        reasoning = "emoji_mode:emoji_only;emoji_tag:无语",
        mode      = "emoji_only",
    )
    reply_draft = _reply_draft_with_parts(
        "笑死",
        (
            {
                "kind": "emoji",
                "file_path": str(image_path),
                "media_hash": "hash-1",
                "marker": "[表情包：无语]",
                "mode": "emoji_only",
            },
        ),
        media_marker=emoji_marker,
    )

    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=asyncio.Lock()),
        patch(
            "plugins.xiaoqing_chat.handlers.build_effective_user_text",
            new=AsyncMock(return_value="[表情包：无语]"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._generate_reply_draft",
            new=AsyncMock(return_value=reply_draft),
        ),
        patch("plugins.xiaoqing_chat.smalltalk_media_helpers.mark_emoji_used"),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("", event, mock_context)

    assert mock_context.send_action.await_count == 0
    assert result[0]["type"] == "emoji"
    await result.delivery_receipt.record(True)
    assistant_append = state.memory_store.append.call_args_list[-1]
    content, media_items = message_parts_to_legacy(assistant_append.kwargs["parts"])
    assert content == "[[xc_media_1]]"
    assert media_items[0]["mode"] == "emoji_only"


@pytest.mark.asyncio
async def test_smalltalk_face_reply_returns_mixed_text_and_face_and_persists_marker(mock_context):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    runtime = _make_media_runtime(
        enable_inbound_media_context=False,
    )

    state                               = MagicMock()
    state.get_mood_state.return_value   = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append             = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async    = AsyncMock()
    state.heartflow.on_no_reply_async     = AsyncMock()
    state.inc_stats                       = Mock()
    state.action_history.append           = Mock()

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
        "message_id": 1,
        "message": [{"type": "text", "data": {"text": "你好"}}],
        "_xc_command_forced": True,
    }
    hctx = SimpleNamespace(
        runtime  = runtime,
        state    = state,
        chat_id  = "g67890",
        bot_name = "小青",
        secrets  = {"api_base": "http://test", "api_key": "key", "model": "model"},
        data_dir = mock_context.data_dir,
    )
    face_marker = SimpleNamespace(
        kind="qq_face",
        entry=SimpleNamespace(face_id="277"),
        marker    = "[QQ表情：狗头]",
        reasoning = "face_mode:text_with_face;face_label:狗头",
        mode      = "text_with_face",
    )
    reply_draft = _reply_draft_with_parts(
        "懂了",
        (
            {"kind": "text", "text": "懂了"},
            {
                "kind": "qq_face",
                "face_id": "277",
                "marker": "[QQ表情：狗头]",
                "mode": "text_with_face",
            },
        ),
        media_marker=face_marker,
    )

    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=asyncio.Lock()),
        patch(
            "plugins.xiaoqing_chat.handlers.build_effective_user_text",
            new=AsyncMock(return_value="你好"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._generate_reply_draft",
            new=AsyncMock(return_value=reply_draft),
        ),
        patch("plugins.xiaoqing_chat.smalltalk_media_helpers.mark_qq_face_used"),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("你好", event, mock_context)

    assert mock_context.send_action.await_count == 0
    assert [segment["type"] for segment in result] == ["text", "face"]
    assert result[0]["data"]["text"] == "懂了"
    assert result[1]["data"]["id"] == "277"
    await result.delivery_receipt.record(True)
    assistant_append = state.memory_store.append.call_args_list[-1]
    content, media_items = message_parts_to_legacy(assistant_append.kwargs["parts"])
    assert content == "懂了[[xc_media_1]]"
    assert media_items[0]["kind"] == "qq_face"
    assert media_items[0]["face_id"] == "277"


@pytest.mark.asyncio
async def test_smalltalk_reply_applies_only_one_outbound_media_plan(mock_context):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    runtime = _make_media_runtime(
        enable_inbound_media_context=False,
    )
    image_path = mock_context.data_dir / "media" / "library" / "mixed_reply.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path = _write_png(image_path)

    state                               = MagicMock()
    state.get_mood_state.return_value   = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append             = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async    = AsyncMock()
    state.heartflow.on_no_reply_async     = AsyncMock()
    state.inc_stats                       = Mock()
    state.action_history.append           = Mock()

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
        "message_id": 1,
        "message": [{"type": "text", "data": {"text": "你好"}}],
        "_xc_command_forced": True,
    }
    hctx = SimpleNamespace(
        runtime  = runtime,
        state    = state,
        chat_id  = "g67890",
        bot_name = "小青",
        secrets  = {"api_base": "http://test", "api_key": "key", "model": "model"},
        data_dir = mock_context.data_dir,
    )
    emoji_marker = SimpleNamespace(
        kind  = "emoji",
        entry = SimpleNamespace(
            file_path    = str(image_path),
            media_hash   = "hash-1",
            description  = "无语",
            emotion_tags = ("无语",),
        ),
        marker    = "[表情包：无语]",
        reasoning = "emoji_mode:text_with_emoji;emoji_tag:无语",
        mode      = "text_with_emoji",
    )
    reply_draft = _reply_draft_with_parts(
        "懂了\n你看这个\n再说",
        (
            {"kind": "text", "text": "懂了"},
            {
                "kind": "emoji",
                "file_path": str(image_path),
                "media_hash": "hash-1",
                "marker": "[表情包：无语]",
                "description": "无语",
                "emotion_tags": ["无语"],
                "mode": "text_with_emoji",
            },
            {"kind": "text", "text": "\n你看这个\n再说"},
        ),
        media_marker=emoji_marker,
    )

    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=asyncio.Lock()),
        patch(
            "plugins.xiaoqing_chat.handlers.build_effective_user_text",
            new=AsyncMock(return_value="你好"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._generate_reply_draft",
            new=AsyncMock(return_value=reply_draft),
        ),
        patch("plugins.xiaoqing_chat.smalltalk_media_helpers.mark_emoji_used"),
        patch("plugins.xiaoqing_chat.smalltalk_media_helpers.mark_qq_face_used"),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("你好", event, mock_context)

    assert mock_context.send_action.await_count == 0
    assert [segment["type"] for segment in result] == ["text", "emoji", "text"]
    assert result[0]["data"]["text"] == "懂了"
    assert result[2]["data"]["text"] == "你看这个\n再说"
    await result.delivery_receipt.record(True)
    assistant_append = state.memory_store.append.call_args_list[-1]
    content, media_items = message_parts_to_legacy(assistant_append.kwargs["parts"])
    assert content == "懂了[[xc_media_1]]\n你看这个\n再说"
    assert [item["kind"] for item in media_items] == ["emoji"]
    assert [part["kind"] for part in assistant_append.kwargs["parts"]] == ["text", "emoji", "text"]
    assert media_items[0]["media_hash"] == "hash-1"


@pytest.mark.asyncio
async def test_smalltalk_face_only_reply_returns_single_face_and_marker_memory(mock_context):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    runtime = _make_media_runtime(
        enable_inbound_media_context=False,
    )

    state                               = MagicMock()
    state.get_mood_state.return_value   = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append             = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async    = AsyncMock()
    state.heartflow.on_no_reply_async     = AsyncMock()
    state.inc_stats                       = Mock()
    state.action_history.append           = Mock()

    event = {
        "post_type": "message",
        "message_type": "private",
        "user_id": 12345,
        "message_id": 1,
        "message": [{"type": "text", "data": {"text": "你好"}}],
        "_xc_command_forced": True,
    }
    hctx = SimpleNamespace(
        runtime  = runtime,
        state    = state,
        chat_id  = "u12345",
        bot_name = "小青",
        secrets  = {"api_base": "http://test", "api_key": "key", "model": "model"},
        data_dir = mock_context.data_dir,
    )
    face_marker = SimpleNamespace(
        kind="qq_face",
        entry=SimpleNamespace(face_id="14"),
        marker    = "[QQ表情：微笑]",
        reasoning = "face_mode:face_only;face_label:微笑",
        mode      = "face_only",
    )
    reply_draft = _reply_draft_with_parts(
        "懂了",
        (
            {
                "kind": "qq_face",
                "face_id": "14",
                "marker": "[QQ表情：微笑]",
                "mode": "face_only",
            },
        ),
        media_marker=face_marker,
    )

    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=asyncio.Lock()),
        patch(
            "plugins.xiaoqing_chat.handlers.build_effective_user_text",
            new=AsyncMock(return_value="你好"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._generate_reply_draft",
            new=AsyncMock(return_value=reply_draft),
        ),
        patch("plugins.xiaoqing_chat.smalltalk_media_helpers.mark_qq_face_used"),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("你好", event, mock_context)

    assert mock_context.send_action.await_count == 0
    assert result[0]["type"] == "face"
    assert result[0]["data"]["id"] == "14"
    await result.delivery_receipt.record(True)
    assistant_append = state.memory_store.append.call_args_list[-1]
    content, media_items = message_parts_to_legacy(assistant_append.kwargs["parts"])
    assert content == "[[xc_media_1]]"
    assert media_items[0]["mode"] == "face_only"


@pytest.mark.asyncio
async def test_smalltalk_does_not_force_reply_when_new_emoji_collected(mock_context):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    runtime = _make_media_runtime(
        enable_inbound_media_context=True,
    )
    state                               = MagicMock()
    state.get_mood_state.return_value   = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append             = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async    = AsyncMock()
    state.heartflow.on_no_reply_async     = AsyncMock()
    state.inc_stats                       = Mock()
    state.action_history.append           = Mock()

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
        "message_id": 1,
        "message": [{"type": "image", "data": {"url": "https://example.com/sticker.png"}}],
        "_xc_new_emoji_count": 1,
    }
    hctx = SimpleNamespace(
        runtime  = runtime,
        state    = state,
        chat_id  = "g67890",
        bot_name = "小青",
        secrets  = {"api_base": "http://test", "api_key": "key", "model": "model"},
        data_dir = mock_context.data_dir,
    )

    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=asyncio.Lock()),
        patch(
            "plugins.xiaoqing_chat.handlers.build_effective_user_text",
            new=AsyncMock(return_value="[表情包：无语]"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=False)),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._generate_reply_draft",
            new=AsyncMock(return_value=_reply_draft("好图，收了")),
        ),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("", event, mock_context)

    assert result == []
    assert state.heartflow.on_no_reply_async.await_count == 1
