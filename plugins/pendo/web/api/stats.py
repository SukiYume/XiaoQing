"""Statistics aggregation endpoints."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query

from ...services.db import Database
from ..analytics.diary_overview import build_diary_overview
from ..analytics.ledger_insights import build_ledger_insights
from ..analytics.notes_overview import build_notes_overview
from ..analytics.task_overview import build_task_overview
from ..deps import get_db, get_current_user

router = APIRouter()


LEDGER_HISTOGRAM_BUCKETS = [
    ("0-20", 0, 20),
    ("20-50", 20, 50),
    ("50-100", 50, 100),
    ("100-300", 100, 300),
    ("300-1000", 300, 1000),
    ("1000+", 1000, None),
]


def _aggregate_monthly(rows):
    """Aggregate monthly rows by month, merging income/expense."""
    months = {}
    for r in rows:
        month, direction, total = r[0], r[1], r[2]
        if month not in months:
            months[month] = {"month": month, "income": 0, "expense": 0}
        if direction == "income":
            months[month]["income"] = total
        else:
            months[month]["expense"] = total
    return list(months.values())


def _aggregate_daily(rows):
    """Aggregate daily rows by date, merging income/expense."""
    days = {}
    for r in rows:
        date, direction, total = r[0], r[1], r[2]
        if date not in days:
            days[date] = {"date": date, "income": 0, "expense": 0}
        if direction == "income":
            days[date]["income"] = total
        else:
            days[date]["expense"] = total
    return list(days.values())


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
    elif range_str == "last_year":
        year = now.year - 1
        start = f"{year}-01-01"
        end = f"{year}-12-31"
    elif ".." in range_str:
        parts = range_str.split("..")
        start, end = parts[0], parts[1]
    else:
        start = range_str + "-01"
        end = range_str + "-31"
    return start, end


def _build_amount_histogram(amounts: list[float]) -> list[dict]:
    """Build expense amount histogram buckets."""
    histogram = []
    for label, lower, upper in LEDGER_HISTOGRAM_BUCKETS:
        if upper is None:
            count = sum(1 for amount in amounts if amount >= lower)
        else:
            count = sum(1 for amount in amounts if lower <= amount < upper)
        histogram.append({"bucket": label, "count": count})
    return histogram


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
        ORDER BY total DESC, ledger_category
    """, (owner_id, start, end)).fetchall()

    daily = conn.execute("""
        SELECT ledger_date, direction, SUM(amount) AS total
        FROM items WHERE type='ledger' AND owner_id=? AND deleted=0
        AND ledger_date BETWEEN ? AND ?
        GROUP BY ledger_date, direction ORDER BY ledger_date
    """, (owner_id, start, end)).fetchall()

    expense_amounts = conn.execute("""
        SELECT amount
        FROM items WHERE type='ledger' AND owner_id=? AND deleted=0
        AND direction='expense' AND ledger_date BETWEEN ? AND ?
        ORDER BY amount
    """, (owner_id, start, end)).fetchall()

    return {
        "ok": True,
        "data": {
            "monthly": _aggregate_monthly(monthly),
            "expense_by_category": [{"category": r[0], "total": r[2]} for r in by_category if r[1] == "expense"],
            "income_by_category": [{"category": r[0], "total": r[2]} for r in by_category if r[1] == "income"],
            "daily": _aggregate_daily(daily),
            "expense_amount_histogram": _build_amount_histogram([float(r[0] or 0) for r in expense_amounts]),
        },
        "message": "",
    }


@router.get("/stats/ledger/insights")
def ledger_visual_insights(
    direction: str | None = None,
    category: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    compare_mode: str = "previous_period",
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Compact ledger insights for the ledger page visual cards."""
    return {
        "ok": True,
        "data": build_ledger_insights(
            db=db,
            owner_id=owner_id,
            direction=direction,
            category=category,
            start_date=start_date,
            end_date=end_date,
            amount_min=amount_min,
            amount_max=amount_max,
            compare_mode=compare_mode,
        ),
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

    range_condition = """
        (
            date(created_at) BETWEEN ? AND ?
            OR (completed_at IS NOT NULL AND date(completed_at) BETWEEN ? AND ?)
        )
    """

    totals = conn.execute("""
        SELECT status, COUNT(*) AS count
        FROM items WHERE type='task' AND owner_id=? AND deleted=0
        AND """ + range_condition + """
        GROUP BY status
    """, (owner_id, start, end, start, end)).fetchall()

    created_weekly = conn.execute("""
        SELECT strftime('%Y-W%W', created_at) AS week, COUNT(*) AS count
        FROM items WHERE type='task' AND owner_id=? AND deleted=0
        AND date(created_at) BETWEEN ? AND ?
        GROUP BY week ORDER BY week
    """, (owner_id, start, end)).fetchall()

    completed_weekly = conn.execute("""
        SELECT strftime('%Y-W%W', completed_at) AS week, COUNT(*) AS count
        FROM items WHERE type='task' AND owner_id=? AND deleted=0
        AND completed_at IS NOT NULL
        AND date(completed_at) BETWEEN ? AND ?
        GROUP BY week ORDER BY week
    """, (owner_id, start, end)).fetchall()

    weekly_map: dict[str, dict[str, int | str]] = {}
    for week, count in created_weekly:
        weekly_map.setdefault(week, {"week": week, "total": 0, "done": 0})
        weekly_map[week]["total"] = count
    for week, count in completed_weekly:
        weekly_map.setdefault(week, {"week": week, "total": 0, "done": 0})
        weekly_map[week]["done"] = count
    weekly = [weekly_map[key] for key in sorted(weekly_map.keys())]

    by_category = conn.execute("""
        SELECT category, COUNT(*) AS count
        FROM items WHERE type='task' AND owner_id=? AND deleted=0
        AND """ + range_condition + """
        GROUP BY category
        ORDER BY count DESC, category
    """, (owner_id, start, end, start, end)).fetchall()

    by_priority = conn.execute("""
        SELECT priority, COUNT(*) AS count
        FROM items WHERE type='task' AND owner_id=? AND deleted=0
        AND """ + range_condition + """
        GROUP BY priority
        ORDER BY priority
    """, (owner_id, start, end, start, end)).fetchall()

    new_this_week = conn.execute("""
        SELECT COUNT(*) FROM items
        WHERE type='task' AND owner_id=? AND deleted=0 AND date(created_at) BETWEEN ? AND ?
    """, (owner_id, start, end)).fetchone()[0]

    return {
        "ok": True,
        "data": {
            "totals": {r[0]: r[1] for r in totals},
            "weekly": weekly,
            "by_category": [{"category": r[0], "count": r[1]} for r in by_category],
            "by_priority": [{"priority": r[0], "count": r[1]} for r in by_priority],
            "new_this_week": new_this_week,
        },
        "message": "",
    }


@router.get("/stats/tasks/overview")
def task_overview(
    today: str | None = None,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Compact task overview for the redesigned tasks page."""
    return {
        "ok": True,
        "data": build_task_overview(db=db, owner_id=owner_id, today=today),
        "message": "",
    }


@router.get("/stats/notes/overview")
def notes_overview(
    today: str | None = None,
    category: str | None = None,
    tags: str | None = None,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Compact note overview for the redesigned notes page."""
    return {
        "ok": True,
        "data": build_notes_overview(db=db, owner_id=owner_id, today=today, category=category, tags=tags),
        "message": "",
    }


@router.get("/stats/diary/overview")
def diary_overview(
    year: int,
    month: int,
    today: str | None = None,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Compact diary overview for the redesigned diary page."""
    return {
        "ok": True,
        "data": build_diary_overview(db=db, owner_id=owner_id, year=year, month=month, today=today),
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
        AND start_time IS NOT NULL
        AND date(start_time) BETWEEN ? AND ?
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
        AND date(start_time) BETWEEN ? AND ?
        GROUP BY time_slot ORDER BY time_slot
    """, (owner_id, start, end)).fetchall()

    weekday_slots = conn.execute("""
        SELECT
            CASE CAST(strftime('%w', start_time) AS INT)
                WHEN 0 THEN '周日'
                WHEN 1 THEN '周一'
                WHEN 2 THEN '周二'
                WHEN 3 THEN '周三'
                WHEN 4 THEN '周四'
                WHEN 5 THEN '周五'
                ELSE '周六'
            END AS weekday,
            CASE
                WHEN CAST(strftime('%H', start_time) AS INT) BETWEEN 6 AND 8 THEN '06-09'
                WHEN CAST(strftime('%H', start_time) AS INT) BETWEEN 9 AND 11 THEN '09-12'
                WHEN CAST(strftime('%H', start_time) AS INT) BETWEEN 12 AND 13 THEN '12-14'
                WHEN CAST(strftime('%H', start_time) AS INT) BETWEEN 14 AND 17 THEN '14-18'
                WHEN CAST(strftime('%H', start_time) AS INT) BETWEEN 18 AND 20 THEN '18-21'
                ELSE '21-24'
            END AS time_slot,
            COUNT(*) AS count
        FROM items WHERE type='event' AND owner_id=? AND deleted=0
        AND start_time IS NOT NULL
        AND date(start_time) BETWEEN ? AND ?
        GROUP BY weekday, time_slot
        ORDER BY CASE weekday
            WHEN '周一' THEN 1
            WHEN '周二' THEN 2
            WHEN '周三' THEN 3
            WHEN '周四' THEN 4
            WHEN '周五' THEN 5
            WHEN '周六' THEN 6
            ELSE 7
        END, time_slot
    """, (owner_id, start, end)).fetchall()

    by_category = conn.execute("""
        SELECT category, COUNT(*) AS count
        FROM items WHERE type='event' AND owner_id=? AND deleted=0
        AND start_time IS NOT NULL
        AND date(start_time) BETWEEN ? AND ?
        GROUP BY category
        ORDER BY count DESC, category
    """, (owner_id, start, end)).fetchall()

    return {
        "ok": True,
        "data": {
            "weekly": [{"week": r[0], "count": r[1]} for r in weekly],
            "time_slots": [{"slot": r[0], "count": r[1]} for r in time_slots],
            "weekday_slots": [{"weekday": r[0], "slot": r[1], "count": r[2]} for r in weekday_slots],
            "by_category": [{"category": r[0], "count": r[1]} for r in by_category],
        },
        "message": "",
    }
