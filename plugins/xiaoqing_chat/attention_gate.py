from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any

from core.message import contains_bot_name, iter_message_segments

_PRONOUN_RE = r"(?:她|他|它|ta)"
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
    force_reason: str = ""
    command_forced: bool = False
    private_forced: bool = False
    direct_mentioned: bool = False
    pending_bot_name_forced: bool = False
    reply_to_bot: bool = False
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
    """Classify whether the current turn is directed at XiaoQing.

    This gate owns "attention" semantics only. Ordinary group participation
    probability stays in frequency_control.
    """
    if command_forced:
        return AttentionDecision(
            mentioned=True,
            forced=True,
            force_reason="command",
            command_forced=True,
            direct_mentioned=direct_mentioned,
            pending_bot_name_forced=pending_bot_name_forced,
        )
    if is_private and not enable_private_brain_chat:
        return AttentionDecision(
            mentioned=True,
            forced=True,
            force_reason="private",
            private_forced=True,
            direct_mentioned=direct_mentioned,
            pending_bot_name_forced=pending_bot_name_forced,
        )
    if direct_mentioned:
        return AttentionDecision(
            mentioned=True,
            forced=True,
            force_reason="mentioned",
            direct_mentioned=True,
            pending_bot_name_forced=pending_bot_name_forced,
        )
    if pending_bot_name_forced:
        return AttentionDecision(
            mentioned=True,
            forced=True,
            force_reason="bot_name_followup",
            pending_bot_name_forced=True,
        )

    reply_to_bot = await is_reply_to_bot(event=event, state=state, chat_id=chat_id)
    if reply_to_bot:
        return AttentionDecision(
            mentioned=True,
            forced=True,
            force_reason="reply_to_bot",
            reply_to_bot=True,
        )

    coreference_mentioned = await is_bot_coreference_mention(
        text=text,
        event=event,
        state=state,
        chat_id=chat_id,
        bot_name=bot_name,
    )
    if coreference_mentioned:
        return AttentionDecision(
            mentioned=True,
            forced=True,
            force_reason="coreference_mention",
            coreference_mentioned=True,
        )

    return AttentionDecision(
        mentioned=False,
        forced=False,
        direct_mentioned=False,
        pending_bot_name_forced=pending_bot_name_forced,
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
    history = await _recent_history(state, chat_id, max_items=8)
    return _has_recent_bot_anchor(
        history,
        bot_name=bot_name,
        current_message_id=event.get("message_id"),
    )


async def is_reply_to_bot(*, event: dict[str, Any], state: Any, chat_id: str) -> bool:
    reply_ids = _reply_segment_message_ids(event)
    if not reply_ids:
        return False
    history = await _recent_history(state, chat_id, max_items=30)
    for msg in history:
        if str(getattr(msg, "role", "") or "").lower() != "assistant":
            continue
        msg_id = getattr(msg, "message_id", None)
        if msg_id is not None and str(msg_id) in reply_ids:
            return True
    return False


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


async def _recent_history(state: Any, chat_id: str, *, max_items: int) -> list[Any]:
    store = getattr(state, "memory_store", None)
    getter = getattr(store, "get_recent_async", None)
    if not callable(getter):
        return []
    try:
        result = getter(chat_id, max_items=max_items)
        if asyncio.iscoroutine(result):
            result = await result
    except Exception:
        return []
    if not isinstance(result, (list, tuple)):
        return []
    return list(result)


def _has_recent_bot_anchor(
    history: list[Any],
    *,
    bot_name: str,
    current_message_id: Any,
) -> bool:
    now = time.time()
    current_id = str(current_message_id) if current_message_id not in (None, "") else ""
    for msg in reversed(history):
        msg_id = getattr(msg, "message_id", None)
        if current_id and msg_id is not None and str(msg_id) == current_id:
            continue
        ts = float(getattr(msg, "ts", 0.0) or 0.0)
        if ts and now - ts > _RECENT_BOT_ANCHOR_MAX_AGE_SECONDS:
            continue
        role = str(getattr(msg, "role", "") or "").lower()
        name = str(getattr(msg, "name", "") or "")
        content = str(getattr(msg, "content", "") or "")
        if role == "assistant":
            return True
        if contains_bot_name(name, bot_name) or contains_bot_name(content, bot_name):
            return True
    return False


def _reply_segment_message_ids(event: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for segment in iter_message_segments(event.get("message")):
        if str(segment.get("type", "") or "") != "reply":
            continue
        data = segment.get("data", {}) or {}
        msg_id = data.get("id") or data.get("message_id")
        if msg_id not in (None, ""):
            ids.add(str(msg_id))
    return ids
