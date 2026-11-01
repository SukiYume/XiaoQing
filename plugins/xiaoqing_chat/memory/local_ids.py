"""会话内本地消息 ID 的分配、查找与可读化替换。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from ..constants import FIND_BY_LOCAL_ID_LIMIT
from ..llm.communication_constraints import requests_silence
from ..message_parts import render_stored_message
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


# 回复离要接的那句有多远（按群里能看到的条数，含小青自己和其他插件的发言）。要接的那句还在
# 最近 5 条里就直接回；再往上就引用原消息，大家知道她在接哪句；超出最近 15 条才算太远，不再接。
DIRECT_REPLY_RECENT_MESSAGES = 5
MAX_REPLY_RECENT_MESSAGES    = 15


def split_history_at(chat_id: str, local_id: str) -> tuple[list[Any], list[Any]] | None:
    """近期历史在 ``local_id`` 处分成两段：到这条为止（含）和它之后；已不在近期历史里时返回 None。"""

    history = list(_state().memory_store.get(chat_id))
    for index in range(len(history) - 1, -1, -1):
        if history[index].local_id == local_id:
            return history[: index + 1], history[index + 1 :]
    return None


def stale_reason(after: Sequence[Any]) -> str:
    """要接的那句之后的消息让这轮回复作废的原因；还能照常回复时返回空串。

    - ``too_far``：要接的那句已经不在最近 ``MAX_REPLY_RECENT_MESSAGES`` 条里。
    - ``silenced``：之后有群友明确叫停（“不用回复了”“不是叫你”）。新消息不再一律作废旧回复，
      叫停就得单独认出来，否则停聊之后旧回复照样发出去。
    """

    if len(after) >= MAX_REPLY_RECENT_MESSAGES:
        return "too_far"
    if any(
        message.role == "user" and requests_silence(render_stored_message(message))
        for message in after
    ):
        return "silenced"
    return ""


def reply_is_stale(chat_id: str, local_id: str) -> bool:
    """要接的那句已经找不到，或之后的消息让这轮回复作废（见 :func:`stale_reason`）。"""

    split = split_history_at(chat_id, local_id)
    return split is None or bool(stale_reason(split[1]))


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
