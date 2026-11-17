"""目标同步、重置和记录。"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from plugins.xiaoqing_chat.planning.pfc_state import PFCConversationState
from plugins.xiaoqing_chat.store_binding import bound_state
from tests.helpers.assertions import logged_events, public_error_reply
from tests.helpers.xiaoqing_chat_test_support import (
    GROUP_GATE_CLOSED,
    GROUP_GATE_OPEN,
    ScriptedChatModel,
    ack_reply_delivery,
    active_review_sessions,
    chat_message_event,
    goals_answer,
    handle_errors,
    plan_answer,
    say_bye_answer,
    scripted_chat_context,
    seed_chat_history,
    set_context_principal,
    settle_chat_background,
)

mock_context       = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event


@pytest.mark.asyncio
async def test_smalltalk_direct_reply_uses_dynamic_history_think_level_when_planner_disabled(
    mock_context, sample_private_event
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    chat_id = "u12345"
    model = ScriptedChatModel(reply="直接回复")
    scripted_chat_context(mock_context, model, {"planner": {"enable_planner": False}})
    # 动态思考等级按历史条数取值：对话已有 20 条时是 2 级。
    seed_chat_history(bound_state(mock_context), chat_id, 20)

    result = await handle_smalltalk(
        "你好", chat_message_event(sample_private_event, "你好"), mock_context
    )

    assert result == [{"type": "text", "data": {"text": "直接回复"}}]
    assert model.calls_of("plan") == []
    # 2 级思考比普通的“系统提示 + 对话”多带一条要求深入思考的提示。
    (reply_call,) = model.calls_of("reply")
    assert [message["role"] for message in reply_call.messages] == ["system", "user", "user"]
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_smalltalk_private_brain_chat_keeps_planner_when_private_always_on_even_if_disabled(
    mock_context, sample_private_event
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    model = ScriptedChatModel(plan=plan_answer("direct_reply"), reply="规划回复")
    scripted_chat_context(
        mock_context,
        model,
        {
            "max_context_size": 6,
            "planner"         : {"enable_planner": False},
            "brain_chat"      : {
                "enable_private_brain_chat": True,
                "private_planner_always_on": True,
                "brain_max_context_size"   : 6,
                "brain_think_level"        : 2,
            },
        },
    )

    result = await handle_smalltalk(
        "你好", chat_message_event(sample_private_event, "你好"), mock_context
    )

    assert result == [{"type": "text", "data": {"text": "规划回复"}}]
    assert len(model.calls_of("plan")) == 1
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_smalltalk_finalize_defers_post_reply_tasks_to_background(
    mock_context, sample_private_event
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    extraction_started = asyncio.Event()
    release_extraction = asyncio.Event()

    async def blocked_life_extraction(_call) -> str:
        extraction_started.set()
        await release_extraction.wait()
        return "[]"

    model = ScriptedChatModel(reply="后台化测试", reasoning=blocked_life_extraction)
    scripted_chat_context(mock_context, model, {"life": {"enable_open_loops": True}})
    # 对话里有带时间点的话头，回复送达后才值得做一次生活记录抽取。
    bound_state(mock_context).memory_store.append(
        "u12345",
        role    = "user",
        name    = "TestUser",
        user_id = 12345,
        content = "周五要考试了",
        ts      = time.time() - 30,
    )
    event = chat_message_event(sample_private_event, "你好", _xc_command_forced=True)

    result = await handle_smalltalk("你好", event, mock_context)
    assert result == [{"type": "text", "data": {"text": "后台化测试"}}]

    # 回复确认送达后，生活记录抽取在后台开始，卡在模型上也不拖住这一轮的结算。
    await asyncio.wait_for(ack_reply_delivery(result), timeout=5)
    await asyncio.wait_for(extraction_started.wait(), timeout=5)
    history = await bound_state(mock_context).memory_store.get_async("u12345")
    assert [message.role for message in history] == ["user", "user", "assistant"]

    release_extraction.set()
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_prepare_smalltalk_turn_does_not_force_coreference_without_bot_anchor(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    chat_id = "g67890"
    model   = ScriptedChatModel()
    scripted_chat_context(mock_context, model, GROUP_GATE_CLOSED)
    state = bound_state(mock_context)
    state.memory_store.append(
        chat_id,
        role       = "user",
        name       = "群友",
        message_id = 101,
        content    = "她刚才还在说外卖",
        ts         = time.time() - 5,
    )
    event = chat_message_event(sample_group_event, "不@她能不能听见啊", message_id=103)

    result = await handle_smalltalk("不@她能不能听见啊", event, mock_context)

    # 没有 @、没叫名字：这句话不被当成冲她来的，交给插话概率的门控，门控没放行就不回复。
    assert result == []
    decision = state.get_reply_gate_decision(chat_id)
    assert decision is not None
    assert (decision.should_reply, decision.reason) == (False, "probability")
    assert model.calls == []
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_smalltalk_commit_syncs_goal_store_to_top_planner_goal_after_state_override_update(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    chat_id = "g67890"
    model   = ScriptedChatModel(
        plan  = [plan_answer("rethink_goal"), plan_answer("direct_reply")],
        goals = goals_answer("新规划目标"),
        reply = "planner-ok",
    )
    scripted_chat_context(mock_context, model, GROUP_GATE_OPEN)
    state = bound_state(mock_context)
    (await state.pfc_state_store.get_async(chat_id)).goal_list = [{"goal": "旧规划目标"}]

    result = await handle_smalltalk(
        "继续聊", chat_message_event(sample_group_event, "继续聊"), mock_context
    )

    assert result == [{"type": "text", "data": {"text": "planner-ok"}}]
    await ack_reply_delivery(result)
    goal = state.goal_store.get(chat_id)
    assert (goal.goal, goal.source) == ("新规划目标", "planner")
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_smalltalk_commit_clears_goal_store_when_planner_has_no_goal(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    chat_id = "g67890"
    model = ScriptedChatModel(plan=plan_answer("direct_reply"), reply="planner-ok")
    scripted_chat_context(mock_context, model, GROUP_GATE_OPEN)
    state = bound_state(mock_context)
    await state.goal_store.set_async(chat_id, goal="围绕旧目标继续", source="user")

    result = await handle_smalltalk(
        "继续聊", chat_message_event(sample_group_event, "继续聊"), mock_context
    )

    assert result == [{"type": "text", "data": {"text": "planner-ok"}}]
    # 门控先按这句话记下了本轮的目标，规划器自己没有目标；回复送达后会话目标跟着清空。
    assert state.goal_store.get(chat_id).goal != ""
    await ack_reply_delivery(result)
    assert state.goal_store.get(chat_id).goal == ""
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_smalltalk_commit_leaves_the_session_goal_alone_when_goals_are_off(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    chat_id = "g67890"
    model = ScriptedChatModel(plan=plan_answer("direct_reply"), reply="planner-ok")
    scripted_chat_context(mock_context, model, GROUP_GATE_OPEN, {"goal": {"enable_goal": False}})
    state = bound_state(mock_context)
    await state.goal_store.set_async(chat_id, goal="围绕旧目标继续", source="user")

    result = await handle_smalltalk(
        "继续聊", chat_message_event(sample_group_event, "继续聊"), mock_context
    )
    await ack_reply_delivery(result)

    # 目标功能关着：规划状态照常写回，会话目标不跟着规划器改，也不请模型分析目标。
    assert result == [{"type": "text", "data": {"text": "planner-ok"}}]
    assert state.pfc_state_store.get(chat_id).last_successful_reply_action == "direct_reply"
    assert state.goal_store.get(chat_id).goal == "围绕旧目标继续"
    assert model.calls_of("goals") == []
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_handle_errors_uses_keyword_context_for_logging(caplog):
    context = SimpleNamespace(request_id="req-xc-handler", secrets={})

    @handle_errors
    async def failing_handler(*, context=None):
        raise RuntimeError("boom")

    result = await failing_handler(context=context)

    assert len(logged_events(caplog, "public_error")) == 1
    text = result[0]["data"]["text"]
    assert public_error_reply(text)["request_id"] == "req-xc-handler"
    assert "boom" not in text


@pytest.mark.asyncio
async def test_handle_internal_reset_clears_goal_heartflow_and_action_history(
    mock_context, sample_group_event, tmp_path
):
    from plugins.xiaoqing_chat.commands import handle_internal
    from plugins.xiaoqing_chat.expression.bw_expression_store import ExpressionRecord
    from plugins.xiaoqing_chat.expression.bw_jargon_store import JargonRecord, JargonStore
    from plugins.xiaoqing_chat.planning.action_history import ActionRecord

    chat_id               = "g67890"
    mock_context.data_dir = tmp_path
    state                 = bound_state(mock_context)

    state.memory_store.append(chat_id, role="user", name="Tester", content="hi")
    state.memory_db.upsert_text(
        doc_id = "topic-current-chat",
        text   = "本群旧话题",
        meta   = {"type": "topic_summary", "chat_id": chat_id},
    )
    state.memory_db.upsert_text(
        doc_id = "topic-other-chat",
        text   = "其他群话题",
        meta   = {"type": "topic_summary", "chat_id": "g-other"},
    )
    state.memory_db.save()
    await state.goal_store.set_async(chat_id, goal="围绕旧目标继续", source="user")
    await state.heartflow.on_user_message_async(chat_id=chat_id)
    await state.heartflow.on_bot_reply_async(chat_id=chat_id)
    state.action_history.append(
        chat_id,
        ActionRecord(
            ts           = time.time(),
            local_target = "u1",
            action       = "reply",
            reasoning    = "old",
            detail       = {"source": "pfc"},
            executed     = True,
        ),
    )
    state.set_continuous_reply_count(chat_id, 3)
    state.set_continuous_cooldown_until(chat_id, 600.0)
    state.set_last_observe_ts(chat_id, 500.0)
    state.set_mood_state(chat_id, "旧情绪", duration_seconds=600.0)
    state.set_reply_gate_decision(chat_id, SimpleNamespace(reason="old"))
    assert state.fetch_and_increment_local_id(chat_id) == 1
    assert state.fetch_and_increment_local_id(chat_id) == 2

    pfc_st                              = await state.pfc_state_store.get_async(chat_id)
    pfc_st.ended                        = True
    pfc_st.last_successful_reply_action = "say_goodbye"
    pfc_st.goal_list                    = [{"goal": "old"}]
    pfc_st.knowledge_list               = [{"text": "old"}]
    await state.pfc_state_store.save_async(chat_id)
    state.bw_expr_store.save(
        [
            ExpressionRecord("expr-current", chat_id, "旧情景", "旧风格"),
            ExpressionRecord("expr-other", "g-other", "其它情景", "其它风格"),
        ]
    )
    state.bw_jargon_store.save(
        [
            JargonRecord(content="本群黑话", scope_chat_id=chat_id),
            JargonRecord(content="其它群黑话", scope_chat_id="g-other"),
            JargonRecord(content="无会话旧词条", chat_id_counts=[["g-other", 1]]),
        ]
    )
    state.bw_recorder.set_last_time(chat_id, 123.0)
    state.bw_tracker_store.set_tracker(chat_id, "expr-current")
    state.bw_tracker_store.set_tracker("g-other", "expr-other")
    state.media_store.upsert_media_items(
        [{"media_key": "shared-media", "kind": "image", "description": "共享图片"}]
    )

    set_context_principal(mock_context, sample_group_event, group_role="admin")
    result = await handle_internal("重置", "确认", sample_group_event, mock_context)

    assert result == [{"type": "text", "data": {"text": "✅ 已重置会话记忆"}}]
    assert state.memory_store.get(chat_id) == []
    assert state.memory_db.get("topic-current-chat") is None
    assert state.memory_db.get("topic-other-chat") is not None
    reloaded_memory_db = type(state.memory_db)()
    reloaded_memory_db.bind(tmp_path)
    assert reloaded_memory_db.get("topic-current-chat") is None
    assert reloaded_memory_db.get("topic-other-chat") is not None
    assert state.goal_store.get(chat_id).goal == ""
    heartflow_state = await state.heartflow.get_async(chat_id)
    assert heartflow_state.no_reply_streak == 0
    assert state.get_continuous_reply_count(chat_id) == 0
    assert state.get_continuous_cooldown_until(chat_id) == 0.0
    assert await state.action_history.get_recent_async(chat_id, max_items=20) == []
    assert chat_id not in state._per_chat.last_observe_ts
    assert state.get_mood_state(chat_id) == ""
    assert state.get_reply_gate_decision(chat_id) is None
    assert state.fetch_and_increment_local_id(chat_id) == 1
    assert [item.expression_id for item in state.bw_expr_store.load()] == ["expr-other"]
    jargon = state.bw_jargon_store.load()
    assert JargonStore.key_for("本群黑话", chat_id) not in jargon
    assert JargonStore.key_for("其它群黑话", "g-other") in jargon
    assert jargon[JargonStore.key_for("无会话旧词条")].chat_id_counts == [["g-other", 1]]
    assert state.bw_recorder.get_last_time(chat_id) == 0.0
    assert state.bw_tracker_store.get_trackers(chat_id) == []
    assert [item.expression_id for item in state.bw_tracker_store.get_trackers("g-other")] == [
        "expr-other"
    ]
    assert (
        state.media_store.resolve_media_items([{"media_key": "shared-media"}])[0]["description"]
        == "共享图片"
    )


@pytest.mark.asyncio
async def test_handle_internal_reset_removes_per_chat_files_and_atomic_backups(
    mock_context, sample_group_event, tmp_path
):
    """重置后不得从旁路缓存或原子备份恢复旧会话。"""
    from core.plugin_base import write_json
    from plugins.xiaoqing_chat.commands import handle_internal

    chat_id               = "g67890"
    mock_context.data_dir = tmp_path
    state                 = bound_state(mock_context)

    json_paths = [
        tmp_path / f"{chat_id}.json",
        tmp_path / "goal_state" / f"{chat_id}.json",
        tmp_path / "heartflow" / f"{chat_id}.json",
        tmp_path / "action_history" / f"{chat_id}.json",
        tmp_path / "pfc_state" / f"{chat_id}.json",
        tmp_path / "hippo_memorizer" / f"{chat_id}.json",
    ]
    for path in json_paths:
        write_json(path, {"old": "conversation"})
        write_json(path, {"new": "conversation"})
        assert path.with_name(f"{path.name}.bak").exists()

    set_context_principal(mock_context, sample_group_event, group_role="admin")
    result = await handle_internal("重置", "确认", sample_group_event, mock_context)

    assert result == [{"type": "text", "data": {"text": "✅ 已重置会话记忆"}}]
    for path in json_paths:
        assert not path.exists()
        assert not path.with_name(f"{path.name}.bak").exists()

    pfc_state = await state.pfc_state_store.get_async(chat_id)
    assert pfc_state.goal_list == []
    assert pfc_state.knowledge_list == []
    assert not (tmp_path / "pfc_state" / f"{chat_id}.json").exists()


@pytest.mark.asyncio
async def test_handle_internal_reset_clears_review_policy_and_sessions_for_current_chat(
    mock_context, sample_group_event, tmp_path
):
    from plugins.xiaoqing_chat.commands import handle_internal
    from plugins.xiaoqing_chat.memory.review_sessions import ReviewPolicy

    chat_id               = "g67890"
    mock_context.data_dir = tmp_path
    state                 = bound_state(mock_context)

    state.review_store.save_policy(
        chat_id,
        ReviewPolicy(
            goal_override = "旧目标覆写",
            strategy_note = "旧策略备注",
        ),
    )
    # 连续写入以生成原子备份，验证重置不会只清理主文件。
    state.review_store.save_policy(
        chat_id,
        ReviewPolicy(goal_override="旧目标覆写", strategy_note="旧策略备注"),
    )
    session = state.review_store.open_session_if_allowed(
        kind             = "goal_strategy",
        chat_id          = chat_id,
        payload          = {"goal": "旧目标", "stats": "旧统计"},
        timeout_seconds  = 600.0,
        cooldown_seconds = 0.0,
    )
    assert session is not None
    second_session = state.review_store.open_session_if_allowed(
        kind             = "reply_style",
        chat_id          = chat_id,
        payload          = {"style": "旧风格"},
        timeout_seconds  = 600.0,
        cooldown_seconds = 0.0,
    )
    assert second_session is not None

    set_context_principal(mock_context, sample_group_event, group_role="admin")
    result = await handle_internal("重置", "确认", sample_group_event, mock_context)

    assert result == [{"type": "text", "data": {"text": "✅ 已重置会话记忆"}}]

    policy_after_reset = state.review_store.get_policy(chat_id)
    assert policy_after_reset.goal_override == ""
    assert policy_after_reset.strategy_note == ""

    sessions_after_reset = [
        x for x in active_review_sessions(state.review_store) if x.chat_id == chat_id
    ]
    assert sessions_after_reset == []

    policy_path = tmp_path / "review_sessions" / "policies" / f"{chat_id}.json"
    assert not policy_path.exists()
    assert not policy_path.with_name(f"{policy_path.name}.bak").exists()
    sessions_path = tmp_path / "review_sessions" / "sessions.json"
    for candidate in (sessions_path, sessions_path.with_name(f"{sessions_path.name}.bak")):
        assert chat_id not in candidate.read_text(encoding="utf-8")


@pytest.mark.parametrize("planner_action", ["send_new_message", "say_goodbye"])
@pytest.mark.asyncio
async def test_smalltalk_action_history_preserves_real_planner_action_name(
    mock_context, sample_group_event, planner_action
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    chat_id = "g67890"
    # 告别是规划器选“结束对话”、并且决定再说一句告别。
    model = ScriptedChatModel(
        plan=plan_answer("end_conversation" if planner_action == "say_goodbye" else planner_action),
        say_bye=say_bye_answer(True),
        reply="补一句",
    )
    scripted_chat_context(mock_context, model, GROUP_GATE_OPEN)

    result = await handle_smalltalk(
        "继续", chat_message_event(sample_group_event, "继续"), mock_context
    )

    assert result == [{"type": "text", "data": {"text": "补一句"}}]
    await ack_reply_delivery(result)
    state = bound_state(mock_context)
    records = await state.action_history.get_recent_async(chat_id, max_items=5)
    assert records[-1].action == planner_action
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_run_pfc_once_rethink_goal_passes_replanned_goal_focus_to_reply_generator(tmp_path):
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.memory.memory import MemoryStore
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore
    from plugins.xiaoqing_chat.planning.pfc_engine import run_pfc_once

    chat_id              = "pfc-goal-replan"
    cfg                  = XiaoQingChatConfig()
    context              = MagicMock()
    context.data_dir     = tmp_path
    context.http_session = AsyncMock()

    memory_store = MemoryStore()
    memory_store.append(chat_id, role="user", name="Tester", content="今天去哪家火锅店")
    action_history = ActionHistoryStore()
    generate_reply = AsyncMock(return_value="ok")
    model = ScriptedChatModel(
        plan  = [plan_answer("rethink_goal", "当前目标过时，需要重设"), plan_answer("direct_reply")],
        goals = goals_answer("帮用户选一家合适的火锅店"),
    )

    result = await run_pfc_once(
        context        = context,
        runtime_cfg    = cfg,
        secrets        = {"_ai": model, "_route": "reasoning"},
        bot_name       = "小青",
        is_private     = False,
        chat_id        = chat_id,
        current_text   = "今天去哪家火锅店",
        memory_store   = memory_store,
        action_history = action_history,
        state          = PFCConversationState(chat_id),
        generate_reply = generate_reply,
    )

    assert result.reply == "ok"
    assert generate_reply.await_count == 1
    assert generate_reply.await_args is not None
    _, planner_reason, extra_reason = generate_reply.await_args.args
    merged_reason = (f"{planner_reason}\n{extra_reason}").strip()
    assert "帮用户选一家合适的火锅店" in merged_reason


_STANDING_GOAL = {"goal": "聊周末去哪", "reasoning": "对方在问周末安排"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        pytest.param("[]", [], id="explicit-empty-list-clears"),
        pytest.param("```json\n[]\n```", [], id="fenced-empty-list-clears"),
        pytest.param("这次没法判断", [_STANDING_GOAL], id="unparseable-keeps"),
        pytest.param("[1, 2]", [_STANDING_GOAL], id="no-valid-item-keeps"),
        pytest.param(
            '[{"goal": "约好时间", "reasoning": "对方说周六有空"}]',
            [{"goal": "约好时间", "reasoning": "对方说周六有空"}],
            id="new-goal-replaces",
        ),
    ],
)
async def test_goal_analysis_clears_goals_only_when_the_model_answers_an_empty_list(
    answer, expected
):
    """模型明确答空数组表示目标都已失效，要清空；答不出或条目全不合格时保留原目标。"""

    from plugins.xiaoqing_chat.config.config import PersonalityConfig
    from plugins.xiaoqing_chat.llm.llm_config import LLMCallConfig
    from plugins.xiaoqing_chat.planning.pfc_goal_analyzer import analyze_goals

    model = ScriptedChatModel(goals=answer)
    result = await analyze_goals(
        secrets             = {"_ai": model, "_route": "reasoning"},
        bot_name            = "小青",
        personality         = PersonalityConfig(),
        history             = [],
        current_goal_list   = [_STANDING_GOAL],
        action_history_text = "",
        llm                 = LLMCallConfig(),
    )

    assert result == expected
    assert [call.kind for call in model.calls] == ["goals"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("乐", "围绕旧话题继续", id="pure-reaction-keeps"),
        pytest.param("[QQ表情：汪汪大哭]", "围绕旧话题继续", id="face-keeps"),
        pytest.param("周末去爬山", '围绕"周末去爬山"继续聊', id="topic-replaces"),
    ],
)
async def test_pure_reaction_turn_keeps_the_existing_goal(
    mock_context, sample_group_event, text, expected
):
    """纯反应消息不能独立成为话题，保留原目标；有内容的消息按这句话换成新目标。"""

    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    chat_id = "g67890"
    model   = ScriptedChatModel()
    scripted_chat_context(mock_context, model, GROUP_GATE_CLOSED)
    state = bound_state(mock_context)
    await state.goal_store.set_async(chat_id, goal="围绕旧话题继续", source="user")

    await handle_smalltalk(text, chat_message_event(sample_group_event, text), mock_context)

    assert state.goal_store.get(chat_id).goal == expected
    await settle_chat_background(mock_context, model)
