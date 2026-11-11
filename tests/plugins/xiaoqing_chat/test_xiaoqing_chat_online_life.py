"""网上生活接入：能力缺失、关闭、超时或报错时回复上下文与未接入前逐字一致；有内容时并入各块。"""

from __future__ import annotations

import asyncio
import time
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
import pytest_asyncio

from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
from plugins.xiaoqing_chat.life.online import (
    EMPTY_ONLINE_LIFE,
    build_request,
    fetch_online_life,
    parse_response,
)
from plugins.xiaoqing_chat.llm.llm_config import LLMCallConfig
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.planning.planned_action import PlannedAction
from plugins.xiaoqing_chat.reply_plan import prepare_reply_generation

COMPARED_FIELDS = (
    "life_block",
    "jargon_explanation",
    "expression_block",
    "profile_block",
    "policy_block",
    "knowledge_block",
    "media_candidates",
    "tool_info_block",
    "effective_identity",
    "effective_style",
    "open_loop_block",
    "merged_reasoning",
    "effective_goal",
    "turn_note",
    "state_text",
    "text",
    "bot_name",
    "chat_id",
)
BASE_LIFE   = "作息底色：现在大概在看番"
BASE_JARGON = "群里的黑话：绝绝子=很棒"


def _context(capability: Any = None, *, with_attribute: bool = True) -> SimpleNamespace:
    capabilities = SimpleNamespace(life_context=capability) if with_attribute else SimpleNamespace()
    return SimpleNamespace(
        capabilities = capabilities,
        data_dir     = "/tmp/xc",
        request_id   = "req-1",
        logger       = MagicMock(),
    )


def _runtime(**life: Any) -> SimpleNamespace:
    cfg = XiaoQingChatConfig()
    for name, value in life.items():
        setattr(cfg.life, name, value)
    return SimpleNamespace(cfg=cfg)


def _state(history: list[StoredMessage]) -> MagicMock:
    state = MagicMock()
    state.memory_store.get_recent_async = AsyncMock(return_value=history)
    state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal=""))
    state.review_store.bind = Mock()
    return state


async def _prepare(
    context: Any, runtime: Any, *, expression: str = "", history: list[StoredMessage] | None = None
):
    now     = time.time()
    history = (
        history
        if history is not None
        else [
            StoredMessage(role="user", name="甲", ts=now - 20, content="最近刷到啥好玩的了吗"),
            StoredMessage(role="assistant", name="小青", ts=now - 10, content="我刚看了个猫猫视频"),
        ]
    )
    action = PlannedAction(
        action="reply", think_level=1, reasoning="", question="", unknown_words=["绝绝子", "哈基米"]
    )
    with ExitStack() as stack:
        for name, value in {
            "configured_bot_name": "小青",
            "event_chat_id"      : "g1",
            "is_private_event"   : False,
            "ai_route_context"   : {"model": "m"},
            "llm_call_config"    : LLMCallConfig(
                timeout_seconds=3.0, max_retry=0, retry_interval_seconds=0.2
            ),
            "sender_profile_context": "",
            "knowledge_context"     : "",
            "expression_context"    : expression,
            "jargon_context"        : BASE_JARGON,
            "_load_life_blocks"     : (BASE_LIFE, ""),
        }.items():
            stack.enter_context(
                patch(f"plugins.xiaoqing_chat.reply_plan.{name}", return_value=value)
            )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_plan.tool_status_context",
                new=AsyncMock(return_value=""),
            )
        )
        return await prepare_reply_generation(
            text           = "最近刷到啥好玩的了吗",
            event          = {"group_id": 1, "user_id": 2},
            context        = context,
            runtime        = runtime,
            state          = _state(history),
            forced         = False,
            action         = action,
            plan_reasoning = "",
            bot_name       = "小青",
            secrets        = None,
            turn_note      = "",
            state_text     = "",
            is_brain_chat  = False,
        )


def _fields(plan: Any) -> dict[str, Any]:
    return {name: getattr(plan, name) for name in COMPARED_FIELDS}


class Capability:
    def __init__(
        self, result: Any = None, *, delay: float = 0.0, error: Exception | None = None
    ) -> None:
        self.result, self.delay, self.error = result, delay, error
        self.requests: list[dict[str, Any]] = []

    async def turn_context(self, payload: dict[str, Any]) -> Any:
        self.requests.append(payload)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result


@pytest.fixture(autouse=True)
def _always_offer_media(monkeypatch) -> None:
    """可发素材平常按概率给；这里固定给，前后两次准备的结果才能逐字段比较。"""

    monkeypatch.setattr(
        "plugins.xiaoqing_chat.reply_plan.offers_outbound_media", lambda *_args, **_kwargs: True
    )


@pytest_asyncio.fixture
async def baseline() -> dict[str, Any]:
    plan = await _prepare(_context(None), _runtime())
    assert plan.life_block == BASE_LIFE and plan.jargon_explanation == BASE_JARGON
    assert plan.expression_block == ""
    return _fields(plan)


@pytest.mark.asyncio
async def test_missing_capability_is_identical_to_before(baseline) -> None:
    assert _fields(await _prepare(_context(with_attribute=False), _runtime())) == baseline
    context = _context(None)
    await _prepare(context, _runtime())
    assert not any("online_life" in str(call) for call in context.logger.info.call_args_list)


@pytest.mark.asyncio
async def test_disabled_switch_never_calls_the_capability(baseline) -> None:
    capability = Capability({"life_lines": "不该出现", "slang_notes": "不该出现"})
    plan = await _prepare(_context(capability), _runtime(enable_online_life=False))
    assert _fields(plan) == baseline and capability.requests == []


@pytest.mark.asyncio
async def test_timeout_is_identical_to_before_and_logs_only_the_error_type(baseline) -> None:
    capability = Capability({"life_lines": "迟到的内容"}, delay=2.0)
    context = _context(capability)
    started = time.monotonic()
    plan = await _prepare(context, _runtime(online_life_timeout_seconds=0.05))
    assert time.monotonic() - started < 1.0
    assert _fields(plan) == baseline
    logged = [str(call.args) for call in context.logger.info.call_args_list]
    assert any("reply.online_life.error" in line and "TimeoutError" in line for line in logged)
    assert not any("迟到的内容" in line for line in logged)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capability",
    [
        Capability(error=RuntimeError("SECRET_ERROR_TEXT")),
        Capability(error=ValueError("x")),
        Capability(["not", "a", "mapping"]),
        Capability("字符串"),
        Capability({"life_lines": 5, "slang_notes": None, "expression_hint": ["x"]}),
        Capability(None),
        Capability({}),
    ],
)
async def test_errors_and_malformed_results_are_identical_to_before(baseline, capability) -> None:
    context = _context(capability)
    assert _fields(await _prepare(context, _runtime())) == baseline
    assert not any("SECRET_ERROR_TEXT" in str(call) for call in context.logger.info.call_args_list)


@pytest.mark.asyncio
async def test_content_is_merged_into_life_jargon_and_expression_blocks() -> None:
    capability = Capability(
        {
            "version"        : 1,
            "life_lines"     : "你最近上网看到的：\n- 今天 12:20 刷B站热门，点开了「猫猫的一天」",
            "slang_notes"    : "你在网上学到的说法：\n- 「哈基米」：对猫咪的爱称",
            "expression_hint": "当想夸可爱时：X基础，Y不基础",
            "episode_ids"    : ["e1"],
            "term_keys"      : ["哈基米"],
        }
    )
    context = _context(capability)
    plan    = await _prepare(context, _runtime())
    assert plan.life_block == BASE_LIFE + "\n" + capability.result["life_lines"]
    assert plan.jargon_explanation == BASE_JARGON + "\n" + capability.result["slang_notes"]
    assert plan.expression_block == "当想夸可爱时：X基础，Y不基础"
    logged = [str(call.args) for call in context.logger.info.call_args_list]
    assert any('"step": "reply.online_life"' in line and '"episodes": 1' in line for line in logged)
    assert not any("猫猫的一天" in line for line in logged)


@pytest.mark.asyncio
async def test_expressions_learned_in_the_group_take_precedence_over_the_hint() -> None:
    capability = Capability(
        {"life_lines": "", "slang_notes": "", "expression_hint": "网上学来的表达"}
    )
    plan = await _prepare(_context(capability), _runtime(), expression="群里学到的表达")
    assert plan.expression_block == "群里学到的表达"
    assert plan.life_block == BASE_LIFE and plan.jargon_explanation == BASE_JARGON


@pytest.mark.asyncio
async def test_request_has_no_ids_and_carries_recent_turns_and_unknown_words() -> None:
    capability = Capability({"life_lines": "x"})
    await _prepare(_context(capability), _runtime())
    request = capability.requests[0]
    assert set(request) == {
        "version",
        "mode",
        "text",
        "recent",
        "bot_recent",
        "is_private",
        "unknown_terms",
    }
    assert request["version"] == 1 and request["mode"] == "reply" and request["is_private"] is False
    assert request["bot_recent"] == ["我刚看了个猫猫视频"]
    assert request["recent"] == ["最近刷到啥好玩的了吗", "我刚看了个猫猫视频"]
    assert request["unknown_terms"] == ["绝绝子", "哈基米"]
    assert "1" not in str(sorted(request)) and "user_id" not in str(request)


def test_request_and_response_are_bounded() -> None:
    history = [
        StoredMessage(role="user", name="甲", ts=float(n), content="长" * 300) for n in range(12)
    ]
    request = build_request(
        text          = "字" * 900,
        history       = history,
        is_private    = True,
        unknown_words = ["超长的词" * 10, "", "b", "c", "d"],
    )
    assert len(request["text"]) == 500 and len(request["recent"]) == 8
    assert all(len(item) == 200 for item in request["recent"])
    assert (
        request["unknown_terms"][0] == ("超长的词" * 10)[:12] and len(request["unknown_terms"]) == 3
    )

    parsed = parse_response(
        {
            "life_lines"     : "a" * 2000,
            "slang_notes"    : "b" * 2000,
            "expression_hint": "c" * 2000,
            "episode_ids"    : ["1", "2"],
            "term_keys"      : ["k"],
        }
    )
    assert (len(parsed.life_lines), len(parsed.slang_notes), len(parsed.expression_hint)) == (
        800,
        600,
        200,
    )
    assert (parsed.episode_count, parsed.term_count) == (2, 1)
    assert parse_response(None) is EMPTY_ONLINE_LIFE and EMPTY_ONLINE_LIFE.empty


@pytest.mark.asyncio
async def test_fetch_online_life_propagates_cancellation() -> None:
    capability = Capability({"life_lines": "x"}, delay=5.0)
    task = asyncio.create_task(
        fetch_online_life(
            _context(capability),
            _runtime(),
            text          = "t",
            history       = [],
            is_private    = False,
            unknown_words = [],
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
