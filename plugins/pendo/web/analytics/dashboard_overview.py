"""Dashboard overview aggregation for the web UI."""

from __future__ import annotations

from datetime import datetime, timedelta

from ...services.db import Database


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


def _date_range(start_date: str, end_date: str) -> list[str]:
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    days: list[str] = []
    cursor = start
    while cursor <= end:
        days.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=1)
    return days


def build_dashboard_overview(
    db: Database,
    owner_id: str,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S")
    month_start_date, month_start_iso, month_end_iso = _month_bounds(now)
    month_ago = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    month_ago_iso = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")

    events_month = db.get_items(owner_id, filters={
        "type": "event",
        "date_field": "start_time",
        "start_date": month_start_iso,
        "end_date": month_end_iso,
        "sort_field": "start_time",
        "sort_order": "ASC",
    }, limit=200)

    tasks_todo = db.get_items(owner_id, filters={
        "type": "task",
        "status": "todo",
        "sort_field": "due_time",
        "sort_order": "ASC",
    }, limit=20)
    tasks_in_progress = db.get_items(owner_id, filters={
        "type": "task",
        "status": "in_progress",
        "sort_field": "due_time",
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

    month_ledger = db.get_items(owner_id, filters={
        "type": "ledger",
        "date_field": "ledger_date",
        "start_date": month_start_date,
        "end_date": today,
    }, limit=500)

    spending_by_day: dict[str, float] = {}
    month_income = 0.0
    month_expense = 0.0
    for item in month_ledger:
        day = getattr(item, "ledger_date", None) or today
        amount = float(getattr(item, "amount", 0) or 0)
        direction = getattr(item, "direction", "expense")
        if direction == "expense":
            spending_by_day[day] = spending_by_day.get(day, 0.0) + amount
            month_expense += amount
        else:
            month_income += amount

    spending_trend = [{
        "date": day,
        "amount": round(spending_by_day.get(day, 0.0), 2),
    } for day in _date_range(month_start_date, today)]

    conn = db.get_connection()
    active_tasks_count = conn.execute(
        """
        SELECT COUNT(*) FROM items
        WHERE type='task' AND owner_id=? AND deleted=0 AND status IN ('todo', 'in_progress')
        """,
        (owner_id,),
    ).fetchone()[0]
    events_month_count = conn.execute(
        """
        SELECT COUNT(*) FROM items
        WHERE type='event' AND owner_id=? AND deleted=0 AND start_time BETWEEN ? AND ?
        """,
        (owner_id, month_start_iso, month_end_iso),
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

    active_tasks = tasks_todo + tasks_in_progress
    active_tasks.sort(key=lambda task: (
        getattr(task, "status", "") != "in_progress",
        getattr(task, "priority", 99) if getattr(task, "priority", None) is not None else 99,
        getattr(task, "due_time", "") or "9999-99-99T99:99:99",
    ))
    tasks_completed.sort(
        key=lambda task: getattr(task, "completed_at", "") or getattr(task, "updated_at", "") or "",
        reverse=True,
    )

    return {
        "summary": {
            "events_month": events_month_count,
            "tasks_pending": active_tasks_count,
            "tasks_done_recent": recent_completed_count,
            "ledger_month_expense": round(month_expense, 2),
            "diary_month": recent_diary_count,
        },
        "events_month": [_to_dict(event) for event in events_month],
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
