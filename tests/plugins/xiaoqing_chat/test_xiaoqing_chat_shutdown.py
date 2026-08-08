from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.xiaoqing_chat import main as xiaoqing_chat


def test_shutdown_store_flushers_persist_all_dirty_conversations(tmp_path) -> None:
    from plugins.xiaoqing_chat.memory.memory import MemoryStore
    from plugins.xiaoqing_chat.planning.pfc_state import PFCConversationState, PFCStateStore

    memory_dir = tmp_path / "memory"
    memory_store = MemoryStore(memory_dir)
    memory_store.append("g1", role="user", name="one", content="first")
    memory_store.append("g2", role="user", name="two", content="second")

    pfc_store = PFCStateStore()
    pfc_store.bind(tmp_path)
    pfc_store.set_state(
        "g1",
        PFCConversationState(chat_id="g1", goal_list=[{"goal": "persist on shutdown"}]),
    )

    memory_store.persist_all()
    pfc_store.flush()

    assert [message.content for message in MemoryStore(memory_dir).get("g1")] == ["first"]
    assert [message.content for message in MemoryStore(memory_dir).get("g2")] == ["second"]
    reloaded_pfc = PFCStateStore()
    reloaded_pfc.bind(tmp_path)
    assert reloaded_pfc.get("g1").goal_list == [{"goal": "persist on shutdown"}]


@pytest.mark.asyncio
async def test_shutdown_flushes_memory_and_pfc_before_cancelling_pending_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    class _PendingTask:
        def cancel(self) -> None:
            order.append("cancel")

    pending = _PendingTask()
    state = SimpleNamespace(
        stop_accepting_background_tasks=Mock(side_effect=lambda: order.append("stop")),
        background_tasks=Mock(return_value={pending}),
        memory_store=SimpleNamespace(persist_all=Mock(side_effect=lambda: order.append("memory"))),
        pfc_state_store=SimpleNamespace(flush=Mock(side_effect=lambda: order.append("pfc"))),
        action_history=SimpleNamespace(flush=Mock(side_effect=lambda: order.append("actions"))),
        media_store=SimpleNamespace(flush=Mock(side_effect=lambda: order.append("media"))),
        memory_db=SimpleNamespace(is_dirty=Mock(return_value=False)),
    )
    monkeypatch.setattr(xiaoqing_chat, "_state", Mock(return_value=state))
    monkeypatch.setattr(
        xiaoqing_chat.asyncio,
        "wait",
        AsyncMock(side_effect=[(set(), {pending}), ({pending}, set())]),
    )
    context = SimpleNamespace(logger=logging.getLogger("test.xiaoqing_chat.shutdown"))

    await xiaoqing_chat.shutdown(context)

    assert order[0] == "stop"
    assert order.index("memory") < order.index("cancel")
    assert order.index("pfc") < order.index("cancel")
    assert order.count("memory") == 2
    assert order.count("pfc") == 2
