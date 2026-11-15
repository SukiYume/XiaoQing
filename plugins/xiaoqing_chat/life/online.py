"""向 xiaoqing_life 取小青自己的网上生活：经历、听懂的流行语和表达方式。

只在能力存在、开关打开时才请求；超时、报错或返回格式不对一律当作没有，
聊天行为与没装 xiaoqing_life 时完全一致。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.atomic_store import AtomicJsonStore
from core.image_validation import (
    WEB_IMAGE_FORMAT_EXTENSIONS,
    WEB_IMAGE_MIME_FORMATS,
    ImageValidationError,
    validate_image_bytes,
)

from ..chat_runtime import optional_capability
from ..logging_utils import log_step

if TYPE_CHECKING:
    from ..runtime_state import ChatRuntime

_MAX_TEXT_CHARS       = 500
_MAX_RECENT_ITEMS     = 8
_MAX_BOT_RECENT       = 6
_MAX_ITEM_CHARS       = 200
_MAX_UNKNOWN_WORDS    = 3
_MAX_UNKNOWN_CHARS    = 12
_MAX_LIFE_LINES       = 800
_MAX_SLANG_NOTES      = 600
_MAX_EXPRESSION       = 200
_MAX_LIFE_TOPICS      = 3
_MAX_SHARE_LINE       = 700
_MAX_KNOWN_TOPICS     = 20
_MAX_SHARE_CANDIDATES = 6


@dataclass(frozen=True, slots=True)
class OnlineLife:
    """一轮对话可用的网上生活文本块；全空表示什么都不加。"""

    life_lines     : str = ""
    slang_notes    : str = ""
    expression_hint: str = ""
    episode_count  : int = 0
    term_count     : int = 0
    # 这一轮交过去的没确认的说法里，网上生活已经去百科查实了的：(说法, 意思)
    verified_terms: tuple[tuple[str, str], ...] = ()
    # life_lines 里给出了细节的经历、查明白的事和日记的键（和统一记忆里的一致）
    shown_keys: tuple[str, ...] = ()

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
    topics: Sequence[dict[str, Any]] = (),
    explained_terms: Sequence[str]   = (),
) -> dict[str, Any]:
    """请求体不含任何用户 ID 和群号；``topics`` 只有群聊才带。

    ``explained_terms`` 是本群黑话块这一轮已经解释了的说法，网上生活不再解释一遍。
    """

    recent, bot_recent = _history_texts(history)
    words = [
        word.strip()[:_MAX_UNKNOWN_CHARS]
        for word in unknown_words or []
        if isinstance(word, str) and word.strip()
    ]
    request: dict[str, Any] = {
        "version"      : 1,
        "mode"         : "reply",
        "text"         : _clip(text, _MAX_TEXT_CHARS),
        "recent"       : recent,
        "bot_recent"   : bot_recent,
        "is_private"   : bool(is_private),
        "unknown_terms": list(dict.fromkeys(words))[:_MAX_UNKNOWN_WORDS],
    }
    if topics and not is_private:
        request["topics"] = list(topics)[:_MAX_LIFE_TOPICS]
    explained = [
        word.strip()[:_MAX_UNKNOWN_CHARS] for word in explained_terms if isinstance(word, str)
    ]
    if any(explained):
        request["explained_terms"] = list(dict.fromkeys(w for w in explained if w))[:6]
    return request


def unconfirmed_jargon(state: Any, data_dir: Any, chat_id: str, text: str) -> list[str]:
    """这一轮原话里出现、本群还没确认意思的说法：交给网上生活去百科查一查。"""

    store = state.bw_jargon_store
    store.bind(Path(data_dir))
    found = [
        rec.content
        for rec in store.load().values()
        if rec.visible_to(chat_id)
        and not rec.confirmed
        and 2 <= len(rec.content) <= _MAX_UNKNOWN_CHARS
        and rec.content in text
    ]
    return list(dict.fromkeys(found))[:_MAX_UNKNOWN_WORDS]


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
        verified_terms  = _verified_terms(raw.get("verified_terms")),
        shown_keys      = tuple(
            key[:80] for key in raw.get("shown_keys") or () if isinstance(key, str) and key
        )[:20]
        if isinstance(raw.get("shown_keys"), list)
        else (),
    )


def _verified_terms(value: Any) -> tuple[tuple[str, str], ...]:
    rows = []
    for row in value if isinstance(value, list) else []:
        if isinstance(row, dict):
            term, meaning = (
                _clip(row.get("term"), _MAX_UNKNOWN_CHARS),
                _clip(row.get("meaning"), 120),
            )
            if term and meaning:
                rows.append((term, meaning))
    return tuple(rows[:_MAX_UNKNOWN_WORDS])


def confirm_jargon(state: Any, data_dir: Any, chat_id: str, rows: Sequence[tuple[str, str]]) -> int:
    """网上生活查实的说法：本群黑话库里还没确认的那条改成百科的意思并标成已确认。返回改了几条。"""

    store = state.bw_jargon_store
    store.bind(Path(data_dir))
    records = store.load()
    changed = 0
    for term, meaning in rows:
        record = records.get(store.key_for(term, chat_id))
        if record is not None and not record.confirmed:
            record.meaning   = meaning
            record.confirmed = True
            record.source    = "百科"
            changed += 1
    if changed:
        store.save(records)
    return changed


async def probe_online_life(context: Any, runtime: ChatRuntime) -> bool:
    """xiaoqing_life 是否在线且开启（``turn_context(mode="probe")``）；任何失败都是 False。"""

    life_cfg   = runtime.cfg.life
    capability = optional_capability(context, "life_context")
    if capability is None or not life_cfg.enable_online_life:
        return False
    try:
        raw = await asyncio.wait_for(
            capability.turn_context({"version": 1, "mode": "probe"}),
            timeout=float(life_cfg.online_life_timeout_seconds),
        )
        return isinstance(raw, dict) and raw.get("online_life") is True
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


async def fetch_share_candidates(
    context: Any,
    runtime: ChatRuntime,
    *,
    exclude_ids: Sequence[str],
    topics: Sequence[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """她手里可以拿出来说的事（``turn_context(mode="proactive")``）；任何失败或关闭都是空。

    ``topics`` 是这个群这两三天的话题词（只在群聊里给）：对方据此认出哪些经历是这个群聊到才去搜
    的，在候选里标成 ``heard_here``。
    """

    life_cfg   = runtime.cfg.life
    capability = optional_capability(context, "life_context")
    if capability is None or not life_cfg.enable_life_share:
        return []
    request = {
        "version"    : 1,
        "mode"       : "proactive",
        "exclude_ids": [str(i) for i in exclude_ids][-20:],
    }
    if topics:
        request["is_private"] = False
        request["topics"]     = list(topics)[:_MAX_KNOWN_TOPICS]
    try:
        raw = await asyncio.wait_for(
            capability.turn_context(request), timeout=float(life_cfg.online_life_timeout_seconds)
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return []
    rows                        = raw.get("share_candidates") if isinstance(raw, dict) else None
    found: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("episode_id"), str):
            continue
        line = _clip(row.get("line"), _MAX_SHARE_LINE)
        if not line or row["episode_id"] in exclude_ids:
            continue
        about = [t.strip()[:12] for t in row.get("topics", []) if isinstance(t, str) and t.strip()][
            :5
        ]
        found.append(
            {
                "episode_id": row["episode_id"][:40],
                "line"      : line,
                "topics"    : about,
                "heard_here": row.get("heard_here") is True,
            }
        )
    return found[:_MAX_SHARE_CANDIDATES]


async def fetch_online_life(
    context: Any,
    runtime: ChatRuntime,
    *,
    text: str,
    history: Any,
    is_private: bool,
    chat_id: str                     = "",
    state: Any                       = None,
    topics: Sequence[dict[str, Any]] = (),
    explained_terms: Sequence[str]   = (),
) -> OnlineLife:
    """取这一轮的网上生活；能力缺失、开关关闭、超时或异常都返回空。

    ``topics`` 是记忆里本群这两三天的话题词（只在群聊里交过去）；给了 ``state`` 时顺带交过去
    这一轮原话里本群还没确认意思的说法。
    """

    life_cfg   = runtime.cfg.life
    capability = optional_capability(context, "life_context")
    if capability is None or not life_cfg.enable_online_life:
        return EMPTY_ONLINE_LIFE
    jargon: list[str] = []
    if state is not None and chat_id:
        try:
            jargon = await asyncio.to_thread(
                unconfirmed_jargon, state, context.data_dir, chat_id, text
            )
        except Exception:
            jargon = []
    request = build_request(
        text            = text,
        history         = history,
        is_private      = is_private,
        unknown_words   = jargon,
        topics          = topics,
        explained_terms = explained_terms,
    )
    try:
        raw = await asyncio.wait_for(
            capability.turn_context(request),
            timeout=float(life_cfg.online_life_timeout_seconds),
        )
        online = parse_response(raw)
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
    if online.verified_terms and state is not None and chat_id:
        await _confirm_verified(context, runtime, state, chat_id, online.verified_terms)
    return online


async def _confirm_verified(
    context: Any, runtime: ChatRuntime, state: Any, chat_id: str, rows: Sequence[tuple[str, str]]
) -> None:
    """回写黑话库失败只记日志，不影响这一轮拿到的网上生活。"""

    try:
        changed = await asyncio.to_thread(confirm_jargon, state, context.data_dir, chat_id, rows)
    except Exception as exc:
        log_step(
            context,
            runtime,
            chat_id = chat_id,
            step    = "reply.online_life.jargon_confirm_error",
            fields  = {"error_type": type(exc).__name__},
        )
        return
    log_step(
        context,
        runtime,
        chat_id = chat_id,
        step    = "reply.online_life.jargon_confirmed",
        fields  = {"count": changed},
    )


# ───────────────────────────── 梗图同步 ─────────────────────────────

_MEME_PAGE_LIMIT = 10
_MEME_MAX_PAGES  = 10
_MEME_MAX_B64    = 3 * 1024 * 1024
_CURSOR_FILE     = "life_memes_cursor.json"


def _cursor_store(context: Any) -> Any:
    return AtomicJsonStore(Path(context.data_dir) / "life" / _CURSOR_FILE)


def _read_cursor(context: Any) -> float:
    value = _cursor_store(context).read({}).get("since", 0.0)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _write_cursor(context: Any, since: float) -> None:
    _cursor_store(context).write_with_backup({"since": since})


def _decode_meme(
    raw: Any, runtime: ChatRuntime
) -> tuple[bytes, str, str, tuple[str, ...], str] | None:
    """校验一张导出的梗图；返回 (字节, 后缀, 名字, 标签, sha256)，不合格返回 None。"""

    import base64
    import binascii
    import hashlib

    if not isinstance(raw, dict):
        return None
    sha, label = raw.get("sha256"), raw.get("label")
    mime     = str(raw.get("mime"))
    suffix   = WEB_IMAGE_FORMAT_EXTENSIONS.get(WEB_IMAGE_MIME_FORMATS.get(mime, ""))
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
    if not _valid_meme_image(image, runtime, mime):
        return None
    tags_raw = raw.get("emotion_tags")
    tags     = tuple(
        t.strip()[:6]
        for t in (tags_raw if isinstance(tags_raw, list) else [])
        if isinstance(t, str) and t.strip()
    )[:3]
    return image, suffix, label.strip()[:8], tags, sha


def _valid_meme_image(image: bytes, runtime: ChatRuntime, mime: str) -> bool:
    """导入前再按表情库自己的图片预算校验一遍（xiaoqing_life 一侧的校验不能替代这一道）。"""

    from ..media.loading import image_validation_limits

    media  = runtime.cfg.media
    limits = image_validation_limits(
        max_bytes  = media.max_analyze_bytes,
        max_pixels = media.max_image_pixels,
        max_frames = media.max_animation_frames,
    )
    try:
        validate_image_bytes(
            image,
            limits            = limits,
            format_extensions = WEB_IMAGE_FORMAT_EXTENSIONS,
            expected_format   = WEB_IMAGE_MIME_FORMATS.get(mime),
        )
    except ImageValidationError:
        return False
    return True


async def _apply_page(context: Any, runtime: ChatRuntime, page: dict[str, Any]) -> int:
    from ..media.emoji_library import import_external_emoji, remove_external_emoji
    from ..media.loading import run_media_blocking
    from ..media.markers import build_marker

    changed = 0
    for raw in page.get("memes", []) if isinstance(page.get("memes"), list) else []:
        decoded = _decode_meme(raw, runtime)
        if decoded is None:
            continue
        image, suffix, label, tags, sha = decoded
        try:
            changed += bool(
                await run_media_blocking(
                    import_external_emoji,
                    context,
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

    capability = optional_capability(context, "life_context")
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
