"""Dashboard aggregation endpoint."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends

from ...services.db import Database
from ..deps import get_db, get_current_user

router = APIRouter()


@router.get("/dashboard")
def get_dashboard(
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Get dashboard overview data."""
    today = datetime.now().strftime("%Y-%m-%d")
    today_start = f"{today}T00:00:00"
    today_end = f"{today}T23:59:59"
    month_start = datetime.now().strftime("%Y-%m-01")
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    # Today's events
    events = db.get_items(owner_id, filters={
        "type": "event",
        "date_field": "start_time",
        "start_date": today_start,
        "end_date": today_end,
    }, limit=50)

    # Pending tasks
    tasks = db.get_items(owner_id, filters={"type": "task", "status": "todo"}, limit=20)
    tasks_in_progress = db.get_items(owner_id, filters={"type": "task", "status": "in_progress"}, limit=20)

    # Recent ledger entries
    recent_ledger = db.get_items(owner_id, filters={
        "type": "ledger", "date_field": "ledger_date",
        "start_date": week_ago, "end_date": today,
    }, limit=20)

    # Monthly spending trend
    month_ledger = db.get_items(owner_id, filters={
        "type": "ledger", "date_field": "ledger_date",
        "start_date": month_start, "end_date": today,
    }, limit=500)

    spending_by_day = {}
    month_income = 0.0
    month_expense = 0.0
    for item in month_ledger:
        d = getattr(item, "ledger_date", None) or today
        amt = getattr(item, "amount", 0) or 0
        direction = getattr(item, "direction", "expense")
        if direction == "expense":
            spending_by_day[d] = spending_by_day.get(d, 0) + amt
            month_expense += amt
        else:
            month_income += amt

    spending_trend = [{"date": k, "amount": v} for k, v in sorted(spending_by_day.items())]

    # Summary counts using COUNT queries (not get_items with limit=0)
    conn = db.get_connection()
    recent_ledger_count = conn.execute(
        "SELECT COUNT(*) FROM items WHERE type='ledger' AND owner_id=? AND deleted=0 AND ledger_date BETWEEN ? AND ?",
        (owner_id, week_ago, today),
    ).fetchone()[0]
    recent_diary_count = conn.execute(
        "SELECT COUNT(*) FROM items WHERE type='diary' AND owner_id=? AND deleted=0 AND diary_date BETWEEN ? AND ?",
        (owner_id, month_ago, today),
    ).fetchone()[0]

    def to_dict(item):
        return item.to_dict() if hasattr(item, "to_dict") else {}

    return {
        "ok": True,
        "data": {
            "summary": {
                "events_today": len(events),
                "tasks_pending": len(tasks) + len(tasks_in_progress),
                "ledger_week": recent_ledger_count,
                "diary_month": recent_diary_count,
            },
            "events": [to_dict(e) for e in events],
            "tasks": [to_dict(t) for t in (tasks + tasks_in_progress)],
            "recent_ledger": [to_dict(l) for l in recent_ledger[:10]],
            "spending_trend": spending_trend,
            "month_summary": {
                "income": month_income,
                "expense": month_expense,
                "balance": month_income - month_expense,
            },
        },
        "message": "",
    }
