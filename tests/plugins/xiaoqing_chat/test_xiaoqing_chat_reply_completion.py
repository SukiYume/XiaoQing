"""回复完成、事实锚定、定向再生成和安全降级。"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from plugins.xiaoqing_chat.store_binding import bound_state
from tests.helpers.assertions import logged_events
from tests.helpers.reply_checker_test_support import checker_verdict
from tests.helpers.xiaoqing_chat_test_support import (
    GROUP_GATE_OPEN,
    ScriptedChatModel,
    chat_message_event,
    plan_answer,
    reply_plan,
    scripted_chat_context,
    settle_chat_background,
)

mock_context       = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event


@pytest.mark.asyncio
async def test_smalltalk_no_reply_preserves_wait_action_name_in_action_history(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    chat_id = "g67890"
    model = ScriptedChatModel(plan=plan_answer("wait", "再观察"))
    scripted_chat_context(mock_context, model, GROUP_GATE_OPEN)

    result = await handle_smalltalk(
        "继续看看", chat_message_event(sample_group_event, "继续看看"), mock_context
    )

    assert result == []
    state = bound_state(mock_context)
    records = await state.action_history.get_recent_async(chat_id, max_items=5)
    assert [record.action for record in records] == ["wait"]
    await settle_chat_background(mock_context, model)


@pytest.mark.parametrize(
    "candidate",
    [
        "那确实挺难受的",
        "他要不要来还没定，先按原计划安排。",
        "文档里的‘你需要什么’是一个示例句。",
        "附件没有传过来，我暂时无法判断。",
        "你需要什么",
    ],
)
@pytest.mark.asyncio
async def test_generate_reply_forced_soft_rejection_uses_last_contextual_candidate(
    mock_context, sample_group_event, candidate, caplog
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    caplog.set_level(logging.INFO, logger="plugins.xiaoqing_chat")

    text  = "请直接说明情况，别反问我。"
    model = ScriptedChatModel(
        reply   = candidate,
        checker = checker_verdict(
            suitable     = False,
            reason       = "风格检查器认为不够自然",
            need_replan  = False,
            severity     = "soft",
            non_template = False,
        ),
    )
    scripted_chat_context(
        mock_context,
        model,
        {"reply_check": {"llm_checker_mode": "always", "max_regen": 0}},
    )
    event = chat_message_event(sample_group_event, text, _xc_command_forced=True)

    result = await handle_smalltalk(text, event, mock_context)

    # 照发最后一稿；真实的出站整理会去掉句末的句号。
    assert result == [{"type": "text", "data": {"text": candidate.removesuffix("。")}}]
    # 软问题不重写，直接照发并记一笔。
    assert logged_events(caplog, "reply.check.soft_pass")
    await settle_chat_background(mock_context, model)


def test_finish_forced_structural_rejection_uses_safe_fallback() -> None:
    from plugins.xiaoqing_chat.llm.check_result import ReplyCheckResult
    from plugins.xiaoqing_chat.reply_draft import RejectedCandidate
    from plugins.xiaoqing_chat.reply_fallbacks import finish_rejected_candidate

    rejected = RejectedCandidate(
        text   = "重复回复",
        result = ReplyCheckResult(
            suitable    = False,
            reason      = "回复与之前机器人消息完全相同",
            need_replan = True,
        ),
    )

    draft = finish_rejected_candidate(reply_plan(forced=True), rejected)

    assert draft is not None
    assert draft.text == "等我一下，脑子突然卡壳了"
    assert draft.text != rejected.text


def test_finish_forced_persona_grounding_rejection_keeps_conversation_alive(caplog) -> None:
    from plugins.xiaoqing_chat.llm.check_result import ReplyCheckResult
    from plugins.xiaoqing_chat.reply_draft import RejectedCandidate
    from plugins.xiaoqing_chat.reply_fallbacks import (
        PERSONA_GROUNDING_FALLBACKS,
        finish_rejected_candidate,
    )

    caplog.set_level(logging.INFO, logger="plugins.xiaoqing_chat")

    plan = reply_plan(
        forced     = True,
        request_id = "grounding-test",
        text       = "一个要求角色补全未知人物信息的问题",
        history    = [],
        bot_name   = "小青",
        chat_id    = "group:1",
        runtime    = SimpleNamespace(
            cfg=SimpleNamespace(
                reply_check=SimpleNamespace(
                    max_repeat_compare   = 8,
                    similarity_threshold = 0.92,
                    max_assistant_in_row = 3,
                ),
            )
        ),
    )
    rejected = RejectedCandidate(
        text   = "一条无依据的人物信息",
        result = ReplyCheckResult(
            suitable     = False,
            reason       = "人物陈述没有直接证据",
            need_replan  = True,
            failure_code = "persona_grounding",
        ),
    )

    draft = finish_rejected_candidate(plan, rejected)

    assert draft is not None
    assert draft.text in PERSONA_GROUNDING_FALLBACKS
    assert rejected.text not in draft.text
    assert logged_events(caplog, "reply.check.exhausted.persona_grounding")


def test_finish_persona_grounding_identity_query_uses_only_known_name() -> None:
    from plugins.xiaoqing_chat.llm.check_result import ReplyCheckResult
    from plugins.xiaoqing_chat.reply_draft import RejectedCandidate
    from plugins.xiaoqing_chat.reply_fallbacks import finish_rejected_candidate

    plan = reply_plan(
        forced     = True,
        request_id = "identity-test",
        text       = "你究竟是谁？",
        history    = [],
        bot_name   = "小青",
        chat_id    = "group:1",
        runtime    = SimpleNamespace(
            cfg=SimpleNamespace(
                reply_check=SimpleNamespace(
                    max_repeat_compare   = 8,
                    similarity_threshold = 0.92,
                    max_assistant_in_row = 3,
                ),
            )
        ),
    )
    rejected = RejectedCandidate(
        text   = "一条夹带了未知身份细节的回复",
        result = ReplyCheckResult(
            suitable     = False,
            reason       = "人物陈述没有直接证据",
            need_replan  = True,
            failure_code = "persona_grounding",
        ),
    )

    draft = finish_rejected_candidate(plan, rejected)

    assert draft is not None
    assert draft.text == "我叫小青。"


@pytest.mark.parametrize(
    ("text", "expected_fragment", "includes_profile"),
    [
        (
            "小青，你具体在哪所学校、哪个城市、读什么专业？",
            "不说",
            False,
        ),
        (
            "小青，说说你是个什么样的人，像群友自我介绍。",
            "别的慢慢聊就知道了",
            True,
        ),
    ],
    ids=("precise-profile", "self-intro"),
)
def test_finish_persona_grounding_uses_configured_persona_fallbacks(
    text,
    expected_fragment,
    includes_profile,
) -> None:
    from plugins.xiaoqing_chat.llm.check_result import ReplyCheckResult
    from plugins.xiaoqing_chat.reply_draft import RejectedCandidate
    from plugins.xiaoqing_chat.reply_fallbacks import finish_rejected_candidate

    configured_identity = (
        "是一名海边书店店员，喜欢旧唱片；具体学校、专业、城市没有设定，不主动补成真实资料。"
    )
    plan = reply_plan(
        forced             = True,
        request_id         = "profile-fallback",
        text               = text,
        effective_identity = configured_identity,
        history            = [],
        bot_name           = "小青",
        chat_id            = "group:1",
        runtime            = SimpleNamespace(
            cfg=SimpleNamespace(
                reply_check=SimpleNamespace(
                    max_repeat_compare   = 8,
                    similarity_threshold = 0.92,
                    max_assistant_in_row = 3,
                ),
            )
        ),
    )
    rejected = RejectedCandidate(
        text   = "一条越界人物回复",
        result = ReplyCheckResult(
            suitable     = False,
            reason       = "人物边界不满足",
            need_replan  = True,
            failure_code = "persona_grounding",
        ),
    )

    draft = finish_rejected_candidate(plan, rejected)

    assert draft is not None
    assert expected_fragment in draft.text
    assert ("海边书店店员" in draft.text) is includes_profile
    assert ("旧唱片" in draft.text) is includes_profile
    assert "不主动补成真实资料" not in draft.text
    assert len(draft.text) <= 120
    assert "住校" not in draft.text
    assert "理工" not in draft.text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("你以前参加过吗？", True),
        ("你更倾向哪一个？", True),
        ("你现在方便吗？", True),
        ("你觉得这个方案稳不稳？", False),
        ("你觉得你平时会怎么选？", True),
        ("小青，冻饺子怎么煮？", False),
        ("小青，两个列表怎么合并？", False),
        ("小青，你以前参加过吗？", True),
        ("小青多大？", True),
        ("今天还挺热闹的", False),
    ],
)
def test_checker_unavailable_persona_guard_uses_general_syntax(text, expected) -> None:
    from plugins.xiaoqing_chat.reply_plan import needs_persona_guard_when_checker_unavailable

    assert needs_persona_guard_when_checker_unavailable(text, bot_name="小青") is expected


def test_persona_grounding_rejection_gets_one_targeted_regeneration() -> None:
    from plugins.xiaoqing_chat.llm.check_result import ReplyCheckResult
    from plugins.xiaoqing_chat.reply_draft import RejectedCandidate, ReplyAttemptState
    from plugins.xiaoqing_chat.reply_fallbacks import queue_reply_regeneration

    plan = reply_plan(
        forced   = True,
        bot_name = "小青",
        chat_id  = "group:1",
        runtime  = SimpleNamespace(
            cfg=SimpleNamespace(
                reply_check=SimpleNamespace(max_regen=1),
            )
        ),
    )
    attempt = ReplyAttemptState(max_items=10)
    rejected = RejectedCandidate(
        text   = "我以前经常这么做",
        result = ReplyCheckResult(
            suitable     = False,
            reason       = "人物经历没有依据",
            need_replan  = True,
            failure_code = "persona_grounding",
        ),
    )

    queued = queue_reply_regeneration(
        plan,
        attempt,
        rejected,
        step="reply.check.regen",
    )

    assert queued is True
    assert attempt.regen_used == 1
    assert "句首称呼" in attempt.extra_check_hint


def test_soft_style_regeneration_keeps_fact_correction_out_of_the_request() -> None:
    from plugins.xiaoqing_chat.llm.check_result import ReplyCheckResult
    from plugins.xiaoqing_chat.reply_draft import RejectedCandidate, ReplyAttemptState
    from plugins.xiaoqing_chat.reply_fallbacks import queue_reply_regeneration

    plan = reply_plan(
        chat_id = "g1",
        runtime = SimpleNamespace(
            cfg=SimpleNamespace(
                reply_check=SimpleNamespace(max_regen=1),
            )
        ),
    )
    attempt = ReplyAttemptState(max_items=10)
    rejected = RejectedCandidate(
        text   = "外卖袋先替你尝了一口。建议联系平台退款。",
        result = ReplyCheckResult(False, "保留第一句的笑点，收住额外建议", False, "soft"),
    )
    assert queue_reply_regeneration(plan, attempt, rejected, step="test")
    assert rejected.text in attempt.extra_check_hint
    assert rejected.result.reason in attempt.extra_check_hint
    assert "保留贴切的观点、笑点" in attempt.extra_check_hint
    assert "无依据信息" not in attempt.extra_check_hint
    assert not queue_reply_regeneration(plan, attempt, rejected, step="test")
    assert attempt.regen_used == 1


def test_postprocess_keeps_quoted_sentence_and_closing_quote_together() -> None:
    from plugins.xiaoqing_chat.config.config import ResponsePostProcessConfig
    from plugins.xiaoqing_chat.llm.postprocess import process_llm_response

    parts = process_llm_response(
        "她盯着耳机线说：“你这耳机想泡个澡是吧。”然后把碗推远了。",
        ResponsePostProcessConfig(),
        bot_name="小青",
    )

    assert parts == [
        "她盯着耳机线说：“你这耳机想泡个澡是吧。”",
        "然后把碗推远了。",
    ]
    assert all(part != "”" for part in parts)


def test_proactive_grounding_exhaustion_prefers_silence_to_full_replan() -> None:
    from plugins.xiaoqing_chat.llm.check_result import ReplyCheckResult
    from plugins.xiaoqing_chat.reply_draft import RejectedCandidate
    from plugins.xiaoqing_chat.reply_fallbacks import finish_rejected_candidate

    plan = reply_plan(
        forced  = False,
        chat_id = "group:1",
    )
    rejected = RejectedCandidate(
        text   = "一条无依据的生活片段",
        result = ReplyCheckResult(
            suitable     = False,
            reason       = "人物经历没有依据",
            need_replan  = True,
            failure_code = "persona_grounding",
        ),
    )

    assert finish_rejected_candidate(plan, rejected) is None


@pytest.mark.parametrize(
    "parts,fragments,kept",
    [
        # 删掉没依据的那一个气泡，其余照发。
        (("有叉烧、两片蛋", "还有几粒绿色配料。", "蛋看着还挺溏心的。"), ("蛋看着还挺溏心的",), 2),
        # 片段跨两个气泡时两个都删。
        (("有叉烧", "两片蛋，挺香", "38一碗"), ("叉烧、两片蛋",), 1),
        # 删完不剩、对不上任何气泡、只有一个气泡时都不删，照原流程重写。
        (("有叉烧", "两片蛋"), ("有叉烧，两片蛋",), None),
        (("有叉烧", "两片蛋"), ("海苔",), None),
        (("有叉烧、两片蛋",), ("两片蛋",), None),
    ],
)
def test_trim_unsupported_bubbles_drops_only_what_lacks_evidence(parts, fragments, kept) -> None:
    from plugins.xiaoqing_chat.llm.check_result import ReplyCheckResult
    from plugins.xiaoqing_chat.reply_draft import RejectedCandidate, build_reply_draft
    from plugins.xiaoqing_chat.reply_fallbacks import trim_unsupported_bubbles

    draft = build_reply_draft(parts, raw_text="")
    rejected = RejectedCandidate(
        text   = draft.text,
        result = ReplyCheckResult(
            False,
            "视觉陈述缺少可引用的画面或文字依据",
            True,
            "hard",
            "media_grounding",
            unsupported_fragments=fragments,
        ),
        draft=draft,
    )
    trimmed = trim_unsupported_bubbles(rejected)
    if kept is None:
        assert trimmed is None
    else:
        assert trimmed is not None and len(trimmed.text_parts) == kept
        assert all(fragment not in trimmed.text for fragment in fragments)


@pytest.mark.parametrize(
    "text,declines",
    [
        ("周末一起去天文台玩呗", True),
        ("你们周末有空出来吃火锅吗", True),
        ("小青，下次一起去看展", True),
        ("周末去哪玩比较好", False),
        ("小青，这道题怎么做", False),
    ],
)
def test_invitation_falls_back_to_a_decline_instead_of_not_following(text, declines) -> None:
    from plugins.xiaoqing_chat.reply_fallbacks import (
        CONTEXT_GROUNDING_FALLBACKS,
        INVITE_DECLINE_FALLBACKS,
        _forced_fallback_texts,
    )

    plan = SimpleNamespace(text=text, event={}, bot_name="小青", request_id="r1")
    texts, _step = _forced_fallback_texts(plan, "context_grounding")
    assert (texts == INVITE_DECLINE_FALLBACKS) is declines
    assert texts != CONTEXT_GROUNDING_FALLBACKS or not declines
