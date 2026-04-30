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


def _date_span(start: date, end: date) -> list[date]:
    days: list[date] = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _resolve_period(
    year: int | None = None,
    month: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[date, date]:
    if start_date or end_date:
        start = _parse_day(start_date)
        end = _parse_day(end_date)
        if start is None or end is None:
            raise ValueError("start_date and end_date must be valid YYYY-MM-DD strings")
        if start > end:
            raise ValueError("start_date must not be after end_date")
        return start, end
    if year is None or month is None:
        raise ValueError("year and month are required when start_date/end_date are not provided")
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    return start, end


def _load_all_diaries(db: Database, owner_id: str) -> list[Any]:
    items: list[Any] = []
    offset = 0
    batch_size = 300

    while True:
        batch = db.get_items(
            owner_id,
            filters={
                "type": ItemType.DIARY.value,
                "sort_field": "entry_time",
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


def _entry_sort_key(item: Any) -> str:
    return str(
        getattr(item, "entry_time", None)
        or getattr(item, "created_at", None)
        or getattr(item, "updated_at", None)
        or getattr(item, "diary_date", None)
        or ""
    )


def _entry_label(item: Any) -> str:
    raw = _entry_sort_key(item)
    if "T" in raw and len(raw) >= 16:
        return raw[11:16]
    return "全天"


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


def _compute_longest_streak(days: list[date]) -> int:
    if not days:
        return 0
    ordered = sorted(set(days))
    longest = 1
    current_run = 1
    for index in range(1, len(ordered)):
        if ordered[index] - ordered[index - 1] == timedelta(days=1):
            current_run += 1
            longest = max(longest, current_run)
        else:
            current_run = 1
    return longest


def _resolve_cadence_granularity(start: date, end: date, cadence_granularity: str) -> str:
    if cadence_granularity != "auto":
        return cadence_granularity
    span_days = (end - start).days + 1
    if start.year != end.year:
        return "year"
    if span_days > 62:
        return "month"
    if span_days > 7:
        return "week"
    return "day"


def _format_day_label(current: date, start: date, end: date) -> str:
    if start.year == end.year and start.month == end.month:
        return str(current.day)
    return f"{current.month}/{current.day}"


def _build_cadence(
    start: date,
    end: date,
    day_counts: dict[str, int],
    day_words: dict[str, int],
    cadence_granularity: str,
) -> tuple[str, list[dict[str, Any]]]:
    resolved = _resolve_cadence_granularity(start, end, cadence_granularity)
    buckets: dict[str, dict[str, Any]] = {}
    ordered_keys: list[str] = []

    for current in _date_span(start, end):
        date_key = current.strftime("%Y-%m-%d")
        if resolved == "year":
            bucket_key = current.strftime("%Y")
            label = bucket_key
        elif resolved == "month":
            bucket_key = current.strftime("%Y-%m")
            label = bucket_key
        elif resolved == "week":
            iso = current.isocalendar()
            bucket_key = f"{iso.year}-W{iso.week:02d}"
            label = bucket_key
        else:
            bucket_key = date_key
            label = _format_day_label(current, start, end)
        if bucket_key not in buckets:
            buckets[bucket_key] = {
                "bucket": bucket_key,
                "date": date_key,
                "label": label,
                "count": 0,
                "words": 0,
            }
            ordered_keys.append(bucket_key)
        buckets[bucket_key]["count"] += day_counts.get(date_key, 0)
        buckets[bucket_key]["words"] += day_words.get(date_key, 0)

    return resolved, [buckets[key] for key in ordered_keys]


def build_diary_overview(
    db: Database,
    owner_id: str,
    year: int | None = None,
    month: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    today: str | None = None,
    cadence_granularity: str = "day",
) -> dict[str, Any]:
    """Build compact diary analytics for a specific month or explicit date range."""
    start, end = _resolve_period(year=year, month=month, start_date=start_date, end_date=end_date)
    today_day = _parse_day(today) or date.today()

    window_items = db.query_items_by_date_range(
        owner_id,
        ItemType.DIARY.value,
        "diary_date",
        start.strftime("%Y-%m-%d"),
        end.strftime("%Y-%m-%d"),
    )
    window_items = sorted(window_items, key=_entry_sort_key, reverse=True)
    all_items = _load_all_diaries(db, owner_id)

    day_counts: dict[str, int] = {}
    day_words: dict[str, int] = {}
    mood_counts: dict[str, int] = {}
    template_counts: dict[str, int] = {}

    for item in window_items:
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
    period_days = [
        parsed
        for key in day_counts.keys()
        if (parsed := _parse_day(key)) is not None
    ]
    current_streak, longest_streak = _compute_streaks(all_days, today_day)
    period_longest_streak = _compute_longest_streak(period_days)

    total_words = sum(len(str(getattr(item, "content", "") or "").strip()) for item in window_items)
    active_days = len(day_counts)
    total_days = (end - start).days + 1

    resolved_cadence_granularity, cadence = _build_cadence(
        start=start,
        end=end,
        day_counts=day_counts,
        day_words=day_words,
        cadence_granularity=cadence_granularity,
    )

    mood_breakdown = [
        {
            "mood": mood,
            "count": count,
            "share": count / len(window_items) if window_items else 0,
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
            "entry_time": getattr(item, "entry_time", None),
            "entry_label": _entry_label(item),
            "mood": item.mood,
            "mood_score": getattr(item, "mood_score", None),
            "weather": item.weather,
            "is_favorite": getattr(item, "is_favorite", False),
            "content_preview": str(item.content or "").strip()[:80],
            "word_count": len(str(item.content or "").strip()),
        }
        for item in window_items[:6]
    ]

    busiest_day = None
    if day_words:
        busiest_date = max(day_words.keys(), key=lambda item: (day_words[item], day_counts.get(item, 0), item))
        busiest_day = {
            "date": busiest_date,
            "count": day_counts.get(busiest_date, 0),
            "words": day_words[busiest_date],
        }

    return {
        "summary": {
            "entry_count": len(window_items),
            "range_start": start.strftime("%Y-%m-%d"),
            "range_end": end.strftime("%Y-%m-%d"),
            "range_days": total_days,
            "active_days": active_days,
            "average_length": round(total_words / len(window_items), 1) if window_items else 0,
            "fill_rate": active_days / total_days if total_days else 0,
            "current_streak": current_streak,
            "longest_streak": longest_streak,
            "period_longest_streak": period_longest_streak,
            "month_longest_streak": period_longest_streak,
            "total_words": total_words,
            "busiest_day": busiest_day,
        },
        "cadence_granularity": resolved_cadence_granularity,
        "mood_breakdown": mood_breakdown,
        "cadence": cadence,
        "template_usage": template_usage,
        "recent_entries": recent_entries,
    }
