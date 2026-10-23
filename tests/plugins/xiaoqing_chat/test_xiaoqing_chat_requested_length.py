"""明确篇幅要求（“写个800字的检讨”）：字数本地数，差一点照样发，不改发兜底句。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.xiaoqing_chat.llm import reply_checker as checker
from plugins.xiaoqing_chat.llm.check_result import ReplyCheckResult
from plugins.xiaoqing_chat.llm.communication_constraints import (
    CharTarget,
    content_char_count,
    requested_char_count,
)
from plugins.xiaoqing_chat.llm.llm_config import LLMCallConfig
from plugins.xiaoqing_chat.reply_draft import RejectedCandidate, ReplyAttemptState
from plugins.xiaoqing_chat.reply_fallbacks import queue_reply_regeneration
from tests.helpers.xiaoqing_chat_test_support import reply_plan


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("小青，帮我写个800字的检讨书", CharTarget(800, "about")),
        ("写个八百字左右的检讨", CharTarget(800, "about")),
        ("一千五字的周报", CharTarget(1500, "about")),
        ("至少一千五百字", CharTarget(1500, "at_least")),
        ("不超过100字说说", CharTarget(100, "at_most")),
        ("控制在200字以内", CharTarget(200, "at_most")),
        ("写个800字的，标题不超过10个字", CharTarget(800, "about")),
        ("别写800字，随便说两句", None),
        ("给我点评下“800字”那篇范文", None),
        ("十字路口怎么走", None),
        ("背一下千字文", None),
        ("先写300字，再补500字", None),
    ],
)
def test_only_a_single_explicit_length_request_counts(text, expected):
    assert requested_char_count(text) == expected


def test_characters_are_counted_as_sent():
    assert content_char_count("检讨书\n\n尊敬的老师：[想发表情:哭] 好") == 10


async def _local_check(reply: str, current_text: str) -> ReplyCheckResult:
    return await checker.check_reply(
        secrets                = {"_ai": None},
        bot_name               = "小青",
        reply                  = reply,
        goal                   = "写检讨",
        current_text           = current_text,
        history                = [],
        chat_history_text      = "",
        enable_llm_checker     = False,
        max_repeat_compare     = 2,
        similarity_threshold   = 0.9,
        max_assistant_in_row   = 5,
        timeout_seconds        = 1.0,
        max_retry              = 0,
        retry_interval_seconds = 0.0,
    )


@pytest.mark.asyncio
async def test_a_short_draft_gets_exact_counts_and_stays_sendable():
    result = await _local_check(
        "检讨书。" + "我错了，以后改正。" * 50, "小青，帮我写个800字的检讨书"
    )

    assert not result.suitable
    assert result.failure_code == "instruction_following"
    # 差一些仍是可用的稿子：软问题重写一次，还不够就发出去，而不是改发兜底句。
    assert not result.is_hard
    assert "约 800 字，这一稿 454 字" in result.reason and "扩写" in result.reason


@pytest.mark.asyncio
async def test_drafts_within_range_pass_and_over_limit_asks_to_cut():
    assert (await _local_check("好" * 760, "写个800字的检讨")).suitable
    too_long = await _local_check("好" * 130, "不超过100字说说")
    assert not too_long.suitable and "压缩" in too_long.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("chars", [800, 450])
async def test_the_remote_checker_leaves_length_to_the_local_count(monkeypatch, chars):
    # 不论够不够字，审查模型都不再凭估计按字数判；否则篇幅不够会被当成硬错误改发兜底句。
    remote = AsyncMock(
        return_value=({"choices": [{"message": {"content": "{}"}}]}, "test"),
    )
    monkeypatch.setattr(checker, "_request_checker_completion", remote)
    await checker._llm_check(
        secrets     = {},
        check_input = checker._LLMCheckInput(
            "小青", "好" * chars, "写检讨", "写个800字的检讨", "", "", "", False
        ),
        request_policy=checker._LLMRequestPolicy(1024, 1, 0, 0),
    )
    prompt = remote.call_args.kwargs["prompt"]
    assert f"候选实际 {chars} 字；篇幅由本地逐字核对，instruction_followed 不按字数判断" in prompt
    assert json.loads(remote.call_args.kwargs["materials"])["待检查回复"] == "好" * chars


@pytest.mark.asyncio
async def test_other_problems_win_over_length(monkeypatch):
    # 篇幅只在其他核对都通过后才看：放行的短稿也一定过了事实与人设审查。
    hard = ReplyCheckResult(False, "编造了经历", True, "hard", "persona_grounding")
    monkeypatch.setattr(checker, "_llm_check", AsyncMock(return_value=hard))
    passed = ReplyCheckResult(True, "", False, "soft")
    kwargs = {
        "secrets": {},
        "bot_name": "小青",
        "goal": "写检讨",
        "current_text": "写个800字的检讨",
        "history": [],
        "chat_history_text": "",
        "enable_llm_checker": True,
        "max_repeat_compare": 2,
        "similarity_threshold": 0.9,
        "max_assistant_in_row": 5,
        "timeout_seconds": 1.0,
        "max_retry": 0,
        "retry_interval_seconds": 0.0,
    }

    assert await checker.check_reply(reply="好" * 450, **kwargs) is hard
    monkeypatch.setattr(checker, "_llm_check", AsyncMock(return_value=passed))
    short = await checker.check_reply(reply="好" * 450, **kwargs)
    assert not short.suitable and not short.is_hard and "这一稿 450 字" in short.reason
    assert (await checker.check_reply(reply="好" * 800, **kwargs)).suitable


def test_a_length_retry_asks_for_the_length_not_for_shorter_replies():
    plan = reply_plan(
        forced  = True,
        text    = "小青，帮我写个800字的检讨书",
        runtime = SimpleNamespace(
            cfg=SimpleNamespace(
                debug=SimpleNamespace(log_steps=False),
                reply_check=SimpleNamespace(max_regen=1),
            )
        ),
    )
    attempt = ReplyAttemptState(max_items=10)
    rejected = RejectedCandidate(
        text   = "检讨书……",
        result = ReplyCheckResult(
            False,
            "本轮要求约 800 字，这一稿 489 字（不算空白），请扩写到要求的篇幅，内容保持完整。",
            True,
            "soft",
            "instruction_following",
        ),
    )

    assert queue_reply_regeneration(plan, attempt, rejected, step="reply.regen")
    assert "这一稿 489 字" in attempt.extra_check_hint
    assert "明确要求" in attempt.extra_check_hint
    assert "短句和留白" not in attempt.extra_check_hint
    assert "留出接话空间" not in attempt.extra_check_hint


def test_long_requests_get_room_to_finish():
    base = LLMCallConfig(timeout_seconds=15.0, max_tokens=512)

    roomy = base.with_room_for(800)
    assert roomy.max_tokens >= 1400 and roomy.timeout_seconds >= 15.0
    assert base.with_room_for(100).max_tokens == 512
    assert base.with_room_for(20000).max_tokens == 8192


@pytest.mark.asyncio
async def test_the_raised_budget_reaches_the_generation_request(monkeypatch):
    # 实测出现过：计划里放宽了预算，生成请求却仍按配置的 512 发，800 字的稿子在半句处截断。
    from plugins.xiaoqing_chat import reply_generator
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.reply_plan import foreground_call_config
    from plugins.xiaoqing_chat.runtime_state import ChatRuntime

    runtime = ChatRuntime(XiaoQingChatConfig(max_tokens=512), [])
    assert foreground_call_config(runtime, "随便聊聊").max_tokens == 512
    assert foreground_call_config(runtime, "不超过100字说说").max_tokens == 512
    foreground = foreground_call_config(runtime, "小青，帮我写个800字的检讨书")
    assert foreground.max_tokens >= 1400

    completion = AsyncMock(return_value=("检讨书", "chat"))
    monkeypatch.setattr(reply_generator, "chat_completions_with_fallback_paths", completion)
    plan = reply_plan(
        forced     = True,
        text       = "小青，帮我写个800字的检讨书",
        runtime    = runtime,
        foreground = foreground,
        secrets    = {"_route": "chat"},
    )
    await reply_generator._request_reply_candidate(plan, [{"role": "user", "content": "x"}])
    assert completion.call_args.kwargs["max_tokens"] == foreground.max_tokens
