"""统一登记后台任务，并对各类持久化写入做防抖和收尾。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from core.public_errors import public_error_message

if TYPE_CHECKING:
    from .runtime_state import _ChatRuntime

from .runtime_state import get_state as _state

_SINGLEFLIGHT_BG_TASK_KINDS = frozenset(
    {
        "expression_learn",
        "facts",
        "media_refine",
        "reflection",
        "review_push",
        "summarizer",
    }
)


def _singleflight_key(name: str) -> str | None:
    normalized_name = str(name or "").strip()
    kind, separator, _scope = normalized_name.partition(":")
    if separator and kind in _SINGLEFLIGHT_BG_TASK_KINDS:
        return normalized_name
    return None


def _close_awaitable(awaitable: Any) -> None:
    close = getattr(awaitable, "close", None)
    if callable(close):
        close()


def _create_task_safely(coro: Any) -> asyncio.Task[Any] | None:
    """创建任务；事件循环已关闭时关闭调用方预先构造的协程。"""

    try:
        return asyncio.create_task(coro)
    except RuntimeError:
        _close_awaitable(coro)
        return None
    except BaseException:
        _close_awaitable(coro)
        raise


def _cancel_pending_task(context: Any, task: asyncio.Task[Any] | None, *, kind: str) -> None:
    """尽力取消旧防抖任务；异常只记录类型，不能阻止新任务接管。"""

    if task is None or task.done():
        return
    try:
        task.cancel()
    except Exception as exc:
        context.logger.debug(
            "xiaoqing_chat failed to cancel task kind=%s error_type=%s",
            kind,
            type(exc).__name__,
        )


def _track_bg_task(
    context: Any,
    task: asyncio.Task[None],
    *,
    name: str,
    singleflight_key: str | None = None,
) -> None:
    state = _state()
    tracked = state.add_bg_task(task, key=singleflight_key)

    def _done(t: asyncio.Task[None]) -> None:
        state.remove_bg_task(t)
        try:
            t.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            public_error_message(
                context,
                exc,
                logger    = context.logger,
                component = f"xiaoqing_chat.bg_task.{name}",
            )

    task.add_done_callback(_done)
    if not tracked:
        task.cancel()


def _spawn_bg_task(context: Any, coro, *, name: str) -> None:
    state            = _state()
    singleflight_key = _singleflight_key(name)
    if not state.can_add_bg_task(key=singleflight_key):
        _close_awaitable(coro)
        context.logger.debug(
            "xiaoqing_chat background task dropped kind=%s",
            str(name or "").partition(":")[0] or "unknown",
        )
        return
    task = _create_task_safely(coro)
    if task is None:
        return
    _track_bg_task(
        context,
        task,
        name             = name,
        singleflight_key = singleflight_key,
    )


def _schedule_memory_persist(context: Any, runtime: _ChatRuntime, *, chat_id: str) -> None:
    delay = max(0.0, runtime.cfg.io_persist_debounce_seconds)
    old   = _state().get_persist_task(chat_id)
    _cancel_pending_task(context, old, kind="memory_persist")

    async def _run() -> None:
        if delay:
            await asyncio.sleep(delay)
        await asyncio.to_thread(_state().memory_store.persist, chat_id)

    task = _create_task_safely(_run())
    if task is None:
        return
    _state().set_persist_task(chat_id, task)

    def _cleanup_done(t: asyncio.Task[Any]) -> None:
        current = _state().get_persist_task(chat_id)
        if current is t:
            _state().pop_persist_task(chat_id)

    task.add_done_callback(_cleanup_done)
    _track_bg_task(context, task, name=f"persist:{chat_id}")


def _schedule_memory_db_save(context: Any, runtime: _ChatRuntime) -> None:
    delay = max(0.0, runtime.cfg.memory_db_save_debounce_seconds)
    old   = _state().get_vdb_save_task()
    _cancel_pending_task(context, old, kind="memory_db_save")

    async def _run() -> None:
        if delay:
            await asyncio.sleep(delay)
        if not _state().memory_db.is_dirty():
            return
        await asyncio.to_thread(_state().memory_db.save)

    task = _create_task_safely(_run())
    if task is None:
        return
    _state().set_vdb_save_task(task)
    _track_bg_task(context, task, name="memory_db_save")


_action_flush_tasks: dict[str, asyncio.Task[Any]]    = {}
_pfc_state_flush_tasks: dict[str, asyncio.Task[Any]] = {}
_media_registry_flush_task: asyncio.Task[Any] | None = None


def _schedule_debounced_flush(
    context: Any,
    runtime: _ChatRuntime,
    *,
    chat_id: str,
    task_registry: dict[str, asyncio.Task[Any]],
    flush_func: Any,
    name_prefix: str,
) -> None:
    """调度防抖刷盘任务的内部公共实现。"""
    delay = max(0.0, runtime.cfg.io_persist_debounce_seconds)
    old   = task_registry.get(chat_id)
    _cancel_pending_task(context, old, kind=name_prefix)

    async def _run() -> None:
        if delay:
            await asyncio.sleep(delay)
        await asyncio.to_thread(flush_func, chat_id)

    task = _create_task_safely(_run())
    if task is None:
        return
    task_registry[chat_id] = task

    def _cleanup_done(t: asyncio.Task[Any]) -> None:
        current = task_registry.get(chat_id)
        if current is t:
            _ = task_registry.pop(chat_id, None)

    task.add_done_callback(_cleanup_done)
    _track_bg_task(context, task, name=f"{name_prefix}:{chat_id}")


def _schedule_action_history_flush(context: Any, runtime: _ChatRuntime, *, chat_id: str) -> None:
    """防抖刷盘动作历史，避免每次追加都写磁盘。"""
    _schedule_debounced_flush(
        context,
        runtime,
        chat_id       = chat_id,
        task_registry = _action_flush_tasks,
        flush_func    = _state().action_history.flush,
        name_prefix   = "action_flush",
    )


def _schedule_pfc_state_flush(context: Any, runtime: _ChatRuntime, *, chat_id: str) -> None:
    _state().pfc_state_store.mark_dirty(chat_id)
    _schedule_debounced_flush(
        context,
        runtime,
        chat_id       = chat_id,
        task_registry = _pfc_state_flush_tasks,
        flush_func    = _state().pfc_state_store.save,
        name_prefix   = "pfc_state_flush",
    )


def _schedule_media_registry_flush(context: Any, runtime: _ChatRuntime) -> None:
    global _media_registry_flush_task

    delay = max(0.0, runtime.cfg.io_persist_debounce_seconds)
    old   = _media_registry_flush_task
    _cancel_pending_task(context, old, kind="media_registry_flush")

    async def _run() -> None:
        if delay:
            await asyncio.sleep(delay)
        await asyncio.to_thread(_state().media_store.flush)

    task = _create_task_safely(_run())
    if task is None:
        return
    _media_registry_flush_task = task

    def _cleanup_done(t: asyncio.Task[Any]) -> None:
        global _media_registry_flush_task
        if _media_registry_flush_task is t:
            _media_registry_flush_task = None

    task.add_done_callback(_cleanup_done)
    _track_bg_task(context, task, name="media_registry_flush")
