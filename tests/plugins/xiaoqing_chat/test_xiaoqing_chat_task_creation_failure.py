from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugins.xiaoqing_chat import task_scheduler
from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState


async def _never_started() -> None:
    return None


def _reject_task_creation(captured):
    def reject(coro):
        captured.append(coro)
        raise RuntimeError("event loop is closing")

    return reject


def test_spawn_background_task_closes_coroutine_when_create_task_fails():
    state = ChatRuntimeState()
    context = SimpleNamespace(logger=MagicMock())
    captured = []
    coro = _never_started()
    try:
        with (
            patch("plugins.xiaoqing_chat.task_scheduler._state", return_value=state),
            patch(
                "plugins.xiaoqing_chat.task_scheduler.asyncio.create_task",
                side_effect=_reject_task_creation(captured),
            ),
        ):
            task_scheduler._spawn_bg_task(context, coro, name="facts:g1")

        assert captured == [coro]
        assert coro.cr_frame is None
        assert state.background_tasks() == set()
    finally:
        coro.close()


@pytest.mark.parametrize(
    "schedule",
    [
        lambda context, runtime, state: task_scheduler._schedule_memory_persist(
            context, runtime, chat_id="g1"
        ),
        lambda context, runtime, state: task_scheduler._schedule_memory_db_save(context, runtime),
        lambda context, runtime, state: task_scheduler._schedule_action_history_flush(
            context, runtime, chat_id="g1"
        ),
        lambda context, runtime, state: task_scheduler._schedule_media_registry_flush(
            context, runtime
        ),
    ],
    ids=[
        "memory-persist",
        "memory-db-save",
        "action-history-flush",
        "media-registry-flush",
    ],
)
def test_persistence_schedulers_close_coroutine_when_create_task_fails(schedule):
    state = MagicMock()
    state.get_persist_task.return_value = None
    state.get_vdb_save_task.return_value = None
    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            io_persist_debounce_seconds=0.0,
            memory_db_save_debounce_seconds=0.0,
        )
    )
    context = SimpleNamespace(logger=MagicMock())
    captured = []
    task_scheduler._action_flush_tasks.clear()
    task_scheduler._media_registry_flush_task = None
    try:
        with (
            patch("plugins.xiaoqing_chat.task_scheduler._state", return_value=state),
            patch(
                "plugins.xiaoqing_chat.task_scheduler.asyncio.create_task",
                side_effect=_reject_task_creation(captured),
            ),
        ):
            try:
                schedule(context, runtime, state)
            except RuntimeError as exc:
                pytest.fail(f"scheduler leaked task-creation failure: {exc}")

        assert len(captured) == 1
        assert captured[0].cr_frame is None
        state.set_persist_task.assert_not_called()
        state.set_vdb_save_task.assert_not_called()
        assert task_scheduler._action_flush_tasks == {}
        assert task_scheduler._media_registry_flush_task is None
    finally:
        for coro in captured:
            coro.close()
        task_scheduler._action_flush_tasks.clear()
        task_scheduler._media_registry_flush_task = None
