from types import SimpleNamespace
from unittest.mock import MagicMock

from plugins.xiaoqing_chat.config.config import (
    ExpressionConfig,
    HumanizeConfig,
    PersonalityConfig,
    ReflectionConfig,
    XiaoQingChatConfig,
)
from plugins.xiaoqing_chat.context_builder import _build_expression_block
from plugins.xiaoqing_chat.expression.bw_expression_learner import _build_dialogue
from plugins.xiaoqing_chat.expression.bw_expression_store import ExpressionRecord
from plugins.xiaoqing_chat.memory.knowledge_extract import build_fact_messages
from plugins.xiaoqing_chat.memory.memory import StoredMessage


def test_expression_block_requires_checked_records_when_approval_required(tmp_path):
    checked = ExpressionRecord(
        expression_id="checked",
        chat_id="g1",
        situation="表示惊讶时",
        style="短促地说一句我去",
        checked=True,
        rejected=False,
        count=1,
        last_active_time=10.0,
    )
    unchecked = ExpressionRecord(
        expression_id="unchecked",
        chat_id="g1",
        situation="听到离谱事情时",
        style="好家伙",
        checked=False,
        rejected=False,
        count=99,
        last_active_time=99.0,
    )
    state = SimpleNamespace(bw_expr_store=MagicMock())
    state.bw_expr_store.load.return_value = [unchecked, checked]
    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            expression=ExpressionConfig(
                enable_expression_selector=True,
                max_injected=5,
                # 关闭自动注入，回到旧的"必须 checked 才注入"行为
                auto_inject_min_count=0,
            ),
            reflection=ReflectionConfig(
                enable_expression_reflection=False,
                require_approval_for_injection=True,
            ),
        )
    )

    block = _build_expression_block(runtime, state, tmp_path, "g1")

    assert "短促地说一句我去" in block
    assert "好家伙" not in block


def test_expression_block_auto_injects_unchecked_high_count_records(tmp_path):
    low_count_unchecked = ExpressionRecord(
        expression_id="low",
        chat_id="g1",
        situation="听到普通新鲜事时",
        style="哦这样啊",
        checked=False,
        rejected=False,
        count=2,
        last_active_time=5.0,
    )
    high_count_unchecked = ExpressionRecord(
        expression_id="high",
        chat_id="g1",
        situation="听到离谱事情时",
        style="好家伙",
        checked=False,
        rejected=False,
        count=10,
        last_active_time=99.0,
    )
    state = SimpleNamespace(bw_expr_store=MagicMock())
    state.bw_expr_store.load.return_value = [low_count_unchecked, high_count_unchecked]
    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            expression=ExpressionConfig(
                enable_expression_selector=True,
                max_injected=5,
                auto_inject_min_count=3,
            ),
            reflection=ReflectionConfig(
                enable_expression_reflection=False,
                require_approval_for_injection=True,
            ),
        )
    )

    block = _build_expression_block(runtime, state, tmp_path, "g1")

    assert "好家伙" in block
    assert "哦这样啊" not in block


def test_expression_block_default_config_requires_approval_for_injection():
    cfg = XiaoQingChatConfig()

    assert cfg.reflection.require_approval_for_injection is True


def test_expression_learning_dialogue_excludes_assistant_messages():
    dialogue = _build_dialogue(
        [
            StoredMessage(role="assistant", name="小青", content="好家伙，又来了", ts=1.0),
            StoredMessage(
                role="user",
                name="群友",
                user_id=123456,
                content="这不比卖烤鸭香",
                ts=2.0,
            ),
        ],
        bot_name="小青",
    )

    assert "这不比卖烤鸭香" in dialogue
    assert "好家伙，又来了" not in dialogue
    assert "你(小青)" not in dialogue


def test_fact_prompt_uses_real_user_ids_and_skips_assistant_messages():
    messages = build_fact_messages(
        bot_name="小青",
        history=[
            StoredMessage(role="assistant", name="小青", content="我也喜欢这个", ts=1.0),
            StoredMessage(
                role="user",
                name="PulsEternal",
                user_id=503906146,
                content="我十连四发",
                ts=2.0,
            ),
        ],
    )

    prompt = messages[1].content
    assert "PulsEternal<503906146>：我十连四发" in prompt
    assert "小青" not in prompt


def test_expression_reflector_can_build_operator_review_action(tmp_path):
    import asyncio

    from plugins.xiaoqing_chat.expression.bw_expression_reflector import maybe_ask_for_reflection

    sent_actions = []
    context = SimpleNamespace(
        data_dir=tmp_path,
        send_action=lambda action: sent_actions.append(action),
    )
    expr_store = MagicMock()
    expr_store.load.return_value = [
        ExpressionRecord(
            expression_id="e1",
            chat_id="g1",
            situation="吐槽离谱事情时",
            style="短促地说一句好家伙",
            checked=False,
            rejected=False,
            count=5,
            last_active_time=10.0,
        )
    ]
    tracker_store = MagicMock()

    async def send_action(action):
        sent_actions.append(action)

    context.send_action = send_action

    sent = asyncio.run(
        maybe_ask_for_reflection(
            context=context,
            expr_store=expr_store,
            tracker_store=tracker_store,
            operator_user_id=123,
            operator_group_id=456,
            min_interval_seconds=0,
            ask_per_check=1,
        )
    )

    assert sent == 1
    assert sent_actions[0]["action"] == "send_group_msg"
    assert sent_actions[0]["params"]["group_id"] == 456


def test_humanize_typing_delay_scales_with_lengths_and_caps():
    from plugins.xiaoqing_chat.smalltalk_execution import _compute_typing_delay

    cfg = HumanizeConfig(
        enable_typing_delay=True,
        read_base_seconds=0.4,
        read_per_char_seconds=0.04,
        type_per_char_seconds=0.05,
        jitter_ratio=0.0,
        max_total_delay_seconds=5.0,
    )
    runtime = SimpleNamespace(cfg=SimpleNamespace(humanize=cfg))

    short = _compute_typing_delay(runtime, input_text="嗨", output_text="嗨")
    assert short > 0
    long_in_short_out = _compute_typing_delay(
        runtime, input_text="一" * 50, output_text=""
    )
    assert long_in_short_out > short

    capped = _compute_typing_delay(runtime, input_text="x" * 5000, output_text="y" * 5000)
    assert capped <= cfg.max_total_delay_seconds + 1e-6


def test_humanize_typing_delay_disabled_when_flag_off():
    from plugins.xiaoqing_chat.smalltalk_execution import _compute_typing_delay

    cfg = HumanizeConfig(enable_typing_delay=False)
    runtime = SimpleNamespace(cfg=SimpleNamespace(humanize=cfg))
    assert _compute_typing_delay(runtime, input_text="abc", output_text="def") == 0.0


def test_refresh_mood_state_reuses_active_mood_without_reroll():
    import random
    import time

    from plugins.xiaoqing_chat.handlers import _refresh_mood_state

    state = MagicMock()
    state.get_mood_state.return_value = "现在心情不错"
    # 最近一分钟有活动，远小于 idle threshold
    state.get_last_observe_ts.return_value = time.time() - 30
    state.get_last_reply_ts.return_value = time.time() - 60

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            personality=PersonalityConfig(
                states=["现在心情不错", "刚吃完饭"],
                state_probability=0.30,
                state_force_refresh_after_idle_seconds=14400,
            )
        )
    )

    # 即使随机数极端，活跃 mood 应直接复用
    random.seed(0)
    mood = _refresh_mood_state(runtime, state, "g1")
    assert mood == "现在心情不错"
    state.set_mood_state.assert_not_called()


def test_refresh_mood_state_resets_after_long_idle():
    import time

    from plugins.xiaoqing_chat.handlers import _refresh_mood_state

    state = MagicMock()
    state.get_mood_state.return_value = "现在心情不错"
    # 上次活跃距今超过 4h
    state.get_last_observe_ts.return_value = time.time() - 6 * 3600
    state.get_last_reply_ts.return_value = 0.0

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            personality=PersonalityConfig(
                states=["现在心情不错", "刚吃完饭"],
                state_probability=1.0,  # 强制进入重摇分支
                state_force_refresh_after_idle_seconds=14400,
                state_min_duration_seconds=7200,
                state_max_duration_seconds=21600,
            )
        )
    )

    mood = _refresh_mood_state(runtime, state, "g1")
    assert mood in {"现在心情不错", "刚吃完饭"}
    assert state.set_mood_state.called
