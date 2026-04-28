from __future__ import annotations

import asyncio
import hashlib
import shutil
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.plugin_base import ensure_dir, load_json, write_json
from ..task_scheduler import _spawn_bg_task

_SUPPORTED_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
_PENDING_DIR_NAME = "pending"
_EMOJI_REPAIR_TASKS: set[str] = set()
_LIBRARY_CACHE: dict[str, tuple[tuple[float, float], list["EmojiLibraryEntry"]]] = {}

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


def _emoji_library_task_key(context, runtime) -> str:
    library_dir = resolve_emoji_library_dir(context, runtime)
    if library_dir is None:
        return ""
    return str(library_dir.resolve())


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


def _library_cache_key(context, runtime) -> str:
    library_dir = resolve_emoji_library_dir(context, runtime)
    if library_dir is None:
        return ""
    try:
        return str(library_dir.resolve())
    except OSError:
        return str(library_dir)


def _library_signature(context, runtime) -> tuple[float, float] | None:
    library_dir = resolve_emoji_library_dir(context, runtime)
    if library_dir is None:
        return None
    index_path = library_dir / "index.json"
    try:
        dir_mtime = library_dir.stat().st_mtime if library_dir.exists() else -1.0
        index_mtime = index_path.stat().st_mtime if index_path.exists() else -1.0
    except OSError:
        return None
    return float(dir_mtime), float(index_mtime)


def _invalidate_library_cache(context, runtime) -> None:
    key = _library_cache_key(context, runtime)
    if key:
        _LIBRARY_CACHE.pop(key, None)


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
    _invalidate_library_cache(context, runtime)


def _save_index_if_changed(
    context,
    runtime,
    *,
    original_payload: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    if payload == original_payload:
        return
    _save_index(context, runtime, payload)


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


def _media_cfg_value(runtime, field: str, default):
    media_cfg = getattr(getattr(runtime, "cfg", None), "media", None)
    if media_cfg is None:
        return default
    return getattr(media_cfg, field, default)


def _pending_emoji_library_dir(context, runtime) -> Path | None:
    root = resolve_emoji_library_dir(context, runtime)
    if root is None:
        return None
    return root / _PENDING_DIR_NAME


def _allowed_emoji_target_dirs(context, runtime) -> tuple[Path, ...]:
    library_dir = resolve_emoji_library_dir(context, runtime)
    if library_dir is None:
        return ()
    pending_dir = _pending_emoji_library_dir(context, runtime)
    roots = [library_dir.resolve()]
    if pending_dir is not None:
        roots.append(pending_dir.resolve())
    return tuple(roots)


def _is_path_within_roots(path: Path, roots: tuple[Path, ...]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _status_from_record(record: dict[str, Any] | None) -> str:
    status = str((record or {}).get("status", "") or "active").strip().lower()
    return status if status in {"active", "pending"} else "active"


def _source_from_record(record: dict[str, Any] | None) -> str:
    source = str((record or {}).get("source", "") or "manual").strip().lower()
    return source if source in {"manual", "auto"} else "manual"


def _average_hash(path: Path) -> str:
    try:
        from PIL import Image, ImageOps
    except Exception:
        return ""

    try:
        with Image.open(path) as image:
            sample = ImageOps.exif_transpose(image).convert("L").resize((8, 8))
            pixels = list(sample.tobytes())
    except Exception:
        return ""

    if not pixels:
        return ""
    average = sum(int(value) for value in pixels) / len(pixels)
    bits = "".join("1" if int(value) >= average else "0" for value in pixels)
    return f"{int(bits, 2):016x}"


def _hamming_distance(left: str, right: str) -> int:
    if not left or not right:
        return 65
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError:
        return 65


def _normalize_entry_record(
    *,
    context,
    file_path: Path,
    entry: EmojiLibraryEntry,
    existing: dict[str, Any] | None,
    status: str,
    source: str,
    perceptual_hash: str,
    touch_collection: bool,
) -> dict[str, Any]:
    existing = existing or {}
    first_collected_ts = float(existing.get("first_collected_ts", 0.0) or 0.0) or float(time.time())
    seen_count = int(existing.get("seen_count", 0) or 0)
    if touch_collection:
        seen_count += 1
    last_collected_ts = float(existing.get("last_collected_ts", 0.0) or 0.0)
    if touch_collection:
        last_collected_ts = float(time.time())
    return {
        "media_hash": entry.media_hash,
        "file_path": _to_plugin_relative_path(context, file_path),
        "description": entry.description,
        "emotion_tags": list(entry.emotion_tags),
        "usage_count": entry.usage_count,
        "last_used_ts": entry.last_used_ts,
        "marker": entry.marker,
        "status": status,
        "source": source,
        "perceptual_hash": perceptual_hash or str(existing.get("perceptual_hash", "") or ""),
        "first_collected_ts": first_collected_ts,
        "last_collected_ts": last_collected_ts,
        "seen_count": seen_count,
    }


def _score_record(record: dict[str, Any]) -> tuple[float, float, float]:
    usage_count = float(record.get("usage_count", 0) or 0.0)
    last_used_ts = float(record.get("last_used_ts", 0.0) or 0.0)
    last_collected_ts = float(record.get("last_collected_ts", 0.0) or 0.0)
    seen_count = float(record.get("seen_count", 0) or 0.0)
    return (usage_count * 3.0 + seen_count, last_used_ts, last_collected_ts)


def _remove_library_file(context, runtime, record: dict[str, Any]) -> None:
    file_path = resolve_emoji_file_path(context, str(record.get("file_path", "") or ""))
    allowed_roots = _allowed_emoji_target_dirs(context, runtime)
    if not allowed_roots:
        return
    if not _is_path_within_roots(file_path, allowed_roots):
        return
    if file_path.exists():
        try:
            file_path.unlink()
        except OSError:
            pass


def _safe_target_file_path(
    context,
    runtime,
    *,
    existing: dict[str, Any] | None,
    base_dir: Path,
    record_key: str,
    suffix: str,
) -> Path:
    if existing is not None:
        raw_existing_path = str(existing.get("file_path", "") or "").strip()
        if raw_existing_path:
            existing_path = resolve_emoji_file_path(context, raw_existing_path)
            if _is_path_within_roots(existing_path, _allowed_emoji_target_dirs(context, runtime)):
                return existing_path
    return base_dir / f"{record_key}{suffix}"


def _copy_into_library_if_needed(source_path: Path, target_path: Path) -> None:
    try:
        if target_path.exists() and source_path.samefile(target_path):
            return
    except OSError:
        pass
    ensure_dir(target_path.parent)
    shutil.copyfile(source_path, target_path)


def _find_similar_entry(
    context,
    runtime,
    entries: dict[str, Any],
    *,
    source_path: Path,
    perceptual_hash: str,
) -> tuple[str, dict[str, Any]] | None:
    threshold = max(0, int(_media_cfg_value(runtime, "emoji_auto_collect_similarity_threshold", 4)))
    if not perceptual_hash or threshold < 0:
        return None

    for media_hash, raw_record in entries.items():
        if not isinstance(raw_record, dict):
            continue
        if _source_from_record(raw_record) != "auto":
            continue
        candidate_hash = str(raw_record.get("perceptual_hash", "") or "").strip()
        if not candidate_hash:
            candidate_path = resolve_emoji_file_path(context, str(raw_record.get("file_path", "") or ""))
            if not candidate_path.exists():
                continue
            candidate_hash = _average_hash(candidate_path)
            raw_record["perceptual_hash"] = candidate_hash
        if _hamming_distance(perceptual_hash, candidate_hash) <= threshold:
            return str(media_hash), raw_record
    return None


def _prune_auto_entries(context, runtime, payload: dict[str, Any], *, keep_hashes: set[str] | None = None) -> None:
    max_entries = int(_media_cfg_value(runtime, "emoji_auto_collect_max_entries", 200) or 0)
    if max_entries <= 0:
        return

    keep_hashes = keep_hashes or set()
    entries = payload.setdefault("entries", {})
    auto_active = [
        (media_hash, record)
        for media_hash, record in entries.items()
        if isinstance(record, dict)
        and _source_from_record(record) == "auto"
        and _status_from_record(record) == "active"
    ]
    if len(auto_active) <= max_entries:
        return

    survivors = {
        media_hash
        for media_hash, _record in sorted(
            auto_active,
            key=lambda item: (_score_record(item[1]), item[0]),
            reverse=True,
        )[:max_entries]
    }
    survivors.update(keep_hashes)
    for media_hash, record in list(auto_active):
        if media_hash in survivors:
            continue
        _remove_library_file(context, runtime, record)
        entries.pop(media_hash, None)


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
    if not bool(_media_cfg_value(runtime, "enable_auto_collect_inbound_emoji", True)):
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
    original_payload = deepcopy(payload)
    entries = payload.setdefault("entries", {})
    perceptual_hash = _average_hash(source_path)
    record_key = rendered.media_hash
    existing = entries.get(record_key)
    if not isinstance(existing, dict):
        similar = _find_similar_entry(
            context,
            runtime,
            entries,
            source_path=source_path,
            perceptual_hash=perceptual_hash,
        )
        if similar is not None:
            record_key, existing = similar
    existing = existing if isinstance(existing, dict) else None
    status = (
        "pending"
        if bool(_media_cfg_value(runtime, "emoji_auto_collect_requires_approval", False))
        else "active"
    )
    if existing is not None:
        status = _status_from_record(existing)
    source = "auto"

    suffix = source_path.suffix.lower()
    if suffix not in _SUPPORTED_IMAGE_SUFFIXES:
        suffix = ".png"
    base_dir = library_dir if status == "active" else (_pending_emoji_library_dir(context, runtime) or library_dir)
    ensure_dir(base_dir)
    target_path = _safe_target_file_path(
        context,
        runtime,
        existing=existing,
        base_dir=base_dir,
        record_key=record_key,
        suffix=suffix,
    )
    is_new = existing is None
    if not target_path.exists():
        _copy_into_library_if_needed(source_path, target_path)

    entry = _entry_from_render(
        context,
        target_path,
        rendered,
        existing,
    )
    normalized = _normalize_entry_record(
        context=context,
        file_path=target_path,
        entry=entry,
        existing=existing,
        status=status,
        source=source,
        perceptual_hash=perceptual_hash,
        touch_collection=True,
    )
    normalized["media_hash"] = record_key
    entries[record_key] = normalized
    _prune_auto_entries(context, runtime, payload, keep_hashes={record_key})
    _save_index_if_changed(
        context,
        runtime,
        original_payload=original_payload,
        payload=payload,
    )
    return entry, is_new


def schedule_emoji_library_repair(context, runtime) -> bool:
    task_key = _emoji_library_task_key(context, runtime)
    if not task_key or task_key in _EMOJI_REPAIR_TASKS:
        return False
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False

    _EMOJI_REPAIR_TASKS.add(task_key)

    async def _run() -> None:
        try:
            await load_emoji_library(
                context,
                runtime,
                repair_invalid=True,
                schedule_background_repair=False,
            )
        finally:
            _EMOJI_REPAIR_TASKS.discard(task_key)

    try:
        _spawn_bg_task(context, _run(), name=f"emoji_library_repair:{Path(task_key).name}")
    except Exception:
        _EMOJI_REPAIR_TASKS.discard(task_key)
        raise
    return True


async def load_emoji_library(
    context,
    runtime,
    *,
    repair_invalid: bool = True,
    schedule_background_repair: bool = False,
) -> list[EmojiLibraryEntry]:
    library_dir = resolve_emoji_library_dir(context, runtime)
    if library_dir is None:
        return []

    cache_key = _library_cache_key(context, runtime)
    cache_signature = _library_signature(context, runtime)
    if repair_invalid and not schedule_background_repair and cache_key and cache_signature is not None:
        cached = _LIBRARY_CACHE.get(cache_key)
        if cached is not None and cached[0] == cache_signature:
            return list(cached[1])

    payload = _load_index(context, runtime)
    original_payload = deepcopy(payload)
    files = _iter_library_files(library_dir)
    if not files:
        if payload.get("entries"):
            payload["entries"] = {}
            _save_index_if_changed(
                context,
                runtime,
                original_payload=original_payload,
                payload=payload,
            )
        return []

    existing_entries = payload.setdefault("entries", {})
    retained_entries: dict[str, dict[str, Any]] = {}
    results: list[EmojiLibraryEntry] = []
    repair_needed = False

    for file_path in files:
        media_hash = _hash_file(file_path)
        existing = existing_entries.get(media_hash)
        status = _status_from_record(existing if isinstance(existing, dict) else None)
        source = _source_from_record(existing if isinstance(existing, dict) else None)
        perceptual_hash = str((existing or {}).get("perceptual_hash", "") or "").strip()
        if not perceptual_hash:
            perceptual_hash = _average_hash(file_path)
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
            if status == "pending":
                retained_entries[media_hash] = _normalize_entry_record(
                    context=context,
                    file_path=file_path,
                    entry=EmojiLibraryEntry(
                        media_hash=media_hash,
                        file_path=_to_plugin_relative_path(context, file_path),
                        description=str((existing or {}).get("description", "") or "").strip(),
                        emotion_tags=tuple(
                            str(item) for item in (existing or {}).get("emotion_tags", []) if str(item).strip()
                        ),
                        usage_count=int((existing or {}).get("usage_count", 0) or 0),
                        last_used_ts=float((existing or {}).get("last_used_ts", 0.0) or 0.0),
                        marker=str((existing or {}).get("marker", "") or "").strip(),
                    ),
                    existing=existing if isinstance(existing, dict) else None,
                    status=status,
                    source=source,
                    perceptual_hash=perceptual_hash,
                    touch_collection=False,
                )
                continue
            repair_needed = True
            if not repair_invalid:
                if isinstance(existing, dict):
                    retained = dict(existing)
                    retained["media_hash"] = media_hash
                    retained["file_path"] = _to_plugin_relative_path(context, file_path)
                    if perceptual_hash:
                        retained["perceptual_hash"] = perceptual_hash
                    retained_entries[media_hash] = retained
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
        retained_entries[entry.media_hash] = _normalize_entry_record(
            context=context,
            file_path=file_path,
            entry=entry,
            existing=existing if isinstance(existing, dict) else None,
            status=status,
            source=source,
            perceptual_hash=perceptual_hash,
            touch_collection=False,
        )
        if status == "active":
            results.append(entry)

    payload["entries"] = retained_entries
    _save_index_if_changed(
        context,
        runtime,
        original_payload=original_payload,
        payload=payload,
    )
    if repair_needed and schedule_background_repair and not repair_invalid:
        schedule_emoji_library_repair(context, runtime)
    if repair_invalid and not schedule_background_repair and cache_key:
        updated_signature = _library_signature(context, runtime)
        if updated_signature is not None:
            _LIBRARY_CACHE[cache_key] = (updated_signature, list(results))
    return results


def mark_emoji_used(context, runtime, entry: EmojiLibraryEntry) -> None:
    mark_emoji_used_by_hash(context, runtime, entry.media_hash)


def mark_emoji_used_by_hash(context, runtime, media_hash: str) -> None:
    normalized_hash = str(media_hash or "").strip()
    if not normalized_hash:
        return
    payload = _load_index(context, runtime)
    entries = payload.setdefault("entries", {})
    current = entries.get(normalized_hash)
    if not isinstance(current, dict):
        return
    current["usage_count"] = int(current.get("usage_count", 0) or 0) + 1
    current["last_used_ts"] = float(time.time())
    _save_index(context, runtime, payload)
