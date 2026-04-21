from __future__ import annotations

import random
import time
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
    payload = load_json(_catalog_path(context), default={"entries": {}})
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        payload["entries"] = {}
    return payload


def _save_payload(context, payload: dict[str, Any]) -> None:
    path = _catalog_path(context)
    ensure_dir(path.parent)
    write_json(path, payload)


def _load_bundled_qq_face_labels() -> dict[str, tuple[str, ...]]:
    global _BUNDLED_QQ_FACE_LABELS
    if _BUNDLED_QQ_FACE_LABELS is not None:
        return _BUNDLED_QQ_FACE_LABELS

    payload = load_json(_bundled_catalog_path(), default={"entries": {}})
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
    if isinstance(values, (list, tuple, set)):
        items = values
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
        usage_count=int(data.get("usage_count", 0) or 0),
        last_used_ts=float(data.get("last_used_ts", 0.0) or 0.0),
        marker=f"[QQ表情：{label}]",
    )


def record_face_observation(context, *, face_id: Any, label: Any) -> None:
    normalized_id = str(face_id or "").strip()
    labels = _normalize_labels(label, face_id=normalized_id, allow_placeholder=True)
    if not normalized_id or not labels:
        return

    payload = _load_payload(context)
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
        "usage_count": int(current.get("usage_count", 0) or 0) if isinstance(current, dict) else 0,
        "last_used_ts": float(current.get("last_used_ts", 0.0) or 0.0) if isinstance(current, dict) else 0.0,
    }
    _save_payload(context, payload)


async def load_qq_face_catalog(context, runtime=None) -> list[QQFaceEntry]:
    payload = _load_payload(context)
    observed_entries = payload.setdefault("entries", {})

    merged_entries: dict[str, dict[str, Any]] = {}
    for face_id, labels in _load_bundled_qq_face_labels().items():
        merged_entries[face_id] = {"labels": list(labels), "usage_count": 0, "last_used_ts": 0.0}

    for face_id, data in observed_entries.items():
        normalized_id = str(face_id or "").strip()
        if not normalized_id or not isinstance(data, dict):
            continue
        existing = merged_entries.setdefault(normalized_id, {"labels": [], "usage_count": 0, "last_used_ts": 0.0})
        labels = list(
            _normalize_labels(data.get("labels", []), face_id=normalized_id, allow_placeholder=True)
        )
        for label in labels:
            if label not in existing["labels"]:
                existing["labels"].append(label)
        existing["usage_count"] = int(data.get("usage_count", existing.get("usage_count", 0)) or 0)
        existing["last_used_ts"] = float(data.get("last_used_ts", existing.get("last_used_ts", 0.0)) or 0.0)

    results: list[QQFaceEntry] = []
    persisted_entries: dict[str, Any] = {}
    for face_id, data in merged_entries.items():
        entry = _entry_from_payload(face_id, data)
        if entry is None:
            continue
        results.append(entry)
        persisted_entries[face_id] = {
            "labels": list(entry.aliases),
            "usage_count": entry.usage_count,
            "last_used_ts": entry.last_used_ts,
        }

    payload["entries"] = persisted_entries
    _save_payload(context, payload)
    results.sort(key=lambda item: (item.usage_count, item.last_used_ts, item.face_id))
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
        "usage_count": int(current.get("usage_count", 0) or 0) + 1 if isinstance(current, dict) else 1,
        "last_used_ts": float(time.time()),
    }
    _save_payload(context, payload)
