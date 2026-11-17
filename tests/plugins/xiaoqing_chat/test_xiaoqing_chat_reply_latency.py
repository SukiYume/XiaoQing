"""回复耗时：本轮阅读与生成并行、重生成复用阅读，以及发送前延迟扣除已用时间。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from core.log import LogEvent
from plugins.xiaoqing_chat.llm import reply_checker as checker
from tests.helpers.assertions import logged_events
from tests.helpers.reply_checker_test_support import checker_verdict
from tests.helpers.xiaoqing_chat_test_support import (
    GROUP_GATE_OPEN,
    ModelCall,
    ScriptedChatModel,
    ack_reply_delivery,
    chat_message_event,
    plan_answer,
    scripted_chat_context,
    settle_chat_background,
)

mock_context       = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event

_READING = {
    "meaning"    : "阿夏问大家这季追了什么番",
    "intent"     : "想听群友分享",
    "bot_stance" : "",
    "facts"      : [],
    "local_terms": [],
    "unresolved" : [],
}
_VERDICT = {
    "scan_complete"       : True,
    "premise_checks"      : [],
    "context_coherent"    : True,
    "speaker_correct"     : True,
    "instruction_followed": True,
}


def _checker_kind(call: ModelCall) -> str:
    """审查路由上的三种调用，靠提示词要求模型输出的 JSON 键区分：证据审查要 ``persona_claims``，
    语义审查第二步要 ``premise_checks``，剩下的是第一步“只读原话”的独立阅读。"""

    system = call.messages[0]["content"]
    if "persona_claims" in system:
        return "evidence"
    if "premise_checks" in system:
        return "semantic"
    return "reading"


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _checker_script(**overrides: Any):
    """审查模型的剧本：阅读答 ``_READING``、语义审查通过；``overrides`` 按调用种类换掉回答
    （字符串，或收到 :class:`ModelCall` 的函数，可为协程）。"""

    answers: dict[str, Any] = {
        "reading" : _json(_READING),
        "semantic": _json(_VERDICT),
        "evidence": checker_verdict(),
        **overrides,
    }

    async def answer(call: ModelCall) -> str:
        reply = answers[_checker_kind(call)]
        if callable(reply):
            reply = reply(call)
            if asyncio.iscoroutine(reply):
                reply = await reply
        return reply

    return answer


def _readings(model: ScriptedChatModel) -> list[ModelCall]:
    return [call for call in model.calls_of("checker") if _checker_kind(call) == "reading"]


async def _wait_for_calls(model: ScriptedChatModel) -> None:
    """让后台任务跑起来，直到剧本模型收到第一次调用。"""

    for _ in range(100):
        if model.calls:
            return
        await asyncio.sleep(0)
    raise AssertionError("model was never called")


def _secrets(model: ScriptedChatModel) -> dict[str, Any]:
    return {"_ai": model, "_route": "chat"}


def _check_input(reply: str, *, history: str = "阿柚：这季新番你们追了啥") -> Any:
    return checker._LLMCheckInput(
        bot_name                         = "小青",
        reply                            = reply,
        goal                             = "自然聊天",
        current_text                     = "小青你呢",
        policy_text                      = "",
        grounding_text                   = "",
        history_text                     = history,
        allow_low_stakes_persona_fiction = True,
        current_sender                   = "阿夏",
    )


_POLICY = checker._LLMRequestPolicy(1024, 5.0, 0, 0.2)


def _prefetch(
    cache: Any, model: ScriptedChatModel, *, history: str = "阿柚：这季新番你们追了啥"
) -> None:
    cache.prefetch(
        secrets            = _secrets(model),
        bot_name           = "小青",
        current_text       = "小青你呢",
        current_sender     = "阿夏",
        chat_history_text  = history,
        jargon_explanation = "",
        max_tokens         = 1024,
        timeout_seconds    = 5.0,
    )


@pytest.mark.asyncio
async def test_prefetched_reading_matches_inline_reading_and_is_reused():
    model = ScriptedChatModel(checker=_checker_script())
    cache = checker.TurnReadingCache()
    _prefetch(cache, model)
    await _wait_for_calls(model)
    assert len(_readings(model)) == 1
    prefetched = json.loads(_readings(model)[0].messages[-1]["content"])

    for reply in ("在追芙莉莲二期", "我也在看那部奇幻番"):
        result = await checker._semantic_turn_check(
            secrets        = _secrets(model),
            check_input    = _check_input(reply),
            request_policy = _POLICY,
            turn_reading   = cache,
        )
        assert result.suitable

    assert len(_readings(model)) == 1
    assert _readings(model)[0].route == "checker"

    await checker._read_turn_meaning(
        checker_secrets = checker._checker_secrets(_secrets(model)),
        check_input     = _check_input("候选不参加阅读"),
        request_policy  = _POLICY,
    )
    inline = json.loads(_readings(model)[-1].messages[-1]["content"])
    prefetched.pop("当前时间")
    inline.pop("当前时间")
    assert prefetched == inline
    await cache.aclose()


@pytest.mark.asyncio
async def test_reading_is_redone_when_dialogue_changes():
    model = ScriptedChatModel(checker=_checker_script())
    cache = checker.TurnReadingCache()
    _prefetch(cache, model)
    await _wait_for_calls(model)
    await checker._semantic_turn_check(
        secrets=_secrets(model),
        check_input=_check_input("在追", history="阿柚：这季新番你们追了啥\n阿梨：我在补旧番"),
        request_policy = _POLICY,
        turn_reading   = cache,
    )
    readings = _readings(model)
    assert len(readings) == 2
    assert "我在补旧番" in readings[-1].messages[-1]["content"]
    await cache.aclose()


@pytest.mark.asyncio
async def test_invalid_reading_is_not_reused_by_the_next_attempt():
    replies = iter([{"meaning": ""}, _READING])
    model = ScriptedChatModel(checker=_checker_script(reading=lambda _call: _json(next(replies))))
    cache = checker.TurnReadingCache()
    _prefetch(cache, model)
    first = await checker._semantic_turn_check(
        secrets        = _secrets(model),
        check_input    = _check_input("在追"),
        request_policy = _POLICY,
        turn_reading   = cache,
    )
    assert first.severity == "infra"
    second = await checker._semantic_turn_check(
        secrets        = _secrets(model),
        check_input    = _check_input("在追芙莉莲"),
        request_policy = _POLICY,
        turn_reading   = cache,
    )
    assert second.suitable
    assert len(_readings(model)) == 2
    await cache.aclose()


@pytest.mark.asyncio
async def test_checker_timeout_keeps_pending_reading_for_next_attempt():
    release = asyncio.Event()

    async def reading(_call: ModelCall) -> str:
        await release.wait()
        return _json(_READING)

    model = ScriptedChatModel(checker=_checker_script(reading=reading))
    cache = checker.TurnReadingCache()
    _prefetch(cache, model)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            checker._semantic_turn_check(
                secrets        = _secrets(model),
                check_input    = _check_input("在追"),
                request_policy = _POLICY,
                turn_reading   = cache,
            ),
            timeout=0.01,
        )
    release.set()
    result = await checker._semantic_turn_check(
        secrets        = _secrets(model),
        check_input    = _check_input("在追"),
        request_policy = _POLICY,
        turn_reading   = cache,
    )
    assert result.suitable
    assert len(_readings(model)) == 1
    await cache.aclose()


@pytest.mark.asyncio
async def test_generation_starts_reading_before_candidate_and_reuses_it_on_regen(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    reading_started = asyncio.Event()
    evidence_calls  = []

    def reading(_call: ModelCall) -> str:
        reading_started.set()
        return _json(_READING)

    def evidence(_call: ModelCall) -> str:
        evidence_calls.append(_call)
        return (
            checker_verdict(suitable=False, reason="没接住上下文", context_coherent=False)
            if len(evidence_calls) == 1
            else checker_verdict()
        )

    async def candidate(call: ModelCall) -> str:
        # 候选还在生成时，独立阅读必须已经发起：否则这里等到超时，整条测试失败。
        await asyncio.wait_for(reading_started.wait(), timeout=5)
        return f"第{len(model.calls_of('reply'))}稿"

    model = ScriptedChatModel(
        plan  = plan_answer("direct_reply"),
        reply = candidate,
        checker=_checker_script(reading=reading, evidence=evidence),
    )
    scripted_chat_context(
        mock_context,
        model,
        GROUP_GATE_OPEN,
        {
            "reply_check": {
                "llm_checker_mode"       : "always",
                "enable_semantic_checker": True,
                "max_regen"              : 1,
            }
        },
    )

    result = await handle_smalltalk(
        "小青你呢", chat_message_event(sample_group_event, "小青你呢"), mock_context
    )

    # 第一稿被审查挡下重生成，第二稿通过；两次审查共用同一份阅读。
    assert result == [{"type": "text", "data": {"text": "第2稿"}}]
    assert len(model.calls_of("reply")) == 2
    assert len(evidence_calls) == 2
    assert len(_readings(model)) == 1
    await ack_reply_delivery(result)
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_risk_mode_does_not_prefetch_reading(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    async def candidate(_call: ModelCall) -> str:
        # 让后台任务有机会跑：若这一轮预取了阅读，它此刻已经叫过审查模型。
        for _ in range(3):
            await asyncio.sleep(0)
        return "在追芙莉莲"

    model = ScriptedChatModel(plan=plan_answer("direct_reply"), reply=candidate)
    scripted_chat_context(
        mock_context,
        model,
        GROUP_GATE_OPEN,
        {"reply_check": {"llm_checker_mode": "risk", "enable_semantic_checker": True}},
    )

    result = await handle_smalltalk(
        "小青你呢", chat_message_event(sample_group_event, "小青你呢"), mock_context
    )

    assert result == [{"type": "text", "data": {"text": "在追芙莉莲"}}]
    assert model.calls_of("checker") == []
    await ack_reply_delivery(result)
    await settle_chat_background(mock_context, model)


# ---------------------------------------------------------------- 发送前延迟

# 读消息和打字的延迟固定成 3 秒（不随字数、不抖动），其余停顿关掉。
_FIXED_THREE_SECOND_DELAY = {
    "humanize": {
        "enable_typing_delay"    : True,
        "read_base_seconds"      : 3.0,
        "read_per_char_seconds"  : 0.0,
        "type_per_char_seconds"  : 0.0,
        "jitter_ratio"           : 0.0,
        "max_total_delay_seconds": 60.0,
        "interbubble_min_seconds": 0.0,
        "interbubble_max_seconds": 0.0,
    }
}


@contextmanager
def _watched_clock():
    """记录发送前的等待，并让测试能“花掉”若干秒：``asyncio.sleep`` 只记录、不真等，
    ``time.monotonic`` 加上测试累计花掉的秒数。返回（等待记录，花时间的函数）。"""

    sleeps: list[float] = []
    skew                = [0.0]
    real_sleep          = asyncio.sleep
    real_monotonic      = time.monotonic

    async def fake_sleep(delay, result=None):
        if delay > 0:
            sleeps.append(delay)
        await real_sleep(0)
        return result

    with (
        patch("asyncio.sleep", fake_sleep),
        patch("time.monotonic", lambda: real_monotonic() + skew[0]),
    ):
        yield sleeps, lambda seconds: skew.__setitem__(0, skew[0] + seconds)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spent,expected",
    [(0.0, 3.0), (1.0, 2.0), (8.0, 0.0)],
    ids=["nothing-spent", "partly-spent", "already-over"],
)
async def test_presend_delay_counts_time_already_spent(
    mock_context, sample_group_event, caplog, spent, expected
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    caplog.set_level(logging.INFO, logger="plugins.xiaoqing_chat")
    started = time.monotonic()
    with _watched_clock() as (sleeps, spend):

        def candidate(_call: ModelCall) -> str:
            spend(spent)
            return "在追"

        model = ScriptedChatModel(plan=plan_answer("direct_reply"), reply=candidate)
        scripted_chat_context(mock_context, model, GROUP_GATE_OPEN, _FIXED_THREE_SECOND_DELAY)
        result = await handle_smalltalk(
            "这季新番你们追了啥",
            chat_message_event(sample_group_event, "这季新番你们追了啥"),
            mock_context,
        )
    # 这一轮自己真实花掉的时间也算“已经花掉的”，等待只会因此更短。
    real_elapsed = time.monotonic() - started

    assert result == [{"type": "text", "data": {"text": "在追"}}]
    await ack_reply_delivery(result)
    (done,) = logged_events(caplog, "smalltalk.done")
    if expected == 0.0:
        assert sleeps == []
        assert "timings.send_wait_s" not in done.fields
    else:
        (waited,) = sleeps
        assert expected - real_elapsed - 0.01 <= waited <= expected + 0.01
        assert done.fields["timings.send_wait_s"] == pytest.approx(waited, abs=0.01)
    await settle_chat_background(mock_context, model)


def _persona_with_routine(tmp_path, routine: str) -> str:
    """人设卡文件路径；``routine`` 非空时带一条全天有效的作息。"""

    card = "## 你是谁\n- 学生。\n"
    if routine:
        card += f"\n## 作息〔作息〕\n- {routine}\n"
    path = tmp_path / "persona.md"
    path.write_text(card, encoding="utf-8")
    return str(path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scene", "routine", "slowed", "delay"),
    [
        pytest.param("addressed", "每天 00:00-24:00 睡觉（睡）", True, 6.0, id="addressed-asleep"),
        pytest.param("private", "每天 00:00-24:00 上课（忙）", True, 4.8, id="private-busy"),
        pytest.param("addressed", "", False, 0.0, id="addressed-free"),
        pytest.param("command", "每天 00:00-24:00 睡觉（睡）", False, 0.0, id="command-asleep"),
    ],
)
async def test_routine_slows_direct_replies_but_not_commands(
    mock_context, sample_group_event, sample_private_event, tmp_path, scene, routine, slowed, delay
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    if scene == "private":
        event = chat_message_event(sample_private_event, "醒着吗")
    elif scene == "command":
        event = chat_message_event(sample_group_event, "醒着吗", _xc_command_forced=True)
    else:
        event = chat_message_event(sample_group_event, "醒着吗")
        event["message"].insert(0, {"type": "at", "data": {"qq": str(event["self_id"])}})
    model = ScriptedChatModel(reply="醒着呢", plan=plan_answer("direct_reply"))
    scripted_chat_context(
        mock_context,
        model,
        _FIXED_THREE_SECOND_DELAY,
        {
            "personality": {"persona_file": _persona_with_routine(tmp_path, routine)},
            "life"       : {
                "enable_schedule_state": True,
                "busy_delay_factor"    : 1.6,
                "asleep_delay_factor"  : 2.0,
            },
        },
    )

    started = time.monotonic()
    with _watched_clock() as (sleeps, _spend):
        result = await handle_smalltalk("醒着吗", event, mock_context)
    # 这一轮自己真实花掉的时间会从等待里扣掉，等待只会因此更短。
    real_elapsed = time.monotonic() - started

    assert result == [{"type": "text", "data": {"text": "醒着呢"}}]
    if slowed:
        (waited,) = sleeps
        assert delay - real_elapsed - 0.01 <= waited <= delay + 0.01
    else:
        assert sleeps == []
    await ack_reply_delivery(result)
    await settle_chat_background(mock_context, model)


def test_timings_record_each_stage_and_stay_visible_in_the_log():
    from plugins.xiaoqing_chat.timing import accumulate_timing, append_timing

    timings: dict[str, Any] = {}
    accumulate_timing(timings, "context_check_s", 0.4)
    accumulate_timing(timings, "context_check_s", 0.35)
    append_timing(timings, "gen_s", 3.1)
    append_timing(timings, "gen_s", 2.8)
    append_timing(timings, "check", {"s": 1.9, "ok": False, "reason_code": "persona_grounding"})
    append_timing(timings, "check", {"s": 1.2, "ok": True, "reason_code": None})

    assert timings["context_check_s"] == 0.75
    # 每一次都按编号展开成单独的字段，日志里逐次可见。
    assert dict(LogEvent("smalltalk.done", timings=timings).fields) == {
        "timings.context_check_s"    : 0.75,
        "timings.gen_s.1"            : 3.1,
        "timings.gen_s.2"            : 2.8,
        "timings.check.1.s"          : 1.9,
        "timings.check.1.ok"         : False,
        "timings.check.1.reason_code": "persona_grounding",
        "timings.check.2.s"          : 1.2,
        "timings.check.2.ok"         : True,
    }
