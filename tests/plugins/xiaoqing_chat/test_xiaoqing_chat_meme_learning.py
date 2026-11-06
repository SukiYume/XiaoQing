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
    "llm"    : LLMCallConfig(
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
                confirmed     = True,
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
        [JargonRecord("空转", scope_chat_id="g1", meaning="旧含义", confirmed=True)]
    )
    payload = [
        {
            "term"     : "空转",
            "is_jargon": True,
            "meaning"  : "忙碌却没有进展",
            "usage"    : "无效忙碌",
            "tone"     : "轻松自嘲",
            "evidence" : text,
            "explained" : True,
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
    assert record.confirmed is True
    assert len(records) == 1


async def _mine(state, text: str, items: list[dict]) -> None:
    response = {"choices": [{"message": {"content": json.dumps(items, ensure_ascii=False)}}]}
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


def _record(state, term: str) -> JargonRecord:
    return state.bw_jargon_store.load()[state.bw_jargon_store.key_for(term, "g1")]


@pytest.mark.asyncio
@pytest.mark.parametrize("explained", [False, None])
async def test_meaning_without_an_explaining_sentence_is_only_a_candidate(state, explained):
    text = "今天又空转了一天"
    item = {"term": "空转", "is_jargon": True, "meaning": "忙碌却没有进展", "evidence": text}
    if explained is not None:
        item["explained"] = explained
    await _mine(state, text, [item])
    record = _record(state, "空转")
    assert record.meaning == "忙碌却没有进展" and record.confirmed is False


@pytest.mark.asyncio
async def test_explained_flag_needs_evidence_from_the_original_message(state):
    text = "今天又空转了一天"
    item = {
        "term"     : "空转",
        "is_jargon": True,
        "meaning"  : "忙碌却没有进展",
        "evidence" : "空转就是瞎忙",
        "explained": True,
    }
    await _mine(state, text, [item])
    assert _record(state, "空转").confirmed is False


@pytest.mark.asyncio
async def test_candidate_never_overwrites_a_confirmed_meaning(state):
    state.bw_jargon_store.save(
        [JargonRecord("空转", scope_chat_id="g1", meaning="已确认的含义", confirmed=True)]
    )
    text = "今天又空转了一天"
    await _mine(
        state,
        text,
        [{"term": "空转", "is_jargon": True, "meaning": "新猜测", "evidence": text}],
    )
    record = _record(state, "空转")
    assert record.meaning == "已确认的含义" and record.confirmed is True


_TWO_USES = ["对方(甲)：来杯快乐水", "对方(乙)：今天又喝了两瓶快乐水"]


async def _infer(db: dict, verdict: dict) -> tuple[int, AsyncMock]:
    from plugins.xiaoqing_chat.expression.bw_jargon_miner import _infer_pending_meanings

    response = {"choices": [{"message": {"content": json.dumps(verdict, ensure_ascii=False)}}]}
    with patch(
        "plugins.xiaoqing_chat.expression.bw_jargon_miner.chat_completions_raw_with_fallback_paths",
        new=AsyncMock(return_value=(response, "test")),
    ) as call:
        changed = await _infer_pending_meanings(
            {}, db, MODEL["llm"], chat_id="g1", infer_threshold=3
        )
    return changed, call


@pytest.mark.asyncio
async def test_inference_confirms_only_with_stated_and_cited_evidence(state):
    db = {
        "a": JargonRecord("快乐水", scope_chat_id="g1", count=3, raw_content=list(_TWO_USES)),
        "b": JargonRecord("乙", scope_chat_id="g1", meaning="定论", count=3, confirmed=True),
    }
    changed, call = await _infer(db, {"meaning": "碳酸饮料", "sufficient": True, "support": [1, 2]})
    assert changed == 1 and call.await_count == 1
    assert (db["a"].meaning, db["a"].confirmed) == ("碳酸饮料", True)
    assert db["b"].meaning == "定论"
    prompt = call.await_args.kwargs["messages"][0]["content"]
    assert "[1]\n对方(甲)：来杯快乐水" in prompt and "[2]\n对方(乙)" in prompt


@pytest.mark.asyncio
async def test_a_single_context_is_never_inferred_even_with_a_high_count(state):
    db = {
        "a": JargonRecord(
            "快乐水", scope_chat_id="g1", count=3, raw_content=["对方(甲)：来杯快乐水"]
        ),
        # 两个窗口重叠在同一句话上，只算一次用法；不含这个词的上下文不算。
        "b": JargonRecord(
            "肥宅",
            scope_chat_id = "g1",
            count         = 5,
            raw_content   = ["对方(甲)：我是肥宅\n对方(乙)：哈哈", "对方(甲)：我是肥宅", "无关的一段"],
        ),
    }
    changed, call = await _infer(
        db, {"meaning": "可能指饮料，具体含义不明确", "sufficient": True, "support": [1, 2]}
    )
    assert changed == 0 and call.await_count == 0
    assert db["a"].confirmed is False and db["b"].confirmed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdict",
    [
        {"meaning": "可能指饮料，具体含义不明确", "sufficient": True, "support": [1, 2]},
        {"meaning": "碳酸饮料", "sufficient": False, "support": [1, 2]},
        {"meaning": "碳酸饮料", "sufficient": "true", "support": [1, 2]},
        {"meaning": "碳酸饮料", "sufficient": True, "support": [1]},
        {"meaning": "碳酸饮料", "sufficient": True, "support": [1, 7]},
        {"meaning": "碳酸饮料", "sufficient": True, "support": [1, 1]},
        {"meaning": "碳酸饮料", "sufficient": True},
    ],
)
async def test_unsupported_or_hedged_inference_stays_a_candidate(state, verdict):
    db = {"a": JargonRecord("快乐水", scope_chat_id="g1", count=3, raw_content=list(_TWO_USES))}
    changed, call = await _infer(db, verdict)
    assert changed == 1 and call.await_count == 1
    assert db["a"].confirmed is False and db["a"].meaning == verdict["meaning"]
    assert db["a"].last_inference_count == 3  # 等出现更多用法再推断
    assert (await _infer(db, verdict))[1].await_count == 0


@pytest.mark.asyncio
async def test_hedged_inference_never_replaces_an_existing_candidate(state):
    db = {
        "a": JargonRecord(
            "快乐水", scope_chat_id="g1", meaning="原候选", count=3, raw_content=list(_TWO_USES)
        )
    }
    await _infer(db, {"meaning": "或许是饮料", "sufficient": True, "support": [1, 2]})
    assert (db["a"].meaning, db["a"].confirmed) == ("原候选", False)


@pytest.mark.asyncio
async def test_stored_context_is_where_the_term_was_used(state):
    filler = [
        StoredMessage(role="user", name="路人", content=f"无关的第{index}句", ts=time.time())
        for index in range(10)
    ]
    messages = [
        StoredMessage(role="user", name="甲", content="先来一句", ts=time.time()),
        StoredMessage(role="user", name="甲", content="今天又空转了一天", ts=time.time()),
        StoredMessage(role="user", name="乙", content="同感", ts=time.time()),
        *filler,
    ]
    response = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        [{"term": "空转", "is_jargon": True, "evidence": "今天又空转了一天"}],
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }
    with patch(
        "plugins.xiaoqing_chat.expression.bw_jargon_miner.chat_completions_raw_with_fallback_paths",
        new=AsyncMock(return_value=(response, "test")),
    ):
        await mine_jargon(
            store=state.bw_jargon_store, chat_id="g1", bot_name="小青", messages=messages, **MODEL
        )
    assert _record(state, "空转").raw_content == [
        "对方(甲)：先来一句\n对方(甲)：今天又空转了一天\n对方(乙)：同感"
    ]


def test_unconfirmed_meaning_is_a_marked_guess_and_never_a_transfer_candidate(state, tmp_path):
    state.bw_jargon_store.save(
        [
            JargonRecord(
                "空转",
                scope_chat_id = "g1",
                meaning       = "忙碌却没有进展",
                usage         = "无效忙碌",
                tone          = "轻松自嘲",
            )
        ]
    )
    mentioned = jargon_context(state, tmp_path, "g1", [], "空转是什么意思")
    assert "空转：忙碌却没有进展" in mentioned and "未经本群确认的猜测" in mentioned
    assert "无效忙碌" not in mentioned and "轻松自嘲" not in mentioned
    assert jargon_context(state, tmp_path, "g1", [], "今天忙了一天没进展") == ""


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
            grounding_text                   = XiaoQingChatConfig().personality.card.facts("小青"),
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
