"""Shared event display helpers for dashboard and events pages."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from ...models.item import EventItem
from ...utils.time_utils import TimezoneHelper


def ensure_datetime(value: str | None, *, is_end: bool = False) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if "T" not in text:
        suffix = "T23:59:59" if is_end else "T00:00:00"
        text = f"{text}{suffix}"
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        return parsed.astimezone(TimezoneHelper.DEFAULT_TZ).replace(tzinfo=None)
    return parsed


def date_key(value: str | None) -> str:
    parsed = ensure_datetime(value)
    return parsed.strftime("%Y-%m-%d") if parsed else ""


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
    start_dt = ensure_datetime(getattr(event, "start_time", None))
    end_dt = ensure_datetime(getattr(event, "end_time", None), is_end=True)
    start_time = start_dt.isoformat(timespec="seconds") if start_dt else ""
    end_time = end_dt.isoformat(timespec="seconds") if end_dt else ""
    start_day = start_dt.strftime("%Y-%m-%d") if start_dt else ""
    end_day = end_dt.strftime("%Y-%m-%d") if end_dt else start_day

    time_summary = start_dt.strftime("%H:%M") if start_dt else "未设置时间"
    if end_time:
        time_summary = f"{time_summary} - {end_dt.strftime('%H:%M')}"

    day_entries: dict[str, list[dict[str, Any]]] = {}
    for day in display_days:
        if not start_dt:
            continue
        if day == start_day:
            row_time = start_time
            time_label = start_dt.strftime("%H:%M")
        elif end_dt and day == end_day and day != start_day:
            row_time = f"{day}T00:00:00"
            time_label = f"至 {end_dt.strftime('%H:%M')}"
        else:
            row_time = f"{day}T00:00:00"
            time_label = "跨天"
        day_entries[day] = [{
            "day": day,
            "kind": kind,
            "title": title,
            "subtitle": time_summary,
            "time": row_time,
            "time_label": time_label,
            "start_time": start_time,
            "end_time": end_time,
            "location": location,
            "category": category,
        }]

    return {
        "kind": kind,
        "display_days": display_days,
        "day_entries": day_entries,
        "time_summary": time_summary,
    }
