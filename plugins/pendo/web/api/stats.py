"""Statistics aggregation endpoints."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query

from ...services.db import Database
from ..deps import get_db, get_current_user

router = APIRouter()


def _parse_range(range_str: str | None) -> tuple[str, str]:
    """Parse range string into (start, end) dates."""
    now = datetime.now()
    if not range_str or range_str == "month":
        start = now.strftime("%Y-%m-01")
        end = now.strftime("%Y-%m-%d")
    elif range_str == "week":
        start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        end = now.strftime("%Y-%m-%d")
    elif range_str == "quarter":
        q_month = ((now.month - 1) // 3) * 3 + 1
        start = f"{now.year}-{q_month:02d}-01"
        end = now.strftime("%Y-%m-%d")
    elif range_str == "year":
        start = f"{now.year}-01-01"
        end = now.strftime("%Y-%m-%d")
    elif ".." in range_str:
        parts = range_str.split("..")
        start, end = parts[0], parts[1]
    else:
        start = range_str + "-01"
        end = range_str + "-31"
    return start, end


@router.get("/stats/ledger")
def ledger_stats(
    range: str | None = Query(None, alias="range"),
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Ledger statistics."""
    start, end = _parse_range(range)
    conn = db.get_connection()

    monthly = conn.execute("""
        SELECT strftime('%Y-%m', ledger_date) AS month, direction, SUM(amount) AS total
        FROM items WHERE type='ledger' AND owner_id=? AND deleted=0
        AND ledger_date BETWEEN ? AND ?
        GROUP BY month, direction ORDER BY month
    """, (owner_id, start, end)).fetchall()

    by_category = conn.execute("""
        SELECT ledger_category, direction, SUM(amount) AS total, COUNT(*) AS count
        FROM items WHERE type='ledger' AND owner_id=? AND deleted=0
        AND ledger_date BETWEEN ? AND ?
        GROUP BY ledger_category, direction
    """, (owner_id, start, end)).fetchall()

    daily = conn.execute("""
        SELECT ledger_date, direction, SUM(amount) AS total
        FROM items WHERE type='ledger' AND owner_id=? AND deleted=0
        AND ledger_date BETWEEN ? AND ?
        GROUP BY ledger_date, direction ORDER BY ledger_date
    """, (owner_id, start, end)).fetchall()

    return {
        "ok": True,
        "data": {
            "monthly": [{"month": r[0], "direction": r[1], "total": r[2]} for r in monthly],
            "by_category": [{"category": r[0], "direction": r[1], "total": r[2], "count": r[3]} for r in by_category],
            "daily": [{"date": r[0], "direction": r[1], "total": r[2]} for r in daily],
        },
        "message": "",
    }


@router.get("/stats/tasks")
def task_stats(
    range: str | None = Query(None, alias="range"),
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Task statistics."""
    start, end = _parse_range(range)
    conn = db.get_connection()

    totals = conn.execute("""
        SELECT status, COUNT(*) AS count
        FROM items WHERE type='task' AND owner_id=? AND deleted=0
        GROUP BY status
    """, (owner_id,)).fetchall()

    weekly = conn.execute("""
        SELECT strftime('%Y-W%W', created_at) AS week,
            COUNT(*) AS total,
            SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done
        FROM items WHERE type='task' AND owner_id=? AND deleted=0
        AND created_at BETWEEN ? AND ?
        GROUP BY week ORDER BY week
    """, (owner_id, start, end)).fetchall()

    by_category = conn.execute("""
        SELECT category, COUNT(*) AS count
        FROM items WHERE type='task' AND owner_id=? AND deleted=0
        GROUP BY category
    """, (owner_id,)).fetchall()

    by_priority = conn.execute("""
        SELECT priority, COUNT(*) AS count
        FROM items WHERE type='task' AND owner_id=? AND deleted=0
        GROUP BY priority
    """, (owner_id,)).fetchall()

    week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
    new_this_week = conn.execute("""
        SELECT COUNT(*) FROM items
        WHERE type='task' AND owner_id=? AND deleted=0 AND created_at >= ?
    """, (owner_id, week_start)).fetchone()[0]

    return {
        "ok": True,
        "data": {
            "totals": {r[0]: r[1] for r in totals},
            "weekly": [{"week": r[0], "total": r[1], "done": r[2]} for r in weekly],
            "by_category": [{"category": r[0], "count": r[1]} for r in by_category],
            "by_priority": [{"priority": r[0], "count": r[1]} for r in by_priority],
            "new_this_week": new_this_week,
        },
        "message": "",
    }


@router.get("/stats/events")
def event_stats(
    range: str | None = Query(None, alias="range"),
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Event statistics."""
    start, end = _parse_range(range)
    conn = db.get_connection()

    weekly = conn.execute("""
        SELECT strftime('%Y-W%W', start_time) AS week, COUNT(*) AS count
        FROM items WHERE type='event' AND owner_id=? AND deleted=0
        AND start_time BETWEEN ? AND ?
        GROUP BY week ORDER BY week
    """, (owner_id, start, end)).fetchall()

    time_slots = conn.execute("""
        SELECT CASE
            WHEN CAST(strftime('%H', start_time) AS INT) BETWEEN 6 AND 8 THEN '06-09'
            WHEN CAST(strftime('%H', start_time) AS INT) BETWEEN 9 AND 11 THEN '09-12'
            WHEN CAST(strftime('%H', start_time) AS INT) BETWEEN 12 AND 13 THEN '12-14'
            WHEN CAST(strftime('%H', start_time) AS INT) BETWEEN 14 AND 17 THEN '14-18'
            WHEN CAST(strftime('%H', start_time) AS INT) BETWEEN 18 AND 20 THEN '18-21'
            ELSE '21-24'
        END AS time_slot, COUNT(*) AS count
        FROM items WHERE type='event' AND owner_id=? AND deleted=0
        AND start_time IS NOT NULL
        GROUP BY time_slot ORDER BY time_slot
    """, (owner_id,)).fetchall()

    by_category = conn.execute("""
        SELECT category, COUNT(*) AS count
        FROM items WHERE type='event' AND owner_id=? AND deleted=0
        GROUP BY category
    """, (owner_id,)).fetchall()

    return {
        "ok": True,
        "data": {
            "weekly": [{"week": r[0], "count": r[1]} for r in weekly],
            "time_slots": [{"slot": r[0], "count": r[1]} for r in time_slots],
            "by_category": [{"category": r[0], "count": r[1]} for r in by_category],
        },
        "message": "",
    }
