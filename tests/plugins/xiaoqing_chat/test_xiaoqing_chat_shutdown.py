# 验证聊天关闭等待后台工作终结并完成最终持久化。
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from plugins.xiaoqing_chat import main
from plugins.xiaoqing_chat.memory.memory import MemoryStore
from plugins.xiaoqing_chat.planning.pfc_state import PFCConversationState, PFCStateStore
from plugins.xiaoqing_chat.store_binding import bound_state
from plugins.xiaoqing_chat.task_scheduler import spawn_bg_task


def test_shutdown_store_flushers_persist_all_dirty_conversations(tmp_path) -> None:
    memory_dir   = tmp_path / "memory"
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


def _shutdown_context(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        logger=logging.getLogger("test.xiaoqing_chat.shutdown"), data_dir=tmp_path
    )


@pytest.mark.asyncio
async def test_shutdown_flushes_memory_and_pfc_before_cancelling_pending_tasks(tmp_path) -> None:
    context = _shutdown_context(tmp_path)
    state   = bound_state(context)
    state.memory_store.append("g1", role="user", name="user", content="before shutdown")
    state.pfc_state_store.set_state(
        "g1", PFCConversationState(chat_id="g1", goal_list=[{"goal": "persist on shutdown"}])
    )
    seen_on_cancel: dict[str, object] = {}

    async def pending_work():
        try:
            await asyncio.sleep(30)
        finally:
            # 被取消的这一刻，磁盘上已经有关闭前的聊天记录和规划状态；后台任务也已不再接收新任务。
            seen_on_cancel["memory"] = [m.content for m in MemoryStore(tmp_path).get("g1")]
            reloaded                 = PFCStateStore()
            reloaded.bind(tmp_path)
            seen_on_cancel["pfc"]       = reloaded.get("g1").goal_list
            seen_on_cancel["accepting"] = state.can_add_bg_task()

    spawn_bg_task(context, pending_work(), name="pending")
    [task] = state.background_tasks()
    await asyncio.sleep(0)

    await main.shutdown(context)

    assert task.cancelled()
    assert seen_on_cancel == {
        "memory"   : ["before shutdown"],
        "pfc"      : [{"goal": "persist on shutdown"}],
        "accepting": False,
    }


@pytest.mark.asyncio
async def test_shutdown_reports_live_cleanup(tmp_path):
    context = _shutdown_context(tmp_path)
    state   = bound_state(context)
    stubborn = SimpleNamespace(cancels=0, finish=False)

    async def ignores_cancellation():
        while True:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                stubborn.cancels += 1
                if stubborn.finish:
                    raise

    spawn_bg_task(context, ignores_cancellation(), name="stubborn")
    [task] = state.background_tasks()
    await asyncio.sleep(0)
    try:
        with pytest.raises(TimeoutError, match="still running"):
            await main.shutdown(context)
        assert stubborn.cancels == 1
    finally:
        stubborn.finish = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_cancels_sleeping_work_and_flushes_within_shared_budget(tmp_path):
    context = _shutdown_context(tmp_path)
    memory  = bound_state(context).memory_store
    memory.append("g1", role="user", name="user", content="before shutdown")
    started = asyncio.Event()

    async def delayed_work():
        try:
            started.set()
            await asyncio.sleep(30)
        finally:
            memory.append("g1", role="assistant", name="bot", content="cleanup complete")

    spawn_bg_task(context, delayed_work(), name="delayed")
    [task] = bound_state(context).background_tasks()
    await started.wait()
    try:
        await asyncio.wait_for(main.shutdown(context), timeout=2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert [message.content for message in MemoryStore(tmp_path).get("g1")] == [
        "before shutdown",
        "cleanup complete",
    ]
