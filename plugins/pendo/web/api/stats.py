"""Statistics aggregation endpoints."""

from collections import Counter
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from ...services.db import Database
from ..analytics.diary_overview import build_diary_overview
from ..analytics.ledger_insights import build_ledger_insights
from ..analytics.notes_overview import build_notes_overview
from ..analytics.task_overview import build_task_overview
from ..deps import get_db, get_current_user

router = APIRouter()
LEDGER_AMOUNT_EXPR = Database._LEDGER_AMOUNT_CENTS_EXPR
LEDGER_AMOUNT_TOTAL_EXPR = f"ROUND(COALESCE(SUM({LEDGER_AMOUNT_EXPR}), 0) / 100.0, 2)"

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
        month, transaction_type, total = r[0], r[1], r[2]
        if month not in months:
            months[month] = {"month": month, "income": 0, "expense": 0}
        if transaction_type == "income":
            months[month]["income"] = total
        elif transaction_type == "expense":
            months[month]["expense"] = total
    return list(months.values())


def _aggregate_daily(rows):
    """Aggregate daily rows by date, merging income/expense."""
    days = {}
    for r in rows:
        date, transaction_type, total = r[0], r[1], r[2]
        if date not in days:
            days[date] = {"date": date, "income": 0, "expense": 0}
        if transaction_type == "income":
            days[date]["income"] = total
        elif transaction_type == "expense":
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
    elif range_str == "all":
        start = "1970-01-01"
        end = now.strftime("%Y-%m-%d")
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


def _month_floor(value: date | datetime) -> date:
    """Return the first day of the month for a date-like value."""
    return date(value.year, value.month, 1)


def _shift_months(value: date, delta: int) -> date:
    """Shift a month-start date by `delta` months."""
    month_index = (value.year * 12 + value.month - 1) + delta
    year, month = divmod(month_index, 12)
    return date(year, month + 1, 1)


@router.get("/stats/ledger")
def ledger_stats(
    range: str | None = Query(None, alias="range"),
    start_date: str | None = None,
    end_date: str | None = None,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Ledger statistics."""
    if start_date and end_date:
        start, end = start_date, end_date
    else:
        start, end = _parse_range(range)
    conn = db.get_connection()

    monthly = conn.execute(
        f"""
        SELECT strftime('%Y-%m', ledger_date) AS month,
               transaction_type AS ledger_kind,
               {LEDGER_AMOUNT_TOTAL_EXPR} AS total
        FROM items WHERE type='ledger' AND owner_id=? AND deleted=0
        AND ledger_date BETWEEN ? AND ?
        GROUP BY month, ledger_kind ORDER BY month
    """,
        (owner_id, start, end),
    ).fetchall()

    by_category = conn.execute(
        f"""
        SELECT ledger_category,
               transaction_type AS ledger_kind,
               {LEDGER_AMOUNT_TOTAL_EXPR} AS total,
               COUNT(*) AS count
        FROM items WHERE type='ledger' AND owner_id=? AND deleted=0
        AND ledger_date BETWEEN ? AND ?
        GROUP BY ledger_category, ledger_kind
        ORDER BY total DESC, ledger_category
    """,
        (owner_id, start, end),
    ).fetchall()

    daily = conn.execute(
        f"""
        SELECT ledger_date,
               transaction_type AS ledger_kind,
               {LEDGER_AMOUNT_TOTAL_EXPR} AS total
        FROM items WHERE type='ledger' AND owner_id=? AND deleted=0
        AND ledger_date BETWEEN ? AND ?
        GROUP BY ledger_date, ledger_kind ORDER BY ledger_date
    """,
        (owner_id, start, end),
    ).fetchall()

    expense_amounts = conn.execute(
        f"""
        SELECT ROUND({LEDGER_AMOUNT_EXPR} / 100.0, 2)
        FROM items WHERE type='ledger' AND owner_id=? AND deleted=0
        AND transaction_type='expense' AND ledger_date BETWEEN ? AND ?
        ORDER BY {LEDGER_AMOUNT_EXPR}
    """,
        (owner_id, start, end),
    ).fetchall()

    return {
        "ok": True,
        "data": {
            "monthly": _aggregate_monthly(monthly),
            "expense_by_category": [
                {"category": r[0], "total": r[2]} for r in by_category if r[1] == "expense"
            ],
            "income_by_category": [
                {"category": r[0], "total": r[2]} for r in by_category if r[1] == "income"
            ],
            "transfer_by_category": [
                {"category": r[0], "total": r[2]} for r in by_category if r[1] == "transfer"
            ],
            "daily": _aggregate_daily(daily),
            "expense_amount_histogram": _build_amount_histogram(
                [float(r[0] or 0) for r in expense_amounts]
            ),
        },
        "message": "",
    }


@router.get("/stats/ledger/insights")
def ledger_visual_insights(
    transaction_type: str | None = None,
    category: str | None = None,
    account_name: str | None = None,
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
            transaction_type=transaction_type,
            category=category,
            account_name=account_name,
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
            OR (cancelled_at IS NOT NULL AND date(cancelled_at) BETWEEN ? AND ?)
            OR (plan_date IS NOT NULL AND plan_date BETWEEN ? AND ?)
        )
    """
    range_params = (start, end, start, end, start, end, start, end)

    totals_rows = conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM items WHERE type='task' AND owner_id=? AND deleted=0
        AND """
        + range_condition
        + """
        GROUP BY status
    """,
        (owner_id, *range_params),
    ).fetchall()

    totals_raw = {row[0]: row[1] for row in totals_rows}
    totals = {
        "open": int(totals_raw.get("open", 0) or 0),
        "done": int(totals_raw.get("done", 0) or 0),
        "cancelled": int(totals_raw.get("cancelled", 0) or 0),
    }
    totals["closed"] = totals["done"] + totals["cancelled"]

    created_weekly = conn.execute(
        """
        SELECT strftime('%Y-W%W', created_at) AS week, COUNT(*) AS count
        FROM items WHERE type='task' AND owner_id=? AND deleted=0
        AND date(created_at) BETWEEN ? AND ?
        GROUP BY week ORDER BY week
    """,
        (owner_id, start, end),
    ).fetchall()

    completed_weekly = conn.execute(
        """
        SELECT strftime('%Y-W%W', completed_at) AS week, COUNT(*) AS count
        FROM items WHERE type='task' AND owner_id=? AND deleted=0
        AND completed_at IS NOT NULL
        AND status='done'
        AND date(completed_at) BETWEEN ? AND ?
        GROUP BY week ORDER BY week
    """,
        (owner_id, start, end),
    ).fetchall()

    cancelled_weekly = conn.execute(
        """
        SELECT strftime('%Y-W%W', cancelled_at) AS week, COUNT(*) AS count
        FROM items WHERE type='task' AND owner_id=? AND deleted=0
        AND cancelled_at IS NOT NULL
        AND status='cancelled'
        AND date(cancelled_at) BETWEEN ? AND ?
        GROUP BY week ORDER BY week
    """,
        (owner_id, start, end),
    ).fetchall()

    weekly_map: dict[str, dict[str, int | str]] = {}
    for week, count in created_weekly:
        weekly_map.setdefault(week, {"week": week, "created": 0, "done": 0, "cancelled": 0})
        weekly_map[week]["created"] = count
    for week, count in completed_weekly:
        weekly_map.setdefault(week, {"week": week, "created": 0, "done": 0, "cancelled": 0})
        weekly_map[week]["done"] = count
    for week, count in cancelled_weekly:
        weekly_map.setdefault(week, {"week": week, "created": 0, "done": 0, "cancelled": 0})
        weekly_map[week]["cancelled"] = count
    weekly = [weekly_map[key] for key in sorted(weekly_map.keys())]

    task_rows = conn.execute(
        """
        SELECT status, category, priority, plan_date, deadline_at
        FROM items WHERE type='task' AND owner_id=? AND deleted=0
        AND """
        + range_condition,
        (owner_id, *range_params),
    ).fetchall()

    open_rows = [row for row in task_rows if row[0] == "open"]
    plan_counter = Counter()
    text_category_counter = Counter()
    for status, category, priority, plan_date, deadline_at in open_rows:
        cat = str(category or "").strip()
        plan_key = str(plan_date or "").strip() or str(deadline_at or "")[:10]
        if plan_key:
            plan_counter[plan_key] += 1
        if cat and cat != "未分类":
            text_category_counter[cat] += 1

    by_priority = conn.execute(
        """
        SELECT priority, COUNT(*) AS count
        FROM items WHERE type='task' AND owner_id=? AND deleted=0
        AND """
        + range_condition
        + """
        GROUP BY priority
        ORDER BY priority
    """,
        (owner_id, *range_params),
    ).fetchall()

    new_this_week = conn.execute(
        """
        SELECT COUNT(*) FROM items
        WHERE type='task' AND owner_id=? AND deleted=0 AND date(created_at) BETWEEN ? AND ?
    """,
        (owner_id, start, end),
    ).fetchone()[0]

    return {
        "ok": True,
        "data": {
            "totals": totals,
            "weekly": weekly,
            "by_plan": [{"plan": key, "count": count} for key, count in sorted(plan_counter.items())],
            "by_category": [{"category": key, "count": count} for key, count in text_category_counter.most_common(8)],
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
    start_date: str | None = None,
    end_date: str | None = None,
    category: str | None = None,
    tags: str | None = None,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Compact note overview for the redesigned notes page."""
    return {
        "ok": True,
        "data": build_notes_overview(
            db=db,
            owner_id=owner_id,
            today=today,
            start_date=start_date,
            end_date=end_date,
            category=category,
            tags=tags,
        ),
        "message": "",
    }


@router.get("/stats/diary/overview")
def diary_overview(
    year: int | None = None,
    month: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    cadence_granularity: str = "day",
    today: str | None = None,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Compact diary overview for the redesigned diary page and stats range cards."""
    try:
        data = build_diary_overview(
            db=db,
            owner_id=owner_id,
            year=year,
            month=month,
            start_date=start_date,
            end_date=end_date,
            today=today,
            cadence_granularity=cadence_granularity,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "data": data, "message": ""}


@router.get("/stats/events")
def event_stats(
    range: str | None = Query(None, alias="range"),
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Event statistics."""
    start, end = _parse_range(range)
    conn = db.get_connection()

    weekly = conn.execute(
        """
        SELECT strftime('%Y-W%W', start_time) AS week, COUNT(*) AS count
        FROM items WHERE type='event' AND owner_id=? AND deleted=0
        AND start_time IS NOT NULL
        AND date(start_time) BETWEEN ? AND ?
        GROUP BY week ORDER BY week
    """,
        (owner_id, start, end),
    ).fetchall()

    time_slots = conn.execute(
        """
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
    """,
        (owner_id, start, end),
    ).fetchall()

    weekday_slots = conn.execute(
        """
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
    """,
        (owner_id, start, end),
    ).fetchall()

    by_category = conn.execute(
        """
        SELECT COALESCE(NULLIF(NULLIF(i.category, ''), '未分类'), NULLIF(c.category, ''), '未分类') AS resolved_category,
               COUNT(*) AS count
        FROM items i
        LEFT JOIN event_collections c
          ON c.id = i.event_collection_id AND c.owner_id = i.owner_id AND c.deleted = 0
        WHERE i.type='event' AND i.owner_id=? AND i.deleted=0
        AND i.start_time IS NOT NULL
        AND date(i.start_time) BETWEEN ? AND ?
        GROUP BY resolved_category
        ORDER BY count DESC, resolved_category
    """,
        (owner_id, start, end),
    ).fetchall()

    return {
        "ok": True,
        "data": {
            "weekly": [{"week": r[0], "count": r[1]} for r in weekly],
            "time_slots": [{"slot": r[0], "count": r[1]} for r in time_slots],
            "weekday_slots": [
                {"weekday": r[0], "slot": r[1], "count": r[2]} for r in weekday_slots
            ],
            "by_category": [{"category": r[0], "count": r[1]} for r in by_category],
        },
        "message": "",
    }


@router.get("/stats/ledger/comparison")
def ledger_comparison(
    months: int = Query(6, ge=3, le=12),
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Monthly expense/income comparison with MoM and YoY data."""
    current_month = _month_floor(datetime.now())
    conn = db.get_connection()
    month_window = [_shift_months(current_month, offset) for offset in range(-(months - 1), 1)]
    current_query_start = _shift_months(month_window[0], -1).strftime("%Y-%m-01")
    current_monthly = conn.execute(
        f"""
        SELECT strftime('%Y-%m', ledger_date) AS month,
               transaction_type AS ledger_kind,
               {LEDGER_AMOUNT_TOTAL_EXPR} AS total
        FROM items WHERE type='ledger' AND owner_id=? AND deleted=0
        AND ledger_date >= ?
        GROUP BY month, ledger_kind ORDER BY month
    """,
        (owner_id, current_query_start),
    ).fetchall()

    monthly_map: dict[str, dict] = {}
    for month, transaction_type, total in current_monthly:
        monthly_map.setdefault(month, {"month": month, "expense": 0, "income": 0})
        if transaction_type == "income":
            monthly_map[month]["income"] = round(total, 2)
        elif transaction_type == "expense":
            monthly_map[month]["expense"] = round(total, 2)

    yoy_query_start = _shift_months(month_window[0], -12).strftime("%Y-%m-01")
    yoy_data = conn.execute(
        f"""
        SELECT strftime('%Y-%m', ledger_date) AS month,
               transaction_type AS ledger_kind,
               {LEDGER_AMOUNT_TOTAL_EXPR} AS total
        FROM items WHERE type='ledger' AND owner_id=? AND deleted=0
        AND ledger_date >= ?
        GROUP BY month, ledger_kind ORDER BY month
    """,
        (owner_id, yoy_query_start),
    ).fetchall()

    yoy_map: dict[str, dict] = {}
    for month, transaction_type, total in yoy_data:
        yoy_map.setdefault(month, {"expense": 0, "income": 0})
        if transaction_type == "income":
            yoy_map[month]["income"] = round(total, 2)
        elif transaction_type == "expense":
            yoy_map[month]["expense"] = round(total, 2)

    result_months = []
    for month_start in month_window:
        mk = month_start.strftime("%Y-%m")
        m = monthly_map.get(mk, {"month": mk, "expense": 0, "income": 0})
        prev_key = _shift_months(month_start, -1).strftime("%Y-%m")
        prev = monthly_map.get(prev_key, {"expense": 0, "income": 0})
        yoy_key = _shift_months(month_start, -12).strftime("%Y-%m")
        yoy = yoy_map.get(yoy_key, {"expense": 0, "income": 0})
        result_months.append(
            {
                "month": mk,
                "expense": m["expense"],
                "income": m["income"],
                "prev_expense": prev["expense"],
                "prev_income": prev["income"],
                "yoy_expense": yoy["expense"],
                "yoy_income": yoy["income"],
            }
        )

    return {
        "ok": True,
        "data": {"months": result_months},
        "message": "",
    }


@router.get("/stats/activity-heatmap")
def activity_heatmap(
    year: int = Query(None),
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Year-round activity heatmap counting all module activities per day."""
    target_year = year or datetime.now().year
    start_date = date(target_year, 1, 1)
    end_date = date(target_year, 12, 31)
    conn = db.get_connection()

    all_days = []
    cur = start_date
    while cur <= end_date:
        all_days.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)

    day_ph = ",".join(["?"] * len(all_days))
    params_base = [owner_id] + all_days

    ledger_map: dict[str, int] = dict(
        conn.execute(
            f"""
        SELECT ledger_date, COUNT(*) FROM items
        WHERE type='ledger' AND owner_id=? AND deleted=0 AND ledger_date IN ({day_ph})
        GROUP BY ledger_date
    """,
            params_base,
        ).fetchall()
    )

    task_map: dict[str, int] = dict(
        conn.execute(
            f"""
        SELECT date(created_at), COUNT(*) FROM items
        WHERE type='task' AND owner_id=? AND deleted=0 AND date(created_at) IN ({day_ph})
        GROUP BY date(created_at)
    """,
            params_base,
        ).fetchall()
    )

    event_map: dict[str, int] = dict(
        conn.execute(
            f"""
        SELECT date(start_time), COUNT(*) FROM items
        WHERE type='event' AND owner_id=? AND deleted=0 AND start_time IS NOT NULL AND date(start_time) IN ({day_ph})
        GROUP BY date(start_time)
    """,
            params_base,
        ).fetchall()
    )

    note_map: dict[str, int] = dict(
        conn.execute(
            f"""
        SELECT date(created_at), COUNT(*) FROM items
        WHERE type='note' AND owner_id=? AND deleted=0 AND date(created_at) IN ({day_ph})
        GROUP BY date(created_at)
    """,
            params_base,
        ).fetchall()
    )

    diary_map: dict[str, int] = dict(
        conn.execute(
            f"""
        SELECT diary_date, COUNT(*) FROM items
        WHERE type='diary' AND owner_id=? AND deleted=0 AND diary_date IN ({day_ph})
        GROUP BY diary_date
    """,
            params_base,
        ).fetchall()
    )

    days = []
    for d in all_days:
        l = ledger_map.get(d, 0)
        t = task_map.get(d, 0)
        e = event_map.get(d, 0)
        n = note_map.get(d, 0)
        dy = diary_map.get(d, 0)
        days.append(
            {
                "date": d,
                "count": l + t + e + n + dy,
                "ledger": l,
                "task": t,
                "event": e,
                "note": n,
                "diary": dy,
            }
        )

    return {
        "ok": True,
        "data": {"year": target_year, "days": days},
        "message": "",
    }
