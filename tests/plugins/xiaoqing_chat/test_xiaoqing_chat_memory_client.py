"""聊天侧接统一记忆：推送队列不丢不重、失败后重推、重置后换代、旧数据只导入一次、记忆不在时当空。"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.xiaoqing_chat.memory import client as memory_client
from plugins.xiaoqing_chat.memory.person_profile import clear_profiles_and_memory
from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState
from tests.plugins.xiaoqing_chat.memory_support import MemoryHarness


class FlakyMemory:
    """转发给真的记忆服务；``down`` 为真时像插件不在一样抛错。"""

    def __init__(self, inner: Any) -> None:
        self.inner                       = inner
        self.down                        = False
        self.calls: list[dict[str, Any]] = []

    async def record(self, payload: dict[str, Any]) -> Any:
        self.calls.append(payload)
        if self.down:
            raise RuntimeError("memory plugin unavailable")
        return await self.inner.record(payload)

    async def forget(self, payload: dict[str, Any]) -> Any:
        if self.down:
            raise TimeoutError("memory plugin unavailable")
        return await self.inner.forget(payload)


def _setup(tmp_path, chat_id: str = "g1"):
    state = ChatRuntimeState()
    state.memory_store.bind_data_dir(tmp_path)
    harness = MemoryHarness(tmp_path)
    flaky   = FlakyMemory(harness.service(chat_id))
    context = SimpleNamespace(data_dir=tmp_path, capabilities=SimpleNamespace(memory=flaky))
    return state, harness, flaky, context


def _say(state, chat_id: str, text: str, local_id: str, *, bot: bool = False) -> None:
    state.memory_store.append(
        chat_id,
        role          = "assistant" if bot else "user",
        name          = "小青" if bot else "阿梨",
        user_id       = None if bot else 7,
        content       = text,
        local_id      = local_id,
        ts            = time.time(),
        source_plugin = "xiaoqing_chat" if bot else "",
    )


@pytest.mark.asyncio
async def test_outbox_pushes_each_message_once_and_retries_after_failure(tmp_path) -> None:
    state, harness, flaky, context = _setup(tmp_path)
    _say(state, "g1", "在吗", "m1")
    _say(state, "g1", "在的", "m2", bot=True)
    await memory_client.push_pending(context, state, "g1", bot_name="小青")
    assert [event.text for event in harness.store.events.events("g1")] == ["在吗", "在的"]
    assert harness.store.events.events("g1")[1].origin == "self"

    assert await memory_client.push_pending(context, state, "g1", bot_name="小青") is None
    flaky.down = True
    _say(state, "g1", "今天好冷", "m3")
    assert await memory_client.push_pending(context, state, "g1", bot_name="小青") is None
    flaky.down = False
    _say(state, "g1", "多穿点", "m4", bot=True)
    await memory_client.push_pending(context, state, "g1", bot_name="小青")
    texts = [event.text for event in harness.store.events.events("g1")]
    assert texts == ["在吗", "在的", "今天好冷", "多穿点"], "失败的那条补推上了，没有重复"
    assert len(flaky.calls[-1]["events"]) == 2


@pytest.mark.asyncio
async def test_reset_starts_a_new_generation_and_forgets_the_scope(tmp_path) -> None:
    state, harness, _flaky, context = _setup(tmp_path, "u7")
    context.capabilities.memory = harness.service("u7")
    _say(state, "u7", "我叫阿梨", "m1")
    await memory_client.push_pending(context, state, "u7", bot_name="小青")
    assert len(harness.store.events.events("u7")) == 1
    clear_profiles_and_memory(tmp_path, "u7", None)
    state.memory_store.clear("u7")
    assert await memory_client.forget_chat(context, "u7")
    assert harness.store.events.events("u7") == []
    _say(state, "u7", "我叫阿梨", "m1")
    await memory_client.push_pending(context, state, "u7", bot_name="小青")
    assert [event.text for event in harness.store.events.events("u7")] == ["我叫阿梨"], (
        "重置后同一个本地编号换了代际，照常写入"
    )


@pytest.mark.asyncio
async def test_failed_forget_is_reported_kept_and_retried_before_anything_new_is_pushed(
    tmp_path,
) -> None:
    """重置时记忆插件不通：如实返回没清成并记下待清；清掉之前不推新消息，恢复后先清再推。"""

    state, harness, flaky, context = _setup(tmp_path, "u7")
    _say(state, "u7", "我叫阿梨", "m1")
    await memory_client.push_pending(context, state, "u7", bot_name="小青")
    assert len(harness.store.events.events("u7")) == 1

    flaky.down = True
    clear_profiles_and_memory(tmp_path, "u7", None)
    state.memory_store.clear("u7")
    assert await memory_client.forget_chat(context, "u7") is False
    box = memory_client.outbox_for(tmp_path)
    assert box.forgetting() == ["u7"]

    flaky.down = False
    calls      = len(flaky.calls)
    _say(state, "u7", "换个话题", "m1")
    flaky.down = True
    assert await memory_client.push_pending(context, state, "u7", bot_name="小青") is None
    assert len(flaky.calls) == calls, "旧记忆清掉之前不往这个范围写"
    assert [event.text for event in harness.store.events.events("u7")] == ["我叫阿梨"]

    flaky.down = False
    assert await memory_client.push_pending(context, state, "u7", bot_name="小青") is not None
    assert box.forgetting() == []
    assert [event.text for event in harness.store.events.events("u7")] == ["换个话题"]


@pytest.mark.asyncio
async def test_sync_retries_a_pending_forget_for_a_chat_with_no_history_file(tmp_path) -> None:
    state, harness, flaky, context = _setup(tmp_path, "u7")
    _say(state, "u7", "我叫阿梨", "m1")
    await memory_client.push_pending(context, state, "u7", bot_name="小青")
    flaky.down = True
    state.memory_store.clear("u7")
    assert await memory_client.forget_chat(context, "u7") is False

    flaky.down = False
    await memory_client.sync_all(context, state, bot_name="小青")

    assert memory_client.outbox_for(tmp_path).forgetting() == []
    assert harness.store.events.events("u7") == []


@pytest.mark.asyncio
async def test_reset_while_the_memory_service_is_away_is_not_reported_as_done(tmp_path) -> None:
    """推过消息的会话，重置时记忆服务暂时不在：返回没清成并记下待清，服务回来后先清掉。"""

    state, harness, flaky, context = _setup(tmp_path, "u7")
    _say(state, "u7", "我叫阿梨", "m1")
    await memory_client.push_pending(context, state, "u7", bot_name="小青")
    assert len(harness.store.events.events("u7")) == 1

    context.capabilities = SimpleNamespace()
    state.memory_store.clear("u7")
    assert await memory_client.forget_chat(context, "u7") is False
    assert memory_client.outbox_for(tmp_path).forgetting() == ["u7"]

    context.capabilities = SimpleNamespace(memory=flaky)
    await memory_client.sync_all(context, state, bot_name="小青")
    assert memory_client.outbox_for(tmp_path).forgetting() == []
    assert harness.store.events.events("u7") == []
    # 清掉之后再重置，服务又不在：已经没有东西留在长期记忆里，算清成。
    context.capabilities = SimpleNamespace()
    assert await memory_client.forget_chat(context, "u7") is True


@pytest.mark.asyncio
async def test_nothing_is_read_from_a_scope_that_is_waiting_to_be_cleared(tmp_path) -> None:
    """清理失败、等着重试的这段时间里，取用和查找都当作没有长期记忆，旧内容不进回复。"""

    state, harness, flaky, context = _setup(tmp_path, "u7")
    _say(state, "u7", "我下周去杭州面试", "m1")
    await memory_client.push_pending(context, state, "u7", bot_name="小青")
    flaky.inner      = harness.service("u7")
    reads: list[str] = []

    async def recall(payload: dict[str, Any]) -> Any:
        reads.append("recall")
        return await harness.service("u7").recall(payload)

    async def search(payload: dict[str, Any]) -> Any:
        reads.append("search")
        return await harness.service("u7").search(payload)

    flaky.recall = recall
    flaky.search = search
    await memory_client.recall(context, purpose="reply", chat_id="u7", text="在吗")
    await memory_client.search(context, "u7", {"query": "杭州"})
    assert reads == ["recall", "search"]

    flaky.down = True
    assert await memory_client.forget_chat(context, "u7") is False
    flaky.down = False

    assert await memory_client.recall(context, purpose="reply", chat_id="u7", text="在吗") == (
        memory_client.EMPTY_RECALL
    )
    assert await memory_client.search(context, "u7", {"query": "杭州"}) == []
    assert reads == ["recall", "search"], "待清期间不向记忆服务读这个范围"


@pytest.mark.asyncio
async def test_forget_without_a_memory_service_has_nothing_left_to_clear(tmp_path) -> None:
    context = SimpleNamespace(data_dir=tmp_path, capabilities=SimpleNamespace())

    assert await memory_client.forget_chat(context, "u7") is True
    assert memory_client.outbox_for(tmp_path).forgetting() == []


def _write_legacy(root, chat_id: str) -> None:
    (root / "vdb").mkdir(parents=True)
    docs = [
        {
            "doc_id": "person:v2:x:7:a",
            "text"  : "阿梨<7>：在准备考研\n证据：我在准备考研",
            "meta"  : {
                "type"        : "person_info",
                "chat_id"     : chat_id,
                "subject_id"  : 7,
                "subject_name": "阿梨",
                "updated_at"  : 100.0,
            },
        },
        {
            "doc_id": f"topic:{chat_id}:1",
            "text"  : "话题：抽卡\n摘要：大家在讨论十连",
            "meta"  : {
                "type"      : "topic_summary",
                "chat_id"   : chat_id,
                "keywords"  : ["抽卡"],
                "updated_at": 200.0,
            },
        },
        {
            "doc_id": "person:v2:x:9:b",
            "text"  : "老王<9>：别的群的事",
            "meta"  : {
                "type"      : "person_info",
                "chat_id"   : "g999",
                "subject_id": 9,
                "updated_at": 100.0,
            },
        },
        {"doc_id": "kb:1", "text": "管理员知识", "meta": {"type": "knowledge"}},
    ]
    (root / "vdb" / "memory.docs.json").write_text(
        json.dumps(docs, ensure_ascii=False), encoding="utf-8"
    )
    (root / "person_profiles" / chat_id).mkdir(parents=True)
    (root / "person_profiles" / chat_id / "7.json").write_text(
        json.dumps(
            {
                "chat_id"     : chat_id,
                "subject_id"  : 7,
                "subject_name": "阿梨",
                "facts"       : ["在准备考研", "养了只猫"],
                "updated_at"  : 100.0,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    now = time.time()
    (root / "life" / "open_loops").mkdir(parents=True)
    (root / "life" / "self_timeline.json").write_text(
        json.dumps(
            [
                {
                    "entry_id"   : "s1",
                    "ts"         : now,
                    "chat_id"    : chat_id,
                    "kind"       : "state",
                    "text"       : "在图书馆",
                    "expires_at" : now + 3600,
                    "status"     : "active",
                    "source"     : "chat",
                    "counterpart": "",
                    "shared"     : True,
                },
                {
                    "entry_id"   : "s2",
                    "ts"         : now,
                    "chat_id"    : chat_id,
                    "kind"       : "promise",
                    "text"       : "帮阿梨查资料",
                    "expires_at" : now + 86400,
                    "status"     : "active",
                    "source"     : "chat",
                    "counterpart": "阿梨",
                    "shared"     : False,
                },
                {
                    # 旧版本从办事回复里抽的“承诺”：提醒由工具兑现，不导入。
                    "entry_id"   : "s4",
                    "ts"         : now,
                    "chat_id"    : chat_id,
                    "kind"       : "promise",
                    "text"       : "周五到点提醒对方出发",
                    "expires_at" : now + 86400,
                    "status"     : "active",
                    "source"     : "agent",
                    "counterpart": "阿梨",
                    "shared"     : False,
                },
                {
                    "entry_id"   : "s3",
                    "ts"         : now,
                    "chat_id"    : "g999",
                    "kind"       : "state",
                    "text"       : "别的群的近况",
                    "expires_at" : now + 3600,
                    "status"     : "active",
                    "source"     : "chat",
                    "counterpart": "",
                    "shared"     : True,
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "life" / "open_loops" / f"{chat_id}.json").write_text(
        json.dumps(
            [
                {
                    "loop_id"   : "l1",
                    "user_id"   : 7,
                    "user_name" : "阿梨",
                    "title"     : "数电考试",
                    "due_at"    : now - 3600,
                    "created_at": now - 86400,
                    "status"    : "open",
                    "asked_at"  : 0.0,
                    "surfaced"  : 0,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_legacy_data_is_imported_once_on_first_push(tmp_path) -> None:
    state, harness, flaky, context = _setup(tmp_path, "g1")
    _write_legacy(tmp_path, "g1")
    _say(state, "g1", "在吗", "m1")
    await memory_client.push_pending(context, state, "g1", bot_name="小青")
    notes                         = harness.store.notes_for(["g1", "global"]).values()
    by_kind: dict[str, list[str]] = {}
    for note in notes:
        by_kind.setdefault(note.kind, []).append(note.text)
    assert sorted(by_kind["person.fact"]) == ["养了只猫", "在准备考研"], "同一句事实只导入一次"
    assert by_kind["legacy.summary"] == ["话题：抽卡 摘要：大家在讨论十连"]
    # 办事回复里的“到点提醒你”不是她欠着的事，不导入。
    assert by_kind["self.state"] == ["在图书馆"] and by_kind["self.promise"] == ["帮阿梨查资料"]
    assert by_kind["person.loop"] == ["数电考试"]
    assert all("别的群" not in text for texts in by_kind.values() for text in texts)
    calls = len(flaky.calls)
    _say(state, "g1", "嗯", "m2")
    await memory_client.push_pending(context, state, "g1", bot_name="小青")
    assert len(flaky.calls) == calls + 1 and not flaky.calls[-1]["ops"], "旧数据不再重复导入"


@pytest.mark.asyncio
async def test_recall_without_memory_is_empty() -> None:
    result = await memory_client.recall(
        SimpleNamespace(capabilities=SimpleNamespace()), purpose="reply", chat_id="g1"
    )
    assert result is memory_client.EMPTY_RECALL and not result.available
