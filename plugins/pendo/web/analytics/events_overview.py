"""Event-focused aggregation for the Pendo web UI."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ...models.item import EventItem
from ...services.db import Database
from ...services.event_graph import EventGraphService
from ..utils import collection_payload
from .event_schedule import (
    build_event_schedule,
    daterange,
    ensure_datetime,
    event_kind,
)


def _iso_or_empty(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _event_matches_range(event: EventItem, range_start: datetime, range_end: datetime) -> bool:
    start_dt = ensure_datetime(getattr(event, "start_time", None))
    end_dt = ensure_datetime(getattr(event, "end_time", None), is_end=True) or start_dt
    if not start_dt:
        return False
    return start_dt <= range_end and (end_dt is None or end_dt >= range_start)


def _event_matches_keyword(
    event: EventItem,
    keyword: str,
    collection: dict[str, Any] | None = None,
) -> bool:
    if not keyword:
        return True
    haystacks = [
        getattr(event, "title", "") or "",
        getattr(event, "content", "") or "",
        getattr(event, "location", "") or "",
        getattr(event, "notes", "") or "",
        getattr(event, "category", "") or "",
    ]
    if collection:
        haystacks.append(str(collection.get("title") or ""))
        haystacks.append(str(collection.get("notes") or ""))
    full_text = "\n".join(haystacks).lower()
    return keyword.lower() in full_text


def _status_for_log(log: dict[str, Any] | None) -> str:
    if log and log.get("confirmed_at"):
        return "confirmed"
    if log and log.get("sent_at"):
        return "sent"
    return "pending"


def _build_reminder_rows(event: EventItem, reminder_logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    remind_times = sorted(str(t) for t in (getattr(event, "remind_times", None) or []) if t)
    log_map = {str(log.get("remind_time")): log for log in reminder_logs}
    rows: list[dict[str, Any]] = []
    for remind_time in remind_times:
        log = log_map.get(remind_time)
        rows.append({
            "time": remind_time,
            "status": _status_for_log(log),
            "sent_at": log.get("sent_at") if log else None,
            "confirmed_at": log.get("confirmed_at") if log else None,
            "repeat_count": int(log.get("repeat_count") or 0) if log else 0,
        })
    return rows


def _build_reminder_summary(reminder_rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {"total": len(reminder_rows), "pending": 0, "sent": 0, "confirmed": 0}
    for row in reminder_rows:
        status = row["status"]
        summary[status] += 1
    return summary


def _reminder_rows_in_range(
    reminder_rows: list[dict[str, Any]],
    range_start_day: date,
    range_end_day: date,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in reminder_rows:
        remind_time = row.get("time")
        if not remind_time:
            continue
        remind_day = datetime.fromisoformat(str(remind_time)).date()
        if range_start_day <= remind_day <= range_end_day:
            rows.append(row)
    return rows


def _event_matches_kind(event: EventItem, kind: str) -> bool:
    actual = event_kind(event)
    return kind in {"", "all", actual}


def _event_matches_reminder(reminder_summary: dict[str, Any], reminder: str) -> bool:
    if reminder in {"", "all"}:
        return True
    if reminder == "with":
        return reminder_summary["total"] > 0
    if reminder == "none":
        return reminder_summary["total"] == 0
    return reminder_summary.get(reminder, 0) > 0


def _timeline_entries_for_day(event_payload: dict[str, Any], day: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for row in event_payload["day_entries"].get(day, []):
        entries.append({
            "event_id": event_payload["id"],
            "collection": event_payload.get("collection"),
            "kind": row["kind"],
            "day": day,
            "time": row["time"],
            "time_label": row["time_label"],
            "title": row["title"],
            "subtitle": row["subtitle"],
            "location": row["location"],
            "category": row["category"],
            "reminder_total": event_payload["range_reminder_summary"]["total"],
        })
    return entries


def _normalize_event(
    event: EventItem,
    reminder_logs: list[dict[str, Any]],
    range_start_day: date,
    range_end_day: date,
    collection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = event.to_dict()
    schedule = build_event_schedule(event, range_start_day, range_end_day)
    reminders = _build_reminder_rows(event, reminder_logs)
    reminder_summary = _build_reminder_summary(reminders)
    range_reminders = _reminder_rows_in_range(reminders, range_start_day, range_end_day)
    range_reminder_summary = _build_reminder_summary(range_reminders)
    kind = schedule["kind"]

    return {
        **base,
        "kind": kind,
        "collection": collection_payload(collection),
        "display_days": schedule["display_days"],
        "day_entries": schedule["day_entries"],
        "reminders": reminders,
        "reminder_summary": reminder_summary,
        "range_reminders": range_reminders,
        "range_reminder_summary": range_reminder_summary,
        "time_summary": schedule["time_summary"],
        "is_recurring_instance": kind == "recurring",
        "series_id": getattr(event, "event_collection_id", None) or None,
    }


def _fetch_reminder_logs_by_event_ids(db: Database, event_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """一次性读取多个事件的 reminder logs，避免按事件逐条查询。"""
    unique_ids = [event_id for event_id in dict.fromkeys(event_ids) if event_id]
    if not unique_ids:
        return {}

    conn = db.get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in unique_ids)
    cursor.execute(
        f"""
        SELECT item_id, remind_time, sent_at, confirmed_at, user_action, repeat_count, last_sent_at
        FROM reminder_logs
        WHERE item_id IN ({placeholders})
        ORDER BY item_id, remind_time
        """,
        unique_ids,
    )

    logs_by_event: dict[str, list[dict[str, Any]]] = {event_id: [] for event_id in unique_ids}
    for row in cursor.fetchall():
        log = dict(row)
        item_id = str(log.pop("item_id"))
        logs_by_event.setdefault(item_id, []).append(log)
    return logs_by_event


def build_events_overview(
    db: Database,
    owner_id: str,
    *,
    start_date: str,
    end_date: str,
    keyword: str = "",
    category: str = "",
    kind: str = "all",
    reminder: str = "all",
) -> dict[str, Any]:
    range_start = ensure_datetime(start_date) or datetime.now()
    range_end = ensure_datetime(end_date, is_end=True) or range_start
    range_start_day = range_start.date()
    range_end_day = range_end.date()

    range_events = db.get_events_for_range(
        owner_id,
        _iso_or_empty(range_start),
        _iso_or_empty(range_end),
    )
    events = [event for event in range_events if _event_matches_range(event, range_start, range_end)]

    categories = sorted({
        (getattr(event, "category", "") or "未分类")
        for event in db.get_all_items(owner_id, filters={"type": "event"})
    })

    reminder_logs_by_event = _fetch_reminder_logs_by_event_ids(db, [event.id for event in events])
    collection_cache: dict[str, dict[str, Any] | None] = {}
    normalized_events: list[dict[str, Any]] = []
    for event in events:
        collection = None
        collection_id = getattr(event, "event_collection_id", None)
        if collection_id:
            if collection_id not in collection_cache:
                collection_cache[collection_id] = db.get_event_collection(collection_id, owner_id)
            collection = collection_cache[collection_id]
        reminder_logs = reminder_logs_by_event.get(event.id, [])
        payload = _normalize_event(event, reminder_logs, range_start_day, range_end_day, collection)
        if category and payload.get("category") != category:
            continue
        if not _event_matches_kind(event, kind):
            continue
        if not _event_matches_keyword(event, keyword, collection):
            continue
        if not payload["display_days"]:
            continue
        if not _event_matches_reminder(payload["range_reminder_summary"], reminder):
            continue
        normalized_events.append(payload)

    normalized_events.sort(key=lambda event: event.get("start_time") or "")

    calendar_days: dict[str, dict[str, Any]] = {}
    timeline_days: dict[str, list[dict[str, Any]]] = {}
    for day in daterange(range_start_day, range_end_day):
        calendar_days[day] = {"date": day, "count": 0, "items": [], "has_events": False}
        timeline_days[day] = []

    for event in normalized_events:
        for day in event["display_days"]:
            if day not in calendar_days:
                continue
            calendar_days[day]["count"] += 1
            calendar_days[day]["has_events"] = True
            if len(calendar_days[day]["items"]) < 3:
                label = event["title"] or "无标题"
                if event["kind"] == "multi_node":
                    label = event["title"] or "无标题"
                elif event["kind"] == "single":
                    label = event["title"] or "无标题"
                elif event["kind"] == "recurring":
                    label = event["title"] or "无标题"
                calendar_days[day]["items"].append({
                    "event_id": event["id"],
                    "kind": event["kind"],
                    "label": label,
                })

        for day in event["display_days"]:
            for row in _timeline_entries_for_day(event, day):
                timeline_days.setdefault(day, []).append(row)

    timeline_list = []
    for day, items in timeline_days.items():
        if not items:
            continue
        items.sort(key=lambda item: item["time"] or "")
        timeline_list.append({"date": day, "items": items})
    timeline_list.sort(key=lambda row: row["date"])

    return {
        "range": {
            "start_date": range_start_day.strftime("%Y-%m-%d"),
            "end_date": range_end_day.strftime("%Y-%m-%d"),
        },
        "summary": {
            "event_count": len(normalized_events),
            "day_count": sum(1 for day in calendar_days.values() if day["count"]),
            "multi_node_count": sum(1 for event in normalized_events if event["kind"] == "multi_node"),
            "recurring_count": sum(1 for event in normalized_events if event["kind"] == "recurring"),
            "reminder_count": sum(event["range_reminder_summary"]["total"] for event in normalized_events),
        },
        "categories": categories,
        "calendar_days": calendar_days,
        "timeline_days": timeline_list,
        "events": normalized_events,
    }


def build_event_detail(db: Database, owner_id: str, event_id: str) -> dict[str, Any] | None:
    family = EventGraphService(db).load_by_id(owner_id, event_id)
    if family.collection and family.leaf is None:
        return build_event_collection_detail(db, owner_id, event_id)

    event = db.get_item(event_id, owner_id=owner_id)
    if not isinstance(event, EventItem):
        return None

    collection = db.get_event_collection(event.event_collection_id, owner_id) if event.event_collection_id else None
    reminder_logs = db.get_reminder_logs(event.id)
    normalized = _normalize_event(
        event,
        reminder_logs,
        (ensure_datetime(getattr(event, "start_time", None)) or datetime.now()).date(),
        (ensure_datetime(getattr(event, "end_time", None), is_end=True) or ensure_datetime(getattr(event, "start_time", None)) or datetime.now()).date(),
        collection,
    )

    related_instances = []
    if collection:
        related_instances = [
            {
                "id": child.id,
                "title": child.title,
                "start_time": child.start_time,
                "end_time": child.end_time,
            }
            for child in db.get_collection_events(str(collection["id"]), owner_id)
            if child.id != event.id
        ]

    return {
        "event": normalized,
        "reminder_logs": reminder_logs,
        "related_instances": related_instances[:12],
    }


def build_event_collection_detail(
    db: Database,
    owner_id: str,
    collection_id: str,
) -> dict[str, Any] | None:
    collection = db.get_event_collection(collection_id, owner_id)
    if not collection:
        return None
    children = db.get_collection_events(collection_id, owner_id)
    child_ids = [child.id for child in children]
    reminder_logs_by_event = _fetch_reminder_logs_by_event_ids(db, child_ids)
    normalized_children = [
        _normalize_event(
            child,
            reminder_logs_by_event.get(child.id, []),
            (ensure_datetime(child.start_time) or datetime.now()).date(),
            (ensure_datetime(child.end_time, is_end=True) or ensure_datetime(child.start_time) or datetime.now()).date(),
            collection,
        )
        for child in children
    ]
    return {
        "collection": collection_payload(collection),
        "children": normalized_children,
    }
