"""Dashboard overview aggregation for the web UI."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ...services.db import Database
from .event_schedule import build_event_schedule


def _month_bounds(now: datetime) -> tuple[str, str, str]:
    month_start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month == 12:
        next_month = now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        next_month = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
    month_end_dt = next_month - timedelta(seconds=1)
    return (
        month_start_dt.strftime("%Y-%m-%d"),
        month_start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        month_end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
    )


def _to_dict(item):
    return item.to_dict() if hasattr(item, "to_dict") else {}


def _ledger_amount_value(item) -> float:
    cents = getattr(item, "amount_cents", None)
    if cents not in (None, ""):
        try:
            return int(cents) / 100
        except (TypeError, ValueError):
            pass
    try:
        return float(getattr(item, "amount", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _get_all_items(db: Database, owner_id: str, filters: dict[str, Any], batch_size: int = 500) -> list:
    items: list = []
    offset = 0
    while True:
        chunk = db.get_items(owner_id, filters=filters, limit=batch_size, offset=offset)
        items.extend(chunk)
        if len(chunk) < batch_size:
            break
        offset += batch_size
    return items


def _collection_payload(collection: dict[str, Any] | None) -> dict[str, Any] | None:
    if not collection:
        return None
    return {
        "id": collection.get("id"),
        "kind": collection.get("kind"),
        "title": collection.get("title"),
        "category": collection.get("category"),
        "location": collection.get("location"),
        "notes": collection.get("notes"),
    }


def _display_fields(row: dict[str, Any], collection: dict[str, Any] | None) -> dict[str, Any]:
    title = row["title"]
    subtitle = row["subtitle"]
    location = row["location"]
    category = row["category"]

    if collection and collection.get("kind") == "multi_node":
        collection_title = str(collection.get("title") or "").strip()
        if collection_title:
            title = collection_title
            subtitle = row["title"] if row["title"] != collection_title else row["subtitle"]
        location = location or collection.get("location") or ""
        category = category or collection.get("category") or ""

    return {
        "title": title,
        "display_title": title,
        "display_subtitle": subtitle,
        "location": location,
        "category": category,
    }


def _event_entries(item, range_start: str, range_end: str, collection: dict[str, Any] | None = None) -> list[dict]:
    entries: list[dict] = []
    schedule = build_event_schedule(
        item,
        datetime.strptime(range_start[:10], "%Y-%m-%d").date(),
        datetime.strptime(range_end[:10], "%Y-%m-%d").date(),
    )
    item_dict = _to_dict(item)
    collection_payload = _collection_payload(collection)
    for day in schedule["display_days"]:
        for row in schedule["day_entries"].get(day, []):
            display = _display_fields(row, collection)
            entries.append({
                **item_dict,
                "id": getattr(item, "id", ""),
                "title": display["title"],
                "display_title": display["display_title"],
                "display_subtitle": display["display_subtitle"],
                "node_title": row["title"],
                "collection": collection_payload,
                "start_time": row["start_time"],
                "end_time": row["end_time"],
                "location": display["location"],
                "category": display["category"],
                "entry_kind": row["kind"],
            })
    return entries


def _date_range(start_date: str, end_date: str) -> list[str]:
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    days: list[str] = []
    cursor = start
    while cursor <= end:
        days.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=1)
    return days


def _collection_for_event(
    db: Database,
    owner_id: str,
    item,
    cache: dict[str, dict[str, Any] | None],
) -> dict[str, Any] | None:
    collection_id = getattr(item, "event_collection_id", None)
    if not collection_id:
        return None
    if collection_id not in cache:
        cache[collection_id] = db.get_event_collection(collection_id, owner_id)
    return cache[collection_id]


def build_dashboard_overview(
    db: Database,
    owner_id: str,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S")
    month_start_date, month_start_iso, month_end_iso = _month_bounds(now)
    agenda_end_iso = (now + timedelta(days=21)).replace(hour=23, minute=59, second=59, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
    month_ago = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    month_ago_iso = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")

    raw_events_month = db.get_events_for_range(owner_id, month_start_iso, month_end_iso)
    raw_events_agenda = db.get_events_for_range(owner_id, month_start_iso, agenda_end_iso)

    tasks_open = db.get_items(owner_id, filters={
        "type": "task",
        "status": "open",
        "sort_field": "plan_date",
        "sort_order": "ASC",
    }, limit=20)
    tasks_completed = db.get_items(owner_id, filters={
        "type": "task",
        "status": "done",
        "sort_field": "updated_at",
        "sort_order": "DESC",
    }, limit=16)

    recent_ledger = db.get_items(owner_id, filters={
        "type": "ledger",
        "date_field": "ledger_date",
        "start_date": month_start_date,
        "end_date": today,
        "sort_field": "ledger_date",
        "sort_order": "DESC",
    }, limit=8)

    month_ledger = _get_all_items(db, owner_id, filters={
        "type": "ledger",
        "date_field": "ledger_date",
        "start_date": month_start_date,
        "end_date": today,
    })

    spending_by_day: dict[str, float] = {}
    month_income = 0.0
    month_expense = 0.0
    for item in month_ledger:
        day = getattr(item, "ledger_date", None) or today
        amount = _ledger_amount_value(item)
        transaction_type = getattr(item, "transaction_type", "expense")
        if transaction_type == "expense":
            spending_by_day[day] = spending_by_day.get(day, 0.0) + amount
            month_expense += amount
        elif transaction_type == "income":
            month_income += amount

    spending_trend = [{
        "date": day,
        "amount": round(spending_by_day.get(day, 0.0), 2),
    } for day in _date_range(month_start_date, today)]

    events_month: list[dict] = []
    collection_cache: dict[str, dict[str, Any] | None] = {}
    for item in raw_events_month:
        events_month.extend(_event_entries(
            item,
            month_start_iso,
            month_end_iso,
            _collection_for_event(db, owner_id, item, collection_cache),
        ))
    events_month.sort(key=lambda event: event.get("start_time") or "")

    events_agenda: list[dict] = []
    for item in raw_events_agenda:
        events_agenda.extend(_event_entries(
            item,
            month_start_iso,
            agenda_end_iso,
            _collection_for_event(db, owner_id, item, collection_cache),
        ))
    events_agenda.sort(key=lambda event: event.get("start_time") or "")

    conn = db.get_connection()
    active_tasks_count = conn.execute(
        """
        SELECT COUNT(*) FROM items
        WHERE type='task' AND owner_id=? AND deleted=0 AND status = 'open'
        """,
        (owner_id,),
    ).fetchone()[0]
    recent_diary_count = conn.execute(
        """
        SELECT COUNT(*) FROM items
        WHERE type='diary' AND owner_id=? AND deleted=0 AND diary_date BETWEEN ? AND ?
        """,
        (owner_id, month_ago, today),
    ).fetchone()[0]
    recent_completed_count = conn.execute(
        """
        SELECT COUNT(*) FROM items
        WHERE type='task'
          AND owner_id=?
          AND deleted=0
          AND status='done'
          AND COALESCE(NULLIF(completed_at, ''), NULLIF(updated_at, '')) BETWEEN ? AND ?
        """,
        (owner_id, month_ago_iso, now_iso),
    ).fetchone()[0]

    active_tasks = list(tasks_open)
    active_tasks.sort(key=lambda task: (
        getattr(task, "priority", 99) if getattr(task, "priority", None) is not None else 99,
        getattr(task, "plan_date", "") or "9999-99-99",
        getattr(task, "deadline_at", "") or "9999-99-99T99:99:99",
    ))
    tasks_completed.sort(
        key=lambda task: getattr(task, "completed_at", "") or getattr(task, "updated_at", "") or "",
        reverse=True,
    )

    return {
        "summary": {
            "events_month": len(events_month),
            "tasks_pending": active_tasks_count,
            "tasks_done_recent": recent_completed_count,
            "ledger_month_expense": round(month_expense, 2),
            "diary_month": recent_diary_count,
        },
        "events_month": events_month,
        "events_agenda": events_agenda,
        "tasks": {
            "active": [_to_dict(task) for task in active_tasks[:8]],
            "completed": [_to_dict(task) for task in tasks_completed[:4]],
        },
        "recent_ledger": [_to_dict(item) for item in recent_ledger],
        "spending_trend": spending_trend,
        "month_summary": {
            "income": round(month_income, 2),
            "expense": round(month_expense, 2),
            "balance": round(month_income - month_expense, 2),
        },
    }
