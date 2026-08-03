"""合并内置 QQ 表情名称与运行时观察记录，并维护有界目录缓存。

内置 JSON 是只读基线，用户目录只持久化使用次数、时间或新增别名，不能把整份内置
清单复制到 data 目录。缓存键按数据目录隔离，并同时校验文件纳秒时间与大小；损坏的
计数、时间或顶层结构按安全默认值修复，不能阻断消息处理。
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.plugin_base import ensure_dir, load_json, write_json

from .qq_face import _clean_face_text

_LEGACY_QQ_FACE_LABEL_OVERRIDES: dict[str, tuple[str, ...]] = {
    "14": ("微笑",),
    "277": ("狗头",),
    "278": ("点赞", "赞"),
    "279": ("踩",),
}
_BUNDLED_QQ_FACE_LABELS: dict[str, tuple[str, ...]] | None = None
_CATALOG_CACHE_MAX_ENTRIES = 32
_CATALOG_CACHE: OrderedDict[
    str,
    tuple[tuple[int, int, int, int], list[QQFaceEntry]],
] = OrderedDict()


@dataclass(frozen=True)
class QQFaceEntry:
    face_id: str
    label: str
    aliases: tuple[str, ...]
    usage_count: int
    last_used_ts: float
    marker: str


def _catalog_path(context) -> Path:
    return (Path(context.data_dir) / "media" / "qq_face_catalog.json").resolve()


def _bundled_catalog_path() -> Path:
    return Path(__file__).with_name("qq_face_builtin_catalog.json").resolve()


def _load_payload(context) -> dict[str, Any]:
    loaded: Any = load_json(_catalog_path(context), default={"entries": {}})
    payload: dict[str, Any] = loaded if isinstance(loaded, dict) else {"entries": {}}
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        payload["entries"] = {}
    return payload


def _save_payload(context, payload: dict[str, Any]) -> None:
    path = _catalog_path(context)
    ensure_dir(path.parent)
    write_json(path, payload)
    _invalidate_catalog_cache(context)


def _catalog_cache_key(context) -> str:
    try:
        return str(_catalog_path(context).resolve())
    except OSError:
        return str(_catalog_path(context))


def _catalog_signature(context) -> tuple[int, int, int, int] | None:
    catalog_path = _catalog_path(context)
    bundled_path = _bundled_catalog_path()
    try:
        catalog_stat = catalog_path.stat() if catalog_path.exists() else None
        bundled_stat = bundled_path.stat() if bundled_path.exists() else None
    except OSError:
        return None
    return (
        catalog_stat.st_mtime_ns if catalog_stat is not None else -1,
        catalog_stat.st_size if catalog_stat is not None else -1,
        bundled_stat.st_mtime_ns if bundled_stat is not None else -1,
        bundled_stat.st_size if bundled_stat is not None else -1,
    )


def _invalidate_catalog_cache(context) -> None:
    key = _catalog_cache_key(context)
    if key:
        _CATALOG_CACHE.pop(key, None)


def _store_catalog_cache(
    key: str,
    signature: tuple[int, int, int, int],
    entries: list[QQFaceEntry],
) -> None:
    _CATALOG_CACHE.pop(key, None)
    _CATALOG_CACHE[key] = (signature, list(entries))
    while len(_CATALOG_CACHE) > _CATALOG_CACHE_MAX_ENTRIES:
        _CATALOG_CACHE.popitem(last=False)


def _save_payload_if_changed(
    context, *, original_payload: dict[str, Any], payload: dict[str, Any]
) -> None:
    if payload == original_payload:
        return
    _save_payload(context, payload)


def _load_bundled_qq_face_labels() -> dict[str, tuple[str, ...]]:
    global _BUNDLED_QQ_FACE_LABELS
    if _BUNDLED_QQ_FACE_LABELS is not None:
        return _BUNDLED_QQ_FACE_LABELS

    payload: Any = load_json(_bundled_catalog_path(), default={"entries": {}})
    raw_entries = payload.get("entries") if isinstance(payload, dict) else {}
    entries: dict[str, tuple[str, ...]] = {}
    if isinstance(raw_entries, dict):
        for face_id, raw_labels in raw_entries.items():
            normalized_id = str(face_id or "").strip()
            if not normalized_id:
                continue
            labels = _normalize_labels(raw_labels, face_id=normalized_id, allow_placeholder=False)
            if labels:
                entries[normalized_id] = labels

    for face_id, override_labels in _LEGACY_QQ_FACE_LABEL_OVERRIDES.items():
        merged = list(_normalize_labels(override_labels, face_id=face_id, allow_placeholder=False))
        for label in entries.get(face_id, ()):
            if label not in merged:
                merged.append(label)
        if merged:
            entries[face_id] = tuple(merged)

    _BUNDLED_QQ_FACE_LABELS = entries
    return entries


def _placeholder_label(face_id: str) -> str:
    normalized_id = str(face_id or "").strip()
    if normalized_id:
        return f"系统表情#{normalized_id}"
    return "系统表情"


def _normalize_labels(
    values: Any,
    *,
    face_id: str = "",
    allow_placeholder: bool = False,
) -> tuple[str, ...]:
    labels: list[str] = []
    if isinstance(values, set):
        items = sorted(values, key=str)
    elif isinstance(values, (list, tuple)):
        items = list(values)
    else:
        items = [values]
    for item in items:
        label = _clean_face_text(item)
        if not label:
            continue
        if label.startswith("id=") or label == "系统表情":
            if not allow_placeholder:
                continue
            label = _placeholder_label(label[3:] if label.startswith("id=") else face_id)
        if label not in labels:
            labels.append(label)
    return tuple(labels)


def _nonnegative_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, parsed)


def _nonnegative_float(value: Any) -> float:
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return max(0.0, parsed) if math.isfinite(parsed) else 0.0


def _entry_from_payload(face_id: str, data: dict[str, Any]) -> QQFaceEntry | None:
    labels = _normalize_labels(data.get("labels", []), face_id=face_id, allow_placeholder=True)
    if not labels:
        if not str(face_id or "").strip():
            return None
        labels = (_placeholder_label(face_id),)
    label = labels[0]
    return QQFaceEntry(
        face_id=face_id,
        label=label,
        aliases=labels,
        usage_count=_nonnegative_int(data.get("usage_count", 0)),
        last_used_ts=_nonnegative_float(data.get("last_used_ts", 0.0)),
        marker=f"[QQ表情：{label}]",
    )


def record_face_observation(context, *, face_id: Any, label: Any) -> None:
    normalized_id = str(face_id or "").strip()
    labels = _normalize_labels(label, face_id=normalized_id, allow_placeholder=True)
    if not normalized_id or not labels:
        return

    payload = _load_payload(context)
    original_payload = deepcopy(payload)
    entries = payload.setdefault("entries", {})
    current = entries.get(normalized_id)
    existing_labels = _normalize_labels(
        current.get("labels", []) if isinstance(current, dict) else [],
        face_id=normalized_id,
        allow_placeholder=True,
    )
    merged: list[str] = list(existing_labels)
    for item in labels:
        if item not in merged:
            merged.append(item)
    entries[normalized_id] = {
        "labels": merged,
        "usage_count": _nonnegative_int(current.get("usage_count", 0))
        if isinstance(current, dict)
        else 0,
        "last_used_ts": _nonnegative_float(current.get("last_used_ts", 0.0))
        if isinstance(current, dict)
        else 0.0,
    }
    _save_payload_if_changed(context, original_payload=original_payload, payload=payload)


async def load_qq_face_catalog(context) -> list[QQFaceEntry]:
    cache_key = _catalog_cache_key(context)
    cache_signature = _catalog_signature(context)
    if cache_key and cache_signature is not None:
        cached = _CATALOG_CACHE.get(cache_key)
        if cached is not None and cached[0] == cache_signature:
            _CATALOG_CACHE.move_to_end(cache_key)
            return list(cached[1])

    payload = _load_payload(context)
    original_payload = deepcopy(payload)
    observed_entries = payload.setdefault("entries", {})
    observed_ids: set[str] = set()

    bundled_catalog = _load_bundled_qq_face_labels()
    merged_entries: dict[str, dict[str, Any]] = {}
    for face_id, labels in bundled_catalog.items():
        merged_entries[face_id] = {"labels": list(labels), "usage_count": 0, "last_used_ts": 0.0}

    for face_id, data in observed_entries.items():
        normalized_id = str(face_id or "").strip()
        if not normalized_id or not isinstance(data, dict):
            continue
        observed_ids.add(normalized_id)
        existing = merged_entries.setdefault(
            normalized_id, {"labels": [], "usage_count": 0, "last_used_ts": 0.0}
        )
        observed_labels = list(
            _normalize_labels(data.get("labels", []), face_id=normalized_id, allow_placeholder=True)
        )
        for label in observed_labels:
            if label not in existing["labels"]:
                existing["labels"].append(label)
        existing["usage_count"] = _nonnegative_int(
            data.get("usage_count", existing.get("usage_count", 0))
        )
        existing["last_used_ts"] = _nonnegative_float(
            data.get("last_used_ts", existing.get("last_used_ts", 0.0))
        )

    results: list[QQFaceEntry] = []
    persisted_entries: dict[str, Any] = {}
    for face_id, data in merged_entries.items():
        entry = _entry_from_payload(face_id, data)
        if entry is None:
            continue
        results.append(entry)
        bundled_labels = bundled_catalog.get(face_id, ())
        has_observed_value = (
            face_id not in bundled_catalog
            or entry.usage_count > 0
            or entry.last_used_ts > 0
            or any(label not in bundled_labels for label in entry.aliases)
        )
        if face_id in observed_ids and has_observed_value:
            persisted_entries[face_id] = {
                "labels": list(entry.aliases),
                "usage_count": entry.usage_count,
                "last_used_ts": entry.last_used_ts,
            }

    payload["entries"] = persisted_entries
    _save_payload_if_changed(context, original_payload=original_payload, payload=payload)
    results.sort(key=lambda item: (item.usage_count, item.last_used_ts, item.face_id))
    updated_signature = _catalog_signature(context)
    if cache_key and updated_signature is not None:
        _store_catalog_cache(cache_key, updated_signature, results)
    return results


def mark_qq_face_used(context, entry: QQFaceEntry) -> None:
    mark_qq_face_used_by_id(context, entry.face_id, label=entry.label)


def mark_qq_face_used_by_id(context, face_id: Any, *, label: Any = "") -> None:
    normalized_id = str(face_id or "").strip()
    if not normalized_id:
        return
    payload = _load_payload(context)
    entries = payload.setdefault("entries", {})
    current = entries.get(normalized_id)
    labels = list(
        _normalize_labels(
            current.get("labels", []) if isinstance(current, dict) else [],
            face_id=normalized_id,
            allow_placeholder=True,
        )
    )
    normalized_labels = _normalize_labels(label, face_id=normalized_id, allow_placeholder=True)
    for item in normalized_labels:
        if item not in labels:
            labels.append(item)
    entries[normalized_id] = {
        "labels": labels,
        "usage_count": _nonnegative_int(current.get("usage_count", 0)) + 1
        if isinstance(current, dict)
        else 1,
        "last_used_ts": float(time.time()),
    }
    _save_payload(context, payload)
