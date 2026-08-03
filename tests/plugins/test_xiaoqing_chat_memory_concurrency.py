import asyncio
import threading
import time
from unittest.mock import patch

import pytest

from plugins.xiaoqing_chat.memory.memory import MemoryStore


@pytest.mark.asyncio
async def test_cold_memory_load_keeps_event_loop_responsive(tmp_path):
    store = MemoryStore(tmp_path)
    load_started = threading.Event()

    def slow_load(_chat_id: str):
        load_started.set()
        time.sleep(0.1)
        return []

    with patch.object(store, "_load", side_effect=slow_load):
        load_task = asyncio.create_task(store.get_async("g1"))
        await asyncio.to_thread(load_started.wait, 1.0)
        await asyncio.sleep(0)

        assert not load_task.done()
        await load_task


@pytest.mark.asyncio
async def test_different_chats_cold_load_concurrently(tmp_path):
    store = MemoryStore(tmp_path)
    counter_lock = threading.Lock()
    active_loads = 0
    maximum_active_loads = 0

    def slow_load(_chat_id: str):
        nonlocal active_loads, maximum_active_loads
        with counter_lock:
            active_loads += 1
            maximum_active_loads = max(maximum_active_loads, active_loads)
        time.sleep(0.1)
        with counter_lock:
            active_loads -= 1
        return []

    with patch.object(store, "_load", side_effect=slow_load):
        await asyncio.gather(store.get_async("g1"), store.get_async("g2"))

    assert maximum_active_loads == 2


@pytest.mark.asyncio
async def test_same_chat_cold_load_remains_singleflight(tmp_path):
    store = MemoryStore(tmp_path)
    load_calls = 0

    def slow_load(_chat_id: str):
        nonlocal load_calls
        load_calls += 1
        time.sleep(0.05)
        return []

    with patch.object(store, "_load", side_effect=slow_load):
        first, second = await asyncio.gather(store.get_async("g1"), store.get_async("g1"))

    assert first == second == []
    assert load_calls == 1
