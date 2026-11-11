"""对方连着发了几条时，等他说完再回。

Core 按“群 + 用户”串行处理消息：处理第一条期间，同一个人后发的几条排在队里，这一轮看不到。
真人看到对方还在一条条往外蹦（“小青”“我今天”“考试挂了”），不会每条都回一句。经
``inbound_backlog`` 能力看到队里还有他的聊天消息时，这一轮就不回：消息已经记进聊天记录，排在
最后的那一轮会看着完整的几条一起回。他刚才是在跟她说话的话，后面这几条也算在跟她说。
"""

from __future__ import annotations

import re
import time
from typing import Any

from core.message import extract_text, iter_message_segments

from .chat_event import event_chat_id

# 让出回复后，同一个人这么久以内发来的下一条算在接着跟她说。
CARRY_SECONDS = 45.0
_MAX_CARRIED  = 512
# 这些消息段本身就是在说话；只有 @ 和引用的一条不算内容。
_CONTENT_SEGMENTS = frozenset({"text", "image", "face", "mface", "record", "video", "file"})

# 一口气最多带这么多条：再多就是刷屏，不是一句话分几条说。
_MAX_CARRIED_TEXTS = 6
BURST_TEXTS_KEY    = "_xc_burst_texts"

# 只有一个链接的消息由链接预览处理，不进聊天：它后面不会有“看着完整几条一起回”的那一轮。
_URL_ONLY_RE = re.compile(r"https?://\S+")

_carried      : dict[tuple[str, str], float]                   = {}
_carried_texts: dict[tuple[str, str], tuple[float, list[str]]] = {}


def _command_prefixes(context: Any) -> tuple[str, ...]:
    try:
        config = context.get_settings_snapshot().config
    except Exception:
        return ("/",)
    prefixes = tuple(str(item) for item in config.get("command_prefixes", ["/"]) or ["/"])
    return tuple(prefix for prefix in prefixes if prefix) or ("/",)


def _is_chat_follow_up(pending: dict[str, Any], event: dict[str, Any], context: Any) -> bool:
    """队里的这一条是不是接着说的聊天消息：命令、单独一个链接、@ 了别人的话和空消息不算。"""

    if pending.get("message_id") == event.get("message_id"):
        return False
    message  = pending.get("message")
    segments = list(iter_message_segments(message))
    text     = (extract_text(message) or str(pending.get("raw_message") or "")).strip()
    if text.startswith(_command_prefixes(context)) or _URL_ONLY_RE.fullmatch(text):
        return False
    self_id = str(event.get("self_id") or "")
    for segment in segments:
        if segment.get("type") != "at":
            continue
        data   = segment.get("data")
        target = str(data.get("qq") or "") if isinstance(data, dict) else ""
        if target and target != self_id:
            return False
    return bool(text) or any(segment.get("type") in _CONTENT_SEGMENTS for segment in segments)


def sender_has_follow_up(event: dict[str, Any], context: Any) -> bool:
    """这位发送者在本会话里是不是还有聊天消息排着没处理。能力不可用或出错时当没有。"""

    backlog = getattr(getattr(context, "capabilities", None), "inbound_backlog", None)
    if backlog is None:
        return False
    try:
        waiting = backlog.waiting()
    except Exception:
        return False
    return any(
        isinstance(pending, dict) and _is_chat_follow_up(pending, event, context)
        for pending in waiting
    )


def _key(event: dict[str, Any]) -> tuple[str, str]:
    return event_chat_id(event), str(event.get("user_id") or "")


def carry_text(event: dict[str, Any], text: str) -> None:
    """把这一轮没回的话（连同更早带过来的）留给下一轮：办事时几条合起来才是完整的请求。"""

    now = time.monotonic()
    for stale in [key for key, (at, _) in _carried_texts.items() if now - at >= CARRY_SECONDS]:
        del _carried_texts[stale]
    earlier = [item for item in event.get(BURST_TEXTS_KEY) or () if isinstance(item, str)]
    texts   = [item for item in (*earlier, str(text or "").strip()) if item]
    if texts and len(_carried_texts) < _MAX_CARRIED:
        _carried_texts[_key(event)] = (now, texts[-_MAX_CARRIED_TEXTS:])


def take_carried_texts(event: dict[str, Any]) -> list[str]:
    """取走这位发送者前几条让出来的话（旧→新）；过期的不算。"""

    carried = _carried_texts.pop(_key(event), None)
    if carried is None or time.monotonic() - carried[0] >= CARRY_SECONDS:
        return []
    return list(carried[1])


def burst_text(event: dict[str, Any], text: str) -> str:
    """这一轮连同前几条让出来的话，按先后连成一段。"""

    earlier = [item for item in event.get(BURST_TEXTS_KEY) or () if isinstance(item, str)]
    return "\n".join([*earlier, text]) if earlier else text


def carry_attention(event: dict[str, Any]) -> None:
    """这一轮本来是在跟她说话、却让给了后面的消息：记下来，下一条也算在跟她说。"""

    now = time.monotonic()
    for stale in [key for key, at in _carried.items() if now - at >= CARRY_SECONDS]:
        del _carried[stale]
    if len(_carried) < _MAX_CARRIED:
        _carried[_key(event)] = now


def take_carried_attention(event: dict[str, Any]) -> bool:
    """取走上一轮让出来的“在跟她说话”；过期的不算。"""

    at = _carried.pop(_key(event), None)
    return at is not None and time.monotonic() - at < CARRY_SECONDS
