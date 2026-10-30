"""防抖刷盘只执行最后一次请求，并统一回收失败路径中的任务。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plugins.xiaoqing_chat import task_scheduler as scheduler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind, store_name, method",
    [
        ("action_history", "action_history", "flush"),
        ("pfc_state", "pfc_state_store", "save"),
        ("media_registry", "media_store", "flush"),
    ],
)
async def test_debounced_flush_replaces_pending_task(monkeypatch, kind, store_name, method):
    state   = MagicMock()
    context = MagicMock()
    runtime = SimpleNamespace(cfg=SimpleNamespace(io_persist_debounce_seconds=0.01))
    tasks = []
    monkeypatch.setattr(scheduler, "_state", lambda: state)
    monkeypatch.setattr(
        scheduler, "_track_bg_task", lambda _context, task, **kwargs: tasks.append(task)
    )
    schedule = getattr(scheduler, f"schedule_{kind}_flush")
    kwargs   = {} if kind == "media_registry" else {"chat_id": "g1"}
    try:
        schedule(context, runtime, **kwargs)
        schedule(context, runtime, **kwargs)
        assert len(tasks) == 2
        assert tasks[0] is not tasks[1]
        assert tasks[0].cancelling() > 0
        await asyncio.wait_for(tasks[1], timeout=5)
        flush = getattr(getattr(state, store_name), method)
        flush.assert_called_once_with(*(() if kind == "media_registry" else ("g1",)))
        if kind == "media_registry":
            assert scheduler._media_registry_flush_task is None
        else:
            registry = getattr(
                scheduler,
                "_action_flush_tasks" if kind == "action_history" else "_pfc_state_flush_tasks",
            )
            assert "g1" not in registry
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
