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


# ───────────────────────────── 梗图同步 ─────────────────────────────

_MEME_PAGE_LIMIT = 10
_MEME_MAX_PAGES  = 10
_MEME_MAX_B64    = 3 * 1024 * 1024
_MEME_SUFFIX     = {
    "image/png" : ".png",
    "image/jpeg": ".jpg",
    "image/gif" : ".gif",
    "image/webp": ".webp",
}
_CURSOR_FILE = "life_memes_cursor.json"


def _cursor_store(context: Any) -> Any:
    from pathlib import Path

    from core.atomic_store import AtomicJsonStore

    return AtomicJsonStore(Path(context.data_dir) / "life" / _CURSOR_FILE)


def _read_cursor(context: Any) -> float:
    value = _cursor_store(context).read({}).get("since", 0.0)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _write_cursor(context: Any, since: float) -> None:
    _cursor_store(context).write_with_backup({"since": since})


def _decode_meme(raw: Any) -> tuple[bytes, str, str, tuple[str, ...], str] | None:
    """校验一张导出的梗图；返回 (字节, 后缀, 名字, 标签, sha256)，不合格返回 None。"""

    import base64
    import binascii
    import hashlib

    if not isinstance(raw, dict):
        return None
    sha, label = raw.get("sha256"), raw.get("label")
    suffix   = _MEME_SUFFIX.get(str(raw.get("mime")))
    data_b64 = raw.get("data_b64")
    if not (isinstance(sha, str) and isinstance(label, str) and label.strip() and suffix):
        return None
    if not isinstance(data_b64, str) or len(data_b64) > _MEME_MAX_B64:
        return None
    try:
        image = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    if hashlib.sha256(image).hexdigest() != sha:
        return None
    tags_raw = raw.get("emotion_tags")
    tags     = tuple(
        t.strip()[:6]
        for t in (tags_raw if isinstance(tags_raw, list) else [])
        if isinstance(t, str) and t.strip()
    )[:3]
    return image, suffix, label.strip()[:8], tags, sha


async def _apply_page(context: Any, runtime: ChatRuntime, page: dict[str, Any]) -> int:
    from ..media.emoji_library import import_external_emoji, remove_external_emoji
    from ..media.loading import run_media_blocking
    from ..media.markers import build_marker

    changed = 0
    for raw in page.get("memes", []) if isinstance(page.get("memes"), list) else []:
        decoded = _decode_meme(raw)
        if decoded is None:
            continue
        image, suffix, label, tags, sha = decoded
        try:
            changed += bool(
                await run_media_blocking(
                    import_external_emoji,
                    context,
                    runtime,
                    image_bytes  = image,
                    suffix       = suffix,
                    description  = label,
                    emotion_tags = tags,
                    marker       = build_marker("emoji", label, tags),
                    source       = f"life:{sha}",
                )
            )
        except Exception as exc:
            log_step(
                context,
                runtime,
                chat_id = "",
                step    = "life.memes.import_error",
                fields  = {"error_type": type(exc).__name__},
            )
    for sha in page.get("revoked", []) if isinstance(page.get("revoked"), list) else []:
        if isinstance(sha, str):
            try:
                changed += bool(await run_media_blocking(remove_external_emoji, context, sha))
            except Exception as exc:
                log_step(
                    context,
                    runtime,
                    chat_id = "",
                    step    = "life.memes.remove_error",
                    fields  = {"error_type": type(exc).__name__},
                )
    return changed


async def sync_life_memes(context: Any, runtime: ChatRuntime) -> int:
    """逐页拉取 xiaoqing_life 收集的梗图导入表情库，revoked 的删掉；游标存在本插件数据目录。

    能力缺失、开关关闭或任何请求出错都安静地什么也不做；返回导入 / 删除的条数。
    """

    capability = getattr(getattr(context, "capabilities", None), "life_context", None)
    if capability is None or not runtime.cfg.life.enable_life_memes:
        return 0
    cursor  = _read_cursor(context)
    changed = 0
    try:
        for _ in range(_MEME_MAX_PAGES):
            page = await capability.export_memes({"since": cursor, "limit": _MEME_PAGE_LIMIT})
            if not isinstance(page, dict):
                break
            changed += await _apply_page(context, runtime, page)
            next_since = page.get("next_since")
            if isinstance(next_since, (int, float)) and not isinstance(next_since, bool):
                cursor = max(cursor, float(next_since))
                _write_cursor(context, cursor)
            if not (isinstance(page.get("memes"), list) and len(page["memes"]) >= _MEME_PAGE_LIMIT):
                break
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log_step(
            context,
            runtime,
            chat_id = "",
            step    = "life.memes.sync_error",
            fields  = {"error_type": type(exc).__name__},
        )
    return changed
