"""向 xiaoqing_life 取小青自己的网上生活：经历、听懂的流行语和表达方式。

只在能力存在、开关打开时才请求；超时、报错或返回格式不对一律当作没有，
聊天行为与没装 xiaoqing_life 时完全一致。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..logging_utils import log_step

if TYPE_CHECKING:
    from ..runtime_state import ChatRuntime

_MAX_TEXT_CHARS    = 500
_MAX_RECENT_ITEMS  = 8
_MAX_BOT_RECENT    = 6
_MAX_ITEM_CHARS    = 200
_MAX_UNKNOWN_WORDS = 3
_MAX_UNKNOWN_CHARS = 12
_MAX_LIFE_LINES    = 800
_MAX_SLANG_NOTES   = 600
_MAX_EXPRESSION    = 200


@dataclass(frozen=True, slots=True)
class OnlineLife:
    """一轮对话可用的网上生活文本块；全空表示什么都不加。"""

    life_lines     : str = ""
    slang_notes    : str = ""
    expression_hint: str = ""
    episode_count  : int = 0
    term_count     : int = 0

    @property
    def empty(self) -> bool:
        return not (self.life_lines or self.slang_notes or self.expression_hint)


EMPTY_ONLINE_LIFE = OnlineLife()


def _clip(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _history_texts(history: Any) -> tuple[list[str], list[str]]:
    """最近对话正文与小青自己最近的回复，供对方挑相关内容和去回声。"""

    recent    : list[str] = []
    bot_recent: list[str] = []
    for message in list(history or [])[-_MAX_RECENT_ITEMS:]:
        content = _clip(getattr(message, "content", ""), _MAX_ITEM_CHARS)
        if not content:
            continue
        recent.append(content)
        if getattr(message, "role", "") == "assistant":
            bot_recent.append(content)
    return recent, bot_recent[-_MAX_BOT_RECENT:]


def build_request(
    *,
    text: str,
    history: Any,
    is_private: bool,
    unknown_words: list[str],
) -> dict[str, Any]:
    """请求体不含任何用户 ID 和群号。"""

    recent, bot_recent = _history_texts(history)
    words = [
        word.strip()[:_MAX_UNKNOWN_CHARS]
        for word in unknown_words or []
        if isinstance(word, str) and word.strip()
    ]
    return {
        "version"      : 1,
        "mode"         : "reply",
        "text"         : _clip(text, _MAX_TEXT_CHARS),
        "recent"       : recent,
        "bot_recent"   : bot_recent,
        "is_private"   : bool(is_private),
        "unknown_terms": words[:_MAX_UNKNOWN_WORDS],
    }


def parse_response(raw: Any) -> OnlineLife:
    """校验响应类型并截断；任何字段类型不对都按空处理。"""

    if not isinstance(raw, dict):
        return EMPTY_ONLINE_LIFE
    return OnlineLife(
        life_lines      = _clip(raw.get("life_lines"), _MAX_LIFE_LINES),
        slang_notes     = _clip(raw.get("slang_notes"), _MAX_SLANG_NOTES),
        expression_hint = _clip(raw.get("expression_hint"), _MAX_EXPRESSION),
        episode_count   = _count(raw.get("episode_ids")),
        term_count      = _count(raw.get("term_keys")),
    )


async def fetch_online_life(
    context: Any,
    runtime: ChatRuntime,
    *,
    text: str,
    history: Any,
    is_private: bool,
    unknown_words: list[str],
    chat_id: str = "",
) -> OnlineLife:
    """取这一轮的网上生活；能力缺失、开关关闭、超时或异常都返回空。"""

    life_cfg   = runtime.cfg.life
    capability = getattr(getattr(context, "capabilities", None), "life_context", None)
    if capability is None or not life_cfg.enable_online_life:
        return EMPTY_ONLINE_LIFE
    request = build_request(
        text=text, history=history, is_private=is_private, unknown_words=unknown_words
    )
    try:
        raw = await asyncio.wait_for(
            capability.turn_context(request),
            timeout=float(life_cfg.online_life_timeout_seconds),
        )
        return parse_response(raw)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log_step(
            context,
            runtime,
            chat_id = chat_id,
            step    = "reply.online_life.error",
            fields  = {"error_type": type(exc).__name__},
        )
        return EMPTY_ONLINE_LIFE
