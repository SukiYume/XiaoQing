"""
Logging utilities for xiaoqing_chat plugin.

Centralized logging functions to avoid code duplication and ensure consistent logging behavior.
"""
from __future__ import annotations

import json
from typing import Any, Optional, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from .runtime_state import _ChatRuntime
    from core.plugin_base import Context
    from .config.config import XiaoQingChatConfig

from .constants import DEFAULT_SHORT_TEXT_LIMIT, LOG_TEXT_LIMIT


def _short_text(s: Any, *, limit: int = DEFAULT_SHORT_TEXT_LIMIT) -> str:
    """
    Truncate text to a specified limit for logging purposes.

    Args:
        s: The text to truncate
        limit: Maximum length before truncation

    Returns:
        Truncated text with ellipsis if truncated, or original text if short enough
    """
    t = str(s or "").strip().replace("\n", "\\n")
    if len(t) <= limit:
        return t
    return t[: max(0, limit - 1)].rstrip() + "…"


def _log_step(
    context: Context,
    runtime: Union[_ChatRuntime, "XiaoQingChatConfig"],
    *,
    chat_id: str,
    step: str,
    fields: Optional[dict[str, Any]] = None,
) -> None:
    """
    Log a step in the conversation flow with structured JSON output.

    Args:
        context: Plugin context with logger
        runtime: Chat runtime configuration (_ChatRuntime or XiaoQingChatConfig)
        chat_id: Chat/group identifier
        step: Step name identifier
        fields: Optional additional fields to include in log
    """
    # Handle both _ChatRuntime and XiaoQingChatConfig types
    if hasattr(runtime, "cfg"):
        log_enabled = runtime.cfg.debug.log_steps
    elif hasattr(runtime, "debug"):
        log_enabled = runtime.debug.log_steps
    else:
        log_enabled = True

    if not log_enabled:
        return

    payload: dict[str, Any] = {"step": str(step), "chat_id": str(chat_id)}
    if fields:
        for k, v in fields.items():
            if v is None:
                continue
            if isinstance(v, str):
                payload[str(k)] = _short_text(v, limit=LOG_TEXT_LIMIT)
            else:
                payload[str(k)] = v
    try:
        context.logger.info("xiaoqing_chat step=%s", json.dumps(payload, ensure_ascii=False))
    except Exception:
        return
