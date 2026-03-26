"""Compact diary overview analytics for the redesigned diary page."""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Any

from ...models.item import ItemType
from ...services.db import Database


def _parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _load_all_diaries(db: Database, owner_id: str) -> list[Any]:
    items: list[Any] = []
    offset = 0
    batch_size = 300

    while True:
        batch = db.get_items(
            owner_id,
            filters={
                "type": ItemType.DIARY.value,
                "sort_field": "diary_date",
                "sort_order": "DESC",
            },
            limit=batch_size,
            offset=offset,
        )
        if not batch:
            break
        items.extend(batch)
        if len(batch) < batch_size:
            break
        offset += batch_size

    return items


def _compute_streaks(days: list[date], today: date) -> tuple[int, int]:
    if not days:
        return 0, 0

    ordered = sorted(set(days))

    longest = 1
    current_run = 1
    for index in range(1, len(ordered)):
        if ordered[index] - ordered[index - 1] == timedelta(days=1):
            current_run += 1
            longest = max(longest, current_run)
        else:
            current_run = 1

    days_set = set(ordered)
    current = 0
    cursor = today
    while cursor in days_set:
        current += 1
        cursor -= timedelta(days=1)

    if current == 0:
        yesterday = today - timedelta(days=1)
        cursor = yesterday
        while cursor in days_set:
            current += 1
            cursor -= timedelta(days=1)

    return current, longest


def build_diary_overview(
    db: Database,
    owner_id: str,
    year: int,
    month: int,
    today: str | None = None,
) -> dict[str, Any]:
    """Build compact diary analytics for a specific month."""
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    today_day = _parse_day(today) or date.today()

    month_items = db.query_items_by_date_range(
        owner_id,
        ItemType.DIARY.value,
        "diary_date",
        start.strftime("%Y-%m-%d"),
        end.strftime("%Y-%m-%d"),
    )
    all_items = _load_all_diaries(db, owner_id)

    day_counts: dict[str, int] = {}
    day_words: dict[str, int] = {}
    mood_counts: dict[str, int] = {}
    template_counts: dict[str, int] = {}

    for item in month_items:
        key = getattr(item, "diary_date", "") or ""
        if not key:
            continue
        day_counts[key] = day_counts.get(key, 0) + 1
        words = len(str(getattr(item, "content", "") or "").strip())
        day_words[key] = day_words.get(key, 0) + words

        mood = str(getattr(item, "mood", "") or "").strip()
        if mood:
            mood_counts[mood] = mood_counts.get(mood, 0) + 1

        template_id = str(getattr(item, "template_id", "") or "").strip()
        if template_id:
            template_counts[template_id] = template_counts.get(template_id, 0) + 1

    all_days = [
        parsed
        for item in all_items
        if (parsed := _parse_day(getattr(item, "diary_date", None))) is not None
    ]
    current_streak, longest_streak = _compute_streaks(all_days, today_day)

    total_words = sum(len(str(getattr(item, "content", "") or "").strip()) for item in month_items)
    active_days = len(day_counts)
    total_days = monthrange(year, month)[1]

    cadence = []
    for day_number in range(1, total_days + 1):
        current = date(year, month, day_number)
        key = current.strftime("%Y-%m-%d")
        cadence.append({
            "date": key,
            "label": str(day_number),
            "count": day_counts.get(key, 0),
            "words": day_words.get(key, 0),
        })

    mood_breakdown = [
        {
            "mood": mood,
            "count": count,
            "share": count / len(month_items) if month_items else 0,
        }
        for mood, count in sorted(mood_counts.items(), key=lambda item: (-item[1], item[0]))
    ]

    template_usage = [
        {"template_id": template_id, "count": count}
        for template_id, count in sorted(template_counts.items(), key=lambda item: (-item[1], item[0]))
    ]

    recent_entries = [
        {
            "id": item.id,
            "title": item.title,
            "diary_date": item.diary_date,
            "mood": item.mood,
            "weather": item.weather,
            "content_preview": str(item.content or "").strip()[:80],
            "word_count": len(str(item.content or "").strip()),
        }
        for item in month_items[:6]
    ]

    busiest_day = None
    if cadence:
        max_count = max(item["count"] for item in cadence)
        if max_count > 0:
            busiest = max(cadence, key=lambda item: (item["count"], item["words"], item["date"]))
            busiest_day = {
                "date": busiest["date"],
                "count": busiest["count"],
                "words": busiest["words"],
            }

    return {
        "summary": {
            "entry_count": len(month_items),
            "active_days": active_days,
            "average_length": round(total_words / len(month_items), 1) if month_items else 0,
            "fill_rate": active_days / total_days if total_days else 0,
            "current_streak": current_streak,
            "longest_streak": longest_streak,
            "total_words": total_words,
            "busiest_day": busiest_day,
        },
        "mood_breakdown": mood_breakdown,
        "cadence": cadence,
        "template_usage": template_usage,
        "recent_entries": recent_entries,
    }
