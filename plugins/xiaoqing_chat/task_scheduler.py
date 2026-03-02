from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime_state import _ChatRuntime
    from core.plugin_base import Context

from .runtime_state import get_state as _state


def _track_bg_task(context: Context, task: asyncio.Task, *, name: str) -> None:
    _state().add_bg_task(task)

    def _done(t: asyncio.Task) -> None:
        _state().remove_bg_task(t)
        try:
            t.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            try:
                context.logger.exception("xiaoqing_chat bg_task=%s failed: %s", name, exc)
            except Exception as log_exc:
                context.logger.error("xiaoqing_chat failed to log bg_task error: %s", log_exc)

    task.add_done_callback(_done)


def _spawn_bg_task(context: Context, coro, *, name: str) -> None:
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        return
    _track_bg_task(context, task, name=name)


def _schedule_memory_persist(context: Context, runtime: _ChatRuntime, *, chat_id: str) -> None:
    delay = max(0.0, float(getattr(runtime.cfg, "io_persist_debounce_seconds", 0.8) or 0.0))
    old = _state().get_persist_task(chat_id)
    if old is not None and not old.done():
        try:
            old.cancel()
        except Exception as exc:
            context.logger.debug("xiaoqing_chat failed to cancel persist task: %s", exc)

    async def _run() -> None:
        if delay:
            await asyncio.sleep(delay)
        await asyncio.to_thread(_state().memory_store.persist, chat_id)

    task = asyncio.create_task(_run())
    _state().set_persist_task(chat_id, task)
    _track_bg_task(context, task, name=f"persist:{chat_id}")


def _schedule_memory_db_save(context: Context, runtime: _ChatRuntime) -> None:
    delay = max(0.0, float(getattr(runtime.cfg, "memory_db_save_debounce_seconds", 20.0) or 0.0))
    old = _state().get_vdb_save_task()
    if old is not None and not old.done():
        try:
            old.cancel()
        except Exception as exc:
            context.logger.debug("xiaoqing_chat failed to cancel vdb save task: %s", exc)

    async def _run() -> None:
        if delay:
            await asyncio.sleep(delay)
        if not _state().memory_db.is_dirty():
            return
        await asyncio.to_thread(_state().memory_db.save)

    task = asyncio.create_task(_run())
    _state().set_vdb_save_task(task)
    _track_bg_task(context, task, name="memory_db_save")


def _schedule_action_history_flush(context: Context, runtime: _ChatRuntime, *, chat_id: str) -> None:
    """Debounced flush for ActionHistoryStore to avoid writing on every append."""
    delay = max(0.0, float(getattr(runtime.cfg, "io_persist_debounce_seconds", 0.8) or 0.0))

    async def _run() -> None:
        if delay:
            await asyncio.sleep(delay)
        _state().action_history.flush(chat_id)

    _spawn_bg_task(context, _run(), name=f"action_flush:{chat_id}")
