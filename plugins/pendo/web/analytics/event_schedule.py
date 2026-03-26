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
    return datetime.fromisoformat(text)


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
    milestones = getattr(event, "milestones", None) or []
    if milestones and len(milestones) >= 2:
        return "milestone"
    if getattr(event, "parent_id", None) or getattr(event, "rrule", None):
        return "recurring"
    return "single"


def milestone_rows(event: EventItem) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for milestone in getattr(event, "milestones", None) or []:
        time_text = str(milestone.get("time") or "").strip()
        if not time_text:
            continue
        rows.append({
            "name": str(milestone.get("name") or "节点").strip() or "节点",
            "time": time_text,
            "date": date_key(time_text),
        })
    rows.sort(key=lambda row: row["time"])
    return rows


def event_display_days(event: EventItem, range_start_day: date, range_end_day: date) -> list[str]:
    rows = milestone_rows(event)
    if rows:
        return [
            row["date"]
            for row in rows
            if range_start_day <= datetime.fromisoformat(row["time"]).date() <= range_end_day
        ]

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
    rows = milestone_rows(event)
    display_days = event_display_days(event, range_start_day, range_end_day)
    start_time = getattr(event, "start_time", None) or ""
    end_time = getattr(event, "end_time", None) or ""

    time_summary = start_time[11:16] if start_time else "未设置时间"
    if end_time:
        time_summary = f"{time_summary} - {end_time[11:16]}"

    day_entries: dict[str, list[dict[str, Any]]] = {}
    for day in display_days:
        entries: list[dict[str, Any]] = []
        if kind == "milestone":
            same_day_rows = [row for row in rows if row["date"] == day]
            if same_day_rows:
                for row in same_day_rows:
                    entries.append({
                        "day": day,
                        "kind": "milestone",
                        "title": title,
                        "subtitle": row["name"],
                        "time": row["time"],
                        "time_label": row["time"][11:16],
                        "start_time": row["time"],
                        "end_time": "",
                        "location": location,
                        "category": category,
                    })
            else:
                entries.append({
                    "day": day,
                    "kind": "milestone",
                    "title": title,
                    "subtitle": "多节点事件",
                    "time": None,
                    "time_label": "节点",
                    "start_time": "",
                    "end_time": "",
                    "location": location,
                    "category": category,
                })
        elif date_key(start_time) == day:
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
        "milestones": rows,
        "display_days": display_days,
        "day_entries": day_entries,
        "time_summary": time_summary,
    }
