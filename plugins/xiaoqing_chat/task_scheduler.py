"""统一登记后台任务，并对各类持久化写入做防抖和收尾。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, MutableMapping
from typing import TYPE_CHECKING, Any

from core.public_errors import public_error_message

if TYPE_CHECKING:
    from .runtime_state import ChatRuntime

from .chat_runtime import configured_bot_name
from .memory.client import push_pending
from .runtime_state import get_state as _state

_SINGLEFLIGHT_BG_TASK_KINDS = frozenset(
    {
        "daily_life",
        "expression_learn",
        "media_refine",
        "reflection",
        "review_push",
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


def create_task_safely(coro: Any) -> asyncio.Task[Any] | None:
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


def spawn_bg_task(context: Any, coro, *, name: str) -> None:
    state            = _state()
    singleflight_key = _singleflight_key(name)
    if not state.can_add_bg_task(key=singleflight_key):
        _close_awaitable(coro)
        context.logger.debug(
            "xiaoqing_chat background task dropped kind=%s",
            str(name or "").partition(":")[0] or "unknown",
        )
        return
    task = create_task_safely(coro)
    if task is None:
        return
    _track_bg_task(
        context,
        task,
        name             = name,
        singleflight_key = singleflight_key,
    )


_action_flush_tasks        : dict[str, asyncio.Task[Any]] = {}
_pfc_state_flush_tasks     : dict[str, asyncio.Task[Any]] = {}
_media_registry_flush_tasks: dict[str, asyncio.Task[Any]] = {}
# 媒体注册表不分会话，登记表里只有这一个键。
_MEDIA_REGISTRY_KEY = "media_registry"


def _schedule_debounced(
    context: Any,
    runtime: ChatRuntime,
    *,
    registry: MutableMapping[str, asyncio.Task[Any]],
    key: str,
    kind: str,
    name: str,
    work: Callable[[], Awaitable[None]],
) -> None:
    """防抖执行一件落盘的事：同一个 ``key`` 上还没跑完的旧任务先取消，等过防抖时间再执行。

    任务登记在 ``registry[key]``，结束后自己摘掉；``kind`` 用在取消旧任务的日志里。
    """

    delay = max(0.0, runtime.cfg.io_persist_debounce_seconds)
    _cancel_pending_task(context, registry.get(key), kind=kind)

    async def _run() -> None:
        if delay:
            await asyncio.sleep(delay)
        await work()

    task = create_task_safely(_run())
    if task is None:
        return
    registry[key] = task

    def _cleanup_done(done: asyncio.Task[Any]) -> None:
        if registry.get(key) is done:
            registry.pop(key, None)

    task.add_done_callback(_cleanup_done)
    _track_bg_task(context, task, name=name)


def schedule_memory_persist(context: Any, runtime: ChatRuntime, *, chat_id: str) -> None:
    """防抖落盘这个会话的聊天记录，落盘后把还没推的消息推给统一记忆。"""

    async def work() -> None:
        await asyncio.to_thread(_state().memory_store.persist, chat_id)
        await push_pending(context, _state(), chat_id, bot_name=configured_bot_name(context))

    _schedule_debounced(
        context,
        runtime,
        registry = _state().persist_tasks,
        key      = chat_id,
        kind     = "memory_persist",
        name     = f"persist:{chat_id}",
        work     = work,
    )


def schedule_action_history_flush(context: Any, runtime: ChatRuntime, *, chat_id: str) -> None:
    """防抖刷盘动作历史，避免每次追加都写磁盘。"""
    _schedule_debounced(
        context,
        runtime,
        registry = _action_flush_tasks,
        key      = chat_id,
        kind     = "action_flush",
        name     = f"action_flush:{chat_id}",
        work     = lambda: asyncio.to_thread(_state().action_history.flush, chat_id),
    )


def schedule_pfc_state_flush(context: Any, runtime: ChatRuntime, *, chat_id: str) -> None:
    _state().pfc_state_store.mark_dirty(chat_id)
    _schedule_debounced(
        context,
        runtime,
        registry = _pfc_state_flush_tasks,
        key      = chat_id,
        kind     = "pfc_state_flush",
        name     = f"pfc_state_flush:{chat_id}",
        work     = lambda: asyncio.to_thread(_state().pfc_state_store.save, chat_id),
    )


def schedule_media_registry_flush(context: Any, runtime: ChatRuntime) -> None:
    _schedule_debounced(
        context,
        runtime,
        registry = _media_registry_flush_tasks,
        key      = _MEDIA_REGISTRY_KEY,
        kind     = "media_registry_flush",
        name     = "media_registry_flush",
        work     = lambda: asyncio.to_thread(_state().media_store.flush),
    )
