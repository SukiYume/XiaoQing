"""网上生活接入：能力缺失、关闭、超时或报错时回复上下文与没有网上生活时逐字一致；有内容时并入各块。"""

from __future__ import annotations

import asyncio
import itertools
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from plugins.xiaoqing_chat import reply_plan
from plugins.xiaoqing_chat.chat_runtime import load_runtime
from plugins.xiaoqing_chat.expression.bw_expression_store import ExpressionRecord
from plugins.xiaoqing_chat.expression.bw_jargon_store import JargonRecord
from plugins.xiaoqing_chat.life.online import (
    EMPTY_ONLINE_LIFE,
    build_request,
    fetch_online_life,
    parse_response,
)
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.planning.planned_action import PlannedAction
from plugins.xiaoqing_chat.reply_plan import prepare_reply_generation
from plugins.xiaoqing_chat.store_binding import bound_state
from plugins.xiaoqing_chat.utils import wall_clock
from tests.helpers.settings_snapshot import settings_snapshot

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
CHAT_ID   = "g1"
ROUTINE   = "现在大概在看番"
JARGON    = "绝绝子"
USER_TEXT = "最近刷到啥好玩的了吗"
# 只有作息底色一项来自生活块：其余生活来源在配置里关掉。
LIFE_OFF = {
    "enable_schedule_state": False,
    "enable_daily_life"    : False,
    "enable_self_timeline" : False,
}


_REVISIONS = itertools.count(1)


@pytest.fixture(autouse=True)
def _fixed_clock_and_dice(monkeypatch: pytest.MonkeyPatch) -> None:
    """提示词里的“当前时间”精确到分钟，可发素材平常按概率给：固定墙钟和骰子，前后两次准备的结果才能逐字段比较。"""

    monkeypatch.setattr(wall_clock, "time", SimpleNamespace(time=lambda: 1_800_000_000.0))
    monkeypatch.setattr(reply_plan, "random", SimpleNamespace(random=lambda: 0.0))


def _context(
    tmp_path: Path,
    capability: Any = None,
    *,
    with_attribute: bool = True,
    **life: Any,
) -> SimpleNamespace:
    capabilities = SimpleNamespace(life_context=capability) if with_attribute else SimpleNamespace()
    config     = {"bot_name": "小青", "plugins": {"xiaoqing_chat": {"life": {**LIFE_OFF, **life}}}}
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir(exist_ok=True)
    # 运行配置按修订号缓存：每个上下文用新的修订号，部署配置的差异才会生效。
    revision = next(_REVISIONS)
    return SimpleNamespace(
        capabilities = capabilities,
        plugin_dir   = plugin_dir,
        data_dir     = tmp_path / "data",
        request_id   = "req-1",
        logger       = MagicMock(),
        get_settings_snapshot=lambda: settings_snapshot(config=config, revision=revision),
    )


async def _prepare(
    context: Any,
    *,
    expression: str                     = "",
    history: list[StoredMessage] | None = None,
    jargon: bool                        = True,
):
    """走真实的回复准备：历史、黑话、表达习惯都放进真实的存储里，由真实代码读出来。"""

    runtime = load_runtime(context)
    state   = bound_state(context)
    now     = time.time()
    if not state.memory_store.get(CHAT_ID):
        for message in (
            history
            if history is not None
            else [
                StoredMessage(role="user", name="甲", ts=now - 20, content=USER_TEXT),
                StoredMessage(
                    role="assistant", name="小青", ts=now - 10, content="我刚看了个猫猫视频"
                ),
            ]
        ):
            state.memory_store.append(
                CHAT_ID,
                role    = message.role,
                name    = message.name,
                user_id = None if message.role == "assistant" else 2,
                content = message.content,
                ts      = message.ts,
            )
    if jargon:
        state.bw_jargon_store.save(
            [JargonRecord(content=JARGON, scope_chat_id=CHAT_ID, meaning="很棒", confirmed=True)]
        )
    if expression:
        state.bw_expr_store.save(
            [ExpressionRecord("expr-1", CHAT_ID, "刷到好玩的", expression, checked=True)]
        )
    return await prepare_reply_generation(
        text    = USER_TEXT,
        event   = {"group_id": 1, "user_id": 2, "_xc_routine_text": ROUTINE},
        context = context,
        runtime = runtime,
        state   = state,
        forced  = False,
        action=PlannedAction(think_level=1, reasoning=""),
        plan_reasoning = "",
        bot_name       = "小青",
        secrets        = {"model": "m"},
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


@pytest_asyncio.fixture
async def baseline(tmp_path: Path) -> dict[str, Any]:
    plan = await _prepare(_context(tmp_path, None))
    assert ROUTINE in plan.life_block and JARGON in plan.jargon_explanation
    assert plan.expression_block == ""
    return _fields(plan)


@pytest.mark.asyncio
async def test_missing_capability_leaves_the_reply_context_unchanged(baseline, tmp_path) -> None:
    assert _fields(await _prepare(_context(tmp_path, with_attribute=False))) == baseline
    context = _context(tmp_path, None)
    await _prepare(context)
    assert not any("online_life" in str(call) for call in context.logger.info.call_args_list)


@pytest.mark.asyncio
async def test_disabled_switch_never_calls_the_capability(baseline, tmp_path) -> None:
    capability = Capability({"life_lines": "不该出现", "slang_notes": "不该出现"})
    plan = await _prepare(_context(tmp_path, capability, enable_online_life=False))
    assert _fields(plan) == baseline and capability.requests == []


@pytest.mark.asyncio
async def test_timeout_leaves_the_reply_context_unchanged_and_logs_only_the_error_type(
    baseline, tmp_path
) -> None:
    capability = Capability({"life_lines": "迟到的内容"}, delay=2.0)
    context = _context(tmp_path, capability, online_life_timeout_seconds=0.05)
    started = time.monotonic()
    plan    = await _prepare(context)
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
async def test_errors_and_malformed_results_leave_the_reply_context_unchanged(
    baseline, capability, tmp_path
) -> None:
    context = _context(tmp_path, capability)
    assert _fields(await _prepare(context)) == baseline
    assert not any("SECRET_ERROR_TEXT" in str(call) for call in context.logger.info.call_args_list)


@pytest.mark.asyncio
async def test_content_is_merged_into_life_jargon_and_expression_blocks(baseline, tmp_path) -> None:
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
    context = _context(tmp_path, capability)
    plan    = await _prepare(context)
    assert plan.life_block == baseline["life_block"] + "\n" + capability.result["life_lines"]
    assert (
        plan.jargon_explanation
        == baseline["jargon_explanation"] + "\n" + capability.result["slang_notes"]
    )
    assert plan.expression_block == "当想夸可爱时：X基础，Y不基础"
    logged = [str(call.args) for call in context.logger.info.call_args_list]
    assert any('"step": "reply.online_life"' in line and '"episodes": 1' in line for line in logged)
    assert not any("猫猫的一天" in line for line in logged)


@pytest.mark.asyncio
async def test_expressions_learned_in_the_group_take_precedence_over_the_hint(tmp_path) -> None:
    group_only = await _prepare(_context(tmp_path, None), expression="群里学到的表达")
    assert "群里学到的表达" in group_only.expression_block

    capability = Capability(
        {"life_lines": "", "slang_notes": "", "expression_hint": "网上学来的表达"}
    )
    plan = await _prepare(_context(tmp_path, capability), expression="群里学到的表达")
    assert plan.expression_block == group_only.expression_block
    assert "网上学来的表达" not in plan.expression_block
    assert plan.life_block == group_only.life_block
    assert plan.jargon_explanation == group_only.jargon_explanation


@pytest.mark.asyncio
async def test_request_has_no_ids_and_carries_recent_turns(tmp_path) -> None:
    capability = Capability({"life_lines": "x"})
    await _prepare(_context(tmp_path, capability), jargon=False)
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
    assert request["unknown_terms"] == []  # 没确认意思的说法来自本群黑话库，这里没有
    assert "1" not in str(sorted(request)) and "user_id" not in str(request)


@pytest.mark.asyncio
async def test_words_the_group_block_explained_go_to_life_so_it_does_not_repeat_them(
    tmp_path,
) -> None:
    capability = Capability({"life_lines": "x"})
    await _prepare(_context(tmp_path, capability))
    assert capability.requests[0]["explained_terms"] == [JARGON]


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
async def test_fetch_online_life_propagates_cancellation(tmp_path) -> None:
    capability = Capability({"life_lines": "x"}, delay=5.0)
    context = _context(tmp_path, capability)
    task    = asyncio.create_task(
        fetch_online_life(
            context,
            load_runtime(context),
            text       = "t",
            history    = [],
            is_private = False,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_proactive_share_request_carries_the_topics_the_group_still_remembers(tmp_path):
    """主动开口时把记忆里这个群还记着的话题词交给网上生活（最多 20 个），不只交最新三个。"""

    from plugins.xiaoqing_chat.life.online import fetch_share_candidates
    from plugins.xiaoqing_chat.memory.client import parse_recall

    requests: list[dict[str, Any]] = []

    class Life:
        async def turn_context(self, request: dict[str, Any]) -> dict[str, Any]:
            requests.append(request)
            return {"version": 1, "candidates": []}

    context  = _context(tmp_path, Life())
    recalled = parse_recall(
        {"topics": [{"topic": f"话题{i}", "keywords": [f"词{i}"]} for i in range(25)]}
    )

    await fetch_share_candidates(
        context, load_runtime(context), exclude_ids=[], topics=recalled.topics
    )

    assert len(recalled.topics) == 25
    assert [topic["topic"] for topic in requests[0]["topics"]] == [f"话题{i}" for i in range(20)]
