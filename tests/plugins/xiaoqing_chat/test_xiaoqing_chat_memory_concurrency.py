# 验证并发记忆更新保留各会话新增内容。
import asyncio
import threading
from unittest.mock import patch

import pytest

from core.plugin_base import load_json as real_load_json
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
    for chat in set(chats):
        (tmp_path / f"{chat}.json").write_text("[]", encoding="utf-8")

    def slow_read(path, *args, **kwargs):
        nonlocal calls
        with lock:
            calls += 1
            if calls == expected_loads:
                loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return real_load_json(path, *args, **kwargs)

    # 屏障放在磁盘读取这条边界上：冷加载在读盘时被卡住，其他并发的加载请求必须等它。
    with patch("plugins.xiaoqing_chat.memory.memory.load_json", new=slow_read):
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
async def test_evicted_borrowed_lock_and_waiter_keep_identity():
    state = ChatRuntimeState()
    old   = state.get_lock("old")
    for index in range(ChatRuntimeState._MAX_TRACKED_CHATS):
        state.set_last_observe_ts(f"new-{index}", 100)
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


@pytest.mark.asyncio
async def test_memory_store_get_async_loads_via_to_thread(tmp_path):
    chat_id = "threaded-load"
    store   = MemoryStore(tmp_path)
    store.persist(chat_id)
    (tmp_path / f"{chat_id}.json").write_text(
        '[{"role":"user","name":"Tester","content":"hello","ts":1.0}]',
        encoding="utf-8",
    )
    read_threads: list[int] = []

    def recording_read(path, *args, **kwargs):
        read_threads.append(threading.get_ident())
        return real_load_json(path, *args, **kwargs)

    # 记下读盘发生在哪个线程：冷加载必须在工作线程里读，不能卡住事件循环所在的线程。
    with patch("plugins.xiaoqing_chat.memory.memory.load_json", new=recording_read):
        history = await store.get_async(chat_id)

    assert len(read_threads) == 1
    assert read_threads[0] != threading.get_ident()
    assert len(history) == 1
    assert history[0].content == "hello"
