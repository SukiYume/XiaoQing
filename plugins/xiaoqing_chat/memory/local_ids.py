"""会话内本地消息 ID 的分配、查找与可读化替换。"""

from __future__ import annotations

import re
from typing import Any

from ..constants import FIND_BY_LOCAL_ID_LIMIT
from ..persona import resolve_bot_name
from ..runtime_state import get_state as _state


def allocate_local_id(chat_id: str) -> str:
    n = _state().fetch_and_increment_local_id(chat_id)
    return f"m{n}"


def _find_by_local_id(chat_id: str, local_id: str) -> Any | None:
    if not local_id:
        return None
    for msg in reversed(_state().memory_store.get(chat_id)[-FIND_BY_LOCAL_ID_LIMIT:]):
        if msg.local_id == local_id:
            return msg
    return None


def most_recent_user_local_id(chat_id: str) -> str:
    for msg in reversed(_state().memory_store.get(chat_id)):
        if msg.role == "user":
            return msg.local_id
    return ""


def replace_local_ids_with_text(chat_id: str, text: str, *, bot_name: str) -> str:
    """用位置感知的正则替换，把本地消息 ID（如 m123）转换为可读引用。"""
    if not text:
        return ""

    def _repl(match: re.Match[str]) -> str:
        local_id = match.group(0)
        msg      = _find_by_local_id(chat_id, local_id)
        if msg:
            role_text = "对方" if msg.role == "user" else resolve_bot_name(bot_name)
            return f"{role_text}说过"
        return local_id

    return re.sub(r"(?<![A-Za-z0-9_])m\d{1,6}(?![A-Za-z0-9_])", _repl, text)
