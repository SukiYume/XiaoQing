"""Filtered ledger insights for the compact web UI analysis cards."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from ...services.db import Database


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _format_bucket_label(key: str, mode: str) -> str:
    if mode == "day":
        dt = _parse_date(key)
        return f"{dt.month}/{dt.day}"
    year, month = key.split("-")
    return f"{year}-{month}"


def _bucket_key(value: str, mode: str) -> str:
    dt = _parse_date(value)
    if mode == "day":
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m")


def _iter_bucket_keys(start: date, end: date, mode: str) -> list[str]:
    keys: list[str] = []
    if mode == "day":
        current = start
        while current <= end:
            keys.append(current.strftime("%Y-%m-%d"))
            current += timedelta(days=1)
        return keys

    current = date(start.year, start.month, 1)
    final = date(end.year, end.month, 1)
    while current <= final:
        keys.append(current.strftime("%Y-%m"))
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    return keys


def _resolve_bucket_mode(start: date, end: date) -> str:
    return "day" if (end - start).days <= 62 else "month"


def _normalize_category(value: Optional[str]) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else "未分类"


def _build_ledger_where(
    owner_id: str,
    direction: Optional[str] = None,
    category: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    amount_min: Optional[float] = None,
    amount_max: Optional[float] = None,
) -> tuple[list[str], list]:
    where = ["type = 'ledger'", "owner_id = ?", "deleted = 0"]
    params: list = [owner_id]

    if direction:
        where.append("direction = ?")
        params.append(direction)
    if category:
        where.append("ledger_category = ?")
        params.append(category)
    if start_date:
        where.append("ledger_date >= ?")
        params.append(start_date)
    if end_date:
        where.append("ledger_date <= ?")
        params.append(end_date)
    if amount_min is not None:
        where.append("amount >= ?")
        params.append(amount_min)
    if amount_max is not None:
        where.append("amount <= ?")
        params.append(amount_max)
    return where, params


def _append_expense_only(where: list[str], params: list) -> tuple[list[str], list]:
    return [*where, "direction = 'expense'"], [*params]


def _query_scalar(conn, sql: str, params: list) -> float:
    row = conn.execute(sql, params).fetchone()
    return float((row[0] if row else 0) or 0)


def _query_bounds(conn, where: list[str], params: list) -> tuple[Optional[str], Optional[str]]:
    row = conn.execute(
        f"SELECT MIN(ledger_date), MAX(ledger_date) FROM items WHERE {' AND '.join(where)}",
        params,
    ).fetchone()
    if not row or not row[0] or not row[1]:
        return None, None
    return row[0], row[1]


def _shift_year(dt: date, years: int = -1) -> date:
    try:
        return dt.replace(year=dt.year + years)
    except ValueError:
        # Handle leap-day rollover by clamping to the last valid day in February.
        return dt.replace(year=dt.year + years, day=28)


def _build_period_delta(
    conn,
    owner_id: str,
    direction: Optional[str],
    category: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
    amount_min: Optional[float],
    amount_max: Optional[float],
    current_total: float,
    compare_mode: str = "previous_period",
) -> Optional[float]:
    if not start_date or not end_date or compare_mode == "none":
        return None

    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if compare_mode == "previous_year_to_date":
        prev_start = _shift_year(start, -1)
        prev_end = _shift_year(end, -1)
    else:
        span_days = max((end - start).days + 1, 1)
        prev_end = start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=span_days - 1)

    prev_where, prev_params = _build_ledger_where(
        owner_id=owner_id,
        direction=direction,
        category=category,
        start_date=prev_start.strftime("%Y-%m-%d"),
        end_date=prev_end.strftime("%Y-%m-%d"),
        amount_min=amount_min,
        amount_max=amount_max,
    )
    prev_where, prev_params = _append_expense_only(prev_where, prev_params)
    previous_total = _query_scalar(
        conn,
        f"SELECT COALESCE(SUM(amount), 0) FROM items WHERE {' AND '.join(prev_where)}",
        prev_params,
    )

    if previous_total == 0:
        return None if current_total == 0 else 1.0
    return (current_total - previous_total) / previous_total


def _delta_label(compare_mode: str) -> str:
    if compare_mode == "previous_year_to_date":
        return "较去年同期"
    if compare_mode == "none":
        return "无对比周期"
    return "较上一周期"


def build_ledger_insights(
    db: Database,
    owner_id: str,
    direction: Optional[str] = None,
    category: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    amount_min: Optional[float] = None,
    amount_max: Optional[float] = None,
    compare_mode: str = "previous_period",
) -> dict:
    conn = db.get_connection()

    base_where, base_params = _build_ledger_where(
        owner_id=owner_id,
        direction=direction,
        category=category,
        start_date=start_date,
        end_date=end_date,
        amount_min=amount_min,
        amount_max=amount_max,
    )
    expense_where, expense_params = _append_expense_only(base_where, base_params)

    direction_rows = conn.execute(
        f"SELECT direction, COALESCE(SUM(amount), 0), COUNT(*) FROM items WHERE {' AND '.join(base_where)} GROUP BY direction",
        base_params,
    ).fetchall()
    totals_by_direction = {row[0]: float(row[1] or 0) for row in direction_rows}
    counts_by_direction = {row[0]: int(row[2] or 0) for row in direction_rows}

    expense_start = start_date
    expense_end = end_date
    if not expense_start or not expense_end:
        expense_start, expense_end = _query_bounds(conn, expense_where, expense_params)

    trend: list[dict] = []
    categories: list[dict] = []
    candles: list[dict] = []
    bucket_mode = "day"

    if expense_start and expense_end:
        start = _parse_date(expense_start)
        end = _parse_date(expense_end)
        bucket_mode = _resolve_bucket_mode(start, end)
        bucket_keys = _iter_bucket_keys(start, end, bucket_mode)

        trend_rows = conn.execute(
            f"SELECT ledger_date, amount, created_at, id FROM items WHERE {' AND '.join(expense_where)} ORDER BY ledger_date, created_at, id",
            expense_params,
        ).fetchall()

        bucket_totals: dict[str, float] = defaultdict(float)
        bucket_counts: dict[str, int] = defaultdict(int)
        candle_map: dict[str, dict] = {}

        for ledger_date, amount, _created_at, _item_id in trend_rows:
            key = _bucket_key(ledger_date, bucket_mode)
            amt = float(amount or 0)
            bucket_totals[key] += amt
            bucket_counts[key] += 1

            candle = candle_map.get(key)
            if candle is None:
                candle_map[key] = {
                    "key": key,
                    "label": _format_bucket_label(key, bucket_mode),
                    "open": amt,
                    "close": amt,
                    "high": amt,
                    "low": amt,
                    "total": amt,
                    "count": 1,
                }
                continue

            candle["high"] = max(candle["high"], amt)
            candle["low"] = min(candle["low"], amt)
            candle["total"] += amt
            candle["count"] += 1
            candle["close"] = amt

        trend = [{
            "key": key,
            "label": _format_bucket_label(key, bucket_mode),
            "total": round(bucket_totals.get(key, 0), 2),
            "count": bucket_counts.get(key, 0),
        } for key in bucket_keys]

        candles = []
        for key in bucket_keys:
            candle = candle_map.get(key)
            if not candle:
                continue
            candles.append({
                "key": key,
                "label": candle["label"],
                "open": round(candle["open"], 2),
                "close": round(candle["close"], 2),
                "high": round(candle["high"], 2),
                "low": round(candle["low"], 2),
                "total": round(candle["total"], 2),
                "count": candle["count"],
            })

        category_rows = conn.execute(
            f"""
            SELECT COALESCE(NULLIF(TRIM(ledger_category), ''), '未分类') AS ledger_category,
                   COALESCE(SUM(amount), 0) AS total,
                   COUNT(*) AS count
            FROM items
            WHERE {' AND '.join(expense_where)}
            GROUP BY COALESCE(NULLIF(TRIM(ledger_category), ''), '未分类')
            ORDER BY total DESC, ledger_category ASC
            """,
            expense_params,
        ).fetchall()

        expense_total = totals_by_direction.get("expense", 0.0)
        categories = [{
            "category": _normalize_category(row[0]),
            "total": round(float(row[1] or 0), 2),
            "count": int(row[2] or 0),
            "share": round((float(row[1] or 0) / expense_total), 4) if expense_total else 0,
        } for row in category_rows]

    expense_total = round(totals_by_direction.get("expense", 0.0), 2)
    income_total = round(totals_by_direction.get("income", 0.0), 2)
    expense_count = counts_by_direction.get("expense", 0)
    average_expense = round(expense_total / expense_count, 2) if expense_count else 0
    peak_bucket = max(trend, key=lambda item: item["total"], default=None)
    delta = _build_period_delta(
        conn,
        owner_id=owner_id,
        direction=direction,
        category=category,
        start_date=start_date,
        end_date=end_date,
        amount_min=amount_min,
        amount_max=amount_max,
        current_total=expense_total,
        compare_mode=compare_mode,
    )

    return {
        "summary": {
            "expense_total": expense_total,
            "income_total": income_total,
            "expense_count": expense_count,
            "average_expense": average_expense,
            "bucket_mode": bucket_mode,
            "peak_bucket_label": peak_bucket["label"] if peak_bucket else "",
            "peak_bucket_total": peak_bucket["total"] if peak_bucket else 0,
            "delta_label": _delta_label(compare_mode),
            "delta_vs_previous": round(delta, 4) if delta is not None else None,
        },
        "expense_timeline": trend,
        "expense_categories": categories,
        "expense_hotspots": categories[:5],
        "expense_candles": candles,
    }
