"""回复门禁和短期跟进计划。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
from plugins.xiaoqing_chat.memory.memory import MemoryStore
from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore
from plugins.xiaoqing_chat.planning.pfc_engine import PFCRunResult, run_pfc_once
from plugins.xiaoqing_chat.planning.pfc_state import PFCConversationState
from plugins.xiaoqing_chat.store_binding import bound_state
from tests.helpers.xiaoqing_chat_test_support import (
    GROUP_GATE_OPEN,
    ScriptedChatModel,
    chat_message_event,
    plan_answer,
    scripted_chat_context,
    settle_chat_background,
)

mock_context       = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event


class TestReplyGate:
    @pytest.mark.asyncio
    async def test_heartflow_low_score_does_not_reduce_group_base_probability(self):
        from unittest.mock import MagicMock, patch

        runtime                                          = MagicMock()
        runtime.cfg.reply_probability_base               = 0.6
        runtime.cfg.min_reply_interval_seconds           = 0.0
        runtime.cfg.max_replies_per_minute               = 100
        runtime.cfg.continuous_reply_limit               = 0
        runtime.cfg.continuous_cooldown_seconds          = 0.0
        runtime.cfg.heartflow.enable_heartflow           = True
        runtime.cfg.heartflow.base_score                 = 0.2
        runtime.cfg.heartflow.weight_short_text          = -0.08
        runtime.cfg.heartflow.weight_no_reply_streak     = 0.05
        runtime.cfg.heartflow.weight_long_silence        = 0.08
        runtime.cfg.brain_chat.enable_private_brain_chat = False
        runtime.cfg.goal.enable_goal                     = False

        state                                            = MagicMock()
        state.get_last_reply_ts.return_value             = 0.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value          = []
        state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal=""))
        state.heartflow.score_async = AsyncMock(return_value=0.2)
        state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))

        from plugins.xiaoqing_chat.frequency_control import decide_reply

        with patch("plugins.xiaoqing_chat.frequency_control.random") as mock_rand:
            mock_rand.random.return_value = 0.55
            result = await decide_reply(runtime, state, "g1", "今天天气不错", False, False)

        assert result is True

    @pytest.mark.asyncio
    async def test_reply_gate_records_random_skip_reason(self):
        from plugins.xiaoqing_chat.frequency_control import decide_reply

        runtime                                          = MagicMock()
        runtime.cfg.reply_probability_base               = 0.6
        runtime.cfg.min_reply_interval_seconds           = 0.0
        runtime.cfg.max_replies_per_minute               = 100
        runtime.cfg.heartflow.enable_heartflow           = False
        runtime.cfg.brain_chat.enable_private_brain_chat = False
        runtime.cfg.goal.enable_goal                     = False

        state                                            = MagicMock()
        state.get_last_reply_ts.return_value             = 0.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value          = []
        state.goal_store.get_async = AsyncMock(return_value=None)
        state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))
        state.set_reply_gate_decision = Mock()

        with patch("plugins.xiaoqing_chat.frequency_control.random.random", return_value=0.99):
            result = await decide_reply(runtime, state, "g1", "今天天气不错", False, False)

        assert result is False
        decision = state.set_reply_gate_decision.call_args.args[1]
        assert decision.reason == "probability"
        assert decision.probability == 0.6
        assert decision.roll == 0.99

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "streak,expected",
        [
            # 基础概率 0.35；连续没回复 3 / 5 条以上时门槛本身放宽到 1.2 / 1.4 倍。
            (0, 0.35 * 0.35),  # 睡着时普通群消息照常压低
            (3, 0.35 * 1.2 * (0.35 + 0.65 * 0.25)),  # 群里接着热闹，压制逐步减轻
            (9, 0.35 * 1.4 * (0.35 + 0.65 * 0.5)),  # 最多减到一半，不会和醒着一样
        ],
    )
    async def test_routine_pressure_eases_while_the_group_keeps_talking(self, streak, expected):
        from plugins.xiaoqing_chat.frequency_control import decide_reply

        runtime                                          = MagicMock()
        runtime.cfg.reply_probability_base               = 0.35
        runtime.cfg.min_reply_interval_seconds           = 0.0
        runtime.cfg.max_replies_per_minute               = 100
        runtime.cfg.heartflow.enable_heartflow           = False
        runtime.cfg.brain_chat.enable_private_brain_chat = False
        runtime.cfg.goal.enable_goal                     = False

        state                                            = MagicMock()
        state.get_last_reply_ts.return_value             = 0.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value          = []
        state.goal_store.get_async = AsyncMock(return_value=None)
        state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=streak))
        state.set_reply_gate_decision = Mock()

        with patch("plugins.xiaoqing_chat.frequency_control.random.random", return_value=0.99):
            await decide_reply(
                runtime, state, "g1", "今天天气不错", False, False, routine_factor=0.35
            )

        decision = state.set_reply_gate_decision.call_args.args[1]
        assert decision.probability == pytest.approx(expected, abs=1e-3)

    @pytest.mark.asyncio
    async def test_reply_gate_skips_standalone_protocol_face_without_planning(self):
        from plugins.xiaoqing_chat.frequency_control import decide_reply

        runtime                                = MagicMock()
        runtime.cfg.reply_probability_base     = 1.0
        runtime.cfg.min_reply_interval_seconds = 0.0
        runtime.cfg.max_replies_per_minute     = 100
        runtime.cfg.heartflow.enable_heartflow = True
        runtime.cfg.goal.enable_goal           = False

        state                                            = MagicMock()
        state.get_last_reply_ts.return_value             = 0.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value          = []
        state.set_reply_gate_decision                    = Mock()

        result = await decide_reply(
            runtime,
            state,
            "g1",
            "[QQ表情：汪汪大哭]",
            False,
            False,
        )

        assert result is False
        decision = state.set_reply_gate_decision.call_args.args[1]
        assert decision.reason == "low_information"
        state.heartflow.score_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_turn_addressed_to_another_member_still_reaches_the_planner(
        self, mock_context, sample_group_event
    ):
        from plugins.xiaoqing_chat.handlers import handle_smalltalk

        text = "小李，这个文件放哪里合适？"
        model = ScriptedChatModel(plan=plan_answer("wait", "点名的是别人"))
        scripted_chat_context(mock_context, model, GROUP_GATE_OPEN)

        result = await handle_smalltalk(
            text, chat_message_event(sample_group_event, text), mock_context
        )

        # 点名别的群友不在门控里拦下，由规划器看了上下文再决定回不回。
        assert result == []
        decision = bound_state(mock_context).get_reply_gate_decision("g67890")
        assert decision is not None
        assert (decision.should_reply, decision.reason) == (True, "allowed")
        assert len(model.calls_of("plan")) == 1
        await settle_chat_background(mock_context, model)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("roll,allowed", [(0.2, True), (0.7, False)])
    @pytest.mark.parametrize(
        "text",
        [
            "大家都散了，这事就到这吧",
            "大家刚才已经聊完了",
            "大家别再提这件事了",
            "大家有没有人还想来玩",
            "你们都说完了",
            "各位辛苦了，今天到这里",
            "我宣布，周一应该从一周里删掉",
            "今晚吃啥啊",
            "论文终于投出去了!!",
            "发个狗头",
            "下次一定",
        ],
    )
    async def test_group_wording_uses_base_probability(self, text, roll, allowed):
        from plugins.xiaoqing_chat.frequency_control import decide_reply

        runtime                                = MagicMock()
        runtime.cfg.reply_probability_base     = 0.45
        runtime.cfg.min_reply_interval_seconds = 0.0
        runtime.cfg.max_replies_per_minute     = 100
        runtime.cfg.heartflow.enable_heartflow = False
        runtime.cfg.goal.enable_goal           = False

        state                                            = MagicMock()
        state.get_last_reply_ts.return_value             = 0.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value          = []
        state.goal_store.get_async = AsyncMock(return_value=None)
        state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))
        state.set_reply_gate_decision       = Mock()
        state.memory_store.get_recent_async = AsyncMock(
            return_value=[
                SimpleNamespace(role="user", content=text),
                SimpleNamespace(role="user", content=text),
            ]
        )

        with patch("plugins.xiaoqing_chat.frequency_control.random.random", return_value=roll):
            result = await decide_reply(
                runtime,
                state,
                "g1",
                text,
                False,
                False,
            )

        assert result is allowed
        decision = state.set_reply_gate_decision.call_args.args[1]
        assert decision.probability == 0.45
        assert decision.reason == ("allowed" if allowed else "probability")

    @pytest.mark.asyncio
    async def test_reply_gate_records_min_interval_skip_reason(self):
        from plugins.xiaoqing_chat.frequency_control import decide_reply

        runtime                                          = MagicMock()
        runtime.cfg.reply_probability_base               = 0.6
        runtime.cfg.min_reply_interval_seconds           = 10.0
        runtime.cfg.max_replies_per_minute               = 100
        runtime.cfg.heartflow.enable_heartflow           = False
        runtime.cfg.brain_chat.enable_private_brain_chat = False
        runtime.cfg.goal.enable_goal                     = False

        state                                            = MagicMock()
        state.get_last_reply_ts.return_value             = 95.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value          = []
        state.goal_store.get_async = AsyncMock(return_value=None)
        state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))
        state.set_reply_gate_decision = Mock()

        with patch("plugins.xiaoqing_chat.frequency_control.time.time", return_value=100.0):
            result = await decide_reply(runtime, state, "g1", "今天天气不错", False, False)

        assert result is False
        decision = state.set_reply_gate_decision.call_args.args[1]
        assert decision.reason == "min_interval"
        assert decision.seconds_since_last_reply == 5.0
        assert decision.min_interval_seconds == 10.0

    @pytest.mark.asyncio
    async def test_active_topic_min_interval_uses_configured_value(self):
        from plugins.xiaoqing_chat.frequency_control import decide_reply

        runtime                                          = MagicMock()
        runtime.cfg.reply_probability_base               = 0.6
        runtime.cfg.min_reply_interval_seconds           = 10.0
        runtime.cfg.active_topic_min_reply_interval      = 4.0
        runtime.cfg.active_topic_reply_probability       = 0.6
        runtime.cfg.max_replies_per_minute               = 100
        runtime.cfg.heartflow.enable_heartflow           = False
        runtime.cfg.brain_chat.enable_private_brain_chat = False
        runtime.cfg.goal.enable_goal                     = True

        state                                            = MagicMock()
        state.get_last_reply_ts.return_value             = 97.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value          = []
        state.goal_store.get_async                       = AsyncMock(
            return_value=SimpleNamespace(goal="自然聊天", ts=95.0)
        )
        state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))
        state.set_reply_gate_decision = Mock()

        with patch("plugins.xiaoqing_chat.frequency_control.time.time", return_value=100.0):
            result = await decide_reply(runtime, state, "g1", "继续聊", False, False)

        assert result is False
        decision = state.set_reply_gate_decision.call_args.args[1]
        assert decision.reason == "min_interval"
        assert decision.active_topic is True
        assert decision.min_interval_seconds == 4.0

        with (
            patch("plugins.xiaoqing_chat.frequency_control.time.time", return_value=100.0),
            patch("plugins.xiaoqing_chat.frequency_control.random.random", return_value=0.0),
        ):
            result = await decide_reply(runtime, state, "g1", "那为什么会这样？", False, False)

        assert result is False
        decision = state.set_reply_gate_decision.call_args.args[1]
        assert decision.min_interval_seconds == 4.0

    @pytest.mark.asyncio
    async def test_active_topic_uses_explicit_probability_without_aggressive_boost(self):
        from plugins.xiaoqing_chat.frequency_control import decide_reply

        runtime                                     = MagicMock()
        runtime.cfg.reply_probability_base          = 0.45
        runtime.cfg.active_topic_reply_probability  = 0.6
        runtime.cfg.min_reply_interval_seconds      = 0.0
        runtime.cfg.active_topic_min_reply_interval = 0.0
        runtime.cfg.max_replies_per_minute          = 100
        runtime.cfg.heartflow.enable_heartflow      = False
        runtime.cfg.goal.enable_goal                = True

        state                                            = MagicMock()
        state.get_last_reply_ts.return_value             = 90.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value          = []
        state.goal_store.get_async                       = AsyncMock(
            return_value=SimpleNamespace(goal="已有话题", ts=80.0)
        )
        state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))
        state.set_reply_gate_decision = Mock()

        with (
            patch("plugins.xiaoqing_chat.frequency_control.time.time", return_value=100.0),
            patch("plugins.xiaoqing_chat.frequency_control.random.random", return_value=0.65),
        ):
            result = await decide_reply(runtime, state, "g1", "继续聊", False, False)

        assert result is False
        decision = state.set_reply_gate_decision.call_args.args[1]
        assert decision.active_topic is True
        assert decision.probability == 0.6

        with (
            patch("plugins.xiaoqing_chat.frequency_control.time.time", return_value=100.0),
            patch("plugins.xiaoqing_chat.frequency_control.random.random", return_value=0.85),
        ):
            result = await decide_reply(runtime, state, "g1", "那为什么会这样？", False, False)

        assert result is False
        decision = state.set_reply_gate_decision.call_args.args[1]
        assert decision.probability == 0.6

    @pytest.mark.asyncio
    async def test_group_heartflow_bonus_stays_soft(self):
        from unittest.mock import MagicMock, patch

        runtime                                          = MagicMock()
        runtime.cfg.reply_probability_base               = 0.6
        runtime.cfg.min_reply_interval_seconds           = 0.0
        runtime.cfg.max_replies_per_minute               = 100
        runtime.cfg.continuous_reply_limit               = 0
        runtime.cfg.continuous_cooldown_seconds          = 0.0
        runtime.cfg.heartflow.enable_heartflow           = True
        runtime.cfg.heartflow.base_score                 = 0.2
        runtime.cfg.heartflow.weight_short_text          = -0.08
        runtime.cfg.heartflow.weight_no_reply_streak     = 0.05
        runtime.cfg.heartflow.weight_long_silence        = 0.08
        runtime.cfg.brain_chat.enable_private_brain_chat = False
        runtime.cfg.goal.enable_goal                     = False

        state                                            = MagicMock()
        state.get_last_reply_ts.return_value             = 0.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value          = []
        state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal=""))
        state.heartflow.score_async = AsyncMock(return_value=0.32)
        state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))

        from plugins.xiaoqing_chat.frequency_control import decide_reply

        with patch("plugins.xiaoqing_chat.frequency_control.random") as mock_rand:
            mock_rand.random.return_value = 0.67
            result = await decide_reply(runtime, state, "g1", "今天天气不错", False, False)

        assert result is False

    @pytest.mark.asyncio
    async def test_long_no_reply_streak_can_break_through_low_interest_group_chatter(self):
        from unittest.mock import MagicMock, patch

        runtime                                          = MagicMock()
        runtime.cfg.reply_probability_base               = 0.5
        runtime.cfg.min_reply_interval_seconds           = 0.0
        runtime.cfg.max_replies_per_minute               = 100
        runtime.cfg.continuous_reply_limit               = 0
        runtime.cfg.continuous_cooldown_seconds          = 0.0
        runtime.cfg.heartflow.enable_heartflow           = False
        runtime.cfg.brain_chat.enable_private_brain_chat = False
        runtime.cfg.goal.enable_goal                     = False

        state                                            = MagicMock()
        state.get_last_reply_ts.return_value             = 0.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value          = []
        state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal=""))
        state.heartflow.score_async = AsyncMock(return_value=1.0)
        state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=12))

        from plugins.xiaoqing_chat.frequency_control import decide_reply

        with patch("plugins.xiaoqing_chat.frequency_control.random") as mock_rand:
            mock_rand.random.return_value = 0.3
            result = await decide_reply(runtime, state, "g1", "哦", False, False)

        assert result is True


_NOW = 1_800_000_000.0


async def _run_planner_turn(
    tmp_path,
    *,
    chat_id: str,
    bot_replied_ago: float,
    last_successful_reply_action: str,
    plan: str,
    user_text: str = "收到",
) -> tuple[PFCRunResult, PFCConversationState, ScriptedChatModel, AsyncMock]:
    """固定时钟，让规划器在“她 ``bot_replied_ago`` 秒前说过话、对方刚刚接了一句”的群里跑一轮。

    返回结果、规划状态、剧本模型和回复生成回调；规划器的回答由 ``plan`` 给定。
    """

    context              = MagicMock()
    context.data_dir     = tmp_path
    context.http_session = AsyncMock()

    memory_store = MemoryStore()
    memory_store.append(
        chat_id, role="assistant", name="小青", content="刚刚说过了", ts=_NOW - bot_replied_ago
    )
    memory_store.append(chat_id, role="user", name="Tester", content=user_text, ts=_NOW - 1)

    pfc_state = PFCConversationState(
        chat_id, last_successful_reply_action=last_successful_reply_action
    )
    model = ScriptedChatModel(plan=plan)
    generate_reply = AsyncMock(return_value="不该发送")
    cfg                                    = XiaoQingChatConfig()
    cfg.pfc_followup_action_window_seconds = 120.0

    with patch("time.time", return_value=_NOW):
        result = await run_pfc_once(
            context        = context,
            runtime_cfg    = cfg,
            secrets        = {"_ai": model, "_route": "reasoning"},
            bot_name       = "小青",
            is_private     = False,
            chat_id        = chat_id,
            current_text   = user_text,
            memory_store   = memory_store,
            action_history = ActionHistoryStore(),
            state          = pfc_state,
            generate_reply = generate_reply,
        )
    return result, pfc_state, model, generate_reply


@pytest.mark.asyncio
async def test_group_wait_plan_is_preserved_after_bot_already_replied(tmp_path):
    wait_plan = json.dumps(
        {"action": "wait", "reason": "先等等", "thinking": "刚说完", "wait_seconds": 20},
        ensure_ascii=False,
    )

    result, _state, _model, generate_reply = await _run_planner_turn(
        tmp_path,
        chat_id                      = "group-2",
        bot_replied_ago              = 5,
        last_successful_reply_action = "direct_reply",
        plan                         = wait_plan,
    )

    assert result.action == "wait"
    assert result.reply == ""
    assert result.wait_seconds == 20
    generate_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_pfc_once_drops_stale_followup_reply_action(tmp_path):
    chat_id = "stale-followup-action"
    wait    = plan_answer("wait", "先看看")

    # 她上次说话是一小时前，早已超出跟进窗口：规划器按“没有刚发过回复”来规划，
    # 看到的提示和从没回过时完全一样，规划状态里的跟进动作也被清掉。
    result, state, model, generate_reply = await _run_planner_turn(
        tmp_path,
        chat_id                      = chat_id,
        bot_replied_ago              = 3600,
        last_successful_reply_action = "direct_reply",
        plan                         = wait,
    )
    _, _, fresh_model, _ = await _run_planner_turn(
        tmp_path,
        chat_id                      = chat_id,
        bot_replied_ago              = 3600,
        last_successful_reply_action = "",
        plan                         = wait,
    )

    assert model.calls_of("plan")[0].text == fresh_model.calls_of("plan")[0].text
    assert state.last_successful_reply_action == ""
    assert result.action == "wait"
    generate_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_pfc_once_keeps_recent_followup_reply_action(tmp_path):
    chat_id = "recent-followup-action"
    wait    = plan_answer("wait", "刚说过，等一下")

    # 她半分钟前刚说过话，仍在跟进窗口内：规划器拿到的是“跟进”的提示，和从没回过时不同。
    result, state, model, _generate_reply = await _run_planner_turn(
        tmp_path,
        chat_id                      = chat_id,
        bot_replied_ago              = 30,
        last_successful_reply_action = "direct_reply",
        plan                         = wait,
    )
    _, _, fresh_model, _ = await _run_planner_turn(
        tmp_path,
        chat_id                      = chat_id,
        bot_replied_ago              = 30,
        last_successful_reply_action = "",
        plan                         = wait,
    )

    assert model.calls_of("plan")[0].text != fresh_model.calls_of("plan")[0].text
    assert state.last_successful_reply_action == "direct_reply"
    assert result.action == "wait"
