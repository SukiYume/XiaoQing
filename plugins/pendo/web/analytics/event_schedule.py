"""Shared event display helpers for dashboard and events pages."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from ...models.item import EventItem


def ensure_datetime(value: str | None, *, is_end: bool = False) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if "T" not in text:
        suffix = "T23:59:59" if is_end else "T00:00:00"
        text = f"{text}{suffix}"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        # The existing web analytics stack treats item datetimes as local wall-clock
        # values. Imported bundles may carry timezone offsets, so normalize them to
        # the same naive representation before range comparisons.
        return parsed.replace(tzinfo=None)
    return parsed


def date_key(value: str | None) -> str:
    return (value or "")[:10]


def daterange(start_day: date, end_day: date) -> list[str]:
    days: list[str] = []
    cursor = start_day
    while cursor <= end_day:
        days.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=1)
    return days


def event_kind(event: EventItem) -> str:
    collection_kind = getattr(event, "event_collection_kind", None)
    if collection_kind in {"multi_node", "recurring"}:
        return collection_kind
    return "single"


def event_display_days(event: EventItem, range_start_day: date, range_end_day: date) -> list[str]:
    start_dt = ensure_datetime(getattr(event, "start_time", None))
    end_dt = ensure_datetime(getattr(event, "end_time", None), is_end=True) or start_dt
    if not start_dt:
        return []

    start_day = max(start_dt.date(), range_start_day)
    end_day = min((end_dt or start_dt).date(), range_end_day)
    if start_day > end_day:
        return []
    return daterange(start_day, end_day)


def build_event_schedule(event: EventItem, range_start_day: date, range_end_day: date) -> dict[str, Any]:
    title = getattr(event, "title", None) or "无标题"
    location = getattr(event, "location", None) or ""
    category = getattr(event, "category", None) or ""
    kind = event_kind(event)
    display_days = event_display_days(event, range_start_day, range_end_day)
    start_time = getattr(event, "start_time", None) or ""
    end_time = getattr(event, "end_time", None) or ""

    time_summary = start_time[11:16] if start_time else "未设置时间"
    if end_time:
        time_summary = f"{time_summary} - {end_time[11:16]}"

    day_entries: dict[str, list[dict[str, Any]]] = {}
    for day in display_days:
        entries: list[dict[str, Any]] = []
        if date_key(start_time) == day:
            entries.append({
                "day": day,
                "kind": kind,
                "title": title,
                "subtitle": time_summary,
                "time": start_time or None,
                "time_label": start_time[11:16] if start_time else "全天",
                "start_time": start_time,
                "end_time": end_time,
                "location": location,
                "category": category,
            })

        if entries:
            day_entries[day] = entries

    return {
        "kind": kind,
        "display_days": display_days,
        "day_entries": day_entries,
        "time_summary": time_summary,
    }
