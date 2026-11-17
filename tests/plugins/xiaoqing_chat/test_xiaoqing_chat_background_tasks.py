# 验证后台任务登记、异常观察和关闭回收。
import asyncio
from types import SimpleNamespace

import pytest

from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState, get_state
from plugins.xiaoqing_chat.task_scheduler import spawn_bg_task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_name",
    [
        "daily_life:today",
        "expression_learn:g1",
        "reflection:g1",
        "review_push:g1",
    ],
)
async def test_expensive_per_chat_background_jobs_are_singleflight(task_name):
    state   = get_state()
    context = SimpleNamespace()
    gate    = asyncio.Event()
    started = 0

    async def blocked_job() -> None:
        nonlocal started
        started += 1
        await gate.wait()

    try:
        spawn_bg_task(context, blocked_job(), name=task_name)
        spawn_bg_task(context, blocked_job(), name=task_name)
        await asyncio.sleep(0)

        assert started == 1
        assert len(state.background_tasks()) == 1
    finally:
        gate.set()
        tasks = state.background_tasks()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_background_task_registry_rejects_work_above_global_capacity():
    state    = get_state()
    capacity = ChatRuntimeState._MAX_BACKGROUND_TASKS
    context  = SimpleNamespace()
    gate     = asyncio.Event()
    started  = 0

    async def blocked_job() -> None:
        nonlocal started
        started += 1
        await gate.wait()

    try:
        for index in range(capacity + 1):
            spawn_bg_task(context, blocked_job(), name=f"facts:g{index}")
        await asyncio.sleep(0)

        assert started == capacity
        assert len(state.background_tasks()) == capacity
    finally:
        gate.set()
        tasks = state.background_tasks()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
