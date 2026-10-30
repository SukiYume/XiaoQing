# 验证并发记忆更新保留各会话新增内容。
import asyncio
import threading
from unittest.mock import patch

import pytest

from plugins.xiaoqing_chat.memory.memory import MemoryStore
from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState


@pytest.mark.parametrize(
    ("chats", "expected_loads"), [(["g1"], 1), (["g1", "g2"], 2), (["g1", "g1"], 1)]
)
@pytest.mark.asyncio
async def test_cold_memory_load_threading_and_singleflight(tmp_path, chats, expected_loads):
    store   = MemoryStore(tmp_path)
    loop    = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    lock    = threading.Lock()
    calls   = 0

    def slow_load(_chat_id):
        nonlocal calls
        with lock:
            calls += 1
            if calls == expected_loads:
                loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return []

    with patch.object(store, "_load", side_effect=slow_load):
        tasks = [asyncio.create_task(store.get_async(chat)) for chat in chats]
        try:
            await asyncio.wait_for(started.wait(), 2)
            assert all(not task.done() for task in tasks)
        finally:
            release.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks), 5)
    assert calls == expected_loads
    assert results == [[] for _ in chats]


@pytest.mark.asyncio
async def test_evicted_borrowed_lock_and_waiter_keep_identity(monkeypatch):
    monkeypatch.setattr(ChatRuntimeState, "_MAX_TRACKED_CHATS", 1)
    state = ChatRuntimeState()
    old   = state.get_lock("old")
    state.set_last_observe_ts("new", 100)
    state.cleanup_stale_chats()
    assert state.get_lock("old") is old
    await old.acquire()
    waiter = asyncio.create_task(old.acquire())
    await asyncio.sleep(0)
    old.release()
    state.cleanup_stale_chats()
    assert state.get_lock("old") is old
    await waiter
    old.release()
