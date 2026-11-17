"""一整轮对话的日志里只有步骤、标签和长度：消息、回复和提示词正文都不进日志。

字段的脱敏规则由 ``tests/core/test_logging.py`` 覆盖；这里只看聊天插件实际交给日志的内容。
"""

from __future__ import annotations

import logging

import pytest

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from tests.helpers.assertions import logged_events, logged_records
from tests.helpers.xiaoqing_chat_test_support import (
    ScriptedChatModel,
    ack_reply_delivery,
    chat_message_event,
    plan_answer,
    scripted_chat_context,
    settle_chat_background,
)

mock_context = _fixture_support.mock_context

MESSAGE_CANARY = "XIAOQING_PRIVATE_MESSAGE_CANARY"
REPLY_CANARY   = "XIAOQING_PRIVATE_REPLY_CANARY"


@pytest.mark.asyncio
async def test_a_turn_logs_its_steps_and_prompt_roles_but_no_conversation_text(
    mock_context, sample_private_event, caplog
) -> None:
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    caplog.set_level(logging.DEBUG, logger="plugins.xiaoqing_chat")
    model = ScriptedChatModel(plan=plan_answer("direct_reply"), reply=f"记下了 {REPLY_CANARY}")
    scripted_chat_context(mock_context, model)
    text = f"帮我记一下 {MESSAGE_CANARY}"

    result = await handle_smalltalk(
        text, chat_message_event(sample_private_event, text), mock_context
    )
    await ack_reply_delivery(result)
    await settle_chat_background(mock_context, model)

    assert REPLY_CANARY in str(result)
    assert logged_events(caplog, "smalltalk.recv") and logged_events(caplog, "smalltalk.done")
    # 提示词只在调试级记每条消息的角色和长度。
    prompts = logged_records(caplog, "reply.prompt")
    assert {record.levelno for record in prompts} == {logging.DEBUG}
    assert {"system", "user"} <= {record.msg.fields["role"] for record in prompts}
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert MESSAGE_CANARY not in logged
    assert REPLY_CANARY not in logged
