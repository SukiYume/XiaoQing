"""解析模型的单个出站媒体意图，并将其绑定到已授权的真实媒体。

解析只选择第一条有效意图，其余标记必须从可见文本中清除；候选匹配只能依据稳定 ID
或描述，不能把无依据数字解释为列表下标。图片路径在收集和生成消息 part 两个边界都要
重新证明位于插件 data 目录内，媒体注册表只补充元数据，不授予路径权限。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

from ..media_registry import MediaRegistryStore, resolve_registered_media_items
from ..message_parts import normalize_message_parts
from .emoji_library import EmojiLibraryEntry, load_emoji_library, resolve_emoji_file_path
from .event_media_common import _SUPPORTED_IMAGE_SUFFIXES
from .qq_face_catalog import QQFaceEntry, load_qq_face_catalog

MediaMarkerKind = Literal["emoji", "qq_face", "image"]
T               = TypeVar("T")

_OUTBOUND_MARKER_RE = re.compile(r"\[想发(表情|QQ表情|图片)[:：]([^\]\n]{1,12})\]")
_OUTBOUND_MARKER_RESIDUE_RE = re.compile(r"\[想发[^\]\n]*(?:\]|$)")
_RENDERED_OUTBOUND_MARKER_RE = re.compile(r"\[(表情包|QQ表情|图片)[:：]([^\]\n]{1,80})\]")
_TOKEN_PATTERN = r"[\u4e00-\u9fff]{1,4}|[a-z0-9_]+"
_MARKER_KIND_BY_LABEL: dict[str, MediaMarkerKind] = {
    "表情": "emoji",
    "表情包": "emoji",
    "QQ表情": "qq_face",
    "图片": "image",
}


@dataclass(frozen=True)
class ParsedMarker:
    kind: MediaMarkerKind
    hint: str
    raw_span: tuple[int, int]


@dataclass(frozen=True)
class ImageLibraryEntry:
    media_key: str
    media_hash: str
    file_path: Path
    description: str
    marker: str


@dataclass(frozen=True)
class ResolvedMarker:
    kind: MediaMarkerKind
    hint: str
    raw_span: tuple[int, int]
    entry: EmojiLibraryEntry | QQFaceEntry | ImageLibraryEntry
    marker: str
    mode: str = "text_with_media"


def parse_marker(text: str) -> ParsedMarker | None:
    """解析第一条当前 marker；渲染格式只作为模型偏差的安全归一化入口。"""

    raw_text        = str(text or "")
    match           = _OUTBOUND_MARKER_RE.search(raw_text)
    rendered_format = match is None
    if match is None:
        # 模型偶尔会直接输出渲染格式；识别后仍走同一候选授权流程，不能把内部标记
        # 当普通文字泄露，也不能仅凭这段文本直接发送媒体。
        match = _RENDERED_OUTBOUND_MARKER_RE.search(raw_text)
        if match is None:
            return None
    label = str(match.group(1) or "").strip()
    hint  = str(match.group(2) or "").strip()
    if rendered_format:
        hint = re.split(r"[；;]", hint, maxsplit=1)[0].strip()
    kind = _MARKER_KIND_BY_LABEL.get(label)
    if kind is None or not hint:
        return None
    return ParsedMarker(kind=kind, hint=hint, raw_span=(match.start(), match.end()))


def _clean_stripped_text(text: str) -> str:
    cleaned = re.sub(r"[ \t]{2,}", " ", str(text or ""))
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    return cleaned.strip()


def strip_marker(text: str, span: tuple[int, int]) -> str:
    start, end = span
    raw   = str(text or "")
    start = max(0, min(int(start), len(raw)))
    end   = max(start, min(int(end), len(raw)))
    return _clean_stripped_text(raw[:start] + raw[end:])


def strip_outbound_marker_residue(text: str) -> str:
    cleaned = _OUTBOUND_MARKER_RESIDUE_RE.sub("", str(text or ""))
    cleaned = _RENDERED_OUTBOUND_MARKER_RE.sub("", cleaned)
    return _clean_stripped_text(cleaned)


def text_without_outbound_marker(text: str) -> str:
    parsed = parse_marker(text)
    if parsed is not None:
        return strip_outbound_marker_residue(strip_marker(text, parsed.raw_span))
    return strip_outbound_marker_residue(text)


def tokenize_media_text(value: str, *, pattern: str = _TOKEN_PATTERN) -> list[str]:
    raw = str(value or "").lower()
    if not raw:
        return []
    tokens             = re.findall(pattern, raw)
    ordered: list[str] = []
    for token in tokens:
        cleaned = token.strip()
        if len(cleaned) <= 1 and not re.search(r"[\u4e00-\u9fff]", cleaned):
            continue
        if cleaned not in ordered:
            ordered.append(cleaned)
    return ordered


def find_candidate_by_hint(
    candidates: Sequence[T],
    hint: str,
    *,
    key_fn: Callable[[T], Sequence[Any]],
) -> T | None:
    normalized_hint = str(hint or "").strip()
    if not normalized_hint:
        return None

    lowered_hint = normalized_hint.lower()
    for entry in candidates:
        keys = [str(value or "").strip() for value in key_fn(entry)]
        if any(lowered_hint == key.lower() for key in keys if key):
            return entry

    compact_hint                 = re.sub(r"\s+", "", lowered_hint)
    hint_tokens                  = tokenize_media_text(normalized_hint)
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


def _resolve_authorized_image_path(data_root: Path, raw_path: str | Path) -> Path | None:
    """解析真实图片文件，并拒绝 data 根外路径、目录和非图片后缀。"""

    try:
        root      = data_root.resolve()
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_file() or resolved.suffix.lower() not in _SUPPORTED_IMAGE_SUFFIXES:
            return None
        return resolved
    except (OSError, RuntimeError, ValueError):
        return None


def _iter_image_library_files(root: Path) -> list[Path]:
    if not root.exists() or not root.is_dir():
        return []
    files = {
        resolved
        for path in root.rglob("*")
        if (resolved := _resolve_authorized_image_path(root, path)) is not None
    }
    ordered = list(files)
    ordered.sort()
    return ordered


def _entry_from_media_ref(media_ref: Mapping[str, Any], *, file_path: Path) -> ImageLibraryEntry:
    description = str(media_ref.get("description", "") or "").strip() or file_path.stem.replace(
        "_", " "
    )
    marker = str(media_ref.get("marker", "") or "").strip()
    if not marker:
        marker = f"[图片：{description}]"
    return ImageLibraryEntry(
        media_key   = str(media_ref.get("media_key", "") or "").strip(),
        media_hash  = str(media_ref.get("media_hash", "") or "").strip(),
        file_path   = file_path,
        description = description,
        marker      = marker,
    )


def _collect_library_image_entries(
    context, *, media_store: MediaRegistryStore | None
) -> list[ImageLibraryEntry]:
    library_dir = (Path(context.data_dir) / "media" / "reply_images").resolve()
    files       = _iter_image_library_files(library_dir)
    if not files:
        return []

    entries: list[ImageLibraryEntry] = []
    for file_path in files:
        media_ref = {
            "kind": "image",
            "file_path": str(file_path),
            "description": file_path.stem.replace("_", " "),
        }
        resolved = resolve_registered_media_items([media_ref], store=media_store)[0]
        entries.append(_entry_from_media_ref(resolved, file_path=file_path))
    return entries


def _image_entry_key(entry: ImageLibraryEntry) -> str:
    return entry.media_key or entry.media_hash or str(entry.file_path).casefold()


def _collect_history_image_entries(
    history: Sequence[Any],
    *,
    data_root: Path,
    media_store: MediaRegistryStore | None,
) -> list[ImageLibraryEntry]:
    seen_keys: set[str]              = set()
    entries: list[ImageLibraryEntry] = []
    for message in reversed(history or ()):
        if str(getattr(message, "role", "") or "").strip() != "assistant":
            continue
        for part in normalize_message_parts(getattr(message, "parts", ()) or ()):
            if str(part.get("kind", "") or "").strip() != "image":
                continue
            resolved = resolve_registered_media_items([part], store=media_store)[0]
            file_path = str(resolved.get("file_path", "") or "").strip()
            if not file_path:
                continue
            path = _resolve_authorized_image_path(data_root, file_path)
            if path is None:
                continue
            candidate = _entry_from_media_ref(resolved, file_path=path)
            dedupe_key = _image_entry_key(candidate)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            entries.append(candidate)
    return entries


def _merge_candidate_entries(*entry_groups: Sequence[ImageLibraryEntry]) -> list[ImageLibraryEntry]:
    seen_keys: set[str]             = set()
    merged: list[ImageLibraryEntry] = []
    for group in entry_groups:
        for entry in group:
            dedupe_key = _image_entry_key(entry)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            merged.append(entry)
    return merged


async def _resolve_emoji_marker(
    parsed: ParsedMarker,
    *,
    context,
    runtime,
    chat_id: str,
) -> ResolvedMarker | None:
    entries = await load_emoji_library(context, runtime, chat_id=chat_id)
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
        kind     = "emoji",
        hint     = parsed.hint,
        raw_span = parsed.raw_span,
        entry    = entry,
        marker   = entry.marker,
        mode     = "text_with_emoji",
    )


async def _resolve_qq_face_marker(parsed: ParsedMarker, *, context) -> ResolvedMarker | None:
    entries = await load_qq_face_catalog(context)
    entry   = find_candidate_by_hint(
        entries,
        parsed.hint,
        key_fn=lambda item: (item.marker, item.label, item.face_id, *tuple(item.aliases)),
    )
    if entry is None:
        return None
    return ResolvedMarker(
        kind     = "qq_face",
        hint     = parsed.hint,
        raw_span = parsed.raw_span,
        entry    = entry,
        marker   = entry.marker,
        mode     = "text_with_face",
    )


async def _resolve_image_marker(
    parsed: ParsedMarker,
    *,
    context,
    history: Sequence[Any] | None,
    media_store: MediaRegistryStore | None,
) -> ResolvedMarker | None:
    entries = _merge_candidate_entries(
        _collect_library_image_entries(context, media_store=media_store),
        _collect_history_image_entries(
            history or (),
            data_root   = Path(context.data_dir),
            media_store = media_store,
        ),
    )
    entry = find_candidate_by_hint(
        entries,
        parsed.hint,
        key_fn=lambda item: (item.marker, item.description, item.media_key, item.media_hash),
    )
    if entry is None:
        return None
    return ResolvedMarker(
        kind     = "image",
        hint     = parsed.hint,
        raw_span = parsed.raw_span,
        entry    = entry,
        marker   = entry.marker,
        mode     = "text_with_image",
    )


async def resolve_marker(
    parsed: ParsedMarker,
    *,
    context,
    runtime,
    history: Sequence[Any] | None          = None,
    chat_id: str                           = "",
    media_store: MediaRegistryStore | None = None,
) -> ResolvedMarker | None:
    if parsed.kind == "emoji":
        return await _resolve_emoji_marker(
            parsed,
            context = context,
            runtime = runtime,
            chat_id = chat_id,
        )
    if parsed.kind == "qq_face":
        return await _resolve_qq_face_marker(parsed, context=context)
    if parsed.kind == "image":
        return await _resolve_image_marker(
            parsed,
            context     = context,
            history     = history,
            media_store = media_store,
        )
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
        file_path = _resolve_authorized_image_path(Path(context.data_dir), entry.file_path)
        if file_path is None:
            return None
        return {
            "kind": "image",
            "media_key": entry.media_key,
            "media_hash": entry.media_hash,
            "marker": resolved.marker,
            "description": entry.description,
            "file_path": str(file_path),
            "mode": resolved.mode,
        }
    return None
