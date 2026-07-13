"""
Logging utilities for xiaoqing_chat plugin.

Centralized logging functions to avoid code duplication and ensure consistent logging behavior.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from core.sensitive_audit import summarize_sensitive

if TYPE_CHECKING:
    from core.plugin_base import Context

    from .config.config import XiaoQingChatConfig
    from .runtime_state import _ChatRuntime

from .constants import DEFAULT_SHORT_TEXT_LIMIT

_SENSITIVE_LOG_KEY_PARTS = (
    "text",
    "content",
    "message",
    "history",
    "dialogue",
    "reply",
    "prompt",
    "response",
    "raw",
    "output",
    "input",
    "thinking",
    "reason",
    "reasoning",
    "goal",
    "hint",
    "description",
    "marker",
    "term",
    "emotion",
    "summary",
    "evidence",
    "path",
    "url",
    "token",
    "secret",
)

_IDENTIFIER_LOG_KEY_PARTS = (
    "chat_id",
    "user_id",
    "group_id",
    "owner_id",
    "message_id",
    "msg_id",
    "local_id",
)

_STATUS_LOG_KEYS = {
    "action",
    "analysis_quality",
    "analysis_source",
    "cached_quality",
    "cached_source",
    "finish_reason",
    "frame_strategy",
    "from",
    "from_provider",
    "kind",
    "llm_mime",
    "model",
    "operation",
    "provider",
    "provider_scope",
    "quality",
    "role",
    "segment_type",
    "source_mime",
    "source_plugin",
    "stage",
    "status",
    "step",
    "to",
    "to_model",
    "to_provider",
}

_CORRELATION_LOG_KEYS = {"request_id", "task_id", "job_id"}
_TYPE_LOG_KEYS = {"error_type", "exception_type", "rejection_type"}
_REASON_CODE_LOG_KEYS = {"force_reason", "reason_code"}
_SAFE_STATUS_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")
_SAFE_REASON_CODE = re.compile(r"[a-z][a-z0-9_.:-]{0,63}\Z")
_SAFE_CORRELATION_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")


def _safe_correlation_id(value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_CORRELATION_ID.fullmatch(value):
        return "-"
    return value


def _redacted_value(value: Any) -> str:
    if isinstance(value, (str, bytes, bytearray, memoryview)):
        payload = value
    else:
        try:
            payload = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=lambda item: f"<type:{type(item).__name__}>",
            )
        except (TypeError, ValueError):
            payload = f"<type:{type(value).__name__}>"
    summary = summarize_sensitive(payload)
    return (
        f"[redacted kind={summary.kind} length={summary.length} "
        f"bytes={summary.byte_length} fingerprint={summary.fingerprint}]"
    )


def sanitize_log_fields(fields: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for raw_key, value in fields.items():
        if value is None:
            continue
        key = str(raw_key)
        lowered = key.lower()
        if any(part in lowered for part in _IDENTIFIER_LOG_KEY_PARTS):
            safe[key] = _redacted_value(value)
        elif lowered in _CORRELATION_LOG_KEYS:
            safe[key] = _safe_correlation_id(value)
        elif lowered in _TYPE_LOG_KEYS:
            type_name = str(value)
            safe[key] = type_name if _SAFE_STATUS_TOKEN.fullmatch(type_name) else "Error"
        elif lowered in _REASON_CODE_LOG_KEYS:
            reason = str(value)
            safe[key] = reason if _SAFE_REASON_CODE.fullmatch(reason) else _redacted_value(reason)
        elif lowered in _STATUS_LOG_KEYS:
            status = str(value)
            safe[key] = status if _SAFE_STATUS_TOKEN.fullmatch(status) else _redacted_value(status)
        elif isinstance(value, (bool, int, float)):
            safe[key] = value
        elif isinstance(value, dict):
            safe[key] = sanitize_log_fields(value)
        elif isinstance(value, (list, tuple)):
            safe[key] = f"[items={len(value)}]"
        elif "error" in lowered or "exception" in lowered:
            safe[key] = _redacted_value(value)
        elif any(part in lowered for part in _SENSITIVE_LOG_KEY_PARTS):
            safe[key] = _redacted_value(value)
        elif isinstance(value, str):
            # Unknown free-form strings fail closed. New observability fields
            # must be explicitly classified above before their content can be
            # written to an ordinary log.
            safe[key] = _redacted_value(value)
        else:
            safe[key] = f"[type={type(value).__name__}]"
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
    runtime: _ChatRuntime | XiaoQingChatConfig,
    *,
    chat_id: str,
    step: str,
    fields: dict[str, Any] | None = None,
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
