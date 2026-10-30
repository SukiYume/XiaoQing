"""回复耗时：本轮阅读与生成并行、重生成复用阅读，以及发送前延迟扣除已用时间。"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
from plugins.xiaoqing_chat.llm import reply_checker as checker
from plugins.xiaoqing_chat.llm.check_result import ReplyCheckResult
from plugins.xiaoqing_chat.reply_draft import RejectedCandidate

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


def _remote_json(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    return {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}, "test"


def _is_reading(call: Any) -> bool:
    return str(call.kwargs["prompt"]).startswith(checker._TURN_READING_PROMPT)


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


def _prefetch(cache: Any, *, history: str = "阿柚：这季新番你们追了啥") -> None:
    cache.prefetch(
        secrets            = {"_ai": object(), "_route": "chat"},
        bot_name           = "小青",
        current_text       = "小青你呢",
        current_sender     = "阿夏",
        chat_history_text  = history,
        jargon_explanation = "",
        max_tokens         = 1024,
        timeout_seconds    = 5.0,
    )


@pytest.mark.asyncio
async def test_prefetched_reading_matches_inline_reading_and_is_reused(monkeypatch):
    remote = AsyncMock(
        side_effect=lambda **kw: _remote_json(
            _READING if _is_reading(Mock(kwargs=kw)) else _VERDICT
        )
    )
    monkeypatch.setattr(checker, "_request_checker_completion", remote)
    cache = checker.TurnReadingCache()
    _prefetch(cache)
    await asyncio.sleep(0)
    assert sum(_is_reading(call) for call in remote.call_args_list) == 1
    prefetched = json.loads(remote.call_args_list[0].kwargs["materials"])

    for reply in ("在追芙莉莲二期", "我也在看那部奇幻番"):
        result = await checker._semantic_turn_check(
            secrets        = {"_ai": object(), "_route": "chat"},
            check_input    = _check_input(reply),
            request_policy = _POLICY,
            turn_reading   = cache,
        )
        assert result.suitable

    readings = [call for call in remote.call_args_list if _is_reading(call)]
    assert len(readings) == 1
    assert readings[0].kwargs["checker_secrets"]["_route"] == "checker"

    remote.reset_mock()
    await checker._read_turn_meaning(
        checker_secrets = checker._checker_secrets({"_ai": object(), "_route": "chat"}),
        check_input     = _check_input("候选不参加阅读"),
        request_policy  = _POLICY,
    )
    inline = json.loads(remote.call_args.kwargs["materials"])
    prefetched.pop("当前时间")
    inline.pop("当前时间")
    assert prefetched == inline
    await cache.aclose()


@pytest.mark.asyncio
async def test_reading_is_redone_when_dialogue_changes(monkeypatch):
    remote = AsyncMock(
        side_effect=lambda **kw: _remote_json(
            _READING if _is_reading(Mock(kwargs=kw)) else _VERDICT
        )
    )
    monkeypatch.setattr(checker, "_request_checker_completion", remote)
    cache = checker.TurnReadingCache()
    _prefetch(cache)
    await asyncio.sleep(0)
    await checker._semantic_turn_check(
        secrets={"_ai": object()},
        check_input=_check_input("在追", history="阿柚：这季新番你们追了啥\n阿梨：我在补旧番"),
        request_policy = _POLICY,
        turn_reading   = cache,
    )
    readings = [call for call in remote.call_args_list if _is_reading(call)]
    assert len(readings) == 2
    assert "我在补旧番" in readings[-1].kwargs["materials"]
    await cache.aclose()


@pytest.mark.asyncio
async def test_invalid_reading_is_not_reused_by_the_next_attempt(monkeypatch):
    replies = iter([{"meaning": ""}, _READING])
    remote  = AsyncMock(
        side_effect=lambda **kw: _remote_json(
            next(replies) if _is_reading(Mock(kwargs=kw)) else _VERDICT
        )
    )
    monkeypatch.setattr(checker, "_request_checker_completion", remote)
    cache = checker.TurnReadingCache()
    _prefetch(cache)
    first = await checker._semantic_turn_check(
        secrets        = {"_ai": object()},
        check_input    = _check_input("在追"),
        request_policy = _POLICY,
        turn_reading   = cache,
    )
    assert first.severity == "infra"
    second = await checker._semantic_turn_check(
        secrets        = {"_ai": object()},
        check_input    = _check_input("在追芙莉莲"),
        request_policy = _POLICY,
        turn_reading   = cache,
    )
    assert second.suitable
    assert sum(_is_reading(call) for call in remote.call_args_list) == 2
    await cache.aclose()


@pytest.mark.asyncio
async def test_checker_timeout_keeps_pending_reading_for_next_attempt(monkeypatch):
    release = asyncio.Event()

    async def remote(**kwargs):
        if str(kwargs["prompt"]).startswith(checker._TURN_READING_PROMPT):
            await release.wait()
            return _remote_json(_READING)
        return _remote_json(_VERDICT)

    calls = AsyncMock(side_effect=remote)
    monkeypatch.setattr(checker, "_request_checker_completion", calls)
    cache = checker.TurnReadingCache()
    _prefetch(cache)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            checker._semantic_turn_check(
                secrets        = {"_ai": object()},
                check_input    = _check_input("在追"),
                request_policy = _POLICY,
                turn_reading   = cache,
            ),
            timeout=0.01,
        )
    release.set()
    result = await checker._semantic_turn_check(
        secrets        = {"_ai": object()},
        check_input    = _check_input("在追"),
        request_policy = _POLICY,
        turn_reading   = cache,
    )
    assert result.suitable
    assert sum(_is_reading(call) for call in calls.call_args_list) == 1
    await cache.aclose()


def _generation_plan(mode: str = "always") -> SimpleNamespace:
    return SimpleNamespace(
        chat_id            = "g1",
        event              = {},
        history            = [],
        max_context_size   = 5,
        secrets            = {"_ai": object(), "_route": "chat"},
        bot_name           = "小青",
        text               = "小青你呢",
        jargon_explanation = "",
        forced             = True,
        context            = Mock(),
        started_at         = time.monotonic(),
        runtime            = SimpleNamespace(
            cfg=SimpleNamespace(
                llm_config  = XiaoQingChatConfig().llm_config,
                reply_check = SimpleNamespace(
                    enable_reply_checker    = True,
                    enable_llm_checker      = True,
                    enable_semantic_checker = True,
                    llm_checker_mode        = mode,
                    max_tokens              = 1024,
                    timeout_seconds         = 5.0,
                ),
                debug=SimpleNamespace(log_steps=False),
            )
        ),
    )


@pytest.mark.asyncio
async def test_generation_starts_reading_before_candidate_and_reuses_it_on_regen(monkeypatch):
    from plugins.xiaoqing_chat import reply_generator

    remote = AsyncMock(
        side_effect=lambda **kw: _remote_json(
            _READING if _is_reading(Mock(kwargs=kw)) else _VERDICT
        )
    )
    monkeypatch.setattr(checker, "_request_checker_completion", remote)
    plan                      = _generation_plan()
    readings_before_candidate = []

    async def request_candidate(plan, payload_messages):
        await asyncio.sleep(0)
        readings_before_candidate.append(sum(_is_reading(c) for c in remote.call_args_list))
        return "候选"

    caches = []

    async def check_candidate(plan, draft, trimmed_history, memory_block, turn_reading):
        caches.append(turn_reading)
        await turn_reading.read(
            checker_secrets=checker._checker_secrets(plan.secrets),
            check_input=_check_input("候选", history="阿夏：小青你呢"),
            request_policy=_POLICY,
        )
        if len(caches) == 1:
            return RejectedCandidate(
                text   = "候选",
                result = ReplyCheckResult(False, "太像客服", True, "soft", "style"),
            )
        return None

    draft = SimpleNamespace(text="候选")
    with patch.multiple(
        reply_generator,
        prepare_reply_generation=AsyncMock(return_value=plan),
        _is_turn_stale=Mock(return_value=False),
        _load_attempt_memory=AsyncMock(return_value=""),
        _build_attempt_messages=Mock(return_value=[]),
        _checker_history_text=Mock(return_value="阿夏：小青你呢"),
        event_sender_name=Mock(return_value="阿夏"),
        _request_reply_candidate=request_candidate,
        _precheck_candidate=Mock(return_value=None),
        _resolve_candidate_draft=AsyncMock(return_value=draft),
        _check_candidate_draft=check_candidate,
        queue_reply_regeneration=Mock(return_value=True),
        log_step=Mock(),
    ):
        result = await reply_generator.generate_reply_draft(
            text           = plan.text,
            event          = {},
            context        = plan.context,
            runtime        = plan.runtime,
            state          = Mock(),
            forced         = True,
            action         = Mock(),
            plan_reasoning = "",
        )

    assert result is draft
    assert readings_before_candidate == [1, 1]
    assert sum(_is_reading(call) for call in remote.call_args_list) == 1
    assert caches[0] is caches[1]
    assert caches[0]._task is None


@pytest.mark.asyncio
async def test_risk_mode_does_not_prefetch_reading(monkeypatch):
    from plugins.xiaoqing_chat import reply_generator

    remote = AsyncMock(return_value=_remote_json(_READING))
    monkeypatch.setattr(checker, "_request_checker_completion", remote)
    plan = _generation_plan(mode="risk")
    attempt = reply_generator.ReplyAttemptState(
        max_items=5, turn_reading=reply_generator._semantic_reading_cache(plan)
    )
    with patch.object(reply_generator, "_checker_history_text", Mock(return_value="")):
        reply_generator._prefetch_turn_reading(plan, attempt, [])
    await asyncio.sleep(0)
    remote.assert_not_awaited()
    assert attempt.turn_reading is not None
    await attempt.turn_reading.aclose()


def _finalization(started_at: float) -> Any:
    from plugins.xiaoqing_chat.turn_delivery import _SmalltalkFinalization

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            llm_config=XiaoQingChatConfig().llm_config,
            humanize=SimpleNamespace(apply_to_forced=False),
        )
    )
    return _SmalltalkFinalization(
        prepared=SimpleNamespace(forced=False, text="这季新番你们追了啥"),
        generated = SimpleNamespace(),
        event     = {},
        context   = Mock(),
        hctx=SimpleNamespace(runtime=runtime, chat_id="g1"),
        started_at=started_at,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spent,target,expected",
    [(0.0, 3.0, 3.0), (1.0, 3.0, 2.0), (8.0, 3.0, 0.0)],
)
async def test_presend_delay_counts_time_already_spent(spent, target, expected):
    from plugins.xiaoqing_chat import turn_delivery

    sleep = AsyncMock()
    with (
        patch.object(turn_delivery, "_compute_typing_delay", return_value=target),
        patch.object(turn_delivery, "display_reply_text", return_value="在追"),
        patch.object(turn_delivery, "log_step"),
        patch.object(turn_delivery.asyncio, "sleep", sleep),
    ):
        applied = await turn_delivery._apply_smalltalk_presend_delay(
            _finalization(time.monotonic() - spent), has_outbound_batches=True
        )

    assert applied is True
    if expected == 0.0:
        sleep.assert_not_awaited()
    else:
        assert sleep.await_args.args[0] == pytest.approx(expected, abs=0.2)
