from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence, TypeVar

from ..media_registry import resolve_registered_media_items
from ..message_parts import normalize_message_parts
from ..utils.json_parsing import parse_first_json_object
from .emoji_library import EmojiLibraryEntry, load_emoji_library, resolve_emoji_file_path
from .event_media_common import _SUPPORTED_IMAGE_SUFFIXES
from .qq_face_catalog import QQFaceEntry, load_qq_face_catalog

MediaMarkerKind = Literal["emoji", "qq_face", "image"]
T = TypeVar("T")

_OUTBOUND_MARKER_RE = re.compile(r"\[想发(表情|QQ表情|图片)[:：]([^\]\n]{1,12})\]")
_OUTBOUND_MARKER_RESIDUE_RE = re.compile(r"\[想发[^\]\n]*(?:\]|$)")
_RENDERED_OUTBOUND_MARKER_RE = re.compile(r"\[(表情包|QQ表情|图片)[:：]([^\]\n]{1,80})\]")
_QQ_FACE_MARKER_RE = re.compile(r"\[QQ表情：([^\]]+)\]")
_IMAGE_MARKER_RE = re.compile(r"\[图片：([^\]]+)\]")
_EMOJI_MARKER_RE = re.compile(r"\[表情包：([^\]]+)\]")
_TOKEN_PATTERN = r"[\u4e00-\u9fff]{1,4}|[a-z0-9_]+"


@dataclass(frozen=True)
class ParsedMarker:
    kind: MediaMarkerKind
    hint: str
    raw_span: tuple[int, int]


@dataclass(frozen=True)
class ResolvedMarker:
    kind: MediaMarkerKind
    hint: str
    raw_span: tuple[int, int]
    entry: Any
    marker: str
    mode: str = "text_with_media"


@dataclass(frozen=True)
class ImageLibraryEntry:
    media_key: str
    media_hash: str
    file_path: str
    description: str
    marker: str


def parse_marker(text: str) -> ParsedMarker | None:
    raw_text = str(text or "")
    match = _OUTBOUND_MARKER_RE.search(raw_text)
    if match is None:
        # Older prompts/checkers described resolved media as [QQ表情：捂脸].
        # Treat that rendered form as an outbound marker too, so a model slip
        # does not leak the marker as plain text.
        match = _RENDERED_OUTBOUND_MARKER_RE.search(raw_text)
        if match is None:
            return None
        label = str(match.group(1) or "").strip()
        hint = str(match.group(2) or "").strip()
        hint = re.split(r"[；;]", hint, maxsplit=1)[0].strip()
        label_map: dict[str, MediaMarkerKind] = {
            "表情包": "emoji",
            "QQ表情": "qq_face",
            "图片": "image",
        }
        kind = label_map.get(label)
        if kind is None or not hint:
            return None
        return ParsedMarker(kind=kind, hint=hint, raw_span=(match.start(), match.end()))

    label = str(match.group(1) or "").strip()
    hint = str(match.group(2) or "").strip()
    if not hint:
        return None
    if label == "表情":
        kind: MediaMarkerKind = "emoji"
    elif label == "QQ表情":
        kind = "qq_face"
    elif label == "图片":
        kind = "image"
    else:
        return None
    return ParsedMarker(kind=kind, hint=hint, raw_span=(match.start(), match.end()))


def _clean_stripped_text(text: str) -> str:
    cleaned = re.sub(r"[ \t]{2,}", " ", str(text or ""))
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    return cleaned.strip()


def strip_marker(text: str, span: tuple[int, int]) -> str:
    start, end = span
    raw = str(text or "")
    start = max(0, min(int(start), len(raw)))
    end = max(start, min(int(end), len(raw)))
    return _clean_stripped_text(raw[:start] + raw[end:])


def strip_outbound_marker_residue(text: str) -> str:
    return _clean_stripped_text(_OUTBOUND_MARKER_RESIDUE_RE.sub("", str(text or "")))


def text_without_outbound_marker(text: str) -> str:
    parsed = parse_marker(text)
    if parsed is not None:
        return strip_marker(text, parsed.raw_span)
    return strip_outbound_marker_residue(text)


def extract_choice_json(output: str) -> dict[str, Any]:
    return parse_first_json_object(output or "") or {}


def tokenize_media_text(value: str, *, pattern: str = _TOKEN_PATTERN) -> list[str]:
    raw = str(value or "").lower()
    if not raw:
        return []
    tokens = re.findall(pattern, raw)
    ordered: list[str] = []
    for token in tokens:
        cleaned = token.strip()
        if len(cleaned) <= 1 and not re.search(r"[\u4e00-\u9fff]", cleaned):
            continue
        if cleaned not in ordered:
            ordered.append(cleaned)
    return ordered


def extract_inbound_marker_labels(value: str, media_kind: str) -> list[str]:
    text = str(value or "")
    if not text:
        return []
    if media_kind == "qq_face":
        pattern = _QQ_FACE_MARKER_RE
    elif media_kind == "image":
        pattern = _IMAGE_MARKER_RE
    else:
        pattern = _EMOJI_MARKER_RE
    labels: list[str] = []
    for match in pattern.finditer(text):
        label = str(match.group(1) or "").strip()
        label = label.split("；内容：", 1)[0].strip()
        label = label.split(";内容:", 1)[0].strip()
        if label and label not in labels:
            labels.append(label)
    return labels


def find_candidate_by_hint(
    candidates: Sequence[T],
    hint: str,
    *,
    key_fn,
) -> T | None:
    normalized_hint = str(hint or "").strip()
    if not normalized_hint:
        return None

    lowered_hint = normalized_hint.lower()
    for entry in candidates:
        keys = [str(value or "").strip() for value in key_fn(entry)]
        if any(lowered_hint == key.lower() for key in keys if key):
            return entry

    if normalized_hint.isdigit():
        idx = int(normalized_hint)
        if 1 <= idx <= len(candidates):
            return candidates[idx - 1]

    compact_hint = re.sub(r"\s+", "", lowered_hint)
    hint_tokens = tokenize_media_text(normalized_hint)
    best: tuple[float, T] | None = None
    for entry in candidates:
        for key in key_fn(entry):
            candidate_text = re.sub(r"\s+", "", str(key or "").strip().lower())
            if not candidate_text:
                continue
            if compact_hint == candidate_text or compact_hint in candidate_text:
                return entry
            score = 0.0
            for token in hint_tokens:
                if token in candidate_text:
                    score += 1.0
            if score > 0 and (best is None or score > best[0]):
                best = (score, entry)
    return best[1] if best is not None else None


def _media_store():
    try:
        from ..runtime_state import get_state as _state

        return getattr(_state(), "media_store", None)
    except Exception:
        return None


def _resolve_image_library_dir(context, runtime=None) -> Path:
    return (Path(context.data_dir) / "media" / "reply_images").resolve()


def _iter_image_library_files(root: Path) -> list[Path]:
    if not root.exists() or not root.is_dir():
        return []
    files = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in _SUPPORTED_IMAGE_SUFFIXES
    ]
    files.sort()
    return files


def _entry_from_media_ref(media_ref: dict[str, str], *, file_path: Path) -> ImageLibraryEntry:
    description = str(media_ref.get("description", "") or "").strip() or file_path.stem.replace("_", " ")
    marker = str(media_ref.get("marker", "") or "").strip()
    if not marker:
        marker = f"[图片：{description or file_path.stem.replace('_', ' ')}]"
    return ImageLibraryEntry(
        media_key=str(media_ref.get("media_key", "") or "").strip(),
        media_hash=str(media_ref.get("media_hash", "") or "").strip(),
        file_path=str(file_path),
        description=description,
        marker=marker,
    )


def _collect_library_image_entries(context, runtime) -> list[ImageLibraryEntry]:
    library_dir = _resolve_image_library_dir(context, runtime)
    files = _iter_image_library_files(library_dir)
    if not files:
        return []

    store = _media_store()
    entries: list[ImageLibraryEntry] = []
    for file_path in files:
        media_ref = {
            "kind": "image",
            "file_path": str(file_path),
            "description": file_path.stem.replace("_", " "),
        }
        resolved_items = resolve_registered_media_items([media_ref], store=store) or [media_ref]
        resolved = resolved_items[0] if resolved_items else media_ref
        entries.append(_entry_from_media_ref(resolved, file_path=file_path))
    return entries


def _collect_history_image_entries(history: Sequence[Any]) -> list[ImageLibraryEntry]:
    store = _media_store()
    seen_keys: set[str] = set()
    entries: list[ImageLibraryEntry] = []
    for message in reversed(history or ()):
        if str(getattr(message, "role", "") or "").strip() != "assistant":
            continue
        for part in normalize_message_parts(getattr(message, "parts", ()) or ()):
            if str(part.get("kind", "") or "").strip() != "image":
                continue
            resolved_items = resolve_registered_media_items([part], store=store) or [part]
            resolved = resolved_items[0] if resolved_items else dict(part)
            file_path = str(resolved.get("file_path", "") or "").strip()
            if not file_path:
                continue
            path = Path(file_path)
            if not path.exists():
                continue
            candidate = _entry_from_media_ref(resolved, file_path=path)
            dedupe_key = candidate.media_key or candidate.media_hash or str(path).lower()
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            entries.append(candidate)
    return entries


def _merge_candidate_entries(*entry_groups: Sequence[ImageLibraryEntry]) -> list[ImageLibraryEntry]:
    seen_keys: set[str] = set()
    merged: list[ImageLibraryEntry] = []
    for group in entry_groups:
        for entry in group:
            dedupe_key = entry.media_key or entry.media_hash or entry.file_path.lower()
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            merged.append(entry)
    return merged


async def _resolve_emoji_marker(parsed: ParsedMarker, *, context, runtime) -> ResolvedMarker | None:
    entries = await load_emoji_library(context, runtime)
    entry = find_candidate_by_hint(
        entries,
        parsed.hint,
        key_fn=lambda item: (
            item.marker,
            item.description,
            item.media_hash,
            *tuple(item.emotion_tags),
        ),
    )
    if entry is None:
        return None
    return ResolvedMarker(
        kind="emoji",
        hint=parsed.hint,
        raw_span=parsed.raw_span,
        entry=entry,
        marker=entry.marker,
        mode="text_with_emoji",
    )


async def _resolve_qq_face_marker(parsed: ParsedMarker, *, context, runtime) -> ResolvedMarker | None:
    entries = await load_qq_face_catalog(context, runtime)
    entry = find_candidate_by_hint(
        entries,
        parsed.hint,
        key_fn=lambda item: (item.marker, item.label, item.face_id, *tuple(item.aliases)),
    )
    if entry is None:
        return None
    return ResolvedMarker(
        kind="qq_face",
        hint=parsed.hint,
        raw_span=parsed.raw_span,
        entry=entry,
        marker=entry.marker,
        mode="text_with_face",
    )


async def _resolve_image_marker(
    parsed: ParsedMarker,
    *,
    context,
    runtime,
    history: Sequence[Any] | None,
) -> ResolvedMarker | None:
    entries = _merge_candidate_entries(
        _collect_library_image_entries(context, runtime),
        _collect_history_image_entries(history or ()),
    )
    entry = find_candidate_by_hint(
        entries,
        parsed.hint,
        key_fn=lambda item: (item.marker, item.description, item.media_key, item.media_hash),
    )
    if entry is None:
        return None
    return ResolvedMarker(
        kind="image",
        hint=parsed.hint,
        raw_span=parsed.raw_span,
        entry=entry,
        marker=entry.marker,
        mode="text_with_image",
    )


async def resolve_marker(
    parsed: ParsedMarker,
    *,
    context,
    runtime,
    history: Sequence[Any] | None = None,
) -> ResolvedMarker | None:
    if parsed.kind == "emoji":
        return await _resolve_emoji_marker(parsed, context=context, runtime=runtime)
    if parsed.kind == "qq_face":
        return await _resolve_qq_face_marker(parsed, context=context, runtime=runtime)
    if parsed.kind == "image":
        return await _resolve_image_marker(parsed, context=context, runtime=runtime, history=history)
    return None


def marker_media_part(context, resolved: ResolvedMarker) -> dict[str, Any] | None:
    entry = resolved.entry
    if resolved.kind == "emoji":
        if not isinstance(entry, EmojiLibraryEntry):
            return None
        raw_file_path = str(entry.file_path or "").strip()
        if not raw_file_path:
            return None
        return {
            "kind": "emoji",
            "media_hash": entry.media_hash,
            "marker": resolved.marker,
            "description": entry.description,
            "emotion_tags": [tag for tag in entry.emotion_tags if str(tag).strip()],
            "file_path": str(resolve_emoji_file_path(context, raw_file_path)),
            "mode": resolved.mode,
        }
    if resolved.kind == "qq_face":
        if not isinstance(entry, QQFaceEntry):
            return None
        return {
            "kind": "qq_face",
            "face_id": entry.face_id,
            "marker": resolved.marker,
            "label": entry.label,
            "aliases": [alias for alias in entry.aliases if str(alias).strip()],
            "mode": resolved.mode,
        }
    if resolved.kind == "image":
        if not isinstance(entry, ImageLibraryEntry):
            return None
        if not entry.file_path:
            return None
        return {
            "kind": "image",
            "media_key": entry.media_key,
            "media_hash": entry.media_hash,
            "marker": resolved.marker,
            "description": entry.description,
            "file_path": entry.file_path,
            "mode": resolved.mode,
        }
    return None
