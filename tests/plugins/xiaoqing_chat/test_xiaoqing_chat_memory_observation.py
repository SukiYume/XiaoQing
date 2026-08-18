"""记忆持久化和消息观察。"""

from __future__ import annotations

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from tests.helpers.xiaoqing_chat_test_support import (
    AsyncMock,
    MagicMock,
    Mock,
    SimpleNamespace,
    _complete_test_runtime_config,
    _make_hctx,
    asyncio,
    message_parts_to_legacy,
    patch,
    pytest,
    xiaoqing_chat,
)

mock_context = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event


@pytest.mark.asyncio
async def test_memory_store_reloads_after_binding_data_dir(tmp_path):
    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    chat_id = "rebinding-load"
    store = MemoryStore()

    assert await store.get_async(chat_id) == []

    store.bind_data_dir(tmp_path)
    (tmp_path / f"{chat_id}.json").write_text(
        '[{"role":"user","name":"Tester","content":"restored","ts":1.0}]',
        encoding="utf-8",
    )

    history = await store.get_async(chat_id)

    assert len(history) == 1
    assert history[0].content == "restored"


def test_memory_store_append_prunes_in_memory_history():
    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    store = MemoryStore()
    chat_id = "bounded-history"

    for index in range(250):
        store.append(chat_id, role="user", name="Tester", content=f"msg-{index}")

    history = store.get(chat_id)
    assert len(history) == 200
    assert history[0].content == "msg-50"
    assert history[-1].content == "msg-249"


def test_memory_store_persists_media_items(tmp_path):
    import json

    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    chat_id = "media-history"
    store = MemoryStore(tmp_path)
    store.append(
        chat_id,
        role="user",
        name="Tester",
        content="[图片：猫猫在发呆]",
        media_items=[
            {
                "kind": "image",
                "media_hash": "hash-cat",
                "marker": "[图片：猫猫在发呆]",
                "emotion_tags": ["发呆"],
            }
        ],
    )
    store.persist(chat_id)

    persisted = json.loads((tmp_path / f"{chat_id}.json").read_text(encoding="utf-8"))

    reloaded = MemoryStore(tmp_path).get(chat_id)

    assert len(reloaded) == 1
    assert "parts" in persisted[0]
    assert "content" not in persisted[0]
    assert "media_items" not in persisted[0]
    assert reloaded[0].media_items[0]["media_hash"] == "hash-cat"
    assert reloaded[0].media_items[0]["marker"] == "[图片：猫猫在发呆]"


def test_memory_store_keeps_media_only_messages_on_reload(tmp_path):
    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    chat_id = "media-only-history"
    store = MemoryStore(tmp_path)
    store.append(
        chat_id,
        role="assistant",
        name="小青",
        content="[[xc_media_1]]",
        media_items=[
            {
                "kind": "qq_face",
                "face_id": "14",
                "marker": "[QQ表情：微笑]",
            }
        ],
    )
    store.persist(chat_id)

    reloaded = MemoryStore(tmp_path).get(chat_id)

    assert len(reloaded) == 1
    assert reloaded[0].content == "[[xc_media_1]]"
    assert reloaded[0].media_items[0]["face_id"] == "14"


def test_memory_store_persists_message_parts_round_trip(tmp_path):
    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    chat_id = "parts-round-trip"
    store = MemoryStore(tmp_path)
    store.append(
        chat_id,
        role="assistant",
        name="小青",
        content="",
        parts=[
            {"kind": "text", "text": "先看这个"},
            {
                "kind": "emoji",
                "media_hash": "hash-emoji-1",
                "marker": "[表情包：猫猫翻白眼]",
                "description": "猫猫翻白眼",
                "emotion_tags": ["无语"],
            },
            {"kind": "text", "text": "再说"},
            {
                "kind": "qq_face",
                "face_id": "277",
                "marker": "[QQ表情：狗头]",
                "label": "狗头",
            },
        ],
    )
    store.persist(chat_id)

    reloaded = MemoryStore(tmp_path).get(chat_id)

    assert len(reloaded) == 1
    assert reloaded[0].content == "先看这个[[xc_media_1]]再说[[xc_media_2]]"
    assert [part["kind"] for part in reloaded[0].parts] == ["text", "emoji", "text", "qq_face"]
    assert reloaded[0].media_items[0]["media_hash"] == "hash-emoji-1"
    assert reloaded[0].media_items[1]["face_id"] == "277"


def test_memory_store_append_prefers_canonical_parts_over_stale_legacy_fields():
    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    chat_id = "parts-first-append"
    store = MemoryStore()
    store.append(
        chat_id,
        role="assistant",
        name="小青",
        content="旧内容[[xc_media_1]]",
        media_items=[
            {
                "kind": "qq_face",
                "face_id": "14",
                "marker": "[QQ表情：微笑]",
            }
        ],
        parts=[
            {"kind": "text", "text": "先看这个"},
            {
                "kind": "emoji",
                "media_hash": "hash-emoji-1",
                "marker": "[表情包：猫猫翻白眼]",
                "description": "猫猫翻白眼",
                "emotion_tags": ["无语"],
            },
            {"kind": "text", "text": "再说"},
        ],
    )

    history = store.get(chat_id)

    assert len(history) == 1
    assert history[0].content == "先看这个[[xc_media_1]]再说"
    assert history[0].media_items[0]["media_hash"] == "hash-emoji-1"
    assert history[0].media_items[0]["marker"] == "[表情包：猫猫翻白眼]"


def test_memory_store_load_prefers_canonical_parts_over_stale_legacy_fields(tmp_path):
    import json

    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    chat_id = "parts-first-load"
    (tmp_path / f"{chat_id}.json").write_text(
        json.dumps(
            [
                {
                    "role": "assistant",
                    "name": "小青",
                    "content": "旧内容[[xc_media_1]]",
                    "media_items": [
                        {
                            "kind": "qq_face",
                            "face_id": "14",
                            "marker": "[QQ表情：微笑]",
                        }
                    ],
                    "parts": [
                        {"kind": "text", "text": "先看这个"},
                        {
                            "kind": "emoji",
                            "media_hash": "hash-emoji-1",
                            "marker": "[表情包：猫猫翻白眼]",
                            "description": "猫猫翻白眼",
                            "emotion_tags": ["无语"],
                        },
                        {"kind": "text", "text": "再说"},
                    ],
                    "ts": 1.0,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    reloaded = MemoryStore(tmp_path).get(chat_id)

    assert len(reloaded) == 1
    assert reloaded[0].content == "先看这个[[xc_media_1]]再说"
    assert reloaded[0].media_items[0]["media_hash"] == "hash-emoji-1"
    assert reloaded[0].media_items[0]["marker"] == "[表情包：猫猫翻白眼]"


@pytest.mark.asyncio
async def test_handle_internal_stats_uses_async_memory_read(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import handle_internal

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            brain_chat=SimpleNamespace(
                enable_private_brain_chat=False,
                brain_max_context_size=30,
                brain_think_level=2,
                brain_temperature=0.7,
            ),
            memory=SimpleNamespace(enable_memory_retrieval=True, top_k=5, min_score=0.1),
            expression=SimpleNamespace(
                enable_expression_learning=True, max_injected=5, max_store=200
            ),
            reply_probability_base=0.6,
            min_reply_interval_seconds=12.0,
            max_replies_per_minute=6,
            max_context_size=30,
        )
    )

    state = MagicMock()
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get.side_effect = AssertionError("sync memory read should not be used")
    state.bw_expr_store.load.return_value = []
    state.bw_jargon_store.load.return_value = {}
    state.action_history.get_recent_async = AsyncMock(return_value=[])
    state.get_stats.return_value = {"replies": 1, "resets": 0}

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx):
        result = await handle_internal("统计", "", sample_group_event, mock_context)

    assert state.memory_store.get_async.await_count == 1
    assert "会话统计" in result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_handle_internal_stats_uses_async_action_history_read(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import handle_internal

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            brain_chat=SimpleNamespace(
                enable_private_brain_chat=False,
                brain_max_context_size=30,
                brain_think_level=2,
                brain_temperature=0.7,
            ),
            memory=SimpleNamespace(enable_memory_retrieval=True, top_k=5, min_score=0.1),
            expression=SimpleNamespace(
                enable_expression_learning=True, max_injected=5, max_store=200
            ),
            reply_probability_base=0.6,
            min_reply_interval_seconds=12.0,
            max_replies_per_minute=6,
            max_context_size=30,
        )
    )

    state = MagicMock()
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.bw_expr_store.load.return_value = []
    state.bw_jargon_store.load.return_value = {}
    state.action_history.get_recent_async = AsyncMock(return_value=[])
    state.get_stats.return_value = {"replies": 1, "resets": 0}

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx):
        result = await handle_internal("统计", "", sample_group_event, mock_context)

    assert state.action_history.get_recent_async.await_count == 1
    assert "近期行动记录" in result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_extract_and_learn_uses_async_memory_read(mock_context):
    from plugins.xiaoqing_chat.expression.bw_message_recorder import (
        MessageRecorder,
        extract_and_learn,
    )

    memory_store = MagicMock()
    memory_store.get_async = AsyncMock(return_value=[])
    memory_store.get.side_effect = AssertionError("sync memory read should not be used")
    expr_store = MagicMock()
    recorder = MessageRecorder()

    changed = await extract_and_learn(
        context=mock_context,
        secrets={},
        bot_name="小青",
        chat_id="g1",
        memory_store=memory_store,
        expr_store=expr_store,
        jargon_store=None,
        recorder=recorder,
        personality=MagicMock(),
        min_interval_seconds=0.0,
        min_messages=10,
        self_reflect=True,
        temperature=0.7,
        top_p=0.9,
        max_tokens=128,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
    )

    assert changed == 0
    assert memory_store.get_async.await_count == 1


@pytest.mark.asyncio
async def test_extract_and_learn_skips_when_same_chat_inflight(mock_context):
    from plugins.xiaoqing_chat.expression.bw_message_recorder import (
        MessageRecorder,
        extract_and_learn,
    )

    memory_store = MagicMock()
    memory_store.get_async = AsyncMock(side_effect=AssertionError("inflight run should skip early"))
    recorder = MessageRecorder()
    assert recorder.try_begin("g1") is True

    try:
        changed = await extract_and_learn(
            context=mock_context,
            secrets={},
            bot_name="小青",
            chat_id="g1",
            memory_store=memory_store,
            expr_store=MagicMock(),
            jargon_store=None,
            recorder=recorder,
            personality=MagicMock(),
            min_interval_seconds=0.0,
            min_messages=10,
            self_reflect=True,
            temperature=0.7,
            top_p=0.9,
            max_tokens=128,
            timeout_seconds=1.0,
            max_retry=0,
            retry_interval_seconds=0.0,
        )
    finally:
        recorder.end("g1")

    assert changed == 0
    assert memory_store.get_async.await_count == 0


@pytest.mark.asyncio
async def test_extract_and_learn_resets_future_watermark_after_clock_rollback(
    mock_context, monkeypatch
):
    from plugins.xiaoqing_chat.expression import bw_message_recorder
    from plugins.xiaoqing_chat.expression.bw_message_recorder import (
        MessageRecorder,
        extract_and_learn,
    )

    recorder = MessageRecorder()
    recorder.bind(mock_context.data_dir)
    recorder.set_last_time("g1", 200.0)
    monkeypatch.setattr(bw_message_recorder.time, "time", lambda: 100.0)
    memory_store = MagicMock()
    memory_store.get_async = AsyncMock(return_value=[])

    changed = await extract_and_learn(
        context=mock_context,
        secrets={},
        bot_name="小青",
        chat_id="g1",
        memory_store=memory_store,
        expr_store=MagicMock(),
        jargon_store=None,
        recorder=recorder,
        personality=MagicMock(),
        min_interval_seconds=60.0,
        min_messages=10,
        self_reflect=True,
        temperature=0.7,
        top_p=0.9,
        max_tokens=128,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
    )

    assert changed == 0
    assert memory_store.get_async.await_count == 1
    assert recorder.get_last_time("g1") == 0.0


@pytest.mark.asyncio
async def test_extract_and_learn_jargon_empty_response_does_not_fail_task(
    mock_context, monkeypatch
):
    from plugins.xiaoqing_chat.expression.bw_expression_store import ExpressionStore
    from plugins.xiaoqing_chat.expression.bw_jargon_store import JargonStore
    from plugins.xiaoqing_chat.expression.bw_message_recorder import (
        MessageRecorder,
        extract_and_learn,
    )
    from plugins.xiaoqing_chat.llm.llm_client import LLMError
    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    memory_store = MemoryStore()
    for idx in range(10):
        memory_store.append(
            "g1",
            role="user",
            name="User",
            content=f"测试消息{idx}",
            local_id=f"m{idx + 1}",
            ts=float(idx + 1),
        )

    async def fake_learn_from_messages(**_kwargs):
        return [{"text": "哈哈", "description": "轻松口语"}]

    async def fake_upsert_learned(**_kwargs):
        return 2

    async def raise_empty_response(**_kwargs):
        raise LLMError("empty_response")

    monkeypatch.setattr(
        "plugins.xiaoqing_chat.expression.bw_message_recorder.learn_from_messages",
        fake_learn_from_messages,
    )
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.expression.bw_message_recorder.upsert_learned",
        fake_upsert_learned,
    )
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.expression.bw_jargon_miner.chat_completions_raw_with_fallback_paths",
        raise_empty_response,
    )

    recorder = MessageRecorder()
    changed = await extract_and_learn(
        context=mock_context,
        secrets={"api_base": "https://example.com", "api_key": "k", "model": "m"},
        bot_name="小青",
        chat_id="g1",
        memory_store=memory_store,
        expr_store=ExpressionStore(),
        jargon_store=JargonStore(),
        recorder=recorder,
        personality=MagicMock(),
        min_interval_seconds=0.0,
        min_messages=10,
        self_reflect=True,
        temperature=0.7,
        top_p=0.9,
        max_tokens=128,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
    )

    assert changed == 2
    assert recorder.get_last_time("g1") > 0
    assert recorder.try_begin("g1") is True
    recorder.end("g1")


@pytest.mark.asyncio
async def test_tick_reflect_tracker_uses_async_memory_read(mock_context):
    from plugins.xiaoqing_chat.expression.bw_reflect_tracker import (
        ReflectTrackerState,
        tick_reflect_tracker,
    )

    tracker_store = MagicMock()
    tracker = ReflectTrackerState(
        operator_chat_id="g1",
        expression_id="expr-1",
        created_time=1.0,
        last_check_count=0,
    )
    tracker_store.get_trackers.return_value = [tracker]
    expr_store = MagicMock()
    expr_store.load.return_value = []
    memory_store = MagicMock()
    memory_store.get_async = AsyncMock(return_value=[])
    memory_store.get.side_effect = AssertionError("sync memory read should not be used")

    with patch("plugins.xiaoqing_chat.expression.bw_reflect_tracker.time.time", return_value=10.0):
        result = await tick_reflect_tracker(
            operator_chat_id="g1",
            memory_store=memory_store,
            expr_store=expr_store,
            tracker_store=tracker_store,
            secrets={},
            timeout_seconds=1.0,
            max_retry=0,
            retry_interval_seconds=0.0,
            max_duration_seconds=900.0,
            max_message_count=30,
        )

    assert result is False
    assert memory_store.get_async.await_count == 1


@pytest.mark.asyncio
async def test_action_history_get_recent_async_loads_via_to_thread(tmp_path):
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore

    chat_id = "action-history-load"
    store = ActionHistoryStore()
    store.bind(tmp_path)
    action_dir = tmp_path / "action_history"
    action_dir.mkdir(parents=True, exist_ok=True)
    (action_dir / f"{chat_id}.json").write_text(
        '[{"ts":1.0,"local_target":"u1","action":"reply","reasoning":"ok","detail":{},"executed":true}]',
        encoding="utf-8",
    )

    items = await store.get_recent_async(chat_id, max_items=10)

    assert len(items) == 1
    assert items[0].action == "reply"


@pytest.mark.asyncio
async def test_build_tool_info_block_uses_async_action_history_read(mock_context):
    from plugins.xiaoqing_chat.context_builder import _build_tool_info_block

    state = MagicMock()
    state.get_last_reply_ts.return_value = 0.0
    state.get_continuous_cooldown_until.return_value = 0.0
    state.get_reply_timestamps.return_value = []
    state.get_continuous_reply_count.return_value = 0
    state.action_history.get_recent_async = AsyncMock(return_value=[])

    block = await _build_tool_info_block(
        state=state,
        data_dir=mock_context.data_dir,
        bot_name="小青",
        chat_id="g1",
        event={"user_id": 1},
        goal="",
    )

    assert isinstance(block, str)
    assert state.action_history.get_recent_async.await_count == 1


@pytest.mark.asyncio
async def test_ensure_user_message_recorded_uses_async_heartflow(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import _ensure_user_message_recorded

    runtime = MagicMock()
    state = MagicMock()
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_user_message.side_effect = AssertionError(
        "sync heartflow update should not be used"
    )

    with (
        patch("plugins.xiaoqing_chat.handlers._state", return_value=state),
        patch("plugins.xiaoqing_chat.handlers._bind_all_stores"),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
    ):
        local_id = await _ensure_user_message_recorded(
            "你好", sample_group_event, mock_context, runtime
        )

    assert local_id
    assert state.heartflow.on_user_message_async.await_count == 1


@pytest.mark.asyncio
async def test_observe_message_skips_prefixed_xc_command(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import observe_message

    runtime = SimpleNamespace(cfg=SimpleNamespace(enable_smalltalk=True))
    event = dict(sample_group_event)
    event["message"] = [{"type": "text", "data": {"text": "/xc 你好"}}]
    event["raw_message"] = "/xc 你好"

    with (
        patch("plugins.xiaoqing_chat.handlers._load_runtime", return_value=runtime),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(),
        ) as mock_record,
    ):
        result = await observe_message("xc 你好", event, mock_context)

    assert result == []
    mock_record.assert_not_awaited()


@pytest.mark.asyncio
async def test_observe_outgoing_action_records_external_plugin_text_only(mock_context):
    from plugins.xiaoqing_chat.handlers import observe_outgoing_action

    await xiaoqing_chat.init(mock_context)
    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            ban_words=[],
        ),
        compiled_ban_regex=[],
    )
    _complete_test_runtime_config(runtime)
    state = MagicMock()
    state.memory_store.append = Mock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.action_history.append = Mock()
    action = {
        "action": "send_group_msg",
        "params": {
            "group_id": 67890,
            "message": [
                {
                    "type": "text",
                    "data": {
                        "text": "中国地震台网正式测定：04月24日11时18分在地中海东部发生5.7级地震。"
                    },
                },
                {"type": "image", "data": {"file": "file:///tmp/quake.jpg"}},
            ],
        },
    }

    with (
        patch("plugins.xiaoqing_chat.handlers._load_runtime", return_value=runtime),
        patch("plugins.xiaoqing_chat.handlers._get_bound_state", return_value=state),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=asyncio.Lock()),
        patch("plugins.xiaoqing_chat.handlers._next_local_id", return_value="m-out"),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
    ):
        result = await observe_outgoing_action(action, mock_context, source_plugin="earthquake")

    assert result == []
    append_kwargs = state.memory_store.append.call_args.kwargs
    assert state.memory_store.append.call_args.args[0] == "g67890"
    assert append_kwargs["role"] == "assistant"
    assert append_kwargs["name"] == "小青"
    content, media_items = message_parts_to_legacy(append_kwargs["parts"])
    assert "地中海东部" in content
    assert "5.7级地震" in content
    assert "[图片" not in content
    assert media_items == []
    state.heartflow.on_bot_reply_async.assert_awaited_once_with(chat_id="g67890")


@pytest.mark.asyncio
async def test_observe_outgoing_action_skips_sensitive_external_plugin_output(mock_context):
    from plugins.xiaoqing_chat.handlers import observe_outgoing_action

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            ban_words=[],
        ),
        compiled_ban_regex=[],
    )
    _complete_test_runtime_config(runtime)
    state = MagicMock()
    state.memory_store.append = Mock()
    action = {
        "action": "send_private_msg",
        "params": {
            "user_id": 123456789,
            "message": [
                {
                    "type": "text",
                    "data": {
                        "text": (
                            "🔑 Pendo Web 登录 Token\n"
                            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
                            "eyJzdWIiOiIxMjMifQ.signature"
                        )
                    },
                },
            ],
        },
    }

    with (
        patch("plugins.xiaoqing_chat.handlers._load_runtime", return_value=runtime),
        patch("plugins.xiaoqing_chat.handlers._get_bound_state", return_value=state),
    ):
        result = await observe_outgoing_action(action, mock_context, source_plugin="pendo")

    assert result == []
    state.memory_store.append.assert_not_called()


@pytest.mark.asyncio
async def test_observe_outgoing_action_skips_xiaoqing_source(mock_context):
    from plugins.xiaoqing_chat.handlers import observe_outgoing_action

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            ban_words=[],
        ),
        compiled_ban_regex=[],
    )
    state = MagicMock()
    action = {
        "action": "send_group_msg",
        "params": {
            "group_id": 67890,
            "message": [{"type": "text", "data": {"text": "这是小青自己回复的内容"}}],
        },
    }

    with (
        patch("plugins.xiaoqing_chat.handlers._load_runtime", return_value=runtime),
        patch("plugins.xiaoqing_chat.handlers._get_bound_state", return_value=state),
    ):
        result = await observe_outgoing_action(action, mock_context, source_plugin="xiaoqing_chat")

    assert result == []
    state.memory_store.append.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_user_message_recorded_persists_rendered_media_items(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _ensure_user_message_recorded
    from plugins.xiaoqing_chat.media.event_media_common import RenderedMedia

    runtime = MagicMock()
    state = MagicMock()
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.media_store.upsert_media_items = Mock(
        return_value=[
            {
                "kind": "image",
                "media_hash": "hash-cat",
                "media_key": "media:hash-cat",
                "marker": "[图片：猫猫在发呆]",
                "description": "猫猫在发呆",
                "emotion_tags": ["发呆"],
                "file_path": str(mock_context.data_dir / "cat.png"),
            }
        ]
    )
    state.media_store.is_dirty.return_value = True
    sample_group_event["_xc_rendered_media_items"] = [
        RenderedMedia(
            kind="image",
            media_hash="hash-cat",
            description="猫猫在发呆",
            emotion_tags=("发呆",),
            marker="[图片：猫猫在发呆]",
            cached_path=mock_context.data_dir / "cat.png",
        )
    ]

    with (
        patch("plugins.xiaoqing_chat.handlers._state", return_value=state),
        patch("plugins.xiaoqing_chat.handlers._bind_all_stores"),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_media_registry_flush") as mock_media_flush,
    ):
        await _ensure_user_message_recorded(
            "[图片：猫猫在发呆]", sample_group_event, mock_context, runtime
        )

    append_kwargs = state.memory_store.append.call_args.kwargs
    parts = append_kwargs["parts"]
    content, media_items = message_parts_to_legacy(parts)
    assert content == "[[xc_media_1]]"
    assert media_items[0]["media_hash"] == "hash-cat"
    assert media_items[0]["marker"] == "[图片：猫猫在发呆]"
    assert media_items[0]["media_key"] == "media:hash-cat"
    assert [part["kind"] for part in parts] == ["image"]
    assert "description" not in media_items[0]
    assert "emotion_tags" not in media_items[0]
    mock_media_flush.assert_called_once_with(mock_context, runtime)


@pytest.mark.asyncio
async def test_ensure_user_message_recorded_reuses_cached_effective_parts(mock_context):
    from plugins.xiaoqing_chat.handlers import _ensure_user_message_recorded
    from plugins.xiaoqing_chat.media.event_media import RenderedMedia, build_effective_user_text

    runtime = MagicMock()
    state = MagicMock()
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.review_store.cleanup_expired = Mock()
    state.set_last_observe_ts = Mock()
    state.media_store.upsert_media_items = Mock(
        return_value=[
            {
                "kind": "image",
                "media_hash": "hash-cat",
                "media_key": "media:hash-cat",
                "marker": "[图片：猫猫在发呆]",
                "description": "猫猫在发呆",
                "file_path": str(mock_context.data_dir / "cat.png"),
            }
        ]
    )
    state.media_store.is_dirty.return_value = False
    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
        "message_id": 1,
        "message": [
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "image", "data": {"url": "https://example.com/cat.png"}},
            {"type": "text", "data": {"text": "笑死"}},
        ],
        "_xc_rendered_media_items": [
            RenderedMedia(
                media_hash="hash-cat",
                kind="image",
                description="猫猫在发呆",
                emotion_tags=(),
                marker="[图片：猫猫在发呆]",
                cached_path=mock_context.data_dir / "cat.png",
            )
        ],
    }

    text = await build_effective_user_text(
        "看这个笑死",
        event,
        context=mock_context,
        runtime=runtime,
    )

    with (
        patch("plugins.xiaoqing_chat.handlers._state", return_value=state),
        patch("plugins.xiaoqing_chat.handlers._bind_all_stores"),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch(
            "plugins.xiaoqing_chat.handlers.build_message_parts",
            side_effect=AssertionError("cached effective parts should bypass rebuild"),
        ),
    ):
        await _ensure_user_message_recorded(text, event, mock_context, runtime)

    append_kwargs = state.memory_store.append.call_args.kwargs
    parts = append_kwargs["parts"]
    content, media_items = message_parts_to_legacy(parts)
    assert text == "看这个\n[图片：猫猫在发呆]\n笑死"
    assert [part["kind"] for part in event["_xc_effective_user_parts"]] == ["text", "image", "text"]
    assert [part["kind"] for part in parts] == ["text", "image", "text"]
    assert content == "看这个[[xc_media_1]]笑死"
    assert media_items[0]["media_hash"] == "hash-cat"
    assert media_items[0]["media_key"] == "media:hash-cat"


@pytest.mark.asyncio
async def test_run_pfc_once_uses_async_pfc_state_store(tmp_path):
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.memory.memory import MemoryStore
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PFCPlan
    from plugins.xiaoqing_chat.planning.pfc_engine import run_pfc_once

    chat_id = "pfc-async-state"
    cfg = XiaoQingChatConfig()
    context = MagicMock()
    context.data_dir = tmp_path
    context.http_session = AsyncMock()

    memory_store = MemoryStore()
    memory_store.append(chat_id, role="user", name="Tester", content="你好")
    action_history = ActionHistoryStore()
    memory_db = MagicMock()
    pfc_state_store = MagicMock()
    pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            ignore_until_ts=0.0,
            ended=False,
            last_successful_reply_action="",
            goal_list=[],
            knowledge_list=[],
            planner_fail_ts=[],
            planner_skip_until=0.0,
        )
    )
    pfc_state_store.get.side_effect = AssertionError("sync pfc state read should not be used")
    pfc_state_store.save_async = AsyncMock()
    pfc_state_store.save.side_effect = AssertionError("sync pfc state write should not be used")
    generate_reply = AsyncMock(return_value="ok")

    with patch(
        "plugins.xiaoqing_chat.planning.pfc_engine.plan_next_action",
        new=AsyncMock(
            return_value=PFCPlan(action="wait", reason="先等等", thinking="观察中", wait_seconds=20)
        ),
    ):
        result = await run_pfc_once(
            context=context,
            runtime_cfg=cfg,
            secrets={"api_base": "http://test", "api_key": "key", "model": "test-model"},
            bot_name="小青",
            is_private=False,
            chat_id=chat_id,
            current_text="你好",
            memory_store=memory_store,
            action_history=action_history,
            memory_db=memory_db,
            pfc_state_store=pfc_state_store,
            generate_reply=generate_reply,
        )

    assert result.action == "wait"
    assert pfc_state_store.get_async.await_count == 1


@pytest.mark.asyncio
async def test_fetch_pfc_knowledge_preserves_planner_intent(monkeypatch, tmp_path):
    from plugins.xiaoqing_chat.planning import pfc_engine
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PFCPlan

    captured: dict = {}

    async def build_memory_block(**kwargs):
        captured.update(kwargs)
        return "retrieved memory"

    next_plan = PFCPlan(action="direct_reply", reason="memory loaded")
    monkeypatch.setattr(pfc_engine, "build_memory_block", build_memory_block)
    monkeypatch.setattr(pfc_engine, "_plan_pfc_action", AsyncMock(return_value=next_plan))
    monkeypatch.setattr(pfc_engine, "_log_step", Mock())
    state = SimpleNamespace(knowledge_list=[])
    session = SimpleNamespace(
        context=SimpleNamespace(data_dir=tmp_path, http_session=None),
        runtime_cfg=SimpleNamespace(
            memory=SimpleNamespace(),
            temperature=0.2,
            top_p=0.8,
            max_tokens=200,
        ),
        chat_id="chat-1",
        memory_db=MagicMock(),
        secrets={"_ai": object()},
        bot_name="小青",
        history=[],
        current_text="把刚才那个偏好找出来",
        planner_timeout=1.0,
        state=state,
        dirty=False,
    )
    request = PFCPlan(action="fetch_knowledge", reason="需要核对用户刚才提过的偏好")

    result = await pfc_engine._fetch_pfc_knowledge(session, request)

    assert result is next_plan
    assert captured["planner_question"] == request.reason
    assert len(state.knowledge_list) == 1
    assert state.knowledge_list[0]["text"] == "retrieved memory"
    assert session.dirty is True
