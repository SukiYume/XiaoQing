from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.plugin_base import ensure_dir, load_json, write_json

from .qq_face import _clean_face_text

_BUILTIN_QQ_FACE_LABELS: dict[str, tuple[str, ...]] = {
    "14": ("微笑",),
    "277": ("狗头",),
    "278": ("点赞", "赞"),
    "279": ("踩",),
}


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


def _normalize_labels(values: Any) -> tuple[str, ...]:
    labels: list[str] = []
    if isinstance(values, (list, tuple, set)):
        items = values
    else:
        items = [values]
    for item in items:
        label = _clean_face_text(item)
        if label and not label.startswith("id=") and label != "系统表情" and label not in labels:
            labels.append(label)
    return tuple(labels)


def _entry_from_payload(face_id: str, data: dict[str, Any]) -> QQFaceEntry | None:
    labels = _normalize_labels(data.get("labels", []))
    if not labels:
        return None
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
    labels = _normalize_labels(label)
    if not normalized_id or not labels:
        return

    payload = _load_payload(context)
    entries = payload.setdefault("entries", {})
    current = entries.get(normalized_id)
    existing_labels = _normalize_labels(current.get("labels", []) if isinstance(current, dict) else [])
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
    for face_id, labels in _BUILTIN_QQ_FACE_LABELS.items():
        merged_entries[face_id] = {"labels": list(labels), "usage_count": 0, "last_used_ts": 0.0}

    for face_id, data in observed_entries.items():
        normalized_id = str(face_id or "").strip()
        if not normalized_id or not isinstance(data, dict):
            continue
        existing = merged_entries.setdefault(normalized_id, {"labels": [], "usage_count": 0, "last_used_ts": 0.0})
        labels = list(_normalize_labels(data.get("labels", [])))
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


def select_qq_face_for_labels(entries: list[QQFaceEntry], labels: list[str]) -> QQFaceEntry | None:
    if not entries:
        return None
    normalized = [str(label or "").strip() for label in labels if str(label or "").strip()]
    if not normalized:
        return min(entries, key=lambda item: (item.usage_count, item.last_used_ts, item.face_id))

    scored: list[tuple[float, float, QQFaceEntry]] = []
    for entry in entries:
        aliases = set(entry.aliases)
        score = 0.0
        for label in normalized:
            if label in aliases:
                score += 3.0
            elif any(label in alias or alias in label for alias in aliases):
                score += 1.5
        score -= entry.usage_count * 0.05
        if time.time() - entry.last_used_ts < 300:
            score -= 1.0
        scored.append((score, -entry.last_used_ts, entry))

    scored.sort(key=lambda item: (item[0], item[1], random.random()), reverse=True)
    best_score, _, best_entry = scored[0]
    if best_score <= 0:
        return min(entries, key=lambda item: (item.usage_count, item.last_used_ts, item.face_id))
    return best_entry


def mark_qq_face_used(context, entry: QQFaceEntry) -> None:
    payload = _load_payload(context)
    entries = payload.setdefault("entries", {})
    current = entries.get(entry.face_id)
    labels = list(_normalize_labels(current.get("labels", []) if isinstance(current, dict) else []))
    if entry.label not in labels:
        labels.append(entry.label)
    entries[entry.face_id] = {
        "labels": labels,
        "usage_count": int(current.get("usage_count", 0) or 0) + 1 if isinstance(current, dict) else 1,
        "last_used_ts": float(time.time()),
    }
    _save_payload(context, payload)
