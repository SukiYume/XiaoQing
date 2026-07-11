"""Handler context — bundles common per-request state."""

from __future__ import annotations

import functools
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
                request_id = str(getattr(context, "request_id", "") or secrets.token_hex(4))
                if context and hasattr(context, "logger"):
                    context.logger.exception(
                        "XiaoQing Chat handler failed label=%s request_id=%s error_type=%s",
                        label,
                        request_id,
                        type(exc).__name__,
                    )
                from core.plugin_base import segments

                return segments(f"❌ {label}暂时不可用，请稍后重试（请求ID: {request_id}）")

        return wrapper

    return decorator
