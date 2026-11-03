"""记录显式消息指向；群聊参与由 PFC 决定。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.message import iter_message_segments


@dataclass(frozen=True)
class AttentionDecision:
    mentioned       : bool
    forced          : bool
    force_reason    : str  = ""
    direct_mentioned: bool = False
    reply_to_bot    : bool = False


async def decide_attention(
    *,
    event: dict[str, Any],
    state: Any,
    chat_id: str,
    is_private: bool,
    command_forced: bool,
    direct_mentioned: bool,
    enable_private_brain_chat: bool,
) -> AttentionDecision:
    """保留 @ 和引用关系；私聊按配置选择直接回复路径。"""
    reply_to_bot = await is_reply_to_bot(event=event, state=state, chat_id=chat_id)
    forced = is_private and (
        command_forced or direct_mentioned or reply_to_bot or not enable_private_brain_chat
    )
    return AttentionDecision(
        mentioned        = command_forced or direct_mentioned or reply_to_bot or is_private,
        forced           = forced,
        force_reason     = ("command" if command_forced else "private") if forced else "",
        direct_mentioned = direct_mentioned,
        reply_to_bot     = reply_to_bot,
    )


async def is_reply_to_bot(*, event: dict[str, Any], state: Any, chat_id: str) -> bool:
    reply_ids = _reply_segment_message_ids(event)
    if not reply_ids:
        return False
    history = await state.memory_store.get_recent_async(chat_id, max_items=30)
    # 长回复拆成几条发出时，引用其中任何一条都算在跟小青说。
    return any(
        msg.role == "assistant" and any(str(mid) in reply_ids for mid in msg.remote_ids())
        for msg in history
    )


def _reply_segment_message_ids(event: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for segment in iter_message_segments(event.get("message")):
        if str(segment.get("type", "") or "") != "reply":
            continue
        data   = segment.get("data", {}) or {}
        msg_id = data.get("id") or data.get("message_id")
        if msg_id not in (None, ""):
            ids.add(str(msg_id))
    return ids
