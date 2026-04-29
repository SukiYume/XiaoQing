from types import SimpleNamespace
from unittest.mock import MagicMock

from plugins.xiaoqing_chat.config.config import (
    ExpressionConfig,
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
            expression=ExpressionConfig(enable_expression_selector=True, max_injected=5),
            reflection=ReflectionConfig(
                enable_expression_reflection=False,
                require_approval_for_injection=True,
            ),
        )
    )

    block = _build_expression_block(runtime, state, tmp_path, "g1")

    assert "短促地说一句我去" in block
    assert "好家伙" not in block


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
