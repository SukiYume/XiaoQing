"""结合消息指向、上下文锚点和回复关系决定群聊注意力。"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from core.message import contains_bot_name, iter_message_segments

from .memory.memory import is_conversation_reply

_PRONOUN_RE                        = r"(?:她|他|它|ta)"
_RECENT_BOT_ANCHOR_MAX_AGE_SECONDS = 10 * 60

_COREFERENCE_ATTENTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        rf"(?:不|没|不用|别)?\s*(?:@|艾特|at)\s*{_PRONOUN_RE}.{{0,12}}(?:听见|听到|看见|看到|知道|回|回复|理|出来|在不在)",
        rf"{_PRONOUN_RE}.{{0,8}}(?:能不能|会不会|能|会|可以|是不是).{{0,12}}(?:听见|听到|看见|看到|知道|回|回复|理|出来)",
        rf"(?:叫|喊|cue|戳|问|让).{{0,6}}{_PRONOUN_RE}.{{0,10}}(?:出来|回|回复|看看|说话|听见|看到)",
        rf"{_PRONOUN_RE}.{{0,6}}(?:在不在|人呢|还在吗|在吗)",
    )
)


@dataclass(frozen=True)
class AttentionDecision:
    mentioned: bool
    forced: bool
    force_reason: str           = ""
    direct_mentioned: bool      = False
    reply_to_bot: bool          = False
    coreference_mentioned: bool = False


async def decide_attention(
    *,
    text: str,
    event: dict[str, Any],
    state: Any,
    chat_id: str,
    bot_name: str,
    is_private: bool,
    command_forced: bool,
    direct_mentioned: bool,
    pending_bot_name_forced: bool,
    enable_private_brain_chat: bool,
) -> AttentionDecision:
    """判断本轮是否在呼叫小青；普通群聊插话概率仍由频率控制模块负责。"""
    if command_forced:
        return AttentionDecision(
            mentioned        = True,
            forced           = True,
            force_reason     = "command",
            direct_mentioned = direct_mentioned,
        )
    if is_private and not enable_private_brain_chat:
        return AttentionDecision(
            mentioned        = True,
            forced           = True,
            force_reason     = "private",
            direct_mentioned = direct_mentioned,
        )
    if direct_mentioned:
        return AttentionDecision(
            mentioned        = True,
            forced           = True,
            force_reason     = "mentioned",
            direct_mentioned = True,
        )
    if pending_bot_name_forced:
        return AttentionDecision(
            mentioned    = True,
            forced       = True,
            force_reason = "bot_name_followup",
        )

    reply_to_bot = await is_reply_to_bot(event=event, state=state, chat_id=chat_id)
    if reply_to_bot:
        return AttentionDecision(
            mentioned    = True,
            forced       = True,
            force_reason = "reply_to_bot",
            reply_to_bot = True,
        )

    coreference_mentioned = await is_bot_coreference_mention(
        text     = text,
        event    = event,
        state    = state,
        chat_id  = chat_id,
        bot_name = bot_name,
    )
    if coreference_mentioned:
        return AttentionDecision(
            mentioned             = True,
            forced                = True,
            force_reason          = "coreference_mention",
            coreference_mentioned = True,
        )

    return AttentionDecision(
        mentioned        = False,
        forced           = False,
        direct_mentioned = False,
    )


async def is_bot_coreference_mention(
    *,
    text: str,
    event: dict[str, Any],
    state: Any,
    chat_id: str,
    bot_name: str,
) -> bool:
    if not _looks_like_bot_coreference_attention(text):
        return False
    history = await state.memory_store.get_recent_async(chat_id, max_items=8)
    return _has_recent_bot_anchor(
        history,
        bot_name           = bot_name,
        current_message_id = event.get("message_id"),
    )


async def is_reply_to_bot(*, event: dict[str, Any], state: Any, chat_id: str) -> bool:
    reply_ids = _reply_segment_message_ids(event)
    if not reply_ids:
        return False
    history = await state.memory_store.get_recent_async(chat_id, max_items=30)
    return any(
        msg.role == "assistant" and msg.message_id is not None and str(msg.message_id) in reply_ids
        for msg in history
    )


def _looks_like_bot_coreference_attention(text: str) -> bool:
    normalized = _normalize_for_coreference(text)
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in _COREFERENCE_ATTENTION_PATTERNS)


def _normalize_for_coreference(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    return re.sub(r"\s+", "", value)


def _has_recent_bot_anchor(
    history: list[Any],
    *,
    bot_name: str,
    current_message_id: Any,
) -> bool:
    now        = time.time()
    current_id = str(current_message_id) if current_message_id not in (None, "") else ""
    for msg in reversed(history):
        if current_id and msg.message_id is not None and str(msg.message_id) == current_id:
            continue
        ts = float(msg.ts or 0.0)
        if ts and now - ts > _RECENT_BOT_ANCHOR_MAX_AGE_SECONDS:
            continue
        role, name, content = msg.role, msg.name, msg.content
        if role == "assistant":
            if is_conversation_reply(msg):
                return True
            continue
        if contains_bot_name(name, bot_name) or contains_bot_name(content, bot_name):
            return True
    return False


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
