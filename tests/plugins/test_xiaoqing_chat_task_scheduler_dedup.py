import asyncio
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from plugins.xiaoqing_chat.task_scheduler import (
    _schedule_action_history_flush,
    _schedule_pfc_state_flush,
    _action_flush_tasks,
    _pfc_state_flush_tasks,
)


@pytest.fixture
def mock_context():
    context = MagicMock()
    context.logger = MagicMock()
    return context


@pytest.mark.asyncio
async def test_schedule_action_history_flush_behavior(mock_context):
    runtime = MagicMock()
    runtime.cfg.io_persist_debounce_seconds = 0.01
    state = MagicMock()

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    with (
        patch("plugins.xiaoqing_chat.task_scheduler._state", return_value=state),
        patch("plugins.xiaoqing_chat.task_scheduler._track_bg_task"),
        patch(
            "plugins.xiaoqing_chat.task_scheduler.asyncio.to_thread",
            new=AsyncMock(side_effect=fake_to_thread),
        ) as mock_to_thread,
    ):
        # First call
        _schedule_action_history_flush(mock_context, runtime, chat_id="g1")
        task1 = _action_flush_tasks.get("g1")
        assert task1 is not None

        # Second call should cancel first
        _schedule_action_history_flush(mock_context, runtime, chat_id="g1")
        task2 = _action_flush_tasks.get("g1")
        assert task2 is not None
        assert task2 is not task1
        # In asyncio, task.cancel() marks it as cancelling, but cancelled() might be False until it yields
        assert task1.cancelling() > 0

        await task2

        assert mock_to_thread.await_count == 1
        state.action_history.flush.assert_called_once_with("g1")

        # Verify registry cleanup
        assert "g1" not in _action_flush_tasks


@pytest.mark.asyncio
async def test_schedule_pfc_state_flush_behavior(mock_context):
    runtime = MagicMock()
    runtime.cfg.io_persist_debounce_seconds = 0.01
    state = MagicMock()

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    with (
        patch("plugins.xiaoqing_chat.task_scheduler._state", return_value=state),
        patch("plugins.xiaoqing_chat.task_scheduler._track_bg_task"),
        patch(
            "plugins.xiaoqing_chat.task_scheduler.asyncio.to_thread",
            new=AsyncMock(side_effect=fake_to_thread),
        ) as mock_to_thread,
    ):
        # First call
        _schedule_pfc_state_flush(mock_context, runtime, chat_id="g1")
        task1 = _pfc_state_flush_tasks.get("g1")
        assert task1 is not None

        # Second call should cancel first
        _schedule_pfc_state_flush(mock_context, runtime, chat_id="g1")
        task2 = _pfc_state_flush_tasks.get("g1")
        assert task2 is not None
        assert task2 is not task1
        # In asyncio, task.cancel() marks it as cancelling, but cancelled() might be False until it yields
        assert task1.cancelling() > 0

        await task2

        assert mock_to_thread.await_count == 1
        state.pfc_state_store.save.assert_called_once_with("g1")

        # Verify registry cleanup
        assert "g1" not in _pfc_state_flush_tasks
