"""记忆持久化和消息观察。"""

from __future__ import annotations

import json
import threading
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from core.interfaces import PluginCapabilities
from core.plugin_base import load_json as real_load_json
from plugins.xiaoqing_chat.chat_runtime import load_runtime
from plugins.xiaoqing_chat.llm.llm_config import LLMCallConfig
from plugins.xiaoqing_chat.store_binding import bound_state
from tests.helpers.xiaoqing_chat_test_support import (
    ScriptedChatModel,
    chat_message_event,
    message_parts_to_legacy,
    scripted_chat_context,
    seed_chat_history,
    settle_chat_background,
)

mock_context       = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event


@pytest.mark.asyncio
async def test_external_source_survives_reload_and_keeps_chat_turn_boundaries(tmp_path):
    from plugins.xiaoqing_chat.llm.local_checks import heuristic_check
    from plugins.xiaoqing_chat.llm.prompt_builder import build_dialogue_prompt
    from plugins.xiaoqing_chat.memory.memory import MemoryStore
    from plugins.xiaoqing_chat.planning.pfc_engine import _seconds_since_last_assistant

    store = MemoryStore(tmp_path)
    for index in range(3):
        store.append(
            "g1",
            role          = "assistant",
            name          = "小青",
            content       = f"自动通知 {index}",
            source_plugin = "earthquake",
            local_id      = f"notice-{index}",
            ts            = 100 + index,
        )
    store.persist("g1")
    loaded  = MemoryStore(tmp_path)
    history = await loaded.get_async("g1")

    assert all(message.source_plugin == "earthquake" for message in history)
    rendered = build_dialogue_prompt(history, bot_name="小青", truncate=False)
    assert "小青[插件通知:earthquake]" in rendered
    assert "小青(你)" not in rendered
    assert _seconds_since_last_assistant(history, now=110) == 9999.0
    assert (
        heuristic_check(
            reply                = "自动通知 2",
            history              = history,
            max_repeat_compare   = 2,
            similarity_threshold = 0.9,
            max_assistant_in_row = 3,
        )
        is None
    )

    loaded.append(
        "g1",
        role        = "assistant",
        name        = "小青",
        content     = "这天气真会挑日子",
        ts          = 108,
        message_ids = (100, 101),
    )
    loaded.persist("g1")
    history = await MemoryStore(tmp_path).get_async("g1")
    assert history[-1].message_id == 100
    assert history[-1].remote_ids() == (100, 101)
    assert _seconds_since_last_assistant(history, now=110) == 2.0
    assert (
        heuristic_check(
            reply                = "这天气真会挑日子",
            history              = history,
            max_repeat_compare   = 2,
            similarity_threshold = 0.9,
            max_assistant_in_row = 3,
        )
        is not None
    )


@pytest.mark.asyncio
async def test_memory_store_reloads_after_binding_data_dir(tmp_path):
    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    chat_id = "rebinding-load"
    store   = MemoryStore()

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

    store   = MemoryStore()
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
    store   = MemoryStore(tmp_path)
    store.append(
        chat_id,
        role        = "user",
        name        = "Tester",
        content     = "[图片：猫猫在发呆]",
        media_items = [
            {
                "kind"        : "image",
                "media_hash"  : "hash-cat",
                "marker"      : "[图片：猫猫在发呆]",
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
    store   = MemoryStore(tmp_path)
    store.append(
        chat_id,
        role        = "assistant",
        name        = "小青",
        content     = "[[xc_media_1]]",
        media_items = [
            {
                "kind"   : "qq_face",
                "face_id": "14",
                "marker" : "[QQ表情：微笑]",
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
    store   = MemoryStore(tmp_path)
    store.append(
        chat_id,
        role    = "assistant",
        name    = "小青",
        content = "",
        parts   = [
            {"kind": "text", "text": "先看这个"},
            {
                "kind"        : "emoji",
                "media_hash"  : "hash-emoji-1",
                "marker"      : "[表情包：猫猫翻白眼]",
                "description" : "猫猫翻白眼",
                "emotion_tags": ["无语"],
            },
            {"kind": "text", "text": "再说"},
            {
                "kind"   : "qq_face",
                "face_id": "277",
                "marker" : "[QQ表情：狗头]",
                "label"  : "狗头",
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
    store   = MemoryStore()
    store.append(
        chat_id,
        role        = "assistant",
        name        = "小青",
        content     = "旧内容[[xc_media_1]]",
        media_items = [
            {
                "kind"   : "qq_face",
                "face_id": "14",
                "marker" : "[QQ表情：微笑]",
            }
        ],
        parts=[
            {"kind": "text", "text": "先看这个"},
            {
                "kind"        : "emoji",
                "media_hash"  : "hash-emoji-1",
                "marker"      : "[表情包：猫猫翻白眼]",
                "description" : "猫猫翻白眼",
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
                    "role"       : "assistant",
                    "name"       : "小青",
                    "content"    : "旧内容[[xc_media_1]]",
                    "media_items": [
                        {
                            "kind"   : "qq_face",
                            "face_id": "14",
                            "marker" : "[QQ表情：微笑]",
                        }
                    ],
                    "parts": [
                        {"kind": "text", "text": "先看这个"},
                        {
                            "kind"        : "emoji",
                            "media_hash"  : "hash-emoji-1",
                            "marker"      : "[表情包：猫猫翻白眼]",
                            "description" : "猫猫翻白眼",
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


def _chat(mock_context, *config, model=None):
    """一个能跑真实记录 / 观察代码的聊天上下文：剧本模型 + 真实运行时状态。"""

    model   = model or ScriptedChatModel()
    context = scripted_chat_context(mock_context, model, *config)
    return context, model, bound_state(context)


@pytest.mark.asyncio
async def test_handle_internal_stats_reports_the_stored_history(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.commands import handle_internal

    context, model, state = _chat(mock_context)
    seed_chat_history(state, "g67890", 3)

    result = await handle_internal("统计", "", sample_group_event, context)

    text = result[0]["data"]["text"]
    assert "会话统计" in text and "上下文消息数: 3" in text
    await settle_chat_background(context, model)


@pytest.mark.asyncio
async def test_handle_internal_stats_reports_the_stored_action_history(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.commands import handle_internal
    from plugins.xiaoqing_chat.planning.action_history import ActionRecord

    context, model, state = _chat(mock_context)
    for index in range(2):
        state.action_history.append(
            "g67890",
            ActionRecord(
                ts           = time.time() - index,
                local_target = "",
                action       = "reply",
                reasoning    = "测试",
                detail       = {},
                executed     = True,
            ),
        )

    result = await handle_internal("统计", "", sample_group_event, context)

    text = result[0]["data"]["text"]
    assert "近期行动记录" in text and "近期行动记录: 2" in text
    await settle_chat_background(context, model)


@pytest.mark.asyncio
async def test_extract_and_learn_uses_async_memory_read(mock_context):
    from plugins.xiaoqing_chat.expression.bw_message_recorder import (
        MessageRecorder,
        extract_and_learn,
    )

    memory_store = MagicMock()
    memory_store.get_async = AsyncMock(return_value=[])
    memory_store.get.side_effect = AssertionError("sync memory read should not be used")
    expr_store                   = MagicMock()
    recorder                     = MessageRecorder()

    changed = await extract_and_learn(
        context              = mock_context,
        secrets              = {},
        bot_name             = "小青",
        chat_id              = "g1",
        memory_store         = memory_store,
        expr_store           = expr_store,
        jargon_store         = None,
        recorder             = recorder,
        personality          = MagicMock(),
        min_interval_seconds = 0.0,
        min_messages         = 10,
        max_store            = 2000,
        self_reflect         = True,
        llm                  = LLMCallConfig(
            temperature            = 0.7,
            top_p                  = 0.9,
            max_tokens             = 128,
            timeout_seconds        = 1.0,
            max_retry              = 0,
            retry_interval_seconds = 0.0,
        ),
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
            context              = mock_context,
            secrets              = {},
            bot_name             = "小青",
            chat_id              = "g1",
            memory_store         = memory_store,
            expr_store           = MagicMock(),
            jargon_store         = None,
            recorder             = recorder,
            personality          = MagicMock(),
            min_interval_seconds = 0.0,
            min_messages         = 10,
            max_store            = 2000,
            self_reflect         = True,
            llm                  = LLMCallConfig(
                temperature            = 0.7,
                top_p                  = 0.9,
                max_tokens             = 128,
                timeout_seconds        = 1.0,
                max_retry              = 0,
                retry_interval_seconds = 0.0,
            ),
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
        context              = mock_context,
        secrets              = {},
        bot_name             = "小青",
        chat_id              = "g1",
        memory_store         = memory_store,
        expr_store           = MagicMock(),
        jargon_store         = None,
        recorder             = recorder,
        personality          = MagicMock(),
        min_interval_seconds = 60.0,
        min_messages         = 10,
        max_store            = 2000,
        self_reflect         = True,
        llm                  = LLMCallConfig(
            temperature            = 0.7,
            top_p                  = 0.9,
            max_tokens             = 128,
            timeout_seconds        = 1.0,
            max_retry              = 0,
            retry_interval_seconds = 0.0,
        ),
    )

    assert changed == 0
    assert memory_store.get_async.await_count == 1
    assert recorder.get_last_time("g1") == 0.0


@pytest.mark.asyncio
async def test_extract_and_learn_jargon_empty_response_does_not_fail_task(mock_context):
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.expression.bw_expression_store import ExpressionStore
    from plugins.xiaoqing_chat.expression.bw_jargon_store import JargonStore
    from plugins.xiaoqing_chat.expression.bw_message_recorder import (
        MessageRecorder,
        extract_and_learn,
    )
    from plugins.xiaoqing_chat.llm.gateway import LLMError
    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    memory_store = MemoryStore()
    for idx in range(10):
        memory_store.append(
            "g1",
            role     = "user",
            name     = "User",
            content  = f"测试消息{idx}",
            local_id = f"m{idx + 1}",
            ts       = float(idx + 1),
        )

    def gateway_answer(payload) -> tuple[dict, str]:
        content = json.dumps(payload, ensure_ascii=False)
        return {"choices": [{"message": {"content": content}}]}, "test"

    # 表达学习：先学到两条，再各自通过审核；黑话挖掘的模型调用返回空响应。
    learned = [
        {"situation": "想轻松带过时", "style": "哈哈", "source_id": "m3"},
        {"situation": "想附和时", "style": "确实", "source_id": "m5"},
    ]
    approved         = {"checked": True, "rejected": False, "reason": "approved"}
    expression_model = AsyncMock(
        side_effect=[gateway_answer(learned), gateway_answer(approved), gateway_answer(approved)]
    )

    async def raise_empty_response(**_kwargs):
        raise LLMError("empty_response")

    recorder = MessageRecorder()
    with (
        patch(
            "plugins.xiaoqing_chat.expression.bw_expression_learner.chat_completions_raw_with_profile",
            new=expression_model,
        ),
        patch(
            "plugins.xiaoqing_chat.expression.bw_jargon_miner.chat_completions_raw_with_profile",
            new=raise_empty_response,
        ),
    ):
        changed = await extract_and_learn(
            context=mock_context,
            secrets={"api_base": "https://example.com", "api_key": "k", "model": "m"},
            bot_name="小青",
            chat_id="g1",
            memory_store=memory_store,
            expr_store=ExpressionStore(),
            jargon_store=JargonStore(),
            recorder=recorder,
            personality=XiaoQingChatConfig().personality,
            min_interval_seconds=0.0,
            min_messages=10,
            max_store=2000,
            self_reflect=True,
            llm=LLMCallConfig(
                temperature            = 0.7,
                top_p                  = 0.9,
                max_tokens             = 128,
                timeout_seconds        = 1.0,
                max_retry              = 0,
                retry_interval_seconds = 0.0,
            ),
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
    tracker       = ReflectTrackerState(
        operator_chat_id = "g1",
        expression_id    = "expr-1",
        created_time     = 1.0,
        last_check_count = 0,
    )
    tracker_store.get_trackers.return_value = [tracker]
    expr_store                              = MagicMock()
    expr_store.load.return_value            = []
    memory_store                            = MagicMock()
    memory_store.get_async = AsyncMock(return_value=[])
    memory_store.get.side_effect = AssertionError("sync memory read should not be used")

    with patch("plugins.xiaoqing_chat.expression.bw_reflect_tracker.time.time", return_value=10.0):
        result = await tick_reflect_tracker(
            operator_chat_id       = "g1",
            memory_store           = memory_store,
            expr_store             = expr_store,
            tracker_store          = tracker_store,
            secrets                = {},
            bot_name               = "小青",
            timeout_seconds        = 1.0,
            max_retry              = 0,
            retry_interval_seconds = 0.0,
            max_duration_seconds   = 900.0,
            max_message_count      = 30,
        )

    assert result is False
    assert memory_store.get_async.await_count == 1


@pytest.mark.asyncio
async def test_action_history_get_recent_async_loads_via_to_thread(tmp_path):
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore

    chat_id = "action-history-load"
    store   = ActionHistoryStore()
    store.bind(tmp_path)
    action_dir = tmp_path / "action_history"
    action_dir.mkdir(parents=True, exist_ok=True)
    (action_dir / f"{chat_id}.json").write_text(
        '[{"ts":1.0,"local_target":"u1","action":"reply","reasoning":"ok","detail":{},"executed":true}]',
        encoding="utf-8",
    )

    caller_thread  = threading.get_ident()
    loader_threads = []

    def recording_read(path, *args, **kwargs):
        loader_threads.append(threading.get_ident())
        return real_load_json(path, *args, **kwargs)

    # 记下读盘发生在哪个线程：冷加载必须在工作线程里读，不能卡住事件循环所在的线程。
    with patch("plugins.xiaoqing_chat.planning.action_history.load_json", new=recording_read):
        items = await store.get_recent_async(chat_id, max_items=10)
    assert len(loader_threads) == 1
    assert loader_threads[0] != caller_thread

    assert len(items) == 1
    assert items[0].action == "reply"


@pytest.mark.asyncio
async def test_build_tool_info_block_uses_async_action_history_read(mock_context):
    from plugins.xiaoqing_chat.context_builder import tool_status_context

    state                                            = MagicMock()
    state.get_last_reply_ts.return_value             = 0.0
    state.get_continuous_cooldown_until.return_value = 0.0
    state.get_reply_timestamps.return_value          = []
    state.get_continuous_reply_count.return_value    = 0
    state.action_history.get_recent_async = AsyncMock(return_value=[])

    block = await tool_status_context(
        state    = state,
        bot_name = "小青",
        chat_id  = "g1",
        event    = {"user_id": 1},
        goal     = "",
    )

    assert isinstance(block, str)
    assert state.action_history.get_recent_async.await_count == 1


@pytest.mark.asyncio
async def test_ensure_user_message_recorded_updates_heartflow(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.turn_generation import ensure_user_message_recorded

    context, model, state = _chat(mock_context)
    runtime = load_runtime(context)
    assert (await state.heartflow.get_async("g67890")).last_user_ts == 0.0

    local_id = await ensure_user_message_recorded(
        "你好", sample_group_event, context, runtime, state=state
    )

    assert local_id
    assert [message.local_id for message in state.memory_store.get("g67890")] == [local_id]
    assert (await state.heartflow.get_async("g67890")).last_user_ts > 0
    await settle_chat_background(context, model)


@pytest.mark.asyncio
async def test_observe_message_skips_prefixed_xc_command(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import observe_message

    context, model, state = _chat(mock_context)
    event = chat_message_event(sample_group_event, "/xc 你好")

    result = await observe_message("xc 你好", event, context)

    assert result == []
    assert state.memory_store.get("g67890") == []
    await settle_chat_background(context, model)


def _group_push(*segments: dict, group_id: int = 67890, **extra) -> dict:
    return {
        "action": "send_group_msg",
        "params": {"group_id": group_id, "message": list(segments)},
        **extra,
    }


@pytest.mark.asyncio
async def test_observe_outgoing_action_records_external_plugin_text_only(mock_context):
    from plugins.xiaoqing_chat.handlers import observe_outgoing_action

    context, model, state = _chat(mock_context)
    action = _group_push(
        {
            "type": "text",
            "data": {"text": "中国地震台网正式测定：04月24日11时18分在地中海东部发生5.7级地震。"},
        },
        {"type": "image", "data": {"file": "file:///tmp/quake.jpg"}},
    )

    result = await observe_outgoing_action(action, context, source_plugin="earthquake")

    assert result == []
    history = state.memory_store.get("g67890")
    assert len(history) == 1
    message = history[0]
    assert message.role == "assistant"
    assert message.name == "小青"
    assert message.source_plugin == "earthquake"
    content, media_items = message_parts_to_legacy(message.parts)
    assert "地中海东部" in content
    assert "5.7级地震" in content
    assert "[图片" not in content
    assert media_items == []
    assert (await state.heartflow.get_async("g67890")).last_bot_ts == 0.0
    # 全文和配图另存进推送记录，她主动开口时看得到
    [push] = state.push_log.since("g67890", 0.0)
    assert push.source == "earthquake" and push.local_id == message.local_id
    assert "5.7级地震" in push.text and push.images == ("/tmp/quake.jpg",)
    await settle_chat_background(context, model)


@pytest.mark.asyncio
async def test_long_agent_results_are_kept_shortened_but_other_long_output_keeps_a_headline(
    mock_context,
):
    """agent 的结果是小青说的话：截短记下，群友只引用它接着说时也能认出来。

    其他插件的长推送只记开头一段：知道群里刚推送过什么，长篇正文不进上下文。
    """

    from core.interfaces import ACTION_RESULT_MESSAGE_ID_KEY
    from plugins.xiaoqing_chat.handlers import observe_outgoing_action

    # agent 的结果送出后，聊天会在后台从中抽取生活记录，那是一次模型调用。
    context, model, state = _chat(mock_context, model=ScriptedChatModel(reasoning="[]"))

    def long_result() -> dict:
        return _group_push(
            {"type": "at", "data": {"qq": "30003"}},
            {"type": "text", "data": {"text": " "}},
            {"type": "text", "data": {"text": "今日推荐\n" + "结果" * 800}},
            **{ACTION_RESULT_MESSAGE_ID_KEY: 4242},
        )

    await observe_outgoing_action(long_result(), context, source_plugin="arxiv_filter")
    headline, _media = message_parts_to_legacy(state.memory_store.get("g67890")[-1].parts)
    assert headline.startswith("今日推荐 结果结果") and headline.endswith(
        "（内容较长，只记下开头）"
    )
    assert len(headline) < 200
    # 对话里只记开头，推送记录里是全文
    [logged] = state.push_log.since("g67890", 0.0)
    assert logged.text.startswith("今日推荐") and len(logged.text) > 1500
    await observe_outgoing_action(long_result(), context, source_plugin="xiaoqing_agent")
    assert len(state.push_log.since("g67890", 0.0)) == 1  # agent 的结果是她自己说的话，不算推送

    last = state.memory_store.get("g67890")[-1]
    assert last.message_id == 4242
    assert last.source_plugin == "xiaoqing_agent"
    content, _media = message_parts_to_legacy(last.parts)
    assert "结果结果" in content and len(content) <= 1000
    await settle_chat_background(context, model)


@pytest.mark.asyncio
async def test_observe_outgoing_action_skips_sensitive_external_plugin_output(mock_context):
    from plugins.xiaoqing_chat.handlers import observe_outgoing_action

    context, model, state = _chat(mock_context)
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

    result = await observe_outgoing_action(action, context, source_plugin="pendo")

    assert result == []
    assert state.memory_store.get("u123456789") == []
    assert state.push_log.since("u123456789", 0.0) == []
    await settle_chat_background(context, model)


@pytest.mark.asyncio
async def test_observe_outgoing_action_skips_xiaoqing_source(mock_context):
    from plugins.xiaoqing_chat.handlers import observe_outgoing_action

    context, model, state = _chat(mock_context)
    action = _group_push({"type": "text", "data": {"text": "这是小青自己回复的内容"}})

    result = await observe_outgoing_action(action, context, source_plugin="xiaoqing_chat")

    assert result == []
    assert state.memory_store.get("g67890") == []
    await settle_chat_background(context, model)


@pytest.mark.asyncio
async def test_ensure_user_message_recorded_persists_rendered_media_items(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.media.models import RenderedMedia
    from plugins.xiaoqing_chat.turn_generation import ensure_user_message_recorded

    context, model, state = _chat(mock_context)
    runtime                                        = load_runtime(context)
    sample_group_event["_xc_rendered_media_items"] = [
        RenderedMedia(
            kind         = "image",
            media_hash   = "hash-cat",
            description  = "猫猫在发呆",
            emotion_tags = ("发呆",),
            marker       = "[图片：猫猫在发呆]",
            cached_path  = context.data_dir / "cat.png",
        )
    ]

    await ensure_user_message_recorded(
        "[图片：猫猫在发呆]", sample_group_event, context, runtime, state=state
    )

    parts = state.memory_store.get("g67890")[-1].parts
    content, media_items = message_parts_to_legacy(parts)
    assert content == "[[xc_media_1]]"
    assert media_items[0]["media_hash"] == "hash-cat"
    assert media_items[0]["marker"] == "[图片：猫猫在发呆]"
    assert media_items[0]["media_key"] == "media:hash-cat"
    assert [part["kind"] for part in parts] == ["image"]
    assert "description" not in media_items[0]
    assert "emotion_tags" not in media_items[0]
    # 媒体登记表在收尾时落盘：这张图的描述进了登记表，下次重启后还在。
    await settle_chat_background(context, model)
    registry = json.loads((context.data_dir / "media" / "index.json").read_text(encoding="utf-8"))
    assert registry["entries"]["media:hash-cat"]["description"] == "猫猫在发呆"


@pytest.mark.asyncio
async def test_ensure_user_message_recorded_reuses_cached_effective_parts(mock_context):
    from plugins.xiaoqing_chat.media.event_media import RenderedMedia, build_effective_user_text
    from plugins.xiaoqing_chat.turn_generation import ensure_user_message_recorded

    context, model, state = _chat(mock_context)
    runtime               = load_runtime(context)
    event: dict[str, Any] = {
        "post_type"   : "message",
        "message_type": "group",
        "user_id"     : 12345,
        "group_id"    : 67890,
        "message_id"  : 1,
        "message"     : [
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "image", "data": {"url": "https://example.com/cat.png"}},
            {"type": "text", "data": {"text": "笑死"}},
        ],
        "_xc_rendered_media_items": [
            RenderedMedia(
                media_hash   = "hash-cat",
                kind         = "image",
                description  = "猫猫在发呆",
                emotion_tags = (),
                marker       = "[图片：猫猫在发呆]",
                cached_path  = context.data_dir / "cat.png",
            )
        ],
    }

    text = await build_effective_user_text(
        "看这个笑死",
        event,
        context = context,
        runtime = runtime,
    )
    # 渲染好的媒体清单拿走：要是记录时重新拼一遍，图片就会丢，只有缓存的 parts 还带着它。
    event["_xc_rendered_media_items"] = []

    await ensure_user_message_recorded(text, event, context, runtime, state=state)

    parts = state.memory_store.get("g67890")[-1].parts
    content, media_items = message_parts_to_legacy(parts)
    assert text == "看这个\n[图片：猫猫在发呆]\n笑死"
    assert [part["kind"] for part in event["_xc_effective_user_parts"]] == ["text", "image", "text"]
    assert [part["kind"] for part in parts] == ["text", "image", "text"]
    assert content == "看这个[[xc_media_1]]笑死"
    assert media_items[0]["media_hash"] == "hash-cat"
    assert media_items[0]["media_key"] == "media:hash-cat"
    await settle_chat_background(context, model)


@pytest.mark.asyncio
async def test_fetch_pfc_knowledge_preserves_planner_intent(tmp_path):
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.memory.memory import MemoryStore
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore
    from plugins.xiaoqing_chat.planning.pfc_engine import run_pfc_once
    from plugins.xiaoqing_chat.planning.pfc_state import PFCConversationState
    from tests.helpers.xiaoqing_chat_test_support import plan_answer

    captured: dict = {}

    class RecordingMemory:
        async def search(self, payload):
            captured.update(payload)
            return {"items": [{"type": "note", "text": "retrieved memory"}]}

    chat_id              = "chat-1"
    context              = MagicMock()
    context.data_dir     = tmp_path
    context.http_session = AsyncMock()
    context.capabilities = PluginCapabilities(memory=RecordingMemory())  # type: ignore[arg-type]
    memory_store = MemoryStore()
    memory_store.append(chat_id, role="user", name="用户", content="把刚才那个偏好找出来")
    state = PFCConversationState(chat_id)
    generate_reply = AsyncMock(return_value="找到了")
    model = ScriptedChatModel(
        plan=[
            plan_answer("fetch_knowledge", "需要核对用户刚才提过的偏好"),
            plan_answer("direct_reply", "memory loaded"),
        ]
    )

    result = await run_pfc_once(
        context        = context,
        runtime_cfg    = XiaoQingChatConfig(),
        secrets        = {"_ai": model, "_route": "reasoning"},
        bot_name       = "小青",
        is_private     = False,
        chat_id        = chat_id,
        current_text   = "把刚才那个偏好找出来",
        memory_store   = memory_store,
        action_history = ActionHistoryStore(),
        state          = state,
        generate_reply = generate_reply,
    )

    assert result.reason == "memory loaded" and result.reply == "找到了"
    assert "需要核对用户刚才提过的偏好" in captured["query"] and captured["scope"] == chat_id
    assert len(state.knowledge_list) == 1
    assert state.knowledge_list[0]["text"] == "- retrieved memory"
    assert not model.unscripted
