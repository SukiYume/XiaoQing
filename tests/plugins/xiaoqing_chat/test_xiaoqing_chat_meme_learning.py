"""Verify scoped meme learning, corrections, contextual use and reset races."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins.xiaoqing_chat.commands import reset_chat_session as _reset_chat_session
from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
from plugins.xiaoqing_chat.context_builder import expression_context, jargon_context
from plugins.xiaoqing_chat.expression.bw_expression_learner import LearnedExpression, upsert_learned
from plugins.xiaoqing_chat.expression.bw_expression_store import ExpressionRecord
from plugins.xiaoqing_chat.expression.bw_jargon_miner import mine_jargon
from plugins.xiaoqing_chat.expression.bw_jargon_store import JargonRecord
from plugins.xiaoqing_chat.expression.bw_message_recorder import extract_and_learn
from plugins.xiaoqing_chat.llm.llm_config import LLMCallConfig
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.runtime_state import ChatRuntime, ChatRuntimeState
from plugins.xiaoqing_chat.store_binding import bind_all_stores

MODEL = {
    "secrets": {},
    "llm": LLMCallConfig(
        temperature            = 0,
        top_p                  = 1,
        max_tokens             = 512,
        timeout_seconds        = 1,
        max_retry              = 0,
        retry_interval_seconds = 0,
    ),
}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("我俩私聊处理，群里先不用跟进。", True),
        ("我只想先安静待会儿，大家聊吧。", True),
        ("小青，这条不用回复。", True),
        ("小青，这条不用回了。", True),
        ("小青，不用回宿舍，来食堂找我。", False),
        ("他说‘别回我’，我有点疑惑。", False),
        ("不用回复上一题，帮我算这道新题。", False),
        ("不用回复是什么意思？", False),
    ],
)
def test_explicit_silence_respects_quotes_and_new_requests(text, expected):
    from plugins.xiaoqing_chat.llm.communication_constraints import requests_silence

    assert requests_silence(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("白矮星和中子星有什么区别，用两句话说清。", 2),
        ("请用三句解释", 3),
        ("天为什么是蓝的？两句话，给小朋友听的。", 2),
        ("两句话，语气轻松一点。", 2),
        ("解释天空的颜色，用三句话，尽量好懂。", 3),
        ("一句话就行。", 1),
        ("先用两句话说清，再给代码。", None),
        ("不用两句话", None),
        ("不要只要两句话", None),
        ("他说‘用两句话说清’，这要求合理吗？", None),
        ("先用两句解释，再用三句总结。", None),
        ("刚才他说了两句话。", None),
        ("两三句话就行。", None),
    ],
)
def test_sentence_count_only_applies_to_explicit_single_output_request(text, expected):
    from plugins.xiaoqing_chat.llm.communication_constraints import requested_sentence_count

    assert requested_sentence_count(text) == expected


@pytest.fixture
def state(tmp_path):
    value = ChatRuntimeState()
    bind_all_stores(value, tmp_path)
    return value


def test_expression_selection_uses_current_context_and_approval(state, tmp_path):
    state.bw_expr_store.save(
        [
            ExpressionRecord(
                "popular", "g1", "饭菜难吃", "食堂又刷出隐藏副本", count=99, checked=True
            ),
            ExpressionRecord("relevant", "g1", "赶作业熬夜", "作业最终章", count=1, checked=True),
            ExpressionRecord("unchecked", "g1", "赶作业熬夜", "未审核风格", count=999),
            ExpressionRecord("other", "g2", "赶作业熬夜", "另一个群的表达", checked=True),
        ]
    )
    runtime = ChatRuntime(XiaoQingChatConfig(), [])
    block   = expression_context(runtime, state, tmp_path, "g1", "又要熬夜赶作业了")
    assert "作业最终章" in block
    assert "隐藏副本" not in block and "未审核" not in block and "另一个群" not in block
    state.memory_store.append("g1", role="assistant", name="小青", content="作业最终章")
    assert expression_context(runtime, state, tmp_path, "g1", "又要熬夜赶作业了") == ""


def test_local_meme_definition_and_transfer_candidates_are_scoped(state, tmp_path):
    state.bw_jargon_store.save(
        [
            JargonRecord(
                "电子烧水",
                scope_chat_id = "g1",
                meaning       = "动静很大，实际没有进展",
                usage         = "无效会议和无效忙碌",
                tone          = "轻松自嘲",
            ),
            JargonRecord("电子烧水", scope_chat_id="g2", meaning="另一个群的私有定义"),
            JargonRecord("电子烧水", is_global=True, meaning="全局词典中的旧含义"),
        ]
    )
    block = jargon_context(state, tmp_path, "g1", [], "电子烧水是什么意思")
    assert "动静很大" in block and "轻松自嘲" in block
    assert "私有定义" not in block and "旧含义" not in block
    transfer = jargon_context(state, tmp_path, "g1", [], "会议开了三小时还没决定")
    assert "无效会议" in transfer
    state.memory_store.append("g1", role="assistant", name="小青", content="又在电子烧水了")
    assert jargon_context(state, tmp_path, "g1", [], "今天又开会") == ""


@pytest.mark.asyncio
async def test_meme_extraction_accepts_supported_correction(state):
    text = "纠正一下，本群的空转指忙半天没有进展，适合轻松自嘲。"
    state.bw_jargon_store.save(
        [JargonRecord("空转", scope_chat_id="g1", meaning="旧含义", is_complete=True)]
    )
    payload = [
        {
            "term": "空转",
            "is_jargon": True,
            "meaning": "忙碌却没有进展",
            "usage": "无效忙碌",
            "tone": "轻松自嘲",
            "evidence": text,
            "correction": True,
        },
        {"term": "凭空新词", "is_jargon": True, "meaning": "无来源"},
    ]
    response = {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}
    with patch(
        "plugins.xiaoqing_chat.expression.bw_jargon_miner.chat_completions_raw_with_fallback_paths",
        new=AsyncMock(return_value=(response, "test")),
    ):
        await mine_jargon(
            store    = state.bw_jargon_store,
            chat_id  = "g1",
            bot_name = "小青",
            messages=[StoredMessage(role="user", name="用户", content=text, ts=time.time())],
            **MODEL,
        )
    records = state.bw_jargon_store.load()
    record  = records[state.bw_jargon_store.key_for("空转", "g1")]
    assert record.meaning == "忙碌却没有进展" and record.source == text
    assert record.usage == "无效忙碌" and record.tone == "轻松自嘲"
    assert len(records) == 1


@pytest.mark.asyncio
async def test_empty_expression_result_still_mines_jargon(state, tmp_path):
    for index in range(10):
        state.memory_store.append("g1", role="user", name="用户", content=f"新梗样本 {index}")
    context = SimpleNamespace(data_dir=tmp_path, logger=MagicMock())
    with (
        patch(
            "plugins.xiaoqing_chat.expression.bw_message_recorder.learn_from_messages",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "plugins.xiaoqing_chat.expression.bw_message_recorder.mine_jargon",
            new=AsyncMock(return_value=1),
        ) as mine,
    ):
        changed = await extract_and_learn(
            context      = context,
            bot_name     = "小青",
            chat_id      = "g1",
            memory_store = state.memory_store,
            expr_store   = state.bw_expr_store,
            jargon_store = state.bw_jargon_store,
            recorder     = state.bw_recorder,
            personality  = XiaoQingChatConfig().personality,
            **MODEL,
        )
    assert changed == 1
    mine.assert_awaited_once()
    assert state.bw_recorder.get_last_time("g1") > 0


@pytest.mark.asyncio
async def test_reset_during_expression_review_drops_new_learned_record(state, tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    async def review(**kwargs):
        started.set()
        await release.wait()
        return True, False, "approved", "", ""

    with patch(
        "plugins.xiaoqing_chat.expression.bw_expression_learner.single_expression_check",
        side_effect=review,
    ):
        task = asyncio.create_task(
            upsert_learned(
                store        = state.bw_expr_store,
                chat_id      = "g1",
                learned      = [LearnedExpression("作业太多", "用副本比喻作业", "m1")],
                self_reflect = True,
                bot_name     = "小青",
                personality  = XiaoQingChatConfig().personality,
                **MODEL,
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 1)
            await _reset_chat_session(state, "g1", tmp_path)
            release.set()
            assert await task == 0
            assert state.bw_expr_store.load() == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_profile_question_uses_semantic_checker_for_natural_boundary():
    from plugins.xiaoqing_chat.llm.check_result import ReplyCheckResult
    from plugins.xiaoqing_chat.llm.reply_checker import check_reply

    remote = AsyncMock(return_value=ReplyCheckResult(True, "自然保留私人资料", False))
    with patch("plugins.xiaoqing_chat.llm.reply_checker._llm_check", remote):
        result = await check_reply(
            secrets                          = {"_ai": object()},
            bot_name                         = "小青",
            reply                            = "学校这些就先不说啦，聊番我可以。",
            goal                             = "自然聊天",
            current_text                     = "小青，你在哪所大学？",
            policy_text                      = "",
            grounding_text                   = XiaoQingChatConfig().personality.identity,
            history                          = [],
            chat_history_text                = "用户：小青，你在哪所大学？",
            enable_llm_checker               = True,
            max_repeat_compare               = 3,
            similarity_threshold             = 0.9,
            max_assistant_in_row             = 5,
            timeout_seconds                  = 1,
            max_retry                        = 0,
            retry_interval_seconds           = 0,
            allow_low_stakes_persona_fiction = True,
        )
    assert result.suitable
    remote.assert_awaited_once()
