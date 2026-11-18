"""只喊一声名字（或只 @ 她）：立刻回一句短应答，不走模型。

喊名字多半是在看她在不在，要快。这一来一回照常记进聊天记录，这个人接下来一分钟内说的那句也算在跟
她说，接着说的“帮我记一笔”照样交给 agent。他紧跟着已经把事情发过来了（队里还排着他的消息），
这一声就不应，直接接下一条。
"""

from __future__ import annotations

import logging
import random
from typing import Any

from core.delivery import DeliverySegments
from core.log import LogEvent
from core.plugin_base import segments
from core.public_errors import log_public_error

from .chat_runtime import load_runtime
from .constants import DEFAULT_BOT_NAME_ONLY_REPLIES
from .follow_ups import carry_attention, sender_has_follow_up
from .turn_delivery import spoken_line_receipt

log = logging.getLogger(__name__)

# 应完这一声，同一个人这么久以内说的下一句算在接着跟她说。
NAME_CALL_FOLLOW_SECONDS = 60.0

_last_reply: dict[str, str] = {}


def _call_event(context: Any) -> dict[str, Any]:
    """Core 不把事件交给这个入口；按上下文里的会话和发送者拼出认人用的最小事件。"""

    group_id = getattr(context, "current_group_id", None)
    return {
        "message_type": "group" if group_id else "private",
        "group_id"    : group_id,
        "user_id"     : getattr(context, "current_user_id", None),
    }


def _pick(replies: list[str], chat_id: str) -> str:
    """随机挑一句；有得选时不和这个会话上一次用的那句重复。"""

    choices = [reply for reply in replies if reply != _last_reply.get(chat_id)] or replies
    text    = random.choice(choices)
    if len(_last_reply) > 512:
        _last_reply.clear()
    _last_reply[chat_id] = text
    return text


async def call_bot_name_only(context: Any) -> list[dict[str, Any]]:
    """Core 在只喊名字、只 @ 她时调用：一句预设短应答，发出后记进聊天记录。"""

    event = _call_event(context)
    try:
        runtime = load_runtime(context)
        replies = [str(item).strip() for item in runtime.cfg.bot_name_only_replies]
        replies = [item for item in replies if item] or list(DEFAULT_BOT_NAME_ONLY_REPLIES)
        chat_id = f"g{event['group_id']}" if event["group_id"] else f"u{event['user_id']}"
        carry_attention(event, seconds=NAME_CALL_FOLLOW_SECONDS)
        if sender_has_follow_up(event, context):
            log.info(LogEvent("smalltalk.name_call.yield", chat_id=chat_id))
            return []
        text = _pick(replies, chat_id)
        log.info(LogEvent("smalltalk.name_call", chat_id=chat_id, text=text))
        receipt = spoken_line_receipt(
            context, runtime, chat_id, text, action="name_call_reply", reasoning="bare_name_call"
        )
        return DeliverySegments(segments(text), receipt)
    except Exception as exc:
        log_public_error(context, exc, component="xiaoqing_chat.name_call")
        return segments(random.choice(DEFAULT_BOT_NAME_ONLY_REPLIES))
