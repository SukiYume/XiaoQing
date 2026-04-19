from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.plugin_base import ensure_dir, load_json, write_json

_SUPPORTED_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})

if TYPE_CHECKING:
    from .event_media import RenderedMedia


@dataclass(frozen=True)
class EmojiLibraryEntry:
    media_hash: str
    file_path: str
    description: str
    emotion_tags: tuple[str, ...]
    usage_count: int
    last_used_ts: float
    marker: str


def _default_emoji_library_dir(context) -> Path:
    return (Path(context.plugin_dir) / "figures" / "library").resolve()


def _to_plugin_relative_path(context, path: Path) -> str:
    root = Path(context.plugin_dir).resolve()
    target = path.resolve()
    try:
        return target.relative_to(root).as_posix()
    except ValueError:
        return target.as_posix()


def resolve_emoji_file_path(context, file_path: str) -> Path:
    path = Path(file_path)
    if path.is_absolute():
        return path
    return (Path(context.plugin_dir) / path).resolve()


def _emoji_index_path(context, runtime) -> Path:
    return resolve_emoji_library_dir(context, runtime) / "index.json"


def _load_index(context, runtime) -> dict[str, Any]:
    payload = load_json(_emoji_index_path(context, runtime), default={"entries": {}})
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        payload["entries"] = {}
    return payload


def _save_index(context, runtime, payload: dict[str, Any]) -> None:
    index_path = _emoji_index_path(context, runtime)
    ensure_dir(index_path.parent)
    write_json(index_path, payload)


def resolve_emoji_library_dir(context, runtime) -> Path | None:
    media_cfg = getattr(getattr(runtime, "cfg", None), "media", None)
    if media_cfg is None:
        return _default_emoji_library_dir(context)
    raw = str(getattr(media_cfg, "emoji_library_dir", "") or "").strip()
    if not raw:
        return _default_emoji_library_dir(context)
    path = Path(raw)
    if not path.is_absolute():
        path = (Path(context.plugin_dir) / raw).resolve()
    return path


def _iter_library_files(root: Path) -> list[Path]:
    if not root.exists() or not root.is_dir():
        return []
    files = [path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in _SUPPORTED_IMAGE_SUFFIXES]
    files.sort()
    return files


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entry_from_render(
    context,
    file_path: Path,
    rendered: "RenderedMedia",
    existing: dict[str, Any] | None = None,
) -> EmojiLibraryEntry:
    existing = existing or {}
    usage_count = int(existing.get("usage_count", 0) or 0)
    last_used_ts = float(existing.get("last_used_ts", 0.0) or 0.0)
    return EmojiLibraryEntry(
        media_hash=rendered.media_hash,
        file_path=_to_plugin_relative_path(context, file_path),
        description=rendered.description,
        emotion_tags=tuple(rendered.emotion_tags),
        usage_count=usage_count,
        last_used_ts=last_used_ts,
        marker=rendered.marker,
    )


def _is_usable_library_metadata(description: str, marker: str, emotion_tags: tuple[str, ...]) -> bool:
    from .event_media import _is_generic_media_label, _looks_like_structured_media_text

    desc = str(description or "").strip()
    mark = str(marker or "").strip()
    tags = tuple(str(item or "").strip() for item in emotion_tags if str(item or "").strip())

    if not desc or not mark:
        return False
    if _is_generic_media_label(desc) or _is_generic_media_label(mark):
        return False
    if _looks_like_structured_media_text(desc) or _looks_like_structured_media_text(mark):
        return False
    if any(_looks_like_structured_media_text(tag) for tag in tags):
        return False
    return True


def collect_emoji_candidate(
    context,
    runtime,
    rendered: "RenderedMedia",
    *,
    source_path: Path,
) -> tuple[EmojiLibraryEntry, bool] | None:
    if rendered.kind != "emoji":
        return None
    if not source_path.exists() or not source_path.is_file():
        return None
    if not _is_usable_library_metadata(rendered.description, rendered.marker, tuple(rendered.emotion_tags)):
        return None

    library_dir = resolve_emoji_library_dir(context, runtime)
    if library_dir is None:
        return None

    ensure_dir(library_dir)
    payload = _load_index(context, runtime)
    entries = payload.setdefault("entries", {})
    existing = entries.get(rendered.media_hash)

    suffix = source_path.suffix.lower()
    if suffix not in _SUPPORTED_IMAGE_SUFFIXES:
        suffix = ".png"
    target_path = library_dir / f"{rendered.media_hash}{suffix}"
    is_new = not target_path.exists()
    if is_new:
        target_path.write_bytes(source_path.read_bytes())

    entry = _entry_from_render(
        context,
        target_path,
        rendered,
        existing if isinstance(existing, dict) else None,
    )
    entries[entry.media_hash] = {
        "media_hash": entry.media_hash,
        "file_path": entry.file_path,
        "description": entry.description,
        "emotion_tags": list(entry.emotion_tags),
        "usage_count": entry.usage_count,
        "last_used_ts": entry.last_used_ts,
        "marker": entry.marker,
    }
    _save_index(context, runtime, payload)
    return entry, is_new


async def load_emoji_library(context, runtime, *, repair_invalid: bool = True) -> list[EmojiLibraryEntry]:
    library_dir = resolve_emoji_library_dir(context, runtime)
    if library_dir is None:
        return []

    files = _iter_library_files(library_dir)
    if not files:
        return []

    payload = _load_index(context, runtime)
    existing_entries = payload.setdefault("entries", {})
    active_entries: dict[str, dict[str, Any]] = {}
    results: list[EmojiLibraryEntry] = []

    for file_path in files:
        media_hash = _hash_file(file_path)
        existing = existing_entries.get(media_hash)
        if (
            isinstance(existing, dict)
            and _is_usable_library_metadata(
                str(existing.get("description", "") or "").strip(),
                str(existing.get("marker", "") or "").strip(),
                tuple(str(item) for item in existing.get("emotion_tags", []) if str(item).strip()),
            )
        ):
            entry = EmojiLibraryEntry(
                media_hash=media_hash,
                file_path=_to_plugin_relative_path(context, file_path),
                description=str(existing.get("description", "") or "").strip(),
                emotion_tags=tuple(
                    str(item) for item in existing.get("emotion_tags", []) if str(item).strip()
                ),
                usage_count=int(existing.get("usage_count", 0) or 0),
                last_used_ts=float(existing.get("last_used_ts", 0.0) or 0.0),
                marker=str(existing.get("marker", "") or "").strip(),
            )
        else:
            if not repair_invalid:
                continue
            from .event_media import render_local_media_file

            rendered = await render_local_media_file(
                file_path,
                context=context,
                runtime=runtime,
                prefer_emoji=True,
            )
            if rendered is None:
                continue
            if not _is_usable_library_metadata(rendered.description, rendered.marker, tuple(rendered.emotion_tags)):
                continue
            entry = _entry_from_render(
                context,
                file_path,
                rendered,
                existing if isinstance(existing, dict) else None,
            )
        results.append(entry)
        active_entries[entry.media_hash] = {
            "media_hash": entry.media_hash,
            "file_path": entry.file_path,
            "description": entry.description,
            "emotion_tags": list(entry.emotion_tags),
            "usage_count": entry.usage_count,
            "last_used_ts": entry.last_used_ts,
            "marker": entry.marker,
        }

    payload["entries"] = active_entries
    _save_index(context, runtime, payload)
    return results


def select_emoji_for_tags(
    entries: list[EmojiLibraryEntry],
    tags: list[str],
) -> EmojiLibraryEntry | None:
    if not entries:
        return None

    normalized = [tag.strip() for tag in tags if tag and tag.strip()]
    if not normalized:
        return min(entries, key=lambda item: (item.usage_count, item.last_used_ts, item.file_path))

    scored: list[tuple[float, float, EmojiLibraryEntry]] = []
    for entry in entries:
        entry_tags = set(entry.emotion_tags)
        score = 0.0
        for tag in normalized:
            if tag in entry_tags:
                score += 3.0
            elif tag and tag in entry.description:
                score += 1.5
        score -= entry.usage_count * 0.05
        score -= max(0.0, time.time() - entry.last_used_ts) < 300 and 1.0 or 0.0
        scored.append((score, -entry.last_used_ts, entry))

    scored.sort(key=lambda item: (item[0], item[1], random.random()), reverse=True)
    best_score, _, best_entry = scored[0]
    if best_score <= 0:
        return min(entries, key=lambda item: (item.usage_count, item.last_used_ts, item.file_path))
    return best_entry


def mark_emoji_used(context, runtime, entry: EmojiLibraryEntry) -> None:
    payload = _load_index(context, runtime)
    entries = payload.setdefault("entries", {})
    current = entries.get(entry.media_hash)
    if not isinstance(current, dict):
        return
    current["usage_count"] = int(current.get("usage_count", 0) or 0) + 1
    current["last_used_ts"] = float(time.time())
    _save_index(context, runtime, payload)
