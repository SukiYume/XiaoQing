"""
消息处理工具

提供消息解析功能。
"""

import re
from typing import Any, Optional

def extract_text(message: Any) -> str:
    """从 OneBot 消息中提取纯文本"""
    if isinstance(message, str):
        return message

    if isinstance(message, list):
        parts = []
        for item in message:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("data", {}).get("text", ""))
        return "".join(parts)

    return ""

def normalize_message(event: dict[str, Any]) -> tuple[str, Optional[int], Optional[int]]:
    """
    标准化消息事件

    返回: (文本内容, user_id, group_id)
    """
    text = extract_text(event.get("message")).strip()
    return text, event.get("user_id"), event.get("group_id")

def is_bot_mentioned(
    text: str,
    event: dict[str, Any],
    bot_name: str = "",
    self_id: str = "",
) -> bool:
    if bot_name and bot_name.lower() in text.lower():
        return True

    if self_id:
        at_cq = f"[CQ:at,qq={self_id}]"
        if at_cq in text:
            return True

    message = event.get("message", [])
    if isinstance(message, list):
        for seg in message:
            if seg.get("type") == "at":
                at_qq = seg.get("data", {}).get("qq")
                if at_qq and self_id and str(at_qq) == str(self_id):
                    return True

    return False

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

    pattern = bot_name_pattern or compile_bot_name_pattern(bot_name)
    if pattern:
        stripped = pattern.sub("", stripped)

    for prefix in prefixes:
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip()

    return stripped.strip()

def parse_text_command_context(
    text: str,
    event: dict[str, Any],
    *,
    bot_name: str = "",
    prefixes: Optional[tuple[str, ...]] = None,
    self_id: str = "",
    bot_name_pattern: Optional[re.Pattern[str]] = None,
) -> tuple[bool, str, bool, bool, bool]:
    prefixes = prefixes or tuple()
    is_at_me = is_bot_mentioned(text, event, self_id=self_id)
    clean_text = strip_message_prefix(
        text,
        bot_name=bot_name,
        prefixes=prefixes,
        self_id=self_id,
        bot_name_pattern=bot_name_pattern,
    )
    has_bot_name = bool(bot_name and bot_name.lower() in text.lower())
    has_prefix = any(text.startswith(p) for p in prefixes)
    is_only_bot_name = (text.strip() == bot_name) or (is_at_me and not clean_text)
    return is_at_me, clean_text, has_bot_name, has_prefix, is_only_bot_name

__all__ = [
    "extract_text",
    "normalize_message",
    "is_bot_mentioned",
    "compile_bot_name_pattern",
    "strip_message_prefix",
    "parse_text_command_context",
]
