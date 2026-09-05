"""在结构化消息片段与旧版 content/media_items 表示之间无损转换。

内部顺序以 parts 为唯一事实来源：文本中的媒体占位符只用于持久化兼容，媒体片段仍
保留哈希、QQ face id、文件路径等结构化字段。规范化会丢弃未知类型和空字段，但不得
改变合法片段顺序；合并媒体时只有稳定身份相同的片段才能覆盖，匿名同类媒体必须并存。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from .media_registry import (
    MEDIA_PLACEHOLDER_RE,
    compact_media_items,
    compact_message_content,
    media_placeholder,
    rebuild_message_content,
    resolve_message_content,
    resolve_registered_media_items,
)

_MEDIA_KINDS = frozenset({"image", "emoji", "qq_face"})


def _clean_text(value: Any) -> str:
    return str(value or "")


def _normalize_text_part(value: Any) -> dict[str, Any] | None:
    text = _clean_text(value)
    if not text:
        return None
    return {"kind": "text", "text": text}


def _normalize_media_part(value: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    kind = str(value.get("kind", "") or "").strip()
    if kind not in _MEDIA_KINDS:
        return None
    normalized: dict[str, Any] = {"kind": kind}
    for field in (
        "media_key",
        "media_hash",
        "face_id",
        "marker",
        "description",
        "file_path",
        "label",
        "mode",
    ):
        text = str(value.get(field, "") or "").strip()
        if text:
            normalized[field] = text
    for field in ("emotion_tags", "aliases"):
        values = value.get(field, ())
        if isinstance(values, (list, tuple)):
            cleaned = [str(item).strip() for item in values if str(item).strip()]
            if cleaned:
                normalized[field] = cleaned
    return normalized


def normalize_message_parts(values: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    normalized: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "") or "").strip()
        if kind == "text":
            text_part = _normalize_text_part(item.get("text"))
            if text_part is not None:
                normalized.append(text_part)
            continue
        media_part = _normalize_media_part(item)
        if media_part is not None:
            normalized.append(media_part)
    return tuple(normalized)


def _normalize_media_items(values: Sequence[dict[str, Any]] | None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in values or ():
        media_part = _normalize_media_part(item) if isinstance(item, dict) else None
        if media_part is not None:
            normalized.append(media_part)
    return normalized


def _resolve_media_items(
    media_items: Sequence[dict[str, Any]] | None,
    *,
    store: Any | None = None,
) -> list[dict[str, Any]]:
    normalized_items = _normalize_media_items(media_items)
    return cast(
        list[dict[str, Any]],
        resolve_registered_media_items(normalized_items, store=store),
    )


def _append_media_part(
    parts: list[dict[str, Any]],
    item: dict[str, Any],
) -> None:
    media_part = _normalize_media_part(item)
    if media_part is not None:
        parts.append(media_part)


def _append_text_part(parts: list[dict[str, Any]], text: str) -> None:
    text_part = _normalize_text_part(text)
    if text_part is not None:
        parts.append(text_part)


def reply_media_insert_index(parts: Sequence[dict[str, Any]]) -> int:
    meaningful_text_indexes = [
        index
        for index, item in enumerate(parts)
        if str(item.get("kind", "") or "").strip() == "text"
        and str(item.get("text", "") or "").strip()
    ]
    if not meaningful_text_indexes:
        return len(parts)

    existing_media_count = sum(
        1 for item in parts if str(item.get("kind", "") or "").strip() != "text"
    )
    if existing_media_count >= len(meaningful_text_indexes):
        return len(parts)
    return meaningful_text_indexes[existing_media_count] + 1


def insert_or_merge_media_part(
    parts: list[dict[str, Any]],
    media_part: dict[str, Any],
) -> None:
    normalized_media = _normalize_media_part(media_part)
    if normalized_media is None:
        return

    media_kind = str(normalized_media.get("kind", "") or "").strip()
    identity   = {
        field: str(normalized_media.get(field, "") or "").strip()
        for field in ("face_id", "media_hash", "media_key")
        if str(normalized_media.get(field, "") or "").strip()
    }

    # 没有稳定身份的图片/表情不能仅凭 kind 判重，否则第二张匿名图片会覆盖第一张。
    if identity:
        for index, item in enumerate(parts):
            if str(item.get("kind", "") or "").strip() != media_kind:
                continue
            if any(
                str(item.get(field, "") or "").strip() != value for field, value in identity.items()
            ):
                continue
            merged = dict(item)
            merged.update(normalized_media)
            parts[index] = merged
            return

    insert_at = reply_media_insert_index(parts)
    if insert_at >= len(parts):
        if parts and str(parts[-1].get("kind", "") or "").strip() != "text":
            parts.append({"kind": "text", "text": "\n"})
        parts.append(normalized_media)
        return

    parts.insert(insert_at, normalized_media)


def merge_reply_media_parts(
    base_parts: Sequence[dict[str, Any]] | None,
    media_parts: Sequence[dict[str, Any]] | None,
) -> tuple[dict[str, Any], ...]:
    parts = [dict(part) for part in normalize_message_parts(base_parts)]

    for media_part in media_parts or ():
        insert_or_merge_media_part(parts, dict(media_part))
    return normalize_message_parts(parts)


def replace_message_media_parts(
    parts: Sequence[dict[str, Any]] | None,
    media_items: Sequence[dict[str, Any]] | None,
    *,
    store: Any | None = None,
) -> tuple[dict[str, Any], ...]:
    normalized_parts = normalize_message_parts(parts)
    if not normalized_parts:
        return ()

    resolved_items = _resolve_media_items(media_items, store=store)
    if not resolved_items:
        return normalized_parts

    media_index                   = 0
    rebuilt: list[dict[str, Any]] = []
    for part in normalized_parts:
        if str(part.get("kind", "") or "").strip() == "text":
            rebuilt.append(dict(part))
            continue
        replacement = None
        if media_index < len(resolved_items):
            replacement = _normalize_media_part(resolved_items[media_index])
            media_index += 1
        merged = dict(part)
        if replacement is not None:
            merged.update(replacement)
        rebuilt.append(merged)
    return normalize_message_parts(rebuilt)


def build_text_message_parts(text: Any) -> tuple[dict[str, Any], ...]:
    raw_text = _clean_text(text).replace("\r\n", "\n").replace("\r", "\n")
    if not raw_text:
        return ()

    parts: list[dict[str, Any]] = []
    for chunk in raw_text.splitlines(keepends=True) or [raw_text]:
        _append_text_part(parts, chunk)
    return normalize_message_parts(parts)


def template_with_fallback_placeholders(
    template: str,
    media_items: Sequence[dict[str, Any]] | None,
) -> str:
    normalized = str(template or "").strip()
    items      = _normalize_media_items(media_items)
    if not items:
        return normalized
    if MEDIA_PLACEHOLDER_RE.search(normalized):
        return normalized
    placeholders = [media_placeholder(index) for index in range(1, len(items) + 1)]
    return "\n".join([part for part in [normalized, *placeholders] if part]).strip()


def build_message_parts_from_template(
    template: str,
    media_items: Sequence[dict[str, Any]] | None,
    *,
    store: Any | None = None,
) -> tuple[dict[str, Any], ...]:
    resolved_items = _resolve_media_items(media_items, store=store)
    normalized_template = template_with_fallback_placeholders(template, resolved_items)
    if not normalized_template and not resolved_items:
        return ()

    parts: list[dict[str, Any]] = []
    cursor                      = 0
    consumed_indexes: set[int]  = set()
    for match in MEDIA_PLACEHOLDER_RE.finditer(normalized_template):
        _append_text_part(parts, normalized_template[cursor : match.start()])
        media_index = max(0, int(match.group(1)) - 1)
        if media_index < len(resolved_items):
            _append_media_part(parts, resolved_items[media_index])
            consumed_indexes.add(media_index)
        cursor = match.end()
    _append_text_part(parts, normalized_template[cursor:])

    trailing_items = [
        resolved_items[index]
        for index in range(len(resolved_items))
        if index not in consumed_indexes
    ]
    if trailing_items:
        if parts and str(parts[-1].get("kind", "") or "").strip() != "text":
            _append_text_part(parts, "\n")
        for index, item in enumerate(trailing_items):
            if index > 0:
                _append_text_part(parts, "\n")
            _append_media_part(parts, item)

    return normalize_message_parts(parts)


def build_message_parts(
    content: str,
    media_items: Sequence[dict[str, Any]] | None,
    *,
    store: Any | None = None,
) -> tuple[dict[str, Any], ...]:
    resolved_items = _resolve_media_items(media_items, store=store)
    raw_text = _clean_text(content)

    if not resolved_items:
        return build_text_message_parts(raw_text)

    if MEDIA_PLACEHOLDER_RE.search(raw_text):
        template = raw_text
    else:
        visible_text = rebuild_message_content(
            raw_text, resolved_items, resolved_items=resolved_items
        )
        template = compact_message_content(visible_text, resolved_items)
    return build_message_parts_from_template(template, resolved_items)


def message_parts_to_legacy(
    parts: Sequence[dict[str, Any]] | None,
) -> tuple[str, list[dict[str, Any]]]:
    normalized_parts = normalize_message_parts(parts)
    if not normalized_parts:
        return "", []

    visible_chunks: list[str]         = []
    media_items: list[dict[str, Any]] = []
    pending_text_prefix               = ""
    total_parts                       = len(normalized_parts)
    for index, part in enumerate(normalized_parts):
        kind = str(part.get("kind", "") or "").strip()
        if kind == "text":
            raw_text = str(part.get("text", "") or "")
            if pending_text_prefix and raw_text.startswith("\n"):
                pending_newlines = len(pending_text_prefix) - len(pending_text_prefix.lstrip("\n"))
                raw_newlines = len(raw_text) - len(raw_text.lstrip("\n"))
                text = ("\n" * max(pending_newlines, raw_newlines)) + raw_text.lstrip("\n")
            else:
                text = pending_text_prefix + raw_text
            pending_text_prefix = ""
            next_kind           = ""
            if index + 1 < total_parts:
                next_kind = str(normalized_parts[index + 1].get("kind", "") or "").strip()
            if next_kind and next_kind != "text" and text.endswith("\n") and text.strip("\n"):
                # 媒体前的换行在旧格式里属于媒体后的分隔符；暂存后与下一文本的
                # 前导换行取最大值，避免 parts→legacy 往返时逐次累加空行。
                stripped            = text.rstrip("\n")
                pending_text_prefix = text[len(stripped) :]
                text                = stripped
            visible_chunks.append(text)
            continue
        media_item = _normalize_media_part(part)
        if media_item is None:
            continue
        media_items.append(media_item)
        visible_chunks.append(str(media_item.get("marker", "") or ""))

    if pending_text_prefix:
        visible_chunks.append(pending_text_prefix)

    visible_text          = "".join(visible_chunks)
    compacted_media_items = compact_media_items(media_items)
    content               = compact_message_content(visible_text, compacted_media_items)
    return content, compacted_media_items


def render_message_parts(
    parts: Sequence[dict[str, Any]] | None,
    *,
    store: Any | None = None,
) -> str:
    content, media_items = message_parts_to_legacy(parts)
    if not content and not media_items:
        return ""
    return cast(str, resolve_message_content(content, media_items, store=store))


def render_stored_message(message: Any, *, store: Any | None = None) -> str:
    parts = normalize_message_parts(getattr(message, "parts", ()) or ())
    if not parts:
        parts = build_message_parts(
            str(getattr(message, "content", "") or ""),
            getattr(message, "media_items", ()) or (),
            store=store,
        )
    return render_message_parts(parts, store=store)
