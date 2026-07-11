"""
Logging utilities for xiaoqing_chat plugin.

Centralized logging functions to avoid code duplication and ensure consistent logging behavior.
"""
from __future__ import annotations

import json
import hashlib
from typing import Any, Optional, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from .runtime_state import _ChatRuntime
    from core.plugin_base import Context
    from .config.config import XiaoQingChatConfig

from .constants import DEFAULT_SHORT_TEXT_LIMIT, LOG_TEXT_LIMIT

_SENSITIVE_LOG_KEY_PARTS = (
    "text",
    "content",
    "reply",
    "prompt",
    "response",
    "raw",
    "thinking",
    "reasoning",
    "summary",
    "evidence",
    "path",
    "url",
    "token",
    "secret",
)


def _redacted_value(value: Any) -> str:
    text = str(value or "")
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"[redacted len={len(text)} sha256={digest}]"


def sanitize_log_fields(fields: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for raw_key, value in fields.items():
        if value is None:
            continue
        key = str(raw_key)
        lowered = key.lower()
        if "error" in lowered or "exception" in lowered:
            safe[key] = str(value).split(":", 1)[0][:80] or "Error"
        elif any(part in lowered for part in _SENSITIVE_LOG_KEY_PARTS):
            safe[key] = _redacted_value(value)
        elif isinstance(value, dict):
            safe[key] = sanitize_log_fields(value)
        elif isinstance(value, (list, tuple)):
            safe[key] = f"[items={len(value)}]"
        elif isinstance(value, str):
            safe[key] = _short_text(value, limit=LOG_TEXT_LIMIT)
        else:
            safe[key] = value
    return safe


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
        log_enabled = getattr(getattr(runtime.cfg, "debug", None), "log_steps", True)
    elif hasattr(runtime, "debug"):
        log_enabled = getattr(runtime.debug, "log_steps", True)
    else:
        log_enabled = True

    if not log_enabled:
        return

    payload: dict[str, Any] = {"step": str(step), "chat_id": _redacted_value(chat_id)}
    if fields:
        payload.update(sanitize_log_fields(fields))
    try:
        context.logger.info("xiaoqing_chat step=%s", json.dumps(payload, ensure_ascii=False))
    except Exception:
        return
