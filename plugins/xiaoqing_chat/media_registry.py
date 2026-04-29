from __future__ import annotations

import hashlib
import re
import threading
import time
from pathlib import Path
from typing import Any, Sequence

from .store_base import StoreBase

_MEDIA_MARKER_RE = re.compile(r"\[(?:图片|表情包|QQ表情)：[^\]\n]{0,400}\]")
_MEDIA_PLACEHOLDER_RE = re.compile(r"\[\[xc_media_(\d+)\]\]")
_MEDIA_PLACEHOLDER_TEMPLATE = "[[xc_media_{index}]]"


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _clean_text_list(values: Any) -> list[str]:
    if isinstance(values, (list, tuple, set)):
        items = values
    else:
        items = [values]
    cleaned: list[str] = []
    for item in items:
        text = _clean_text(item)
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _media_placeholder(index: int) -> str:
    return _MEDIA_PLACEHOLDER_TEMPLATE.format(index=max(1, int(index)))


def _extract_marker_label(marker: Any) -> str:
    text = _clean_text(marker)
    if not text:
        return ""
    match = re.match(r"^\[(?:图片|表情包|QQ表情)：([^\]]+)\]$", text)
    if not match:
        return ""
    return _clean_text(match.group(1))


def _stable_media_key(item: dict[str, Any]) -> str:
    media_key = _clean_text(item.get("media_key"))
    if media_key:
        return media_key

    face_id = _clean_text(item.get("face_id"))
    if face_id:
        return f"qq_face:{face_id}"

    media_hash = _clean_text(item.get("media_hash"))
    if media_hash:
        return f"media:{media_hash}"

    file_path = _clean_text(item.get("file_path"))
    if file_path:
        return "file:" + hashlib.sha1(file_path.lower().encode("utf-8")).hexdigest()

    marker = _clean_text(item.get("marker"))
    if marker:
        return "marker:" + hashlib.sha1(marker.encode("utf-8")).hexdigest()
    return ""


def _normalize_media_ref(item: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for field in ("kind", "media_hash", "face_id", "marker", "description", "file_path", "mode", "label"):
        value = _clean_text(item.get(field))
        if value:
            normalized[field] = value

    emotion_tags = _clean_text_list(item.get("emotion_tags", []))
    if emotion_tags:
        normalized["emotion_tags"] = emotion_tags

    aliases = _clean_text_list(item.get("aliases", []))
    if aliases:
        normalized["aliases"] = aliases

    media_key = _stable_media_key({**normalized, **item})
    if media_key:
        normalized["media_key"] = media_key
    return normalized


def normalize_media_refs(items: Sequence[dict[str, Any]] | None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        resolved = _normalize_media_ref(item)
        if resolved:
            normalized.append(resolved)
    return normalized


def _quality_score(item: dict[str, Any]) -> float:
    try:
        from .media.event_media import _is_generic_media_label
    except Exception:
        def _is_generic_media_label(_: str) -> bool:
            return False

    kind = _clean_text(item.get("kind"))
    description = _clean_text(item.get("description"))
    marker = _clean_text(item.get("marker"))
    emotion_tags = _clean_text_list(item.get("emotion_tags", []))
    aliases = _clean_text_list(item.get("aliases", []))

    score = 0.0
    if description:
        score += 1.0
        if not _is_generic_media_label(description):
            score += 2.5
        if description not in {"一张图片", "一张表情包"} and not description.startswith("系统表情#"):
            score += 0.8
    if marker:
        score += 0.5
        if "一张图片" not in marker and "一张表情包" not in marker:
            score += 0.6
    if emotion_tags:
        score += min(len(emotion_tags), 4) * 0.7
    if aliases:
        score += min(len(aliases), 4) * 0.3
    if kind == "emoji" and emotion_tags:
        score += 0.4
    if _clean_text(item.get("file_path")):
        score += 0.1
    return score


def _render_media_marker(item: dict[str, Any]) -> str:
    try:
        from .media.event_media import _is_generic_media_label, _normalize_media_label
    except Exception:
        def _is_generic_media_label(_: str) -> bool:
            return False

        def _normalize_media_label(value: str) -> str:
            return _clean_text(value)

    try:
        from .media.event_media_common import split_emoji_visible_text
    except Exception:
        def split_emoji_visible_text(value: str) -> tuple[str, str]:
            return _clean_text(value), ""

    kind = _clean_text(item.get("kind"))
    marker = _clean_text(item.get("marker"))
    description = _clean_text(item.get("description"))
    label = _clean_text(item.get("label"))
    emotion_tags = _clean_text_list(item.get("emotion_tags", []))
    aliases = _clean_text_list(item.get("aliases", []))

    if kind == "qq_face":
        resolved_label = label or description or (aliases[0] if aliases else "") or _extract_marker_label(marker)
        if not resolved_label:
            face_id = _clean_text(item.get("face_id"))
            resolved_label = f"系统表情#{face_id}" if face_id else ""
        return f"[QQ表情：{resolved_label}]" if resolved_label else marker

    if kind == "emoji":
        clean_desc, visible_text = split_emoji_visible_text(description)
        label_text = "，".join(emotion_tags[:2]).strip()
        if not label_text:
            label_text = (
                _extract_marker_label(marker)
                or clean_desc
                or description
                or "一张表情包"
            )
        if visible_text:
            return f'[表情包：{label_text}；写着“{visible_text}”]'
        if (
            clean_desc
            and not _is_generic_media_label(clean_desc)
            and _normalize_media_label(clean_desc) != _normalize_media_label(label_text)
        ):
            return f"[表情包：{label_text}；内容：{clean_desc}]"
        return f"[表情包：{label_text}]"

    if kind == "image":
        desc_text = description or _extract_marker_label(marker) or "一张图片"
        return f"[图片：{desc_text}]"

    return marker or description


def compact_media_items(items: Sequence[dict[str, Any]] | None) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for normalized in normalize_media_refs(items):
        compact: dict[str, Any] = {}
        for field in ("kind", "media_key", "media_hash", "face_id", "marker", "mode"):
            value = _clean_text(normalized.get(field))
            if value:
                compact[field] = value

        if not compact.get("media_key"):
            for field in ("description", "label", "file_path"):
                value = _clean_text(normalized.get(field))
                if value:
                    compact[field] = value
            for field in ("emotion_tags", "aliases"):
                values = _clean_text_list(normalized.get(field, []))
                if values:
                    compact[field] = values

        if compact:
            compacted.append(compact)
    return compacted


def resolve_registered_media_items(
    items: Sequence[dict[str, Any]] | None,
    *,
    store: Any | None = None,
) -> list[dict[str, Any]]:
    normalized_items = normalize_media_refs(items)
    if not normalized_items:
        return []
    if store is not None:
        resolver = getattr(store, "resolve_media_items", None)
        if callable(resolver):
            try:
                resolved = resolver(normalized_items)
            except Exception:
                resolved = None
            if isinstance(resolved, (list, tuple)):
                normalized_resolved = normalize_media_refs(resolved)
                if normalized_resolved:
                    return normalized_resolved
    return normalized_items


def upsert_registered_media_items(
    items: Sequence[dict[str, Any]] | None,
    *,
    store: Any | None = None,
    compact: bool = True,
) -> list[dict[str, Any]]:
    normalized_items = normalize_media_refs(items)
    if not normalized_items:
        return []
    if store is not None:
        upsert = getattr(store, "upsert_media_items", None)
        if callable(upsert):
            try:
                resolved = upsert(normalized_items)
            except Exception:
                resolved = None
            if isinstance(resolved, (list, tuple)):
                normalized_items = normalize_media_refs(resolved) or normalized_items
    return compact_media_items(normalized_items) if compact else normalized_items


def compact_message_content(content: str, media_items: Sequence[dict[str, Any]] | None) -> str:
    text = _clean_text(content)
    items = [item for item in media_items or [] if isinstance(item, dict)]
    if not items:
        return text

    matches = list(_MEDIA_MARKER_RE.finditer(text))
    if not matches:
        placeholders = [_media_placeholder(index) for index in range(1, len(items) + 1)]
        if not placeholders:
            return text
        return "\n".join([part for part in [text, *placeholders] if part]).strip()

    parts: list[str] = []
    cursor = 0
    replaced_count = 0
    for index, match in enumerate(matches, start=1):
        parts.append(text[cursor : match.start()])
        if index <= len(items):
            parts.append(_media_placeholder(index))
            replaced_count = index
        else:
            parts.append(match.group(0))
        cursor = match.end()
    parts.append(text[cursor:])
    compacted = "".join(parts).strip()

    trailing_placeholders = [
        _media_placeholder(index)
        for index in range(replaced_count + 1, len(items) + 1)
    ]
    if trailing_placeholders:
        compacted = "\n".join([part for part in [compacted, *trailing_placeholders] if part]).strip()
    return compacted


def _merge_media_record(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    now = float(time.time())
    merged = dict(existing) if isinstance(existing, dict) else {}

    incoming_score = _quality_score(incoming)
    existing_score = float(merged.get("quality_score", 0.0) or 0.0)
    should_upgrade = incoming_score > existing_score + 0.05

    merged["media_key"] = incoming.get("media_key") or merged.get("media_key") or ""
    merged["kind"] = incoming.get("kind") or merged.get("kind") or ""
    merged["media_hash"] = incoming.get("media_hash") or merged.get("media_hash") or ""
    merged["face_id"] = incoming.get("face_id") or merged.get("face_id") or ""

    if should_upgrade or not _clean_text(merged.get("description")):
        merged["description"] = incoming.get("description") or merged.get("description") or ""
    if should_upgrade or not _clean_text(merged.get("marker")):
        merged["marker"] = incoming.get("marker") or merged.get("marker") or ""

    incoming_tags = _clean_text_list(incoming.get("emotion_tags", []))
    existing_tags = _clean_text_list(merged.get("emotion_tags", []))
    tag_source = incoming_tags if should_upgrade and incoming_tags else existing_tags
    if not tag_source:
        tag_source = incoming_tags or existing_tags
    if tag_source:
        merged["emotion_tags"] = tag_source
    else:
        merged.pop("emotion_tags", None)

    incoming_aliases = _clean_text_list(incoming.get("aliases", []))
    existing_aliases = _clean_text_list(merged.get("aliases", []))
    alias_source = existing_aliases[:]
    for alias in incoming_aliases:
        if alias not in alias_source:
            alias_source.append(alias)
    if alias_source:
        merged["aliases"] = alias_source
    else:
        merged.pop("aliases", None)

    if should_upgrade or not _clean_text(merged.get("label")):
        merged["label"] = incoming.get("label") or merged.get("label") or ""
    if not _clean_text(merged.get("file_path")) and _clean_text(incoming.get("file_path")):
        merged["file_path"] = incoming.get("file_path")

    merged["quality_score"] = max(existing_score, incoming_score)
    merged["first_seen_ts"] = float(merged.get("first_seen_ts", 0.0) or 0.0) or now
    merged["last_seen_ts"] = now
    merged["seen_count"] = int(merged.get("seen_count", 0) or 0) + 1
    return merged


def rebuild_message_content(
    content: str,
    media_items: Sequence[dict[str, Any]] | None,
    *,
    resolved_items: Sequence[dict[str, Any]] | None = None,
) -> str:
    text = _clean_text(content)
    items = list(resolved_items if resolved_items is not None else media_items or [])
    markers = [_render_media_marker(item) for item in items if isinstance(item, dict)]
    markers = [marker for marker in markers if marker]
    if not markers:
        return text

    placeholder_matches = list(_MEDIA_PLACEHOLDER_RE.finditer(text))
    if placeholder_matches:
        parts: list[str] = []
        cursor = 0
        used_indexes: set[int] = set()
        for match in placeholder_matches:
            parts.append(text[cursor : match.start()])
            marker_index = max(0, int(match.group(1)) - 1)
            if marker_index < len(markers):
                parts.append(markers[marker_index])
                used_indexes.add(marker_index)
            cursor = match.end()
        parts.append(text[cursor:])
        rebuilt = "".join(parts).strip()
        for index, marker in enumerate(markers):
            if index in used_indexes:
                continue
            if marker not in rebuilt:
                rebuilt = f"{rebuilt}\n{marker}".strip() if rebuilt else marker
        return rebuilt

    matches = list(_MEDIA_MARKER_RE.finditer(text))
    if matches:
        parts: list[str] = []
        cursor = 0
        marker_index = 0
        for match in matches:
            parts.append(text[cursor : match.start()])
            if marker_index < len(markers):
                parts.append(markers[marker_index])
                marker_index += 1
            else:
                parts.append(match.group(0))
            cursor = match.end()
        parts.append(text[cursor:])
        rebuilt = "".join(parts).strip()
    else:
        rebuilt = text

    for marker in markers:
        if marker not in rebuilt:
            rebuilt = f"{rebuilt}\n{marker}".strip() if rebuilt else marker
    return rebuilt


def resolve_message_content(
    content: str,
    media_items: Sequence[dict[str, Any]] | None,
    *,
    store: Any | None = None,
) -> str:
    items = normalize_media_refs(media_items)
    if not items:
        return _clean_text(content)

    resolved_items: Sequence[dict[str, Any]] = items
    if store is None:
        try:
            from .runtime_state import get_state as _state

            store = getattr(_state(), "media_store", None)
        except Exception:
            store = None
    resolved_items = resolve_registered_media_items(items, store=store) or items
    return rebuild_message_content(content, items, resolved_items=resolved_items)


def message_has_media_kind(message: Any, *kinds: str) -> bool:
    normalized_kinds = {_clean_text(kind) for kind in kinds if _clean_text(kind)}
    if not normalized_kinds:
        return False

    try:
        from .message_parts import build_message_parts, normalize_message_parts

        message_parts = normalize_message_parts(getattr(message, "parts", ()) or ())
        if not message_parts:
            message_parts = build_message_parts(
                str(getattr(message, "content", "") or ""),
                getattr(message, "media_items", ()) or (),
            )
    except Exception:
        message_parts = ()

    return any(
        _clean_text(item.get("kind")) in normalized_kinds
        for item in message_parts
        if isinstance(item, dict)
    )


class MediaRegistryStore(StoreBase):
    def __init__(self) -> None:
        super().__init__()
        self._entries_cache: dict[str, dict[str, Any]] | None = None
        self._dirty = False
        self._lock = threading.Lock()

    def bind(self, data_dir: Path) -> None:
        with self._lock:
            if self._data_dir != data_dir:
                self._entries_cache = None
                self._dirty = False
        super().bind(data_dir)

    def _index_path(self) -> Path | None:
        return self._resolve_path("media", "index.json")

    def _load_entries_locked(self) -> dict[str, dict[str, Any]]:
        if self._entries_cache is not None:
            return self._entries_cache
        path = self._index_path()
        payload = self._load_json(path, default={"entries": {}}) if path else {"entries": {}}
        entries = payload.get("entries") if isinstance(payload, dict) else {}
        self._entries_cache = entries if isinstance(entries, dict) else {}
        return self._entries_cache

    def _save_entries_locked(self) -> bool:
        path = self._index_path()
        if not path:
            return False
        return self._save_json(path, {"entries": self._entries_cache or {}})

    def is_dirty(self) -> bool:
        with self._lock:
            return bool(self._dirty)

    def flush(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            if self._save_entries_locked():
                self._dirty = False

    def flush_all(self) -> None:
        self.flush()

    def upsert_media_items(self, items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized_items = [_normalize_media_ref(item) for item in items if isinstance(item, dict)]
        if not normalized_items:
            return []

        with self._lock:
            entries = self._load_entries_locked()
            changed = False
            resolved_items: list[dict[str, Any]] = []

            for item in normalized_items:
                media_key = _clean_text(item.get("media_key"))
                if not media_key:
                    resolved_items.append(item)
                    continue
                existing = entries.get(media_key, {})
                merged = _merge_media_record(existing if isinstance(existing, dict) else {}, item)
                if merged != existing:
                    entries[media_key] = merged
                    changed = True
                resolved_items.append(self._resolve_item_locked(item, entries=entries))

            if changed:
                self._dirty = True
            return resolved_items

    def resolve_media_items(self, items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized_items = [_normalize_media_ref(item) for item in items if isinstance(item, dict)]
        if not normalized_items:
            return []
        with self._lock:
            entries = self._load_entries_locked()
            return [self._resolve_item_locked(item, entries=entries) for item in normalized_items]

    def _resolve_item_locked(
        self,
        item: dict[str, Any],
        *,
        entries: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        media_key = _clean_text(item.get("media_key"))
        if not media_key:
            return dict(item)
        existing = entries.get(media_key, {})
        if not isinstance(existing, dict):
            return dict(item)

        resolved = dict(item)
        for field in ("kind", "media_hash", "face_id", "marker", "description", "file_path", "label"):
            value = _clean_text(existing.get(field))
            if value:
                resolved[field] = value
        for field in ("emotion_tags", "aliases"):
            values = _clean_text_list(existing.get(field, []))
            if values:
                resolved[field] = values
        resolved["media_key"] = media_key
        return resolved
