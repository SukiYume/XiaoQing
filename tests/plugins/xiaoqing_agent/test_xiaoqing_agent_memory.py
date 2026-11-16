"""agent 和统一记忆：开会话时取一次、存进会话、系统提示里原样带着；办完写一条办事经过；记忆不在时照常。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.xiaoqing_agent import memory as agent_memory
from plugins.xiaoqing_agent.prompts import build_system_message
from plugins.xiaoqing_agent.sessions import Session, SessionStore
from plugins.xiaoqing_agent.tasks import AgentTask, ChatRef, Outcome
from tests.helpers.xiaoqing_agent_test_support import ADMIN_ID, GROUP_ID

CHAT = ChatRef(ADMIN_ID, GROUP_ID)


def _task(text: str = "帮我记一笔奶茶 18") -> AgentTask:
    task = AgentTask(task_id="t1", chat=CHAT, request_text=text, source="chat", sender="阿柚")
    task.status      = "done"
    task.finished_at = 100.0
    return task


class FakeMemory:
    def __init__(self, result: Any = None, *, fail: bool = False) -> None:
        self.result                        = result
        self.fail                          = fail
        self.recalls: list[dict[str, Any]] = []
        self.records: list[dict[str, Any]] = []

    async def recall(self, payload: dict[str, Any]) -> Any:
        self.recalls.append(payload)
        if self.fail:
            raise RuntimeError("down")
        return self.result

    async def record(self, payload: dict[str, Any]) -> Any:
        self.records.append(payload)
        if self.fail:
            raise RuntimeError("down")
        return {"version": 1}


def _context(memory: Any) -> Any:
    return SimpleNamespace(capabilities=SimpleNamespace(memory=memory))


@pytest.mark.asyncio
async def test_recall_for_session_combines_speaker_page_and_promises() -> None:
    memory = FakeMemory(
        {
            "blocks": {
                "speaker" : "你记得的关于阿柚的事：\n- [办事] 记账默认用微信",
                "promises": "- 答应阿柚：查快递",
            }
        }
    )
    text = await agent_memory.recall_for_session(_context(memory), _task())
    assert "记账默认用微信" in text and "你答应过他、还没办完的事" in text and "查快递" in text
    sent = memory.recalls[0]
    assert sent["purpose"] == "task" and sent["scope"] == CHAT.key
    assert sent["speaker"]["user_id"] == ADMIN_ID
    assert await agent_memory.recall_for_session(_context(FakeMemory(fail=True)), _task()) == ""
    assert (
        await agent_memory.recall_for_session(
            SimpleNamespace(capabilities=SimpleNamespace()), _task()
        )
        == ""
    )


def test_task_event_lists_commands_results_reply_and_outcome() -> None:
    call = {
        "id"      : "c1",
        "type"    : "function",
        "function": {
            "name"     : "xiaoqing_command",
            "arguments": json.dumps({"commands": ["/记账 奶茶 18 餐饮 微信"]}, ensure_ascii=False),
        },
    }
    messages = [
        {"role": "user", "content": "帮我记一笔奶茶 18"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "c1", "content": "已记账：奶茶 18 元（微信）\n明细……"},
        {"role": "assistant", "content": "记好啦，奶茶 18，按微信记的"},
    ]
    event = agent_memory.task_event(
        _task(), ["用微信"], messages, Outcome("记好啦，奶茶 18，按微信记的")
    )
    assert event["kind"] == "task" and event["origin"] == "tool" and event["key"] == "task:t1"
    assert event["actor"] == {"role": "user", "user_id": ADMIN_ID, "name": "阿柚"}
    text = event["text"]
    assert "原话：帮我记一笔奶茶 18" in text and "补充：用微信" in text
    assert "/记账 奶茶 18 餐饮 微信 → 已记账：奶茶 18 元（微信）" in text
    assert "明细" not in text, "工具输出只留第一行"
    assert "结局：办成了" in text and event["refs"]["task_id"] == "t1"


@pytest.mark.asyncio
async def test_record_task_is_silent_when_memory_is_missing_or_down() -> None:
    await agent_memory.record_task(
        SimpleNamespace(capabilities=SimpleNamespace()), _task(), [], [], None
    )
    down = FakeMemory(fail=True)
    await agent_memory.record_task(_context(down), _task(), [], [], None)
    assert len(down.records) == 1


@pytest.mark.asyncio
async def test_session_keeps_its_memory_and_the_system_prompt_carries_it(tmp_path) -> None:
    store = SessionStore(tmp_path)
    base  = Session(
        {"role": "system", "content": "系统"}, memory="你记得的关于阿柚的事：\n- 记账默认用微信"
    )
    await store.append(CHAT, base, {"task_id": "t1", "messages": []}, max_turns=5)
    loaded = await store.load(CHAT, idle_seconds=600)
    assert loaded is not None and loaded.memory == base.memory

    task                   = _task()
    kwargs: dict[str, Any] = {
        "bot_name"  : "小青",
        "sender"    : "阿柚",
        "task"      : task,
        "timezone"  : "Asia/Shanghai",
        "scene"     : "私聊",
        "python"    : "python",
        "shell"     : "bash",
        "workspace" : "/w",
        "tool_names": ("xiaoqing_command",),
    }
    with_memory = build_system_message(**kwargs, memory=loaded.memory)["content"]
    assert "记账默认用微信" in with_memory
    assert build_system_message(**kwargs, memory=loaded.memory) == build_system_message(
        **kwargs, memory=loaded.memory
    ), "同一会话里系统提示逐字不变"
    assert "（暂时没有）" in build_system_message(**kwargs)["content"]
