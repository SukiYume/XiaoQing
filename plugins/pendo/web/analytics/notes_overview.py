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


def _note_activity_day(note: dict[str, Any]) -> date | None:
    return _parse_date(note.get("created_at") or note.get("updated_at"))


def _in_range(day: date | None, start_day: date | None, end_day: date | None) -> bool:
    if day is None:
        return False
    if start_day and day < start_day:
        return False
    if end_day and day > end_day:
        return False
    return True


def _build_day_cadence(notes: list[dict[str, Any]], start_day: date, end_day: date) -> list[dict[str, Any]]:
    day = start_day
    counter = Counter(
        _note_activity_day(note)
        for note in notes
        if _note_activity_day(note) is not None
    )
    cadence: list[dict[str, Any]] = []
    while day <= end_day:
        cadence.append({
            "date": day.strftime("%Y-%m-%d"),
            "label": f"{day.month}/{day.day}",
            "count": counter.get(day, 0),
        })
        day += timedelta(days=1)
    return cadence


def _build_week_cadence(notes: list[dict[str, Any]], start_day: date, end_day: date) -> list[dict[str, Any]]:
    counter = Counter()
    for note in notes:
        day = _note_activity_day(note)
        if not _in_range(day, start_day, end_day):
            continue
        week_start = day - timedelta(days=day.weekday())
        counter[week_start] += 1

    cursor = start_day - timedelta(days=start_day.weekday())
    cadence: list[dict[str, Any]] = []
    while cursor <= end_day:
        iso_year, iso_week, _ = cursor.isocalendar()
        cadence.append({
            "date": cursor.strftime("%Y-%m-%d"),
            "label": f"{cursor.month}/{cursor.day}",
            "week": f"{iso_year}-W{iso_week:02d}",
            "count": counter.get(cursor, 0),
        })
        cursor += timedelta(days=7)
    return cadence


def _build_month_cadence(notes: list[dict[str, Any]], start_day: date, end_day: date) -> list[dict[str, Any]]:
    counter = Counter()
    for note in notes:
        day = _note_activity_day(note)
        if not _in_range(day, start_day, end_day):
            continue
        key = (day.year, day.month)
        counter[key] += 1

    cursor = date(start_day.year, start_day.month, 1)
    cadence: list[dict[str, Any]] = []
    while cursor <= end_day:
        key = (cursor.year, cursor.month)
        cadence.append({
            "date": cursor.strftime("%Y-%m-%d"),
            "label": f"{cursor.year}-{cursor.month:02d}",
            "month": f"{cursor.year}-{cursor.month:02d}",
            "count": counter.get(key, 0),
        })
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return cadence


def _build_year_cadence(notes: list[dict[str, Any]], start_day: date, end_day: date) -> list[dict[str, Any]]:
    counter = Counter()
    for note in notes:
        day = _note_activity_day(note)
        if not _in_range(day, start_day, end_day):
            continue
        counter[day.year] += 1

    cadence: list[dict[str, Any]] = []
    for year in range(start_day.year, end_day.year + 1):
        cadence.append({
            "date": f"{year}-01-01",
            "label": str(year),
            "year": str(year),
            "count": counter.get(year, 0),
        })
    return cadence


def _resolve_cadence_granularity(start_day: date, end_day: date) -> str:
    span_days = max(0, (end_day - start_day).days)
    if start_day.year != end_day.year:
        return "year"
    if span_days > 62:
        return "month"
    if span_days > 7:
        return "week"
    return "day"


def build_notes_overview(
    db: Database,
    owner_id: str,
    today: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    category: str | None = None,
    tags: str | None = None,
) -> dict[str, Any]:
    today_day = _parse_date(today) or datetime.now().date()
    range_start = _parse_date(start_date)
    range_end = _parse_date(end_date)
    all_notes = _load_all_notes(db=db, owner_id=owner_id)
    tag_query = str(tags or "").strip().lower()
    notes = [
        note for note in all_notes
        if (
            not range_start
            or not range_end
            or _in_range(_note_activity_day(note), range_start, range_end)
        )
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

    cadence_granularity = "day"
    if range_start and range_end:
        cadence_granularity = _resolve_cadence_granularity(range_start, range_end)
        if cadence_granularity == "year":
            cadence = _build_year_cadence(notes, range_start, range_end)
        elif cadence_granularity == "month":
            cadence = _build_month_cadence(notes, range_start, range_end)
        elif cadence_granularity == "week":
            cadence = _build_week_cadence(notes, range_start, range_end)
        else:
            cadence = _build_day_cadence(notes, range_start, range_end)
    else:
        last_days = [today_day - timedelta(days=offset) for offset in range(13, -1, -1)]
        cadence_counter = Counter(
            _note_activity_day(note)
            for note in notes
            if _note_activity_day(note) is not None
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
            "range_start": range_start.strftime("%Y-%m-%d") if range_start else None,
            "range_end": range_end.strftime("%Y-%m-%d") if range_end else None,
        },
        "categories": categories,
        "hot_tags": hot_tags,
        "cadence": cadence,
        "cadence_granularity": cadence_granularity,
        "recent_notes": recent_notes,
        "all_categories": sorted({(note.get("category") or "未分类") for note in all_notes}),
    }
