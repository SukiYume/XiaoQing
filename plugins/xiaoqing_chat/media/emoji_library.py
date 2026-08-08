"""持久化按隐私范围隔离的表情库，并修复其派生元数据。

收到的表情只复制到插件数据目录，自动收集项只对来源会话可见。索引路径均视为
不可信，任何文件系统变更前都要验证其位于允许的表情库根目录内。
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import threading
import time
from collections import OrderedDict
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.image_validation import ImageValidationError, validate_image_path
from core.plugin_base import ensure_dir, load_json, write_json

from ..task_scheduler import _spawn_bg_task
from .event_media_common import (
    _image_validation_limits,
    _is_generic_media_label,
    _looks_like_structured_media_text,
    _run_media_blocking,
)

_SUPPORTED_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
_PENDING_DIR_NAME = "pending"
_REJECTED_DIR_NAME = "rejected"
_EMOJI_REPAIR_TASKS: set[str] = set()
_LIBRARY_CACHE_MAX_ENTRIES = 32
_LIBRARY_CACHE: OrderedDict[
    str,
    tuple[tuple[int, int], list[EmojiLibraryEntry]],
] = OrderedDict()
_LIBRARY_CACHE_LOCK = threading.RLock()

if TYPE_CHECKING:
    from ..runtime_state import _ChatRuntime
    from .event_media_common import RenderedMedia


@dataclass(frozen=True)
class EmojiLibraryEntry:
    """单个有效表情库条目的不可变、提示词安全视图。"""

    media_hash: str
    file_path: str
    description: str
    emotion_tags: tuple[str, ...]
    usage_count: int
    last_used_ts: float
    marker: str
    source_chat_ids: tuple[str, ...] = ()
    owner_id: str = ""
    visibility: str = "chat"
    global_approved: bool = False


@dataclass(frozen=True)
class _ScannedEmojiFile:
    file_path: Path
    media_hash: str
    existing_record: dict[str, Any] | None
    perceptual_hash: str
    file_size: int
    file_mtime_ns: int


def _emoji_library_task_key(context) -> str:
    library_dir = resolve_emoji_library_dir(context)
    return str(library_dir.resolve())


def _to_data_relative_path(context, path: Path) -> str:
    root = Path(context.data_dir).resolve()
    target = path.resolve()
    try:
        return target.relative_to(root).as_posix()
    except ValueError:
        return target.as_posix()


def resolve_emoji_file_path(context, file_path: str) -> Path:
    """解析索引路径，但不因此授予访问权限。

    调用方在读取、移动或删除结果前，仍须确认路径属于
    :func:`_allowed_emoji_target_dirs`。
    """

    path = Path(file_path)
    if path.is_absolute():
        return path
    return (Path(context.data_dir) / path).resolve()


def _emoji_index_path(context) -> Path:
    return resolve_emoji_library_dir(context) / "index.json"


def _library_cache_key(context) -> str:
    library_dir = resolve_emoji_library_dir(context)
    try:
        return str(library_dir.resolve())
    except OSError:
        return str(library_dir)


def _library_signature(context) -> tuple[int, int] | None:
    library_dir = resolve_emoji_library_dir(context)
    index_path = library_dir / "index.json"
    try:
        dir_mtime = library_dir.stat().st_mtime_ns if library_dir.exists() else -1
        index_mtime = index_path.stat().st_mtime_ns if index_path.exists() else -1
    except OSError:
        return None
    return int(dir_mtime), int(index_mtime)


def _invalidate_library_cache(context) -> None:
    key = _library_cache_key(context)
    if key:
        with _LIBRARY_CACHE_LOCK:
            _LIBRARY_CACHE.pop(key, None)


def _get_library_cache(
    key: str,
    signature: tuple[int, int],
) -> list[EmojiLibraryEntry] | None:
    with _LIBRARY_CACHE_LOCK:
        cached = _LIBRARY_CACHE.get(key)
        if cached is None:
            return None
        if cached[0] != signature:
            _LIBRARY_CACHE.pop(key, None)
            return None
        _LIBRARY_CACHE.move_to_end(key)
        return list(cached[1])


def _store_library_cache(
    key: str,
    signature: tuple[int, int],
    entries: list[EmojiLibraryEntry],
) -> None:
    with _LIBRARY_CACHE_LOCK:
        _LIBRARY_CACHE[key] = (signature, list(entries))
        _LIBRARY_CACHE.move_to_end(key)
        while len(_LIBRARY_CACHE) > _LIBRARY_CACHE_MAX_ENTRIES:
            _LIBRARY_CACHE.popitem(last=False)


def _load_index(context) -> dict[str, Any]:
    loaded: object = load_json(_emoji_index_path(context), default={"entries": {}})
    payload = loaded if isinstance(loaded, dict) else {"entries": {}}
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        payload["entries"] = {}
    return payload


def _save_index(context, payload: dict[str, Any]) -> None:
    index_path = _emoji_index_path(context)
    ensure_dir(index_path.parent)
    write_json(index_path, payload)
    _invalidate_library_cache(context)


def _save_index_if_changed(
    context,
    *,
    original_payload: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    if payload == original_payload:
        return
    _save_index(context, payload)


def resolve_emoji_library_dir(context) -> Path:
    """返回表情库唯一使用且限定在数据根目录内的目录。"""

    return (Path(context.data_dir) / "media" / "library").resolve()


def _iter_library_files(root: Path) -> list[Path]:
    if not root.exists() or not root.is_dir():
        return []
    files = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in _SUPPORTED_IMAGE_SUFFIXES
        and _REJECTED_DIR_NAME not in path.relative_to(root).parts
    ]
    files.sort()
    return files


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _entry_from_render(
    context,
    file_path: Path,
    rendered: RenderedMedia,
    existing: dict[str, Any] | None = None,
) -> EmojiLibraryEntry:
    existing = existing or {}
    usage_count = int(existing.get("usage_count", 0) or 0)
    last_used_ts = float(existing.get("last_used_ts", 0.0) or 0.0)
    return EmojiLibraryEntry(
        media_hash=rendered.media_hash,
        file_path=_to_data_relative_path(context, file_path),
        description=rendered.description,
        emotion_tags=tuple(rendered.emotion_tags),
        usage_count=usage_count,
        last_used_ts=last_used_ts,
        marker=rendered.marker,
    )


def _pending_emoji_library_dir(context) -> Path:
    root = resolve_emoji_library_dir(context)
    return root / _PENDING_DIR_NAME


def _allowed_emoji_target_dirs(context) -> tuple[Path, ...]:
    library_dir = resolve_emoji_library_dir(context)
    pending_dir = _pending_emoji_library_dir(context)
    return library_dir.resolve(), pending_dir.resolve()


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
    status = str((record or {}).get("status", "") or "pending").strip().lower()
    return status if status in {"active", "pending"} else "pending"


def _source_from_record(record: dict[str, Any] | None) -> str:
    source = str((record or {}).get("source", "") or "auto").strip().lower()
    return source if source in {"manual", "auto"} else "auto"


def _is_pending_library_file(context, file_path: Path) -> bool:
    pending_dir = _pending_emoji_library_dir(context)
    try:
        file_path.resolve().relative_to(pending_dir.resolve())
        return True
    except (OSError, ValueError):
        return False


def _average_hash(path: Path) -> str:
    """从已经通过媒体边界的文件中提取感知特征。"""

    try:
        from PIL import Image, ImageOps
    except ImportError:
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


def _is_valid_library_image(
    path: Path,
    *,
    max_bytes: int,
    max_pixels: int,
    max_frames: int,
) -> bool:
    try:
        validate_image_path(
            path,
            limits=_image_validation_limits(
                max_bytes=max_bytes,
                max_pixels=max_pixels,
                max_frames=max_frames,
            ),
        )
    except (ImageValidationError, OSError):
        return False
    return True


def _file_identity(path: Path) -> tuple[int, int]:
    file_stat = path.stat()
    return int(file_stat.st_size), int(file_stat.st_mtime_ns)


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value.lower())


def _record_matches_file_identity(record: dict[str, Any], size: int, mtime_ns: int) -> bool:
    try:
        return (
            int(record.get("file_size", -1)) == size
            and int(record.get("file_mtime_ns", -1)) == mtime_ns
        )
    except (TypeError, ValueError):
        return False


def _read_library_snapshot(
    context,
    library_dir: Path,
    *,
    max_bytes: int,
    max_pixels: int,
    max_frames: int,
) -> tuple[dict[str, Any], dict[str, Any], list[_ScannedEmojiFile]]:
    """在有界工作线程中读取表情库快照并计算文件指纹。"""

    payload = _load_index(context)
    original_payload = deepcopy(payload)
    existing_entries = payload.setdefault("entries", {})
    records_by_path: dict[str, tuple[str, dict[str, Any]]] = {}
    for record_key, raw_record in existing_entries.items():
        if not isinstance(raw_record, dict):
            continue
        raw_path = str(raw_record.get("file_path", "") or "").strip()
        if not raw_path:
            continue
        try:
            resolved = str(resolve_emoji_file_path(context, raw_path).resolve())
        except OSError:
            continue
        records_by_path[resolved] = str(record_key), raw_record

    scanned: list[_ScannedEmojiFile] = []
    for file_path in _iter_library_files(library_dir):
        if not _is_valid_library_image(
            file_path,
            max_bytes=max_bytes,
            max_pixels=max_pixels,
            max_frames=max_frames,
        ):
            continue
        file_size, file_mtime_ns = _file_identity(file_path)
        matched = records_by_path.get(str(file_path.resolve()))
        matched_key = matched[0] if matched else ""
        matched_record = matched[1] if matched else None
        unchanged = (
            matched_record is not None
            and _is_sha256(matched_key)
            and str(matched_record.get("media_hash", matched_key) or "").strip() == matched_key
            and _record_matches_file_identity(matched_record, file_size, file_mtime_ns)
        )
        media_hash = matched_key if unchanged else _hash_file(file_path)
        existing = existing_entries.get(media_hash)
        existing_record = existing if isinstance(existing, dict) else None
        perceptual_hash = str((existing_record or {}).get("perceptual_hash", "") or "").strip()
        if not perceptual_hash:
            perceptual_hash = _average_hash(file_path)
        scanned.append(
            _ScannedEmojiFile(
                file_path=file_path,
                media_hash=media_hash,
                existing_record=existing_record,
                perceptual_hash=perceptual_hash,
                file_size=file_size,
                file_mtime_ns=file_mtime_ns,
            )
        )
    return payload, original_payload, scanned


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
    source_chat_id: str = "",
    source_user_id: str = "",
    file_size: int | None = None,
    file_mtime_ns: int | None = None,
) -> dict[str, Any]:
    """合并条目，同时保留可见范围、所有权和使用历史。

    待审核记录强制限定在会话内；普通收集操作可以补充来源会话，但绝不能把条目
    提升为全局可见。
    """

    existing = existing or {}
    first_collected_ts = float(existing.get("first_collected_ts", 0.0) or 0.0) or float(time.time())
    seen_count = int(existing.get("seen_count", 0) or 0)
    if touch_collection:
        seen_count += 1
    last_collected_ts = float(existing.get("last_collected_ts", 0.0) or 0.0)
    if touch_collection:
        last_collected_ts = float(time.time())
    source_chat_ids = [
        str(item).strip() for item in existing.get("source_chat_ids", []) if str(item).strip()
    ]
    legacy_chat_id = str(existing.get("source_chat_id", "") or "").strip()
    if legacy_chat_id and legacy_chat_id not in source_chat_ids:
        source_chat_ids.append(legacy_chat_id)
    normalized_source_chat_id = str(source_chat_id or "").strip()
    if normalized_source_chat_id and normalized_source_chat_id not in source_chat_ids:
        source_chat_ids.append(normalized_source_chat_id)
    visibility = str(existing.get("visibility", "") or "").strip().lower()
    if visibility not in {"chat", "global"}:
        visibility = "chat"
    global_approved = existing.get("global_approved") is True
    if status != "active":
        visibility = "chat"
        global_approved = False
    if file_size is None or file_mtime_ns is None:
        try:
            file_size, file_mtime_ns = _file_identity(file_path)
        except OSError:
            file_size, file_mtime_ns = -1, -1
    return {
        "media_hash": entry.media_hash,
        "file_path": _to_data_relative_path(context, file_path),
        "description": entry.description,
        "emotion_tags": list(entry.emotion_tags),
        "usage_count": entry.usage_count,
        "last_used_ts": entry.last_used_ts,
        "marker": entry.marker,
        "status": status,
        "source": source,
        "perceptual_hash": perceptual_hash or str(existing.get("perceptual_hash", "") or ""),
        "file_size": int(file_size),
        "file_mtime_ns": int(file_mtime_ns),
        "first_collected_ts": first_collected_ts,
        "last_collected_ts": last_collected_ts,
        "seen_count": seen_count,
        "source_chat_id": source_chat_ids[0] if source_chat_ids else "",
        "source_chat_ids": source_chat_ids,
        "owner_id": str(existing.get("owner_id", "") or source_user_id or "").strip(),
        "visibility": visibility,
        "global_approved": bool(global_approved),
    }


def _score_record(record: dict[str, Any]) -> tuple[float, float, float]:
    usage_count = float(record.get("usage_count", 0) or 0.0)
    last_used_ts = float(record.get("last_used_ts", 0.0) or 0.0)
    last_collected_ts = float(record.get("last_collected_ts", 0.0) or 0.0)
    seen_count = float(record.get("seen_count", 0) or 0.0)
    return (usage_count * 3.0 + seen_count, last_used_ts, last_collected_ts)


def _remove_library_file(context, record: dict[str, Any]) -> None:
    file_path = resolve_emoji_file_path(context, str(record.get("file_path", "") or ""))
    allowed_roots = _allowed_emoji_target_dirs(context)
    if not allowed_roots:
        return
    if not _is_path_within_roots(file_path, allowed_roots):
        return
    if file_path.exists():
        # 自动清理是配额维护，单个文件正被占用时保留索引并在下轮重试。
        with suppress(OSError):
            file_path.unlink()


def _safe_target_file_path(
    context,
    *,
    existing: dict[str, Any] | None,
    base_dir: Path,
    record_key: str,
    suffix: str,
) -> Path:
    """只有索引路径仍位于允许根目录内时才复用。"""

    if existing is not None:
        raw_existing_path = str(existing.get("file_path", "") or "").strip()
        if raw_existing_path:
            existing_path = resolve_emoji_file_path(context, raw_existing_path)
            if _is_path_within_roots(existing_path, _allowed_emoji_target_dirs(context)):
                return existing_path
    return base_dir / f"{record_key}{suffix}"


def _copy_into_library_if_needed(source_path: Path, target_path: Path) -> None:
    # ``samefile`` 在任一路径刚被删除时可能失败，此时按普通复制路径继续。
    with suppress(OSError):
        if target_path.exists() and source_path.samefile(target_path):
            return
    ensure_dir(target_path.parent)
    shutil.copyfile(source_path, target_path)


def _find_similar_entry(
    context,
    runtime,
    entries: dict[str, Any],
    *,
    perceptual_hash: str,
    source_chat_id: str,
) -> tuple[str, dict[str, Any]] | None:
    """查找同一来源会话可见且感知相似的自动收集条目。

    相似度去重刻意不跨会话，防止一个会话获知另一会话提交了相同媒体。
    """

    threshold = max(0, runtime.cfg.media.emoji_auto_collect_similarity_threshold)
    if not perceptual_hash or threshold < 0:
        return None

    for media_hash, raw_record in entries.items():
        if not isinstance(raw_record, dict):
            continue
        if _source_from_record(raw_record) != "auto":
            continue
        visible_chats = {
            str(item).strip() for item in raw_record.get("source_chat_ids", []) if str(item).strip()
        }
        legacy_chat = str(raw_record.get("source_chat_id", "") or "").strip()
        if legacy_chat:
            visible_chats.add(legacy_chat)
        if source_chat_id and source_chat_id not in visible_chats:
            continue
        candidate_path = resolve_emoji_file_path(
            context, str(raw_record.get("file_path", "") or "")
        )
        if not candidate_path.exists():
            continue
        if not _is_path_within_roots(candidate_path, _allowed_emoji_target_dirs(context)):
            continue
        if not _is_valid_library_image(
            candidate_path,
            max_bytes=int(runtime.cfg.media.max_analyze_bytes),
            max_pixels=int(runtime.cfg.media.max_image_pixels),
            max_frames=int(runtime.cfg.media.max_animation_frames),
        ):
            continue
        candidate_hash = str(raw_record.get("perceptual_hash", "") or "").strip()
        if not candidate_hash:
            candidate_hash = _average_hash(candidate_path)
            raw_record["perceptual_hash"] = candidate_hash
        if _hamming_distance(perceptual_hash, candidate_hash) <= threshold:
            return str(media_hash), raw_record
    return None


def _prune_auto_entries(
    context,
    runtime: _ChatRuntime,
    payload: dict[str, Any],
    *,
    keep_hashes: set[str] | None = None,
) -> None:
    """执行自动条目容量限制，同时保留受保护键。

    只有索引路径通过与显式删除相同的根目录包含检查后，才会同时删除索引行和文件。
    """

    max_entries = runtime.cfg.media.emoji_auto_collect_max_entries
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
        _remove_library_file(context, record)
        entries.pop(media_hash, None)


def _is_usable_library_metadata(
    description: str, marker: str, emotion_tags: tuple[str, ...]
) -> bool:
    """拒绝不适合复用的泛化标签或机器结构化标签。"""

    desc = str(description or "").strip()
    mark = str(marker or "").strip()
    tags = tuple(str(item or "").strip() for item in emotion_tags if str(item or "").strip())

    if not desc or not mark:
        return False
    if _is_generic_media_label(desc) or _is_generic_media_label(mark):
        return False
    if _looks_like_structured_media_text(desc) or _looks_like_structured_media_text(mark):
        return False
    return not any(_looks_like_structured_media_text(tag) for tag in tags)


def collect_emoji_candidate(
    context,
    runtime,
    rendered: RenderedMedia,
    *,
    source_path: Path,
    source_chat_id: str = "",
    source_user_id: str = "",
) -> tuple[EmojiLibraryEntry, bool] | None:
    """以会话级可见范围收集一个符合条件的入站表情。

    来源必须是真实文件，分析结果也必须包含可复用的人类语义标签。完全相同或感知
    相似的条目只在同一会话范围内合并，自动收集不会提升条目的可见范围。
    """

    if rendered.kind != "emoji":
        return None
    if not runtime.cfg.media.enable_auto_collect_inbound_emoji:
        return None
    if not source_path.exists() or not source_path.is_file():
        return None
    if not _is_valid_library_image(
        source_path,
        max_bytes=int(runtime.cfg.media.max_analyze_bytes),
        max_pixels=int(runtime.cfg.media.max_image_pixels),
        max_frames=int(runtime.cfg.media.max_animation_frames),
    ):
        return None
    if not _is_usable_library_metadata(
        rendered.description, rendered.marker, tuple(rendered.emotion_tags)
    ):
        return None

    library_dir = resolve_emoji_library_dir(context)

    ensure_dir(library_dir)
    payload = _load_index(context)
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
            perceptual_hash=perceptual_hash,
            source_chat_id=str(source_chat_id or "").strip(),
        )
        if similar is not None:
            record_key, existing = similar
    existing = existing if isinstance(existing, dict) else None
    status = "pending" if runtime.cfg.media.emoji_auto_collect_requires_approval else "active"
    if existing is not None:
        status = _status_from_record(existing)
    source = "auto"

    suffix = source_path.suffix.lower()
    if suffix not in _SUPPORTED_IMAGE_SUFFIXES:
        suffix = ".png"
    base_dir = library_dir if status == "active" else _pending_emoji_library_dir(context)
    ensure_dir(base_dir)
    target_path = _safe_target_file_path(
        context,
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
        source_chat_id=source_chat_id,
        source_user_id=source_user_id,
    )
    normalized["media_hash"] = record_key
    entries[record_key] = normalized
    _prune_auto_entries(context, runtime, payload, keep_hashes={record_key})
    _save_index_if_changed(
        context,
        original_payload=original_payload,
        payload=payload,
    )
    return entry, is_new


def schedule_emoji_library_repair(context, runtime) -> bool:
    """每个表情库根目录最多调度一个非阻塞元数据修复任务。"""

    task_key = _emoji_library_task_key(context)
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
    chat_id: str | None = None,
) -> list[EmojiLibraryEntry]:
    """加载有效条目，并可在有界缓存下修复元数据。

    只有表情库目录和索引的修改时间都未变化时缓存才有效。无效或不匹配的索引行在
    重新分析成功前都视为待处理。``chat_id`` 过滤始终在缓存查询后执行，防止缓存
    绕过会话级可见性。
    """

    library_dir = resolve_emoji_library_dir(context)

    cache_key = _library_cache_key(context)
    cache_signature = await _run_media_blocking(_library_signature, context)
    if (
        repair_invalid
        and not schedule_background_repair
        and cache_key
        and cache_signature is not None
    ):
        cached = _get_library_cache(cache_key, cache_signature)
        if cached is not None:
            return _filter_visible_entries(cached, chat_id)

    payload, original_payload, scanned_files = await _run_media_blocking(
        _read_library_snapshot,
        context,
        library_dir,
        max_bytes=int(runtime.cfg.media.max_analyze_bytes),
        max_pixels=int(runtime.cfg.media.max_image_pixels),
        max_frames=int(runtime.cfg.media.max_animation_frames),
    )
    if not scanned_files:
        if payload.get("entries"):
            payload["entries"] = {}
            await _run_media_blocking(
                _save_index_if_changed,
                context,
                original_payload=original_payload,
                payload=payload,
            )
        return []

    retained_entries: dict[str, dict[str, Any]] = {}
    results: list[EmojiLibraryEntry] = []
    repair_needed = False

    for scanned_file in scanned_files:
        file_path = scanned_file.file_path
        media_hash = scanned_file.media_hash
        existing_record = scanned_file.existing_record
        existing = existing_record or {}
        physical_pending = _is_pending_library_file(context, file_path)
        declared_status = str((existing_record or {}).get("status", "") or "").strip().lower()
        declared_path = str((existing_record or {}).get("file_path", "") or "").strip()
        path_matches = False
        if declared_path:
            try:
                path_matches = (
                    resolve_emoji_file_path(context, declared_path).resolve() == file_path.resolve()
                )
            except OSError:
                path_matches = False
        record_valid = (
            existing_record is not None
            and declared_status in {"active", "pending"}
            and path_matches
        )
        status = "pending" if physical_pending or not record_valid else declared_status
        source = _source_from_record(existing_record)
        perceptual_hash = scanned_file.perceptual_hash
        if isinstance(existing, dict) and _is_usable_library_metadata(
            str(existing.get("description", "") or "").strip(),
            str(existing.get("marker", "") or "").strip(),
            tuple(str(item) for item in existing.get("emotion_tags", []) if str(item).strip()),
        ):
            entry = EmojiLibraryEntry(
                media_hash=media_hash,
                file_path=_to_data_relative_path(context, file_path),
                description=str(existing.get("description", "") or "").strip(),
                emotion_tags=tuple(
                    str(item) for item in existing.get("emotion_tags", []) if str(item).strip()
                ),
                usage_count=int(existing.get("usage_count", 0) or 0),
                last_used_ts=float(existing.get("last_used_ts", 0.0) or 0.0),
                marker=str(existing.get("marker", "") or "").strip(),
                source_chat_ids=tuple(
                    str(item).strip()
                    for item in existing.get("source_chat_ids", [])
                    if str(item).strip()
                )
                or tuple(
                    [str(existing.get("source_chat_id", "") or "").strip()]
                    if str(existing.get("source_chat_id", "") or "").strip()
                    else []
                ),
                owner_id=str(existing.get("owner_id", "") or "").strip(),
                visibility=(
                    str(existing.get("visibility", "") or "chat").strip().lower()
                    if status == "active"
                    else "chat"
                ),
                global_approved=(
                    existing.get("global_approved") is True if status == "active" else False
                ),
            )
        else:
            repair_needed = True
            if not repair_invalid:
                retained_entries[media_hash] = _normalize_entry_record(
                    context=context,
                    file_path=file_path,
                    entry=EmojiLibraryEntry(
                        media_hash=media_hash,
                        file_path=_to_data_relative_path(context, file_path),
                        description=str((existing or {}).get("description", "") or "").strip(),
                        emotion_tags=tuple(
                            str(item)
                            for item in (existing or {}).get("emotion_tags", [])
                            if str(item).strip()
                        ),
                        usage_count=int((existing or {}).get("usage_count", 0) or 0),
                        last_used_ts=float((existing or {}).get("last_used_ts", 0.0) or 0.0),
                        marker=str((existing or {}).get("marker", "") or "").strip(),
                    ),
                    existing=existing_record,
                    status="pending",
                    source=source,
                    perceptual_hash=perceptual_hash,
                    touch_collection=False,
                    file_size=scanned_file.file_size,
                    file_mtime_ns=scanned_file.file_mtime_ns,
                )
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
            if not _is_usable_library_metadata(
                rendered.description, rendered.marker, tuple(rendered.emotion_tags)
            ):
                continue
            entry = _entry_from_render(
                context,
                file_path,
                rendered,
                existing_record,
            )
        normalized_record = _normalize_entry_record(
            context=context,
            file_path=file_path,
            entry=entry,
            existing=existing_record,
            status=status,
            source=source,
            perceptual_hash=perceptual_hash,
            touch_collection=False,
            file_size=scanned_file.file_size,
            file_mtime_ns=scanned_file.file_mtime_ns,
        )
        retained_entries[entry.media_hash] = normalized_record
        if status == "active":
            results.append(
                EmojiLibraryEntry(
                    media_hash=entry.media_hash,
                    file_path=entry.file_path,
                    description=entry.description,
                    emotion_tags=entry.emotion_tags,
                    usage_count=entry.usage_count,
                    last_used_ts=entry.last_used_ts,
                    marker=entry.marker,
                    source_chat_ids=tuple(normalized_record.get("source_chat_ids", [])),
                    owner_id=str(normalized_record.get("owner_id", "") or ""),
                    visibility=str(normalized_record.get("visibility", "chat") or "chat"),
                    global_approved=normalized_record.get("global_approved") is True,
                )
            )

    payload["entries"] = retained_entries
    await _run_media_blocking(
        _save_index_if_changed,
        context,
        original_payload=original_payload,
        payload=payload,
    )
    if repair_needed and schedule_background_repair and not repair_invalid:
        schedule_emoji_library_repair(context, runtime)
    if repair_invalid and not schedule_background_repair and cache_key:
        updated_signature = await _run_media_blocking(_library_signature, context)
        if updated_signature is not None:
            _store_library_cache(cache_key, updated_signature, results)
    return _filter_visible_entries(results, chat_id)


def _filter_visible_entries(
    entries: list[EmojiLibraryEntry],
    chat_id: str | None,
) -> list[EmojiLibraryEntry]:
    """只暴露显式全局条目，或来源属于当前会话的条目。"""

    scope = str(chat_id or "").strip()
    if not scope:
        return list(entries)
    return [
        entry
        for entry in entries
        if (entry.visibility == "global" and entry.global_approved)
        or scope in set(entry.source_chat_ids)
    ]


def mark_emoji_used(context, entry: EmojiLibraryEntry) -> None:
    """记录条目的成功复用，供排序和清理决策使用。"""

    mark_emoji_used_by_hash(context, entry.media_hash)


def mark_emoji_used_by_hash(context, media_hash: str) -> None:
    """更新已有哈希的使用元数据，不创建新记录。"""

    normalized_hash = str(media_hash or "").strip()
    if not normalized_hash:
        return
    payload = _load_index(context)
    entries = payload.setdefault("entries", {})
    current = entries.get(normalized_hash)
    if not isinstance(current, dict):
        return
    current["usage_count"] = int(current.get("usage_count", 0) or 0) + 1
    current["last_used_ts"] = float(time.time())
    _save_index(context, payload)
