"""回复检查异常与内部重置的任务生命周期。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from unittest.mock import AsyncMock, patch

import pytest

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from plugins.xiaoqing_chat.store_binding import bound_state
from tests.helpers.assertions import logged_events
from tests.helpers.xiaoqing_chat_test_support import (
    GROUP_GATE_OPEN,
    ScriptedChatModel,
    ack_reply_delivery,
    chat_message_event,
    plan_answer,
    scripted_chat_context,
    set_context_principal,
    settle_chat_background,
)

mock_context       = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event


@pytest.mark.asyncio
async def test_generate_reply_checker_unexpected_error_logs_error_not_timeout(
    mock_context, sample_group_event, caplog
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    caplog.set_level(logging.INFO, logger="plugins.xiaoqing_chat")

    model = ScriptedChatModel(plan=plan_answer("direct_reply"), reply="这条回复需要检查")
    scripted_chat_context(
        mock_context,
        model,
        GROUP_GATE_OPEN,
        {"reply_check": {"enable_llm_checker": False, "llm_checker_mode": "risk"}},
    )

    # 审查本身出了没想到的错：记成审查出错（不是超时），风险模式下候选照常发出。
    with patch(
        "plugins.xiaoqing_chat.reply_generator.check_reply",
        new=AsyncMock(side_effect=RuntimeError("checker bug")),
    ):
        result = await handle_smalltalk(
            "你好", chat_message_event(sample_group_event, "你好"), mock_context
        )

    assert result == [{"type": "text", "data": {"text": "这条回复需要检查"}}]
    steps = [event.name for event in logged_events(caplog)]
    assert "reply.check.error" in steps
    assert "reply.check.timeout" not in steps
    await ack_reply_delivery(result)
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_handle_internal_reset_cancels_pending_persist_task(
    mock_context, sample_group_event, tmp_path
):
    from plugins.xiaoqing_chat.commands import handle_internal

    chat_id               = "g67890"
    mock_context.data_dir = tmp_path
    state                 = bound_state(mock_context)

    async def _pending_persist():
        await asyncio.sleep(30)

    persist_task                 = asyncio.create_task(_pending_persist())
    state.persist_tasks[chat_id] = persist_task

    set_context_principal(mock_context, sample_group_event, group_role="admin")
    result = await handle_internal("重置", "确认", sample_group_event, mock_context)

    assert result == [{"type": "text", "data": {"text": "✅ 已重置会话记忆"}}]
    assert chat_id not in state.persist_tasks
    assert persist_task.cancelled()
    with suppress(asyncio.CancelledError):
        await persist_task
