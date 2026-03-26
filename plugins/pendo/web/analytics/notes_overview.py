"""Note-focused aggregation for the Pendo web UI."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from ...services.db import Database


def _note_dict(note) -> dict[str, Any]:
    return note.to_dict() if hasattr(note, "to_dict") else {}


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = str(value)
    try:
        if "T" in text:
            return datetime.fromisoformat(text).date()
        return datetime.fromisoformat(f"{text}T00:00:00").date()
    except ValueError:
        return None


def _load_all_notes(db: Database, owner_id: str, batch_size: int = 200) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    offset = 0

    while True:
        chunk = db.get_items(owner_id, filters={"type": "note"}, limit=batch_size, offset=offset)
        if not chunk:
            break
        notes.extend(_note_dict(note) for note in chunk)
        if len(chunk) < batch_size:
            break
        offset += batch_size

    return notes


def build_notes_overview(
    db: Database,
    owner_id: str,
    today: str | None = None,
    category: str | None = None,
    tags: str | None = None,
) -> dict[str, Any]:
    today_day = _parse_date(today) or datetime.now().date()
    all_notes = _load_all_notes(db=db, owner_id=owner_id)
    tag_query = str(tags or "").strip().lower()
    notes = [
        note for note in all_notes
        if (not category or (note.get("category") or "未分类") == category)
        and (
            not tag_query
            or any(tag_query in str(tag).lower() for tag in (note.get("tags") if isinstance(note.get("tags"), list) else []))
        )
    ]

    category_counter = Counter((note.get("category") or "未分类") for note in notes)
    tag_counter = Counter()
    words_total = 0
    tagged_count = 0
    week_new = 0

    for note in notes:
        content = str(note.get("content") or "").strip()
        words_total += len(content)
        tags = note.get("tags") if isinstance(note.get("tags"), list) else []
        if tags:
            tagged_count += 1
            for tag in tags:
                if tag:
                    tag_counter[str(tag)] += 1
        created_day = _parse_date(note.get("created_at"))
        if created_day and created_day >= today_day - timedelta(days=6):
            week_new += 1

    last_days = [today_day - timedelta(days=offset) for offset in range(13, -1, -1)]
    cadence_counter = Counter(
        _parse_date(note.get("created_at") or note.get("updated_at"))
        for note in notes
        if _parse_date(note.get("created_at") or note.get("updated_at")) is not None
    )
    cadence = [
        {
            "date": day.strftime("%Y-%m-%d"),
            "label": f"{day.month}/{day.day}",
            "count": cadence_counter.get(day, 0),
        }
        for day in last_days
    ]

    categories = [
        {
            "category": category,
            "count": count,
            "share": count / len(notes) if notes else 0,
        }
        for category, count in category_counter.most_common(6)
    ]
    hot_tags = [
        {"tag": tag, "count": count}
        for tag, count in tag_counter.most_common(8)
    ]

    recent_notes = sorted(
        notes,
        key=lambda item: item.get("updated_at") or item.get("created_at") or "",
        reverse=True,
    )[:6]

    average_length = round(words_total / len(notes), 1) if notes else 0

    return {
        "summary": {
            "total_count": len(notes),
            "week_new_count": week_new,
            "average_length": average_length,
            "tagged_rate": round(tagged_count / len(notes), 4) if notes else 0,
        },
        "categories": categories,
        "hot_tags": hot_tags,
        "cadence": cadence,
        "recent_notes": recent_notes,
        "all_categories": sorted({(note.get("category") or "未分类") for note in all_notes}),
    }
