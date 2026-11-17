"""回复生成器在复核超时和草稿缺失时的降级行为。"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from core.ai import AIError
from tests.helpers.xiaoqing_chat_test_support import (
    GROUP_GATE_OPEN,
    ScriptedChatModel,
    chat_message_event,
    draft_reply,
    plan_answer,
    reply_plan,
    scripted_chat_context,
    settle_chat_background,
)

mock_context       = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event

# 对方明说“别反问”：风险模式下这类交流约束也要送审，所以审查模型一定会被问到。
_NO_FOLLOWUP_TEXT = "别反问我了，随便聊聊"
# 审查路由的总时限压到最低（0.1 秒），“审查模型一直不回答”不必真的等半分钟。
_FAST_CHECKER_TIMEOUT = {
    "ai": {
        "routes": {
            "checker": {
                "models"               : ["scripted-chat"],
                "timeout_seconds"      : 0.1,
                "total_timeout_seconds": 0.1,
            }
        }
    }
}


async def _checker_never_answers(_call) -> str:
    await asyncio.sleep(3600)
    return ""


def _checker_down(_call) -> str:
    raise AIError("checker down")


# 审查模型没能给出审查结果的三种方式：一直不答、调用失败、答的不是审查 JSON。
_CHECKER_FAILURES = {
    "timeout" : _checker_never_answers,
    "error"   : _checker_down,
    "protocol": "这不是审查 JSON",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["always", "risk"])
@pytest.mark.parametrize("failure", ["timeout", "error", "protocol"])
async def test_checker_infrastructure_failure_respects_review_mode(
    mock_context, sample_group_event, mode, failure
):
    model = ScriptedChatModel(reply="未经审查的候选", checker=_CHECKER_FAILURES[failure])
    scripted_chat_context(
        mock_context, model, {"reply_check": {"llm_checker_mode": mode}}, _FAST_CHECKER_TIMEOUT
    )
    event = chat_message_event(sample_group_event, _NO_FOLLOWUP_TEXT)

    forced = await draft_reply(mock_context, event, text=_NO_FOLLOWUP_TEXT, forced=True)

    # 审查模型被问到了一次；审查本身出了故障，重生成救不回来，不会再叫生成模型。
    assert len(model.calls_of("checker")) == 1
    assert len(model.calls_of("reply")) == 1
    assert forced is not None
    if mode == "risk":
        # 风险模式：没审成就按没发现风险放行候选。
        assert forced.text == "未经审查的候选"
        await settle_chat_background(mock_context, model)
        return
    # 总是审查模式：不发没审过的候选。被点名时回一句安全的兜底，主动接话则沉默。
    assert forced.text == "等我一下，脑子突然卡壳了"
    unforced = await draft_reply(mock_context, event, text=_NO_FOLLOWUP_TEXT, forced=False)
    assert unforced is None
    assert len(model.calls_of("reply")) == 2
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_generate_reply_checker_timeout_allows_non_forced_reply(
    mock_context, sample_group_event
):
    model = ScriptedChatModel(reply="这条回复需要检查", checker=_checker_never_answers)
    scripted_chat_context(
        mock_context, model, {"reply_check": {"llm_checker_mode": "risk"}}, _FAST_CHECKER_TIMEOUT
    )
    event = chat_message_event(sample_group_event, _NO_FOLLOWUP_TEXT)

    draft = await draft_reply(mock_context, event, text=_NO_FOLLOWUP_TEXT, reasoning="正常回复")

    assert len(model.calls_of("checker")) == 1
    assert draft is not None
    assert draft.text == "这条回复需要检查"
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_smalltalk_sends_nothing_when_the_model_gives_no_reply_text(
    mock_context, sample_group_event, caplog
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    model = ScriptedChatModel(plan=plan_answer("direct_reply"), reply="")
    scripted_chat_context(mock_context, model, GROUP_GATE_OPEN)

    result = await handle_smalltalk(
        "你好", chat_message_event(sample_group_event, "你好"), mock_context
    )

    assert result == []
    assert len(model.calls_of("reply")) >= 1
    # 没有草稿不是故障：处理器没有记错误。
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    await settle_chat_background(mock_context, model)


def test_context_grounding_failure_reports_unavailable_reply() -> None:
    from types import SimpleNamespace

    from plugins.xiaoqing_chat.llm.check_result import ReplyCheckResult
    from plugins.xiaoqing_chat.reply_draft import RejectedCandidate
    from plugins.xiaoqing_chat.reply_fallbacks import finish_rejected_candidate

    plan = SimpleNamespace(
        forced     = True,
        request_id = "opinion-fallback",
        text       = "我觉得所有方案只要加缓存就都能变快，你必须同意我。",
        event      = {},
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
        text   = "一条被上下文证据检查拒绝的候选",
        result = ReplyCheckResult(
            suitable     = False,
            reason       = "上下文证据不足",
            need_replan  = True,
            severity     = "hard",
            failure_code = "context_grounding",
        ),
    )

    draft = finish_rejected_candidate(plan, rejected)

    assert draft is not None
    assert draft.text == "等等，我好像没跟上你们说的是哪件事"


_NOTICE_MARKER = (
    "[图片：网页通知截图，文字内容是“报名截止时间：以系统岗位发布页面为准，有什么问题联系人事处”]"
)


@pytest.mark.parametrize(
    "readable,text,expected",
    [
        # 只发了图：图上文字里的“什么”不算用户在提问，读出来了就说看到了。
        (True, _NOTICE_MARKER, "看到了"),
        (True, f"小青 这个几号截止？{_NOTICE_MARKER}", "这个我不敢打包票，先不乱说"),
        (False, "[图片：图片内容读取失败]", "图我这边没刷出来"),
    ],
    ids=["shared", "asked", "unreadable"],
)
def test_media_rejection_fallback_never_contradicts_the_reading(readable, text, expected):
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.llm.check_result import ReplyCheckResult
    from plugins.xiaoqing_chat.reply_draft import RejectedCandidate
    from plugins.xiaoqing_chat.reply_fallbacks import finish_rejected_candidate

    plan = reply_plan(
        forced = True,
        text   = text,
        runtime=SimpleNamespace(cfg=XiaoQingChatConfig()),
        event={
            "message"                 : [{"type": "image", "data": {}}],
            "_xc_effective_user_parts": [
                {
                    "kind"       : "image",
                    "description": "示例研究所实验室安全检查通知"
                    if readable
                    else "图片内容读取失败",
                }
            ],
        },
    )
    rejected = RejectedCandidate(
        text   = "图片上是明年夏天截止",
        result = ReplyCheckResult(False, "图片只写以系统页面为准", True, "hard", "media_grounding"),
    )
    draft = finish_rejected_candidate(plan, rejected)

    assert draft is not None
    assert draft.text == expected
    assert "没看清" not in draft.text


def test_context_rejection_on_a_read_image_does_not_claim_losing_the_thread():
    """只发了一张已读出的通知截图，上下文依据检查拒绝候选时，兜底说“看到了”。

    图已经读出来，兜底不能说“没跟上你们说的是哪件事”。
    """
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.llm.check_result import ReplyCheckResult
    from plugins.xiaoqing_chat.reply_draft import RejectedCandidate
    from plugins.xiaoqing_chat.reply_fallbacks import finish_rejected_candidate

    plan = reply_plan(
        forced = True,
        text   = "[图片：网页通知截图，文字内容是“现面向在读研究生招聘冬季学期课程助教”]",
        runtime=SimpleNamespace(cfg=XiaoQingChatConfig()),
        event={
            "message"                 : [{"type": "image", "data": {}}],
            "_xc_effective_user_parts": [{"kind": "image", "description": "研究生助教招聘通知"}],
        },
    )
    rejected = RejectedCandidate(
        text   = "助教招的是研究生吧，咱本科的看看就好",
        result = ReplyCheckResult(
            False, "对话事实的引用或具体测量值缺少原文依据。", True, "hard", "context_grounding"
        ),
    )

    draft = finish_rejected_candidate(plan, rejected)

    assert draft is not None
    assert draft.text == "看到了"
