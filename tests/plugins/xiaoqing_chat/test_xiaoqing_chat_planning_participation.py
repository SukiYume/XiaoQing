"""规划、目标和群聊参与。"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from plugins.xiaoqing_chat.config.config import PersonalityConfig, XiaoQingChatConfig
from plugins.xiaoqing_chat.llm.llm_config import LLMCallConfig
from plugins.xiaoqing_chat.planning.goal_state import GoalState
from plugins.xiaoqing_chat.planning.pfc_state import PFCConversationState
from plugins.xiaoqing_chat.store_binding import bound_state
from tests.helpers.assertions import logged_events
from tests.helpers.xiaoqing_chat_test_support import (
    GROUP_GATE_CLOSED,
    GROUP_GATE_OPEN,
    ScriptedChatModel,
    ack_reply_delivery,
    active_review_sessions,
    chat_message_event,
    goals_answer,
    plan_answer,
    scripted_chat_context,
    set_context_principal,
    settle_chat_background,
)

mock_context       = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event


def test_pfc_state_set_state_refreshes_updated_at():
    from plugins.xiaoqing_chat.planning.pfc_state import PFCConversationState, PFCStateStore

    store = PFCStateStore()
    state = PFCConversationState(chat_id="g1", updated_at=1.0)

    before = state.updated_at
    store.set_state("g1", state)

    assert state.updated_at > before
    assert store._cache["g1"] is state


def _fill_pfc_cache(store, count: int, *, dirty: bool = False) -> None:
    """往规划状态缓存里放 ``count`` 个无关会话；``dirty`` 为真时它们都带着未落盘的修改。"""

    for index in range(count):
        chat_id = f"filler-{index}"
        if dirty:
            store.set_state(chat_id, PFCConversationState(chat_id=chat_id))
        else:
            store.get(chat_id)


def test_pfc_dirty_state_survives_cache_pressure_until_delayed_save(tmp_path):
    from plugins.xiaoqing_chat.planning.pfc_state import PFCConversationState, PFCStateStore

    store = PFCStateStore()
    store.bind(tmp_path)
    dirty = PFCConversationState(
        chat_id   = "dirty",
        goal_list = [{"goal": "must survive delayed persistence"}],
    )
    store.set_state("dirty", dirty)

    # A debounced save has not run yet while unrelated conversations overfill the cache.
    _fill_pfc_cache(store, store._MAX_CACHE_SIZE + 1)

    assert store._cache["dirty"] is dirty

    store.save("dirty")

    reloaded = PFCStateStore()
    reloaded.bind(tmp_path)
    assert reloaded.get("dirty").goal_list == [{"goal": "must survive delayed persistence"}]
    assert len(store._cache) <= store._MAX_CACHE_SIZE


@pytest.mark.parametrize("clear_each", [True, False])
def test_pfc_turn_records_follow_cache_lifetime(clear_each):
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore

    store = PFCStateStore()
    for index in range(1000):
        chat_id = f"g{index}"
        turn    = store.begin_turn(chat_id)
        assert store.claim_commit(chat_id, turn)
        if clear_each:
            store.clear(chat_id)
    assert len(store._committed_turns) <= (0 if clear_each else store._MAX_CACHE_SIZE)
    assert set(store._committed_turns) <= set(store._cache)
    assert not store.claim_commit("g0", 1)


@pytest.mark.parametrize("cleanup", ["clear", "evict"])
def test_pfc_recreated_chat_rejects_old_turns(cleanup):
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore

    store    = PFCStateStore()
    old_turn = store.begin_turn("g1")
    assert store.claim_commit("g1", old_turn)
    if cleanup == "clear":
        store.clear("g1")
    else:
        _fill_pfc_cache(store, store._MAX_CACHE_SIZE)
        assert "g1" not in store._cache
    assert not store.claim_commit("g1", old_turn)
    new_turn = store.begin_turn("g1")
    assert new_turn > old_turn
    assert not store.claim_commit("g1", old_turn)
    assert store.claim_commit("g1", new_turn)


def test_pfc_dirty_turn_guard_survives_until_saved(tmp_path):
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore

    store = PFCStateStore()
    store.bind(tmp_path)
    first  = store.begin_turn("g1")
    second = store.begin_turn("g1")
    assert store.claim_commit("g1", second)
    store.set_state("g1", PFCConversationState(chat_id="g1", ended=True))
    # 缓存里全是没落盘的状态：谁也淘汰不了，没落盘的 g1 连同它的轮次登记都留着。
    _fill_pfc_cache(store, store._MAX_CACHE_SIZE, dirty=True)
    store.get("g2")
    assert "g1" in store._cache
    assert store.claim_commit("g1", second)
    assert not store.claim_commit("g1", first)
    store.save("g1")
    assert set(store._committed_turns) <= set(store._cache)
    reloaded = PFCStateStore()
    reloaded.bind(tmp_path)
    assert reloaded.get("g1").ended


@pytest.mark.asyncio
async def test_forced_private_turn_records_the_derived_goal(mock_context, sample_private_event):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    model = ScriptedChatModel(reply="ok")
    scripted_chat_context(mock_context, model)
    event = chat_message_event(sample_private_event, "你好", _xc_command_forced=True)

    result = await handle_smalltalk("你好", event, mock_context)

    assert result == [{"type": "text", "data": {"text": "ok"}}]
    goal = bound_state(mock_context).goal_store.get("u12345")
    assert (goal.goal, goal.source) == ('围绕"你好"继续聊', "user")
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_should_reply_uses_async_goal_and_heartflow_state():
    from plugins.xiaoqing_chat.frequency_control import decide_reply

    runtime                                      = MagicMock()
    runtime.cfg.goal.enable_goal                 = True
    runtime.cfg.min_reply_interval_seconds       = 0.0
    runtime.cfg.max_replies_per_minute           = 0
    runtime.cfg.reply_probability_base           = 0.6
    runtime.cfg.heartflow.enable_heartflow       = True
    runtime.cfg.heartflow.base_score             = 0.2
    runtime.cfg.heartflow.weight_short_text      = -0.08
    runtime.cfg.heartflow.weight_no_reply_streak = 0.05
    runtime.cfg.heartflow.weight_long_silence    = 0.08

    state                                            = MagicMock()
    state.get_last_reply_ts.return_value             = 0.0
    state.get_continuous_cooldown_until.return_value = 0.0
    state.get_reply_timestamps.return_value          = []
    state.set_reply_timestamps                       = Mock()
    state.goal_store.get_async = AsyncMock(return_value=GoalState(goal="回答用户问题"))
    state.goal_store.get.side_effect = AssertionError("sync goal store read should not be used")
    state.heartflow.score_async = AsyncMock(return_value=0.7)
    state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))
    state.heartflow._load.side_effect = AssertionError("sync heartflow load should not be used")

    with patch("plugins.xiaoqing_chat.frequency_control.random.random", return_value=0.0):
        result = await decide_reply(runtime, state, "g1", "你好？", False, False)

    assert result is True
    assert state.goal_store.get_async.await_count == 1
    assert state.heartflow.get_async.await_count == 1


# 群里一条没叫她的闲聊；门控关着（概率为 0），这一轮不会回复。
_SYNC_NOTICE = "今天先同步一下近期排期和联调情况，暂时不需要你立即回复"


@pytest.mark.asyncio
async def test_ordinary_group_turn_refreshes_goal_after_should_reply_blocks(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    chat_id = "g67890"
    model   = ScriptedChatModel()
    scripted_chat_context(mock_context, model, GROUP_GATE_CLOSED)
    state = bound_state(mock_context)
    # 她刚在群里说过话：本轮目标要是在门控之前就记下，这一轮会被算成“活跃话题”。
    state.set_last_reply_ts(chat_id, time.time() - 1)

    result = await handle_smalltalk(
        _SYNC_NOTICE, chat_message_event(sample_group_event, _SYNC_NOTICE), mock_context
    )

    assert result == []
    decision = state.get_reply_gate_decision(chat_id)
    assert decision is not None and decision.active_topic is False
    goal = state.goal_store.get(chat_id)
    assert (goal.goal, goal.source) == ("自然聊天", "user")
    assert model.calls == []
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_ordinary_group_with_planner_top_goal_keeps_it_when_reply_gate_blocks(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    chat_id = "g67890"
    model   = ScriptedChatModel()
    scripted_chat_context(mock_context, model, GROUP_GATE_CLOSED)
    state = bound_state(mock_context)
    (await state.pfc_state_store.get_async(chat_id)).goal_list = [{"goal": "已有规划目标"}]
    await state.goal_store.set_async(chat_id, goal="已有规划目标", source="planner")

    result = await handle_smalltalk(
        _SYNC_NOTICE, chat_message_event(sample_group_event, _SYNC_NOTICE), mock_context
    )

    assert result == []
    goal = state.goal_store.get(chat_id)
    assert (goal.goal, goal.source) == ("已有规划目标", "planner")
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_ordinary_group_pre_pfc_block_still_updates_no_reply_adaptation(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    chat_id = "g67890"
    model   = ScriptedChatModel()
    scripted_chat_context(mock_context, model, GROUP_GATE_CLOSED)
    state = bound_state(mock_context)

    result = await handle_smalltalk(
        _SYNC_NOTICE, chat_message_event(sample_group_event, _SYNC_NOTICE), mock_context
    )

    assert result == []
    assert (await state.heartflow.get_async(chat_id)).no_reply_streak == 1
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_expression_reflection_does_not_spawn_before_reply_gate_passes(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.expression.bw_expression_store import ExpressionRecord
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    chat_id = "g67890"
    model   = ScriptedChatModel()
    scripted_chat_context(
        mock_context,
        model,
        GROUP_GATE_CLOSED,
        {
            "reflection": {
                "enable_expression_reflection": True,
                "operator_user_id"            : 10001,
                "operator_group_id"           : 20001,
                "min_interval_seconds"        : 60,
                "ask_per_check"               : 1,
            }
        },
    )
    # 有一条待审的表达：反思真的跑起来，就会向操作者发一条确认消息。
    bound_state(mock_context).bw_expr_store.save(
        [ExpressionRecord("expr-1", chat_id, "闲聊情景", "闲聊风格")]
    )

    result = await handle_smalltalk(
        "今天只是同步状态，不需要立即回复",
        chat_message_event(sample_group_event, "今天只是同步状态，不需要立即回复"),
        mock_context,
    )
    await asyncio.sleep(0.05)

    assert result == []
    mock_context.send_action.assert_not_awaited()
    assert model.calls == []
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_planner_can_wait_on_fresh_group_invitation(monkeypatch):
    from plugins.xiaoqing_chat.config.config import PersonalityConfig
    from plugins.xiaoqing_chat.planning import pfc_action_planner
    from plugins.xiaoqing_chat.planning.pfc_action_planner import plan_next_action

    captured: dict[str, str] = {}

    async def fake_chat(*_args, **kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        return (
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "thinking"    : "刚刚接连说过几句",
                                    "action"      : "wait",
                                    "reason"      : "给其他人留出轮次",
                                    "wait_seconds": 20,
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
            "responses",
        )

    monkeypatch.setattr(
        pfc_action_planner,
        "chat_completions_raw_with_profile",
        fake_chat,
    )
    plan = await plan_next_action(
        secrets                      = {},
        bot_name                     = "小青",
        is_private                   = False,
        personality                  = PersonalityConfig(),
        history                      = [],
        goal_list                    = [],
        knowledge_list               = [],
        action_history_summary       = "",
        last_action_context          = "",
        timeout_context              = "",
        last_successful_reply_action = None,
        llm                          = LLMCallConfig(
            temperature            = 0.7,
            top_p                  = 0.9,
            max_tokens             = 256,
            timeout_seconds        = 1.0,
            max_retry              = 0,
            retry_interval_seconds = 0.0,
        ),
        current_text="你们有没有那种越困越不想睡的时候",
    )

    assert plan.action == "wait"
    assert plan.reason == "给其他人留出轮次"
    assert plan.wait_seconds == 20
    assert "你们有没有那种越困越不想睡的时候" in captured["prompt"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[CQ:face,id=14]", True),
        ("[CQ:face,id=14] ？！", True),
        ("[QQ表情：汪汪大哭]", True),
        ("[CQ:face,id=14] 我也觉得", False),
        ("今晚吃什么？", False),
    ],
)
def test_low_information_group_turn_only_matches_protocol_faces(text, expected):
    from plugins.xiaoqing_chat.participation import is_low_information_group_turn

    assert is_low_information_group_turn(text) is expected


@pytest.mark.asyncio
async def test_planner_unavailable_fails_closed_in_group_but_replies_in_private(monkeypatch):
    from plugins.xiaoqing_chat.config.config import PersonalityConfig
    from plugins.xiaoqing_chat.planning import pfc_action_planner
    from plugins.xiaoqing_chat.planning.pfc_action_planner import plan_next_action

    kwargs = {
        "secrets"                     : {"_ai": None},
        "bot_name"                    : "小青",
        "personality"                 : PersonalityConfig(),
        "history"                     : [],
        "goal_list"                   : [],
        "knowledge_list"              : [],
        "action_history_summary"      : "",
        "last_action_context"         : "",
        "timeout_context"             : "",
        "last_successful_reply_action": None,
        "llm"                         : LLMCallConfig(
            temperature            = 0.7,
            top_p                  = 0.9,
            max_tokens             = 256,
            timeout_seconds        = 1.0,
            max_retry              = 0,
            retry_interval_seconds = 0.0,
        ),
    }

    group_plan = await plan_next_action(is_private=False, **kwargs)
    private_plan = await plan_next_action(is_private=True, **kwargs)

    assert group_plan.action == "wait"
    assert private_plan.action == "direct_reply"

    async def _raise_timeout(*_args, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr(
        pfc_action_planner,
        "chat_completions_raw_with_profile",
        _raise_timeout,
    )
    timeout_kwargs = {**kwargs, "secrets": {}}
    group_timeout_plan = await plan_next_action(is_private=False, **timeout_kwargs)
    private_timeout_plan = await plan_next_action(is_private=True, **timeout_kwargs)

    assert group_timeout_plan.action == "wait"
    assert private_timeout_plan.action == "direct_reply"


@pytest.mark.parametrize(
    ("is_private", "expected_action"),
    [(False, "wait"), (True, "direct_reply")],
)
@pytest.mark.asyncio
async def test_planner_unknown_action_uses_scope_safe_fallback(
    monkeypatch,
    is_private: bool,
    expected_action: str,
):
    from plugins.xiaoqing_chat.config.config import PersonalityConfig
    from plugins.xiaoqing_chat.planning import pfc_action_planner
    from plugins.xiaoqing_chat.planning.pfc_action_planner import plan_next_action

    async def fake_chat(*_args, **_kwargs):
        return (
            {"choices": [{"message": {"content": '{"action":"speak_anyway","reason":"bad"}'}}]},
            "responses",
        )

    monkeypatch.setattr(
        pfc_action_planner,
        "chat_completions_raw_with_profile",
        fake_chat,
    )
    plan = await plan_next_action(
        secrets                      = {},
        bot_name                     = "小青",
        is_private                   = is_private,
        personality                  = PersonalityConfig(),
        history                      = [],
        goal_list                    = [],
        knowledge_list               = [],
        action_history_summary       = "",
        last_action_context          = "",
        timeout_context              = "",
        last_successful_reply_action = None,
        llm                          = LLMCallConfig(
            temperature            = 0.7,
            top_p                  = 0.9,
            max_tokens             = 256,
            timeout_seconds        = 1.0,
            max_retry              = 0,
            retry_interval_seconds = 0.0,
        ),
    )

    assert plan.action == expected_action
    assert plan.reason == "planner_invalid_action"


def test_pure_reactions_and_faces_are_low_information_turns():
    from plugins.xiaoqing_chat.planning.goal_state import is_low_information_turn

    assert is_low_information_turn("乐") is True
    assert is_low_information_turn("噔噔咚") is True
    assert is_low_information_turn("[QQ表情：汪汪大哭]") is True
    assert is_low_information_turn("6 公斤每立方厘米") is False


def test_scientific_numeric_turn_uses_reasoning_route_classifier():
    from plugins.xiaoqing_chat.reply_generator import _needs_reasoning_route

    assert _needs_reasoning_route("6 公斤每立方厘米") is True
    assert _needs_reasoning_route("半径和重力知道了，能算密度吗") is True
    assert _needs_reasoning_route("今天吃什么") is False


def test_long_non_question_turn_gets_the_natural_chat_goal():
    from plugins.xiaoqing_chat.planning.goal_state import derive_goal

    goal = derive_goal("今天我主要在整理部署流程、补文档、顺便看了下监控曲线，没有新的问题要问")

    assert goal == "自然聊天"


@pytest.mark.asyncio
@pytest.mark.parametrize("goal", ["自然聊天", ""], ids=["goal", "no-goal"])
async def test_post_reply_tasks_open_a_goal_strategy_review_from_goal_and_action_history(
    mock_context, sample_group_event, goal
):
    from plugins.xiaoqing_chat.handler_context import HandlerContext
    from plugins.xiaoqing_chat.planning.action_history import ActionRecord
    from plugins.xiaoqing_chat.post_reply_tasks import spawn_post_reply_tasks

    chat_id = "g67890"
    model   = ScriptedChatModel()
    scripted_chat_context(
        mock_context,
        model,
        {
            "reflection": {
                "enable_review_sessions": True,
                "operator_user_id"      : 10001,
                "operator_group_id"     : 20001,
            }
        },
    )
    event = chat_message_event(sample_group_event, "你好")
    hctx  = HandlerContext.from_event(event, mock_context)
    state = hctx.state
    if goal:
        await state.goal_store.set_async(chat_id, goal=goal, source="user")
    # 最近有一次回复被拒：只有读到动作历史，复盘才会开。
    state.action_history.append(
        chat_id,
        ActionRecord(
            ts           = time.time(),
            local_target = "m1",
            action       = "reply_rejected",
            reasoning    = "r",
            detail       = {},
            executed     = False,
        ),
    )

    await spawn_post_reply_tasks(hctx, [], event)

    # 复盘围绕会话目标：没有目标就不开。
    if not goal:
        assert active_review_sessions(state.review_store) == []
    else:
        (session,) = active_review_sessions(state.review_store)
        assert session.kind == "goal_strategy" and session.chat_id == chat_id
        assert session.payload["goal"] == goal
    await settle_chat_background(mock_context, model)


def _xc_reset_confirm_node():
    import json

    from core.router import build_command_catalog_node
    from tests.helpers.paths import REPOSITORY_ROOT

    manifest = json.loads(
        (REPOSITORY_ROOT / "plugins" / "xiaoqing_chat" / "plugin.json").read_text(encoding="utf-8")
    )
    root = build_command_catalog_node("xiaoqing_chat", manifest["commands"][0], root=True)
    reset = root.resolve_child("reset")
    assert reset is not None and reset.permission == "public"
    confirm = reset.resolve_child("确认")
    assert confirm is not None
    return confirm


class _UnavailableMemory:
    """统一记忆能力的替身：清空请求得不到确认。"""

    async def forget(self, payload: dict) -> None:
        raise RuntimeError("memory service unavailable")


@pytest.mark.asyncio
async def test_group_reset_requires_admin_confirmation_and_writes_audit_log(
    mock_context, sample_group_event, caplog
):
    from plugins.xiaoqing_chat import commands

    caplog.set_level(logging.INFO, logger="plugins.xiaoqing_chat")

    chat_id = "g67890"
    state   = bound_state(mock_context)
    state.memory_store.append(chat_id, role="user", name="Tester", content="hi")
    set_context_principal(mock_context, sample_group_event, group_role="admin")

    # 确认这一步谁能做写在 plugin.json 里：只给群管理员，且只在群里。
    confirm = _xc_reset_confirm_node()
    assert confirm.permission == "group_admin" and confirm.contexts == ("group",)

    preview = await commands.handle_internal("重置", "", sample_group_event, mock_context)
    assert "会清空本群" in preview[0]["data"]["text"]
    assert "/xc 重置 确认" in preview[0]["data"]["text"]
    assert len(state.memory_store.get(chat_id)) == 1
    assert logged_events(caplog, "session.reset") == []

    completed = await commands.handle_internal("重置", "确认", sample_group_event, mock_context)
    assert completed == [{"type": "text", "data": {"text": "✅ 已重置会话记忆"}}]
    assert state.memory_store.get(chat_id) == []
    assert state.get_stats(chat_id)["resets"] == 1
    (audit,) = logged_events(caplog, "session.reset")
    assert audit.fields["scope"] == "group"
    assert audit.fields["operator_user_id"] == sample_group_event["user_id"]


@pytest.mark.asyncio
async def test_private_reset_remains_limited_to_callers_private_scope(
    mock_context, sample_private_event, sample_group_event, caplog
):
    from plugins.xiaoqing_chat import commands

    caplog.set_level(logging.INFO, logger="plugins.xiaoqing_chat")

    state = bound_state(mock_context)
    state.memory_store.append("u12345", role="user", name="Tester", content="私聊里说的")
    state.memory_store.append("g67890", role="user", name="Tester", content="群里说的")
    set_context_principal(mock_context, sample_private_event)

    result = await commands.handle_internal("重置", "", sample_private_event, mock_context)

    assert result == [{"type": "text", "data": {"text": "✅ 已重置会话记忆"}}]
    assert state.memory_store.get("u12345") == []
    assert len(state.memory_store.get("g67890")) == 1
    (audit,) = logged_events(caplog, "session.reset")
    assert audit.fields["scope"] == "private"


@pytest.mark.asyncio
async def test_reset_says_so_when_long_term_memory_was_not_cleared(
    mock_context, sample_private_event
):
    from plugins.xiaoqing_chat import commands

    state = bound_state(mock_context)
    state.memory_store.append("u12345", role="user", name="Tester", content="hi")
    set_context_principal(mock_context, sample_private_event)
    mock_context.capabilities = dataclasses.replace(
        mock_context.capabilities, memory=_UnavailableMemory()
    )

    result = await commands.handle_internal("重置", "", sample_private_event, mock_context)

    text = result[0]["data"]["text"]
    assert "没能确认长期记忆已经清掉" in text and "自动重试" in text
    assert "✅" not in text
    # 长期记忆没清成，本地的会话状态照样清空。
    assert state.memory_store.get("u12345") == []


@pytest.mark.asyncio
async def test_handle_internal_reset_clears_pfc_state(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.commands import handle_internal

    chat_id             = "g67890"
    state               = bound_state(mock_context)
    pfc_state           = await state.pfc_state_store.get_async(chat_id)
    pfc_state.ended     = True
    pfc_state.goal_list = [{"goal": "old"}]

    set_context_principal(mock_context, sample_group_event, group_role="admin")
    result = await handle_internal("重置", "确认", sample_group_event, mock_context)

    assert result == [{"type": "text", "data": {"text": "✅ 已重置会话记忆"}}]
    cleared = await state.pfc_state_store.get_async(chat_id)
    assert (cleared.ended, cleared.goal_list) == (False, [])


def _seed_action_history(state) -> None:
    from plugins.xiaoqing_chat.planning.action_history import ActionRecord

    state.action_history.append(
        "g1",
        ActionRecord(
            ts           = 1.0,
            local_target = "m1",
            action       = "reply",
            reasoning    = "r",
            detail       = {},
            executed     = True,
        ),
    )


async def _persisted_action_actions(data_dir) -> list[str]:
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore

    reloaded = ActionHistoryStore()
    reloaded.bind(data_dir)
    return [record.action for record in await reloaded.get_recent_async("g1")]


def _seed_pfc_state(state) -> None:
    state.pfc_state_store.set_state(
        "g1", PFCConversationState(chat_id="g1", goal_list=[{"goal": "持久化目标"}])
    )


async def _persisted_pfc_goals(data_dir) -> list[str]:
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore

    reloaded = PFCStateStore()
    reloaded.bind(data_dir)
    return [goal["goal"] for goal in reloaded.get("g1").goal_list]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scheduler_name", "tasks_name", "seed", "persisted", "expected"),
    [
        pytest.param(
            "schedule_action_history_flush",
            "_action_flush_tasks",
            _seed_action_history,
            _persisted_action_actions,
            ["reply"],
            id="action_history",
        ),
        pytest.param(
            "schedule_pfc_state_flush",
            "_pfc_state_flush_tasks",
            _seed_pfc_state,
            _persisted_pfc_goals,
            ["持久化目标"],
            id="pfc_state",
        ),
    ],
)
async def test_flush_scheduler_writes_the_store_in_a_thread(
    mock_context, scheduler_name, tasks_name, seed, persisted, expected
):
    from plugins.xiaoqing_chat import task_scheduler

    schedule = getattr(task_scheduler, scheduler_name)
    tasks    = getattr(task_scheduler, tasks_name)

    runtime = SimpleNamespace(cfg=XiaoQingChatConfig(io_persist_debounce_seconds=0.0))
    seed(bound_state(mock_context))

    with patch.object(asyncio, "to_thread", wraps=asyncio.to_thread) as to_thread:
        schedule(mock_context, runtime, chat_id="g1")
        task = tasks.get("g1")
        assert task is not None
        await task

    assert to_thread.call_count == 1
    assert await persisted(mock_context.data_dir) == expected
    tasks.pop("g1", None)


@pytest.mark.asyncio
async def test_smalltalk_planner_runs_outside_chat_lock_but_commits_inside(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.chat_runtime import chat_lock
    from plugins.xiaoqing_chat.handlers import handle_smalltalk
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore

    chat_id        = "g67890"
    lock           = chat_lock(chat_id)
    lock_free_when = []
    plans          = iter([plan_answer("rethink_goal"), plan_answer("direct_reply")])

    # 规划和生成都在模型这条边界上被叫到：这时不能占着会话锁。
    def planning(_call) -> str:
        lock_free_when.append(not lock.locked())
        return next(plans)

    def drafting(_call) -> str:
        lock_free_when.append(not lock.locked())
        return "planner-ok"

    model = ScriptedChatModel(plan=planning, goals=goals_answer("新规划目标"), reply=drafting)
    scripted_chat_context(mock_context, model, GROUP_GATE_OPEN)
    state = bound_state(mock_context)

    result = await handle_smalltalk(
        "你好", chat_message_event(sample_group_event, "你好"), mock_context
    )

    assert result == [{"type": "text", "data": {"text": "planner-ok"}}]
    assert len(lock_free_when) == 3 and all(lock_free_when)

    # 回复送达后才提交规划状态，提交要等会话锁：锁被占着时，状态和聊天记录都还没变。
    goal_before = state.goal_store.get(chat_id).goal
    async with lock:
        ack = asyncio.create_task(ack_reply_delivery(result))
        await asyncio.wait({ack}, timeout=0.05)
        assert not ack.done()
        assert state.goal_store.get(chat_id).goal == goal_before
        assert (await state.pfc_state_store.get_async(chat_id)).top_goal == ""
        assert [m.role for m in await state.memory_store.get_async(chat_id)] == ["user"]
    await ack

    goal = state.goal_store.get(chat_id)
    assert (goal.goal, goal.source) == ("新规划目标", "planner")
    assert (await state.pfc_state_store.get_async(chat_id)).top_goal == "新规划目标"
    assert [m.role for m in await state.memory_store.get_async(chat_id)] == ["user", "assistant"]
    await settle_chat_background(mock_context, model)
    # 提交后的规划状态已经落盘。
    reloaded = PFCStateStore()
    reloaded.bind(mock_context.data_dir)
    assert reloaded.get(chat_id).top_goal == "新规划目标"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "seconds,expected", [(15, 15), (99, 30), (-4, 0), (True, 0), ("15", 0), (None, 0)]
)
async def test_planner_preserves_optional_wait(monkeypatch, seconds, expected):
    from plugins.xiaoqing_chat.planning import pfc_action_planner as planner

    response = {
        "action"      : "wait",
        "reason"      : "wait",
        "thinking"    : "user typing",
        "wait_seconds": seconds,
    }
    fake = AsyncMock(
        return_value=({"choices": [{"message": {"content": json.dumps(response)}}]}, "test")
    )
    monkeypatch.setattr(planner, "chat_completions_raw_with_profile", fake)
    plan = await planner.plan_next_action(
        secrets                      = {},
        bot_name                     = "小青",
        is_private                   = False,
        personality                  = PersonalityConfig(),
        history                      = [],
        goal_list                    = [],
        knowledge_list               = [],
        action_history_summary       = "",
        last_action_context          = "",
        timeout_context              = "",
        last_successful_reply_action = None,
        llm                          = LLMCallConfig(
            temperature            = 0.5,
            top_p                  = 0.9,
            max_tokens             = 300,
            timeout_seconds        = 1,
            max_retry              = 0,
            retry_interval_seconds = 0,
        ),
        current_text="",
    )
    assert plan.wait_seconds == expected
    assert plan.thinking == "user typing"


# ---------------------------------------------------------------- 规划器不会越听越不开口


def _group_turns(count: int, *, bot_spoke_after: int | None = None):
    from plugins.xiaoqing_chat.memory.memory import StoredMessage

    history = []
    for index in range(count):
        history.append(
            StoredMessage(
                role     = "user",
                name     = f"群友{index % 3}",
                ts       = 1_000.0 + index,
                user_id  = 10 + index % 3,
                local_id = f"m{len(history) + 1}",
                content  = f"车上餐食换了{index}",
            )
        )
        if bot_spoke_after == index:
            history.append(
                StoredMessage(
                    role     = "assistant",
                    name     = "小青",
                    ts       = 1_000.5 + index,
                    local_id = f"m{len(history) + 1}",
                    content  = "还有这种操作",
                )
            )
    return history


@pytest.mark.parametrize(
    ("history_args", "expected"),
    [
        ((5, None), "这段对话里你还没开过口，群友已经说了 5 条"),
        ((6, 1), "你上次开口之后，群友又说了 4 条"),
        ((2, None), ""),
        ((5, 3), ""),
    ],
)
def test_participation_hint_counts_messages_since_she_last_spoke(history_args, expected):
    from plugins.xiaoqing_chat.planning.pfc_action_planner import _participation_hint

    count, spoke_after = history_args
    hint = _participation_hint(_group_turns(count, bot_spoke_after=spoke_after))
    if expected:
        assert expected in hint
    else:
        assert hint == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("is_private", [False, True])
async def test_planner_prompt_tells_group_planner_how_long_she_has_been_quiet(
    monkeypatch, is_private
):
    from plugins.xiaoqing_chat.planning import pfc_action_planner

    captured: dict[str, str] = {}

    async def fake_chat(*_args, **kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        return (
            {"choices": [{"message": {"content": '{"action":"listening","reason":"r"}'}}]},
            "responses",
        )

    monkeypatch.setattr(pfc_action_planner, "chat_completions_raw_with_profile", fake_chat)
    await pfc_action_planner.plan_next_action(
        secrets                      = {},
        bot_name                     = "小青",
        is_private                   = is_private,
        personality                  = PersonalityConfig(),
        history                      = _group_turns(6),
        goal_list                    = [],
        knowledge_list               = [],
        action_history_summary       = "",
        last_action_context          = "",
        timeout_context              = "",
        last_successful_reply_action = None,
        llm                          = LLMCallConfig(
            temperature            = 0.7,
            top_p                  = 0.9,
            max_tokens             = 256,
            timeout_seconds        = 1.0,
            max_retry              = 0,
            retry_interval_seconds = 0.0,
        ),
        current_text="嘶",
    )

    assert ("群友已经说了 6 条" in captured["prompt"]) is (not is_private)


@pytest.mark.asyncio
async def test_action_summary_folds_listening_runs_without_their_reasons(tmp_path):
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore, ActionRecord
    from plugins.xiaoqing_chat.planning.pfc_engine import _action_history_summary_async

    store = ActionHistoryStore()
    store.bind(tmp_path)

    def record(ts: float, action: str, reasoning: str) -> None:
        store.append(
            "g1",
            ActionRecord(
                ts           = ts,
                local_target = f"m{int(ts)}",
                action       = action,
                reasoning    = reasoning,
                detail       = {},
                executed     = True,
            ),
        )

    for ts in range(1, 4):
        record(float(ts), "listening", "listening:这是群友之间的对话，我先听着")
    record(10.0, "direct_reply", "direct_reply:接一句")
    for ts in range(20, 26):
        record(float(ts), "wait", "wait:没有在问我")

    summary, last_context = await _action_history_summary_async(store, "g1")

    lines = summary.splitlines()
    assert len(lines) == 3
    assert "连续旁听 3 条" in lines[0]
    assert "direct_reply" in lines[1] and "接一句" in lines[1]
    assert "连续旁听 6 条" in lines[2]
    assert "群友之间的对话" not in summary
    assert "没有在问我" not in summary + last_context
    assert last_context == "action=wait（已连续旁听 6 条）"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actions,expected",
    [
        (["rethink_goal", "rethink_goal"], "wait"),
        (["fetch_knowledge", "fetch_knowledge"], "wait"),
        (["fetch_knowledge", "rethink_goal"], "wait"),
        (["rethink_goal", "fetch_knowledge", "rethink_goal"], "wait"),
        (["rethink_goal", "fetch_knowledge", "fetch_knowledge"], "wait"),
        (["fetch_knowledge", "wait"], "wait"),
        (["rethink_goal", "direct_reply"], "direct_reply"),
        (["fetch_knowledge", "direct_reply"], "direct_reply"),
    ],
)
async def test_pfc_requires_a_reply_action_before_generation(tmp_path, actions, expected):
    from plugins.xiaoqing_chat.memory.memory import MemoryStore
    from plugins.xiaoqing_chat.planning import pfc_engine
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore

    cfg = XiaoQingChatConfig()
    context = SimpleNamespace(data_dir=tmp_path)
    generate = AsyncMock(return_value="回复")
    # 重新分析目标时，模型答“空数组”：所有目标都已失效。
    model = ScriptedChatModel(
        plan  = [plan_answer(action, "上下文判断") for action in actions],
        goals = "[]",
    )

    result = await pfc_engine.run_pfc_once(
        context        = context,
        runtime_cfg    = cfg,
        secrets        = {"_ai": model, "_route": "reasoning"},
        bot_name       = "小青",
        is_private     = False,
        chat_id        = "g1",
        current_text   = "待讨论的事情",
        memory_store   = MemoryStore(),
        action_history = ActionHistoryStore(),
        state=PFCConversationState(chat_id="g1"),
        generate_reply=generate,
    )

    assert result.action == expected
    assert generate.await_count == (1 if expected == "direct_reply" else 0)
    assert len(model.calls_of("plan")) == len(actions)
    assert not model.unscripted
