# 验证后台任务登记、异常观察和关闭回收。
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState
from plugins.xiaoqing_chat.task_scheduler import _spawn_bg_task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_name",
    [
        "summarizer:g1",
        "expression_learn:g1",
        "reflection:g1",
        "review_push:g1",
        "facts:g1",
    ],
)
async def test_expensive_per_chat_background_jobs_are_singleflight(task_name):
    state = ChatRuntimeState()
    context = SimpleNamespace(logger=MagicMock())
    gate    = asyncio.Event()
    started = 0

    async def blocked_job() -> None:
        nonlocal started
        started += 1
        await gate.wait()

    try:
        with patch("plugins.xiaoqing_chat.task_scheduler._state", return_value=state):
            _spawn_bg_task(context, blocked_job(), name=task_name)
            _spawn_bg_task(context, blocked_job(), name=task_name)
            await asyncio.sleep(0)

            assert started == 1
            assert len(state.background_tasks()) == 1
    finally:
        gate.set()
        tasks = state.background_tasks()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_background_task_registry_rejects_work_above_global_capacity(monkeypatch):
    state = ChatRuntimeState()
    monkeypatch.setattr(ChatRuntimeState, "_MAX_BACKGROUND_TASKS", 2, raising=False)
    context = SimpleNamespace(logger=MagicMock())
    gate    = asyncio.Event()
    started = 0

    async def blocked_job() -> None:
        nonlocal started
        started += 1
        await gate.wait()

    try:
        with patch("plugins.xiaoqing_chat.task_scheduler._state", return_value=state):
            for index in range(3):
                _spawn_bg_task(context, blocked_job(), name=f"facts:g{index}")
            await asyncio.sleep(0)

            assert started == 2
            assert len(state.background_tasks()) == 2
    finally:
        gate.set()
        tasks = state.background_tasks()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
