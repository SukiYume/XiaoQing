"""Handler context — bundles common per-request state."""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.public_errors import public_error_response

logger = logging.getLogger(__name__)

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
    def from_event(
        cls,
        event: dict[str, Any],
        context: Any,
        *,
        runtime: _ChatRuntime | None = None,
    ) -> HandlerContext:
        from .helper_utils import (
            _chat_id,
            _get_bot_name,
            _get_llm_secrets,
            _load_runtime,
        )
        from .runtime_state import get_state as _state
        from .store_binding import _bind_all_stores

        runtime = runtime or _load_runtime(context)
        state = _state()
        _bind_all_stores(state, context.data_dir)
        chat_id = _chat_id(event)
        return cls(
            chat_id=chat_id,
            runtime=runtime,
            state=state,
            secrets=_get_llm_secrets(context, chat_id=chat_id),
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
            context = kwargs.get("context")
            if context is None and args:
                context = args[-1]
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                return public_error_response(
                    context,
                    exc,
                    logger=getattr(context, "logger", logger),
                    component=f"xiaoqing_chat.handler.{fn.__name__}",
                )

        return wrapper

    return decorator
