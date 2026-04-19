from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .runtime_state import _ChatRuntime

from .runtime_state import get_state as _state


def _track_bg_task(context: Any, task: asyncio.Task[None], *, name: str) -> None:
    _state().add_bg_task(task)

    def _done(t: asyncio.Task[None]) -> None:
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


def _spawn_bg_task(context: Any, coro, *, name: str) -> None:
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        return
    _track_bg_task(context, task, name=name)


def _schedule_memory_persist(context: Any, runtime: _ChatRuntime, *, chat_id: str) -> None:
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

    def _cleanup_done(t: asyncio.Task[Any]) -> None:
        current = _state().get_persist_task(chat_id)
        if current is t:
            _state().pop_persist_task(chat_id)

    task.add_done_callback(_cleanup_done)
    _track_bg_task(context, task, name=f"persist:{chat_id}")


def _cancel_memory_persist_task(chat_id: str) -> None:
    task = _state().pop_persist_task(chat_id)
    if task is not None and not task.done():
        task.cancel()


def _schedule_memory_db_save(context: Any, runtime: _ChatRuntime) -> None:
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


_action_flush_tasks: dict[str, asyncio.Task[Any]] = {}
_pfc_state_flush_tasks: dict[str, asyncio.Task[Any]] = {}


def _schedule_debounced_flush(
    context: Any,
    runtime: _ChatRuntime,
    *,
    chat_id: str,
    task_registry: dict[str, asyncio.Task[Any]],
    flush_func: Any,
    name_prefix: str,
) -> None:
    """Shared internal helper for debounced flush scheduling."""
    delay = max(0.0, float(getattr(runtime.cfg, "io_persist_debounce_seconds", 0.8) or 0.0))
    old = task_registry.get(chat_id)
    if old is not None and not old.done():
        try:
            _ = old.cancel()
        except Exception:
            pass

    async def _run() -> None:
        if delay:
            await asyncio.sleep(delay)
        await asyncio.to_thread(flush_func, chat_id)

    try:
        task = asyncio.create_task(_run())
    except RuntimeError:
        return
    task_registry[chat_id] = task

    def _cleanup_done(t: asyncio.Task[Any]) -> None:
        current = task_registry.get(chat_id)
        if current is t:
            _ = task_registry.pop(chat_id, None)

    task.add_done_callback(_cleanup_done)
    _track_bg_task(context, task, name=f"{name_prefix}:{chat_id}")


def _schedule_action_history_flush(context: Any, runtime: _ChatRuntime, *, chat_id: str) -> None:
    """Debounced flush for ActionHistoryStore to avoid writing on every append."""
    _schedule_debounced_flush(
        context,
        runtime,
        chat_id=chat_id,
        task_registry=_action_flush_tasks,
        flush_func=_state().action_history.flush,
        name_prefix="action_flush",
    )


def _schedule_pfc_state_flush(context: Any, runtime: _ChatRuntime, *, chat_id: str) -> None:
    _schedule_debounced_flush(
        context,
        runtime,
        chat_id=chat_id,
        task_registry=_pfc_state_flush_tasks,
        flush_func=_state().pfc_state_store.save,
        name_prefix="pfc_state_flush",
    )
