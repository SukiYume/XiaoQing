"""
消息处理工具

提供消息解析功能。
"""

import re
from dataclasses import dataclass
from typing import Any, Optional

_MEDIA_SEGMENT_TYPES = frozenset({"image", "mface", "face"})


@dataclass(frozen=True)
class MessageScan:
    """Single-pass summary of a OneBot message payload."""

    text: str
    has_media: bool
    is_at_me: bool


def normalize_inbound_message(event: dict[str, Any]) -> dict[str, Any]:
    """Fill an absent or empty OneBot ``message`` from ``raw_message``.

    Several OneBot implementations send only ``raw_message``.  Normalizing at
    the boundary gives all later consumers the standard segment-list contract
    while preserving non-empty segment payloads such as images and mentions.
    """
    normalized = dict(event)
    message = normalized.get("message")
    raw_message = normalized.get("raw_message")
    raw_text = raw_message.strip() if isinstance(raw_message, str) else ""
    if raw_text and message in (None, "", []):
        normalized["message"] = [{"type": "text", "data": {"text": raw_text}}]
    return normalized


def iter_message_segments(event_or_message: Any) -> tuple[dict[str, Any], ...]:
    """Return normalized message segments from either an event or raw payload."""
    message = event_or_message.get("message") if isinstance(event_or_message, dict) else event_or_message
    if not isinstance(message, list):
        return tuple()
    return tuple(item for item in message if isinstance(item, dict))


def _has_cq_at(value: Any, self_id: str) -> bool:
    if not self_id or not isinstance(value, str):
        return False
    return f"[CQ:at,qq={self_id}]" in value


def scan_message(
    message: Any,
    *,
    self_id: str = "",
    raw_message: str = "",
) -> MessageScan:
    """Scan a message payload once and extract text / media / @mention flags."""
    if isinstance(message, str):
        return MessageScan(
            text=message,
            has_media=False,
            is_at_me=_has_cq_at(message, self_id) or _has_cq_at(raw_message, self_id),
        )

    text_parts: list[str] = []
    has_media = False
    is_at_me = False

    for item in iter_message_segments(message):
        segment_type = item.get("type")
        data = item.get("data", {}) or {}

        if segment_type == "text":
            text_parts.append(str(data.get("text", "")))
            continue

        if segment_type in _MEDIA_SEGMENT_TYPES:
            has_media = True
            continue

        if segment_type == "at" and self_id:
            at_qq = data.get("qq")
            if at_qq is not None and str(at_qq) == self_id:
                is_at_me = True

    if not is_at_me and _has_cq_at(raw_message, self_id):
        is_at_me = True

    return MessageScan(
        text="".join(text_parts),
        has_media=has_media,
        is_at_me=is_at_me,
    )


def extract_text(message: Any) -> str:
    """从 OneBot 消息中提取纯文本"""
    return scan_message(message).text


def has_media_segment(message: Any) -> bool:
    """判断消息中是否包含当前支持的媒体段。"""
    return scan_message(message).has_media


def contains_bot_name(text: str, bot_name: str) -> bool:
    if not text or not bot_name:
        return False
    return bot_name.lower() in text.lower()


def has_at_mention(
    event_or_message: Any,
    *,
    self_id: str = "",
    raw_message: str = "",
) -> bool:
    message = event_or_message.get("message") if isinstance(event_or_message, dict) else event_or_message
    if isinstance(event_or_message, dict) and not raw_message:
        raw_message = str(event_or_message.get("raw_message", "") or "")
    return scan_message(message, self_id=self_id, raw_message=raw_message).is_at_me

def normalize_message(event: dict[str, Any]) -> tuple[str, Optional[int], Optional[int]]:
    """
    标准化消息事件

    返回: (文本内容, user_id, group_id)
    """
    text = scan_message(event.get("message")).text.strip()
    return text, event.get("user_id"), event.get("group_id")

def is_bot_mentioned(
    text: str,
    event: dict[str, Any],
    bot_name: str = "",
    self_id: str = "",
) -> bool:
    return contains_bot_name(text, bot_name) or has_at_mention(event, self_id=self_id)

def compile_bot_name_pattern(bot_name: str) -> Optional[re.Pattern[str]]:
    if not bot_name:
        return None
    return re.compile(
        rf"^{re.escape(bot_name)}[\s,，.。!！?？]*",
        re.IGNORECASE,
    )

def strip_message_prefix(
    text: str,
    *,
    bot_name: str = "",
    prefixes: Optional[tuple[str, ...]] = None,
    self_id: str = "",
    bot_name_pattern: Optional[re.Pattern[str]] = None,
) -> str:
    stripped = text.strip()
    prefixes = prefixes or tuple()

    if self_id:
        at_cq = f"[CQ:at,qq={self_id}]"
        if stripped.startswith(at_cq):
            stripped = stripped[len(at_cq):].lstrip(" ,，.。!！?？\t\n")
        if at_cq in stripped:
            stripped = re.sub(rf"\s*{re.escape(at_cq)}\s*", " ", stripped).strip()

    pattern = bot_name_pattern or compile_bot_name_pattern(bot_name)
    if pattern:
        stripped = pattern.sub("", stripped)

    for prefix in prefixes:
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip()

    return stripped.strip()

_URL_ONLY_PATTERN = re.compile(r"^https?://\S+$")


def is_clean_text_url_only(clean_text: str) -> bool:
    """Return True if `clean_text` (after strip) is exactly one http/https URL."""
    if not clean_text:
        return False
    return _URL_ONLY_PATTERN.fullmatch(clean_text.strip()) is not None


@dataclass(frozen=True)
class TextCommandContext:
    """Result of parsing a single text message for routing signals."""
    clean_text: str
    is_at_me: bool
    has_bot_name: bool
    has_command_prefix: bool   # text.startswith(any configured command prefix)
    has_prefix: bool           # has_command_prefix OR has_bot_name OR is_at_me
    is_only_bot_name: bool
    is_url_only: bool


def parse_text_command_context(
    text: str,
    event: dict[str, Any],
    *,
    bot_name: str = "",
    prefixes: Optional[tuple[str, ...]] = None,
    self_id: str = "",
    bot_name_pattern: Optional[re.Pattern[str]] = None,
    message_scan: MessageScan | None = None,
) -> TextCommandContext:
    prefixes = prefixes or tuple()
    message_scan = message_scan or scan_message(
        event.get("message"),
        self_id=self_id,
        raw_message=str(event.get("raw_message", "") or ""),
    )
    is_at_me = message_scan.is_at_me
    clean_text = strip_message_prefix(
        text,
        bot_name=bot_name,
        prefixes=prefixes,
        self_id=self_id,
        bot_name_pattern=bot_name_pattern,
    )
    has_bot_name = contains_bot_name(text, bot_name)
    has_command_prefix = any(text.startswith(p) for p in prefixes)
    has_prefix = has_command_prefix or has_bot_name or is_at_me
    is_only_bot_name = (text.strip() == bot_name) or (is_at_me and not clean_text)
    is_url_only = is_clean_text_url_only(clean_text)
    return TextCommandContext(
        clean_text=clean_text,
        is_at_me=is_at_me,
        has_bot_name=has_bot_name,
        has_command_prefix=has_command_prefix,
        has_prefix=has_prefix,
        is_only_bot_name=is_only_bot_name,
        is_url_only=is_url_only,
    )

__all__ = [
    "MessageScan",
    "TextCommandContext",
    "contains_bot_name",
    "extract_text",
    "has_media_segment",
    "has_at_mention",
    "iter_message_segments",
    "is_clean_text_url_only",
    "normalize_inbound_message",
    "normalize_message",
    "is_bot_mentioned",
    "compile_bot_name_pattern",
    "scan_message",
    "strip_message_prefix",
    "parse_text_command_context",
]
