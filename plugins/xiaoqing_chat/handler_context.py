"""集中保存一次请求中重复使用的处理器上下文。"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from core.public_errors import public_error_response

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .runtime_state import ChatRuntimeState, _ChatRuntime

ActionList = list[dict[str, Any]]


def validate_action_list(value: object, *, source: str) -> ActionList:
    """校验动态处理器返回值，阻止畸形动作进入 Core 投递链。"""

    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise TypeError(f"{source} must return a list of action dictionaries")
    return cast(ActionList, value)


@dataclass(frozen=True)
class HandlerContext:
    """入口处一次性解析出的不可变请求上下文。"""

    chat_id: str
    runtime: _ChatRuntime
    state: ChatRuntimeState
    secrets: dict[str, Any]
    data_dir: Path
    bot_name: str
    context: Any  # 插件框架上下文没有稳定的静态类型。

    @classmethod
    def from_event(
        cls,
        event: dict[str, Any],
        context: Any,
        *,
        runtime: _ChatRuntime | None = None,
    ) -> HandlerContext:
        from .helper_utils import (
            _chat_id,
            _get_ai_route_context,
            _get_bot_name,
            _load_runtime,
        )
        from .runtime_state import get_state as _state
        from .store_binding import _bind_all_stores

        runtime = runtime or _load_runtime(context)
        state   = _state()
        _bind_all_stores(state, context.data_dir)
        chat_id = _chat_id(event)
        return cls(
            chat_id = chat_id,
            runtime = runtime,
            state   = state,
            secrets=_get_ai_route_context(context, chat_id=chat_id),
            data_dir = context.data_dir,
            bot_name = _get_bot_name(context),
            context  = context,
        )


def handle_errors(fn):
    """为异步处理器统一记录异常，并返回脱敏的用户提示。"""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        context = kwargs.get("context")
        if context is None and args:
            context = args[-1]
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            return public_error_response(
                context,
                exc,
                logger    = getattr(context, "logger", logger),
                component = f"xiaoqing_chat.handler.{fn.__name__}",
            )

    return wrapper
