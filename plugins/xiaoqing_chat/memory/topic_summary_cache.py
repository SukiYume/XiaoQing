from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast


@dataclass(frozen=True)
class TopicSummaryCacheEntry:
    topic_id: str
    topic: str
    summary: str
    keywords: list[str]
    key_points: list[str]
    updated_at: float


def topic_summary_cache_path(data_dir: Path, chat_id: str) -> Path:
    return data_dir / "hippo_memorizer" / f"{chat_id}.json"


def load_topic_summary_entries(data_dir: Path, chat_id: str) -> list[TopicSummaryCacheEntry]:
    path = topic_summary_cache_path(data_dir, chat_id)
    if not path.exists():
        return []
    try:
        raw_obj = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return []
    if not isinstance(raw_obj, list):
        return []

    entries: list[TopicSummaryCacheEntry] = []
    for item in cast(list[object], raw_obj):
        if not isinstance(item, dict):
            continue
        item_dict = cast(dict[str, object], item)
        topic_id = str(item_dict.get("topic_id", "") or "").strip()
        topic = str(item_dict.get("topic", "") or "").strip()
        summary = str(item_dict.get("summary", "") or "").strip()
        if not topic_id and not topic and not summary:
            continue

        keywords_raw = item_dict.get("keywords", [])
        key_points_raw = item_dict.get("key_points", [])
        keywords: list[str] = []
        key_points: list[str] = []
        if isinstance(keywords_raw, list):
            for raw_keyword in cast(list[object], keywords_raw):
                if isinstance(raw_keyword, str) and raw_keyword.strip():
                    keywords.append(raw_keyword.strip())
        if isinstance(key_points_raw, list):
            for raw_point in cast(list[object], key_points_raw):
                if isinstance(raw_point, str) and raw_point.strip():
                    key_points.append(raw_point.strip())
        updated_at_raw = item_dict.get("updated_at", 0.0)
        updated_at = 0.0
        if isinstance(updated_at_raw, (int, float)):
            updated_at = float(updated_at_raw)
        elif isinstance(updated_at_raw, str):
            try:
                updated_at = float(updated_at_raw) if updated_at_raw else 0.0
            except ValueError:
                updated_at = 0.0

        entries.append(
            TopicSummaryCacheEntry(
                topic_id=topic_id,
                topic=topic,
                summary=summary,
                keywords=keywords,
                key_points=key_points,
                updated_at=updated_at,
            )
        )
    return entries
