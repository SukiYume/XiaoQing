"""回复门禁和短期跟进计划。"""

from __future__ import annotations

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from tests.helpers.xiaoqing_chat_test_support import (
    AsyncMock,
    MagicMock,
    Mock,
    SimpleNamespace,
    asyncio,
    patch,
    pytest,
    time,
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
        runtime.cfg.heartflow.weight_question            = 0.12
        runtime.cfg.heartflow.weight_goal_match          = 0.06
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

        from plugins.xiaoqing_chat.frequency_control import _should_reply

        with patch("plugins.xiaoqing_chat.frequency_control.random") as mock_rand:
            mock_rand.random.return_value = 0.55
            result = await _should_reply(runtime, state, "g1", "今天天气不错", False, False)

        assert result is True

    @pytest.mark.asyncio
    async def test_reply_gate_records_random_skip_reason(self):
        from plugins.xiaoqing_chat.frequency_control import _should_reply

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
            result = await _should_reply(runtime, state, "g1", "今天天气不错", False, False)

        assert result is False
        decision = state.set_reply_gate_decision.call_args.args[1]
        assert decision.reason == "probability"
        assert decision.probability == 0.6
        assert decision.roll == 0.99

    @pytest.mark.asyncio
    async def test_reply_gate_skips_standalone_protocol_face_without_planning(self):
        from plugins.xiaoqing_chat.frequency_control import _should_reply

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

        result = await _should_reply(
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
    async def test_reply_gate_skips_turn_explicitly_addressed_to_another_member(self):
        from plugins.xiaoqing_chat.frequency_control import _should_reply

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

        result = await _should_reply(
            runtime,
            state,
            "g1",
            "小李，这个文件放哪里合适？",
            False,
            False,
        )

        assert result is False
        decision = state.set_reply_gate_decision.call_args.args[1]
        assert decision.reason == "directed_to_other"
        state.heartflow.score_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_clear_group_participation_cue_uses_higher_probability(self):
        from plugins.xiaoqing_chat.frequency_control import _should_reply

        runtime                                         = MagicMock()
        runtime.cfg.reply_probability_base              = 0.45
        runtime.cfg.participation_cue_reply_probability = 0.8
        runtime.cfg.min_reply_interval_seconds          = 0.0
        runtime.cfg.max_replies_per_minute              = 100
        runtime.cfg.heartflow.enable_heartflow          = False
        runtime.cfg.goal.enable_goal                    = False

        state                                            = MagicMock()
        state.get_last_reply_ts.return_value             = 0.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value          = []
        state.goal_store.get_async = AsyncMock(return_value=None)
        state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))
        state.set_reply_gate_decision = Mock()

        with patch("plugins.xiaoqing_chat.frequency_control.random.random", return_value=0.7):
            result = await _should_reply(
                runtime,
                state,
                "g1",
                "我宣布，周一应该从一周里删掉",
                False,
                False,
            )

        assert result is True
        decision = state.set_reply_gate_decision.call_args.args[1]
        assert decision.probability == 0.8
        assert decision.participation_cue == "opening_banter"

    @pytest.mark.asyncio
    async def test_message_directed_to_other_is_rejected_before_probability_roll(self):
        from plugins.xiaoqing_chat.frequency_control import _should_reply

        runtime                                         = MagicMock()
        runtime.cfg.reply_probability_base              = 0.45
        runtime.cfg.participation_cue_reply_probability = 0.8
        runtime.cfg.min_reply_interval_seconds          = 0.0
        runtime.cfg.max_replies_per_minute              = 100
        runtime.cfg.heartflow.enable_heartflow          = False
        runtime.cfg.goal.enable_goal                    = False

        state                                            = MagicMock()
        state.get_last_reply_ts.return_value             = 0.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value          = []
        state.goal_store.get_async = AsyncMock(return_value=None)
        state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))
        state.set_reply_gate_decision = Mock()

        with patch("plugins.xiaoqing_chat.frequency_control.random.random", return_value=0.7):
            result = await _should_reply(
                runtime,
                state,
                "g1",
                "阿泽你上次推荐那家店到底好不好吃",
                False,
                False,
            )

        assert result is False
        decision = state.set_reply_gate_decision.call_args.args[1]
        assert decision.reason == "directed_to_other"
        assert decision.probability is None
        assert decision.roll is None
        assert decision.participation_cue == ""

    @pytest.mark.asyncio
    async def test_reply_gate_records_min_interval_skip_reason(self):
        from plugins.xiaoqing_chat.frequency_control import _should_reply

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
            result = await _should_reply(runtime, state, "g1", "今天天气不错", False, False)

        assert result is False
        decision = state.set_reply_gate_decision.call_args.args[1]
        assert decision.reason == "min_interval"
        assert decision.seconds_since_last_reply == 5.0
        assert decision.min_interval_seconds == 10.0

    @pytest.mark.asyncio
    async def test_active_topic_min_interval_uses_configured_value(self):
        from plugins.xiaoqing_chat.frequency_control import _should_reply

        runtime                                              = MagicMock()
        runtime.cfg.reply_probability_base                   = 0.6
        runtime.cfg.min_reply_interval_seconds               = 10.0
        runtime.cfg.active_topic_min_reply_interval          = 4.0
        runtime.cfg.active_topic_question_min_reply_interval = 2.0
        runtime.cfg.participation_cue_reply_probability      = 0.8
        runtime.cfg.active_topic_reply_probability           = 0.6
        runtime.cfg.active_topic_question_reply_probability  = 0.9
        runtime.cfg.max_replies_per_minute                   = 100
        runtime.cfg.heartflow.enable_heartflow               = False
        runtime.cfg.brain_chat.enable_private_brain_chat     = False
        runtime.cfg.goal.enable_goal                         = True

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
            result = await _should_reply(runtime, state, "g1", "继续聊", False, False)

        assert result is False
        decision = state.set_reply_gate_decision.call_args.args[1]
        assert decision.reason == "min_interval"
        assert decision.active_topic is True
        assert decision.min_interval_seconds == 4.0

        with (
            patch("plugins.xiaoqing_chat.frequency_control.time.time", return_value=100.0),
            patch("plugins.xiaoqing_chat.frequency_control.random.random", return_value=0.0),
        ):
            result = await _should_reply(runtime, state, "g1", "那为什么会这样？", False, False)

        assert result is True
        decision = state.set_reply_gate_decision.call_args.args[1]
        assert decision.min_interval_seconds == 2.0

    @pytest.mark.asyncio
    async def test_active_topic_uses_explicit_probability_without_aggressive_boost(self):
        from plugins.xiaoqing_chat.frequency_control import _should_reply

        runtime                                              = MagicMock()
        runtime.cfg.reply_probability_base                   = 0.45
        runtime.cfg.active_topic_reply_probability           = 0.6
        runtime.cfg.min_reply_interval_seconds               = 0.0
        runtime.cfg.active_topic_min_reply_interval          = 0.0
        runtime.cfg.active_topic_question_min_reply_interval = 0.0
        runtime.cfg.participation_cue_reply_probability      = 0.8
        runtime.cfg.max_replies_per_minute                   = 100
        runtime.cfg.heartflow.enable_heartflow               = False
        runtime.cfg.goal.enable_goal                         = True

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
            result = await _should_reply(runtime, state, "g1", "继续聊", False, False)

        assert result is False
        decision = state.set_reply_gate_decision.call_args.args[1]
        assert decision.active_topic is True
        assert decision.probability == 0.6

        runtime.cfg.active_topic_question_reply_probability = 0.9
        with (
            patch("plugins.xiaoqing_chat.frequency_control.time.time", return_value=100.0),
            patch("plugins.xiaoqing_chat.frequency_control.random.random", return_value=0.85),
        ):
            result = await _should_reply(runtime, state, "g1", "那为什么会这样？", False, False)

        assert result is True
        decision = state.set_reply_gate_decision.call_args.args[1]
        assert decision.probability == 0.9

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
        runtime.cfg.heartflow.weight_question            = 0.12
        runtime.cfg.heartflow.weight_goal_match          = 0.06
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

        from plugins.xiaoqing_chat.frequency_control import _should_reply

        with patch("plugins.xiaoqing_chat.frequency_control.random") as mock_rand:
            mock_rand.random.return_value = 0.67
            result = await _should_reply(runtime, state, "g1", "今天天气不错", False, False)

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

        from plugins.xiaoqing_chat.frequency_control import _should_reply

        with patch("plugins.xiaoqing_chat.frequency_control.random") as mock_rand:
            mock_rand.random.return_value = 0.3
            result = await _should_reply(runtime, state, "g1", "哦", False, False)

        assert result is True


def test_group_wait_plan_is_preserved_before_first_reply(tmp_path):
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.memory.memory import MemoryStore
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PFCPlan
    from plugins.xiaoqing_chat.planning.pfc_engine import run_pfc_once
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore

    chat_id              = "group-1"
    cfg                  = XiaoQingChatConfig()
    context              = MagicMock()
    context.data_dir     = tmp_path
    context.http_session = AsyncMock()

    memory_store = MemoryStore()
    memory_store.append(chat_id, role="user", name="Tester", content="有人在聊火锅")

    action_history  = ActionHistoryStore()
    memory_db       = MagicMock()
    pfc_state_store = PFCStateStore()
    generate_reply = AsyncMock(return_value="我也想吃火锅")

    with patch(
        "plugins.xiaoqing_chat.planning.pfc_engine.plan_next_action",
        new=AsyncMock(
            return_value=PFCPlan(
                action="wait", reason="先看看", thinking="群里还在聊", wait_seconds=20
            )
        ),
    ):
        result = asyncio.run(
            run_pfc_once(
                context=context,
                runtime_cfg=cfg,
                secrets={"api_base": "http://test", "api_key": "key", "model": "test-model"},
                bot_name="小青",
                is_private=False,
                chat_id=chat_id,
                current_text="有人在聊火锅",
                memory_store=memory_store,
                action_history=action_history,
                memory_db=memory_db,
                pfc_state_store=pfc_state_store,
                generate_reply=generate_reply,
            )
        )

    assert result.action == "wait"
    assert result.reply == ""
    assert result.wait_seconds == 20
    generate_reply.assert_not_awaited()


def test_group_wait_plan_is_preserved_after_bot_already_replied(tmp_path):
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.memory.memory import MemoryStore
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PFCPlan
    from plugins.xiaoqing_chat.planning.pfc_engine import run_pfc_once
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore

    chat_id              = "group-2"
    cfg                  = XiaoQingChatConfig()
    context              = MagicMock()
    context.data_dir     = tmp_path
    context.http_session = AsyncMock()

    memory_store = MemoryStore()
    memory_store.append(chat_id, role="assistant", name="小青", content="刚刚说过了")
    memory_store.append(chat_id, role="user", name="Tester", content="收到")

    action_history  = ActionHistoryStore()
    memory_db       = MagicMock()
    pfc_state_store = PFCStateStore()
    pfc_state_store.bind(tmp_path)
    st                              = pfc_state_store.get(chat_id)
    st.last_successful_reply_action = "direct_reply"
    pfc_state_store.save(chat_id)
    generate_reply = AsyncMock(return_value="不该发送")

    with patch(
        "plugins.xiaoqing_chat.planning.pfc_engine.plan_next_action",
        new=AsyncMock(
            return_value=PFCPlan(action="wait", reason="先等等", thinking="刚说完", wait_seconds=20)
        ),
    ):
        result = asyncio.run(
            run_pfc_once(
                context=context,
                runtime_cfg=cfg,
                secrets={"api_base": "http://test", "api_key": "key", "model": "test-model"},
                bot_name="小青",
                is_private=False,
                chat_id=chat_id,
                current_text="收到",
                memory_store=memory_store,
                action_history=action_history,
                memory_db=memory_db,
                pfc_state_store=pfc_state_store,
                generate_reply=generate_reply,
            )
        )

    assert result.action == "wait"
    assert result.reply == ""
    assert result.wait_seconds == 20
    generate_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_pfc_once_drops_stale_followup_reply_action(tmp_path):
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.memory.memory import MemoryStore
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PFCPlan
    from plugins.xiaoqing_chat.planning.pfc_engine import run_pfc_once
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore

    now                                    = time.time()
    chat_id                                = "stale-followup-action"
    cfg                                    = XiaoQingChatConfig()
    cfg.pfc_followup_action_window_seconds = 120.0
    context                                = MagicMock()
    context.data_dir                       = tmp_path
    context.http_session                   = AsyncMock()

    memory_store = MemoryStore()
    memory_store.append(chat_id, role="assistant", name="小青", content="很久前说过", ts=now - 3600)
    memory_store.append(chat_id, role="user", name="Tester", content="新图来了", ts=now - 1)

    action_history  = ActionHistoryStore()
    memory_db       = MagicMock()
    pfc_state_store = PFCStateStore()
    pfc_state_store.bind(tmp_path)
    st                              = pfc_state_store.get(chat_id)
    st.last_successful_reply_action = "direct_reply"
    pfc_state_store.save(chat_id)
    generate_reply = AsyncMock(return_value="不该发送")
    captured: dict[str, str] = {}

    async def fake_plan_next_action(**kwargs):
        captured["last_successful_reply_action"] = kwargs["last_successful_reply_action"]
        return PFCPlan(action="wait", reason="先看看", thinking="", wait_seconds=0)

    with patch(
        "plugins.xiaoqing_chat.planning.pfc_engine.plan_next_action",
        new=fake_plan_next_action,
    ):
        result = await run_pfc_once(
            context         = context,
            runtime_cfg     = cfg,
            secrets         = {"api_base": "http://test", "api_key": "key", "model": "test-model"},
            bot_name        = "小青",
            is_private      = False,
            chat_id         = chat_id,
            current_text    = "新图来了",
            memory_store    = memory_store,
            action_history  = action_history,
            memory_db       = memory_db,
            pfc_state_store = pfc_state_store,
            generate_reply  = generate_reply,
        )

    assert captured["last_successful_reply_action"] == ""
    assert pfc_state_store.get(chat_id).last_successful_reply_action == ""
    assert result.action == "wait"
    generate_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_pfc_once_keeps_recent_followup_reply_action(tmp_path):
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.memory.memory import MemoryStore
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PFCPlan
    from plugins.xiaoqing_chat.planning.pfc_engine import run_pfc_once
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore

    now                                    = time.time()
    chat_id                                = "recent-followup-action"
    cfg                                    = XiaoQingChatConfig()
    cfg.pfc_followup_action_window_seconds = 120.0
    context                                = MagicMock()
    context.data_dir                       = tmp_path
    context.http_session                   = AsyncMock()

    memory_store = MemoryStore()
    memory_store.append(chat_id, role="assistant", name="小青", content="刚说过", ts=now - 30)
    memory_store.append(chat_id, role="user", name="Tester", content="接一句", ts=now - 1)

    action_history  = ActionHistoryStore()
    memory_db       = MagicMock()
    pfc_state_store = PFCStateStore()
    pfc_state_store.bind(tmp_path)
    st                              = pfc_state_store.get(chat_id)
    st.last_successful_reply_action = "direct_reply"
    pfc_state_store.save(chat_id)
    generate_reply = AsyncMock(return_value="")
    captured: dict[str, str] = {}

    async def fake_plan_next_action(**kwargs):
        captured["last_successful_reply_action"] = kwargs["last_successful_reply_action"]
        return PFCPlan(action="wait", reason="刚说过，等一下", thinking="", wait_seconds=0)

    with patch(
        "plugins.xiaoqing_chat.planning.pfc_engine.plan_next_action",
        new=fake_plan_next_action,
    ):
        result = await run_pfc_once(
            context         = context,
            runtime_cfg     = cfg,
            secrets         = {"api_base": "http://test", "api_key": "key", "model": "test-model"},
            bot_name        = "小青",
            is_private      = False,
            chat_id         = chat_id,
            current_text    = "接一句",
            memory_store    = memory_store,
            action_history  = action_history,
            memory_db       = memory_db,
            pfc_state_store = pfc_state_store,
            generate_reply  = generate_reply,
        )

    assert captured["last_successful_reply_action"] == "direct_reply"
    assert result.action == "wait"


@pytest.mark.asyncio
async def test_run_pfc_once_preserves_group_wait_plan_for_live_short_followups(tmp_path):
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.memory.memory import MemoryStore
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PFCPlan
    from plugins.xiaoqing_chat.planning.pfc_engine import run_pfc_once
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore

    now                  = time.time()
    chat_id              = "group-live-short-followups"
    cfg                  = XiaoQingChatConfig()
    context              = MagicMock()
    context.data_dir     = tmp_path
    context.http_session = AsyncMock()

    memory_store = MemoryStore()
    memory_store.append(chat_id, role="assistant", name="小青", content="刚刚说过了", ts=now - 25)
    memory_store.append(chat_id, role="user", name="Tester", content="乐", ts=now - 5)
    memory_store.append(chat_id, role="user", name="Tester", content="今儿碰见了", ts=now - 1)

    action_history  = ActionHistoryStore()
    memory_db       = MagicMock()
    pfc_state_store = PFCStateStore()
    pfc_state_store.bind(tmp_path)
    st                              = pfc_state_store.get(chat_id)
    st.last_successful_reply_action = "direct_reply"
    pfc_state_store.save(chat_id)
    generate_reply = AsyncMock(return_value="细说，碰见谁了")

    with patch(
        "plugins.xiaoqing_chat.planning.pfc_engine.plan_next_action",
        new=AsyncMock(
            return_value=PFCPlan(
                action       = "wait",
                reason       = "没有人在跟我直接对话，先等等",
                thinking     = "群里在接话，但不是明确问我",
                wait_seconds = 20,
            )
        ),
    ):
        result = await run_pfc_once(
            context         = context,
            runtime_cfg     = cfg,
            secrets         = {"api_base": "http://test", "api_key": "key", "model": "test-model"},
            bot_name        = "小青",
            is_private      = False,
            chat_id         = chat_id,
            current_text    = "今儿碰见了",
            memory_store    = memory_store,
            action_history  = action_history,
            memory_db       = memory_db,
            pfc_state_store = pfc_state_store,
            generate_reply  = generate_reply,
        )

    assert result.action == "wait"
    assert result.reply == ""
    generate_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_store_get_async_loads_via_to_thread(tmp_path):
    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    chat_id = "threaded-load"
    store   = MemoryStore(tmp_path)
    store.persist(chat_id)
    (tmp_path / f"{chat_id}.json").write_text(
        '[{"role":"user","name":"Tester","content":"hello","ts":1.0}]',
        encoding="utf-8",
    )

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    with patch(
        "plugins.xiaoqing_chat.memory.memory.asyncio.to_thread",
        new=AsyncMock(side_effect=fake_to_thread),
    ) as mock_to_thread:
        history = await store.get_async(chat_id)

    assert mock_to_thread.await_count == 1
    assert len(history) == 1
    assert history[0].content == "hello"
