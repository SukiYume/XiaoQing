"""Progress feedback follows all send results and preserves per-file receipts."""

from __future__ import annotations

import sys
import time
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.delivery import DeliveryReceipt
from plugins.xiaoqing_agent import receipts
from plugins.xiaoqing_agent.config import AgentConfig
from plugins.xiaoqing_agent.runtime import AgentRuntime
from plugins.xiaoqing_agent.shells import resolve_shell
from plugins.xiaoqing_agent.tasks import AgentTask, ChatRef
from plugins.xiaoqing_agent.tools import ToolEnv, build_tools, execute_tool
from tests.helpers.xiaoqing_agent_test_support import ADMIN_ID, FakeAgentContext


@pytest.mark.asyncio
@pytest.mark.parametrize("message_sent", [True, False, None])
@pytest.mark.parametrize("first_sent", [True, False, None])
@pytest.mark.parametrize("second_sent", [True, False, None])
async def test_progress_feedback_and_receipts_follow_each_send(
    tmp_path: Path, message_sent: bool | None, first_sent: bool | None, second_sent: bool | None
) -> None:
    context              = FakeAgentContext(tmp_path / "data")
    context.send_results = [message_sent, first_sent, second_sent]
    runtime              = AgentRuntime(context.data_dir)
    task = AgentTask("progress", ChatRef(ADMIN_ID, None), "目标", "原话", "chat", admin=True)
    initial_delivery = task.delivery
    workspace        = tmp_path / "workspace"
    workspace.mkdir()
    callbacks = []
    files     = []
    for name in ("plot.png", "first.csv", "second.csv"):
        path = workspace / name
        path.write_bytes(b"test attachment")
        files.append(str(path))
        commit, rollback, unknown = AsyncMock(), AsyncMock(), AsyncMock()
        callbacks.append((commit, rollback, unknown))
        await receipts.hold(
            task.held_receipts,
            DeliveryReceipt(expected_actions=1, commit=commit, rollback=rollback, unknown=unknown),
            [path],
        )
    env = ToolEnv(
        context=context,
        config=AgentConfig(enabled=True),
        task          = task,
        workspace     = workspace,
        out_dir       = workspace,
        python        = sys.executable,
        shell         = resolve_shell(""),
        deadline      = time.monotonic() + 600,
        send_progress = partial(runtime._send_progress, context, task),
    )
    tools = build_tools(env, loaded_plugins=frozenset({"xiaoqing_agent"}))
    before = time.monotonic()
    result = await execute_tool(tools, env, "send_message", {"text": "进度", "files": files})

    assert [action["action"] for action in context.actions] == [
        "send_private_msg",
        "upload_private_file",
        "upload_private_file",
    ]
    assert context.send_results == []
    assert task.held_receipts == []
    for callbacks_for_file, sent in zip(
        callbacks, (message_sent, first_sent, second_sent), strict=True
    ):
        expected = 0 if sent is True else 1 if sent is False else 2
        assert [callback.await_count for callback in callbacks_for_file] == [
            int(index == expected) for index in range(3)
        ]
    assert task.progress_sent == 1
    assert task.last_progress_at >= before
    assert task.delivery == initial_delivery
    limited = await execute_tool(tools, env, "send_message", {"text": "下一条"})
    assert "太近" in limited.content
    assert len(context.actions) == 3
    assert task.progress_sent == 1
    outcomes = (message_sent, first_sent, second_sent)
    if False in outcomes:
        expected_feedback = "发送失败"
    elif None in outcomes:
        expected_feedback = "已交给发送通道，是否送达未确认。"
    else:
        expected_feedback = "已发送"
    assert result.content == expected_feedback

    # Attachments bypass the interval; the total progress limit still applies.
    again = await execute_tool(tools, env, "send_message", {"text": "附件", "files": files})
    assert again.content == "已发送"
    assert len(context.actions) == 6
    assert task.progress_sent == 2
    task.progress_sent = 8
    capped = await execute_tool(tools, env, "send_message", {"text": "附件", "files": files})
    assert "已达上限" in capped.content
    assert len(context.actions) == 6
    assert task.progress_sent == 8
