"""从 OneBot 事件读取会话、发送者和对机器人的称呼信息。"""

from __future__ import annotations

from typing import Any

from core.message import contains_bot_name, extract_text, has_at_mention


def event_chat_id(event: dict[str, Any]) -> str:
    """从 OneBot 事件提取统一会话 ID：群聊以 g 开头，私聊以 u 开头。"""
    group_id = event.get("group_id")
    user_id  = event.get("user_id")
    if group_id not in (None, ""):
        return f"g{group_id}"
    if user_id not in (None, ""):
        return f"u{user_id}"
    raise ValueError("missing chat identifier: expected group_id or user_id")


def event_sender_name(event: dict[str, Any]) -> str:
    """依次从群名片、昵称和名称字段提取发送者显示名。"""
    sender = event.get("sender") or {}
    for key in ("card", "nickname", "name"):
        v = sender.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    user_id = event.get("user_id")
    return f"用户{user_id}" if user_id else "用户"


def is_private_event(event: dict[str, Any]) -> bool:
    """判断事件是否来自私聊。"""
    return event.get("group_id") is None


def is_bot_mentioned(event: dict[str, Any]) -> bool:
    """判断消息是否通过 @ 提及机器人。"""
    self_id = str(event.get("self_id", "") or "")
    return has_at_mention(event, self_id=self_id)


def mentions_bot_name(event: dict[str, Any], bot_name: str) -> bool:
    """判断消息正文是否包含机器人名称。"""
    if not bot_name:
        return False
    text = extract_text(event.get("message")).strip()
    return contains_bot_name(text, bot_name)
