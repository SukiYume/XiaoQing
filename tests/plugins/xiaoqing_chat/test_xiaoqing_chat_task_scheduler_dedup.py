"""防抖刷盘只执行最后一次请求，并统一回收失败路径中的任务。"""

import asyncio
from types import SimpleNamespace

import pytest

from plugins.xiaoqing_chat import task_scheduler as scheduler
from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore, ActionRecord
from plugins.xiaoqing_chat.planning.pfc_state import PFCConversationState, PFCStateStore
from plugins.xiaoqing_chat.store_binding import bound_state


def _dirty_action_history(state) -> None:
    state.action_history.append(
        "g1",
        ActionRecord(
            ts           = 1.0,
            local_target = "t",
            action       = "reply",
            reasoning    = "ok",
            detail       = {},
            executed     = True,
        ),
    )


async def _action_history_on_disk(data_dir) -> list[str]:
    reloaded = ActionHistoryStore()
    reloaded.bind(data_dir)
    return [record.action for record in await reloaded.get_recent_async("g1")]


def _dirty_pfc_state(state) -> None:
    state.pfc_state_store.set_state(
        "g1", PFCConversationState(chat_id="g1", goal_list=[{"goal": "persist"}])
    )


async def _pfc_state_on_disk(data_dir) -> list[dict]:
    reloaded = PFCStateStore()
    reloaded.bind(data_dir)
    return reloaded.get("g1").goal_list


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind, dirty, on_disk, persisted",
    [
        ("action_history", _dirty_action_history, _action_history_on_disk, ["reply"]),
        ("pfc_state", _dirty_pfc_state, _pfc_state_on_disk, [{"goal": "persist"}]),
        ("media_registry", lambda state: None, None, None),
    ],
    ids=["action-history", "pfc-state", "media-registry"],
)
async def test_debounced_flush_replaces_pending_task(tmp_path, kind, dirty, on_disk, persisted):
    context = SimpleNamespace(data_dir=tmp_path)
    state = bound_state(context)
    runtime = SimpleNamespace(cfg=SimpleNamespace(io_persist_debounce_seconds=0.01))
    dirty(state)
    schedule = getattr(scheduler, f"schedule_{kind}_flush")
    kwargs   = {} if kind == "media_registry" else {"chat_id": "g1"}
    tasks    = []

    def schedule_once():
        schedule(context, runtime, **kwargs)
        [task] = state.background_tasks() - set(tasks)
        tasks.append(task)

    try:
        schedule_once()
        schedule_once()
        first, second = tasks
        assert first.cancelling() > 0
        await asyncio.wait_for(second, timeout=5)
        await asyncio.gather(first, return_exceptions=True)
        assert first.cancelled() and not second.cancelled()
        if kind == "media_registry":
            assert scheduler._media_registry_flush_tasks == {}
        else:
            registry = getattr(
                scheduler,
                "_action_flush_tasks" if kind == "action_history" else "_pfc_state_flush_tasks",
            )
            assert "g1" not in registry
            assert await on_disk(tmp_path) == persisted
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
