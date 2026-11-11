"""办事会话：一轮结束时补齐消息结构，按（会话, 发起人）追加，超过轮数和字数时丢最早的轮。"""

from __future__ import annotations

import json
import time

import pytest

from plugins.xiaoqing_agent.sessions import Session, SessionStore, settle_turn
from plugins.xiaoqing_agent.tasks import ChatRef
from tests.helpers.xiaoqing_agent_test_support import ADMIN_ID, GROUP_ID, MEMBER_ID

CHAT = ChatRef(ADMIN_ID, GROUP_ID)


def _call(call_id: str, name: str = "xiaoqing_command") -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


def _turn(task_id: str, text: str) -> dict:
    return {
        "task_id" : task_id,
        "messages": [
            {"role": "user", "content": text},
            {"role": "assistant", "content": f"回复：{text}"},
        ],
    }


def test_settle_turn_closes_unfinished_calls_and_records_what_was_sent():
    messages = [
        {"role": "user", "content": "记两笔"},
        {"role": "assistant", "content": "", "tool_calls": [_call("a"), _call("b")]},
        {"role": "tool", "tool_call_id": "a", "content": "已记下甲"},
    ]
    settled = settle_turn(messages, "这件事超时了，没能做完。")
    # 停下时没有结果的调用补一条回报，接口要求每个调用都有回报。
    assert settled[3] == {"role": "tool", "tool_call_id": "b", "content": settled[3]["content"]}
    # 最后不是模型的回复：把实际发出的话补成一条模型回复。
    assert settled[-1] == {"role": "assistant", "content": "这件事超时了，没能做完。"}


@pytest.mark.parametrize(
    "ending",
    [
        [{"role": "assistant", "content": "记好了"}],
        [
            {"role": "assistant", "content": "", "tool_calls": [_call("f", "final_answer")]},
            {"role": "tool", "tool_call_id": "f", "content": "已交付"},
        ],
    ],
)
def test_settle_turn_keeps_a_turn_the_model_finished(ending):
    messages = [{"role": "user", "content": "记一笔"}, *ending]
    assert settle_turn(messages, "记好了") == messages


@pytest.mark.asyncio
async def test_turns_append_per_initiator_and_trim_oldest(tmp_path):
    store = SessionStore(tmp_path)
    base  = Session({"role": "system", "content": "系统"})
    for index in range(4):
        turn            = _turn(f"t{index}", f"第{index}件")
        turn["plugins"] = ["pendo"] if index == 0 else []
        await store.append(CHAT, base, turn, max_turns=3)
        if index == 0:
            loaded = await store.load(CHAT, idle_seconds=60)
            assert loaded.plugins == ["pendo"]
    session = await store.load(CHAT, idle_seconds=60)
    assert session is not None and [turn["task_id"] for turn in session.turns] == ["t1", "t2", "t3"]
    assert session.plugins == [] and session.messages()[0]["content"] == "系统"
    assert await store.load(ChatRef(MEMBER_ID, GROUP_ID), idle_seconds=60) is None
    assert await store.load(CHAT, idle_seconds=0) is None


@pytest.mark.asyncio
async def test_tasks_finishing_together_both_keep_their_turns(tmp_path):
    store = SessionStore(tmp_path)
    base  = Session({"role": "system", "content": "系统"})
    await store.append(CHAT, base, _turn("t0", "先"), max_turns=8)
    loaded = await store.load(CHAT, idle_seconds=60)
    assert loaded is not None
    # 两个任务从同一份会话开始；先结束的追加后，后结束的接在它后面，不互相覆盖。
    await store.append(CHAT, loaded, _turn("a", "甲"), max_turns=8)
    await store.append(CHAT, loaded, _turn("b", "乙"), max_turns=8)
    session = await store.load(CHAT, idle_seconds=60)
    assert session is not None and [turn["task_id"] for turn in session.turns] == ["t0", "a", "b"]


@pytest.mark.asyncio
async def test_a_new_session_replaces_an_expired_one_and_size_is_bounded(tmp_path):
    store = SessionStore(tmp_path)
    old   = Session({"role": "system", "content": "旧"})
    await store.append(CHAT, old, _turn("old", "旧事"), max_turns=8)
    path = store.root / f"{CHAT.key}-{CHAT.user_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["updated_at"] = time.time() - 3600
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert await store.load(CHAT, idle_seconds=60) is None

    fresh = Session({"role": "system", "content": "新"})
    await store.append(CHAT, fresh, _turn("big", "长" * 150_000), max_turns=8)
    await store.append(CHAT, fresh, _turn("next", "长" * 150_000), max_turns=8)
    session = await store.load(CHAT, idle_seconds=60)
    # 过期的会话不会被接上；总字数超限时从最早的一轮丢，最后一轮总会留下。
    assert session is not None and session.messages()[0]["content"] == "新"
    assert [turn["task_id"] for turn in session.turns] == ["next"]
    await store.clear(CHAT)
    assert await store.load(CHAT, idle_seconds=60) is None
