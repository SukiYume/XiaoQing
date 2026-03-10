"""Handler context — bundles common per-request state."""
from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime_state import ChatRuntimeState, _ChatRuntime


@dataclass(frozen=True)
class HandlerContext:
    """Immutable bundle of per-request context extracted once at entry point."""
    chat_id: str
    runtime: _ChatRuntime
    state: ChatRuntimeState
    secrets: dict[str, Any]
    data_dir: Path
    bot_name: str
    context: Any  # plugin context (untyped)

    @classmethod
    def from_event(cls, event: dict[str, Any], context: Any) -> HandlerContext:
        from .helper_utils import (
            _chat_id,
            _get_bot_name,
            _get_llm_secrets,
            _load_runtime,
        )
        from .runtime_state import get_state as _state
        from .store_binding import _bind_all_stores

        runtime = _load_runtime(context)
        state = _state()
        _bind_all_stores(state, context.data_dir)
        return cls(
            chat_id=_chat_id(event),
            runtime=runtime,
            state=state,
            secrets=_get_llm_secrets(context),
            data_dir=context.data_dir,
            bot_name=_get_bot_name(context),
            context=context,
        )


def handle_errors(label: str):
    """Decorator that wraps async handler functions with standard error handling.

    Catches all exceptions, logs them, and returns a user-friendly error message.
    Expects ``context`` as the last positional argument of the wrapped function.
    """
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            # context is always the last positional arg in handler signatures
            context = kwargs.get("context") or args[-1] if args else None
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                if context and hasattr(context, "logger"):
                    context.logger.exception(
                        "XiaoQing Chat %s 处理失败: %s", label, exc
                    )
                from core.plugin_base import segments
                return segments(f"\u274c {label}出错: {exc}")
        return wrapper
    return decorator
