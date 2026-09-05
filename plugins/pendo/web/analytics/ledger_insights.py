"""按账本筛选条件生成 Web 端趋势、分类和对比洞察。"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any, Literal, cast

from ...services.db import Database
from ...utils.currency import currency_code
from ...utils.time_utils import now_in_timezone
from ..utils import amount_filter_cents

JsonObject      = dict[str, Any]
SqlParams       = list[str | int]
TransactionType = Literal["expense", "income", "transfer"]
CompareMode     = Literal["previous_period", "previous_year_to_date", "none"]
BucketMode      = Literal["day", "month"]

_TRANSACTION_TYPES = frozenset({"expense", "income", "transfer"})
_COMPARE_MODES = frozenset({"previous_period", "previous_year_to_date", "none"})
_DELTA_LABELS: dict[CompareMode, str] = {
    "previous_period": "较上一周期",
    "previous_year_to_date": "较去年同期",
    "none": "无对比周期",
}
_LEDGER_AMOUNT_EXPR       = Database._LEDGER_AMOUNT_CENTS_EXPR
_LEDGER_AMOUNT_TOTAL_EXPR = f"ROUND(COALESCE(SUM({_LEDGER_AMOUNT_EXPR}), 0) / 100.0, 2)"


@dataclass(frozen=True, slots=True)
class _LedgerFilters:
    """一次账本洞察查询使用的完整筛选条件。"""

    owner_id: str
    currency: str                            = "CNY"
    transaction_type: TransactionType | None = None
    category: str | None                     = None
    account_name: str | None                 = None
    start_date: str | None                   = None
    end_date: str | None                     = None
    amount_min: float | None                 = None
    amount_max: float | None                 = None


def _parse_date(value: str) -> date:
    """严格解析 YYYY-MM-DD，避免宽松格式进入 SQL 日期边界。"""

    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"日期必须是有效的 YYYY-MM-DD：{value}") from exc
    if len(value) != 10 or parsed.isoformat() != value:
        raise ValueError(f"日期必须是有效的 YYYY-MM-DD：{value}")
    return parsed


def _bucket_label(key: str, mode: BucketMode) -> str:
    """把内部桶键转换为图表横轴标签。"""

    if mode == "month":
        return key
    current = _parse_date(key)
    return f"{current.month}/{current.day}"


def _iter_bucket_keys(start: date, end: date, mode: BucketMode) -> list[str]:
    """按日或月补齐闭区间内的全部桶键。"""

    if mode == "day":
        return [
            (start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)
        ]

    keys: list[str] = []
    current = start.replace(day=1)
    final = end.replace(day=1)
    while current <= final:
        keys.append(current.strftime("%Y-%m"))
        current = date(current.year + current.month // 12, current.month % 12 + 1, 1)
    return keys


def _build_ledger_where(filters: _LedgerFilters) -> tuple[list[str], SqlParams]:
    """生成参数化账本查询条件；金额始终以整数分字段比较。"""

    where = ["type = 'ledger'", "owner_id = ?", "deleted = 0"]
    where.append("COALESCE(NULLIF(UPPER(TRIM(currency)), ''), 'CNY') = ?")
    params: SqlParams = [filters.owner_id, filters.currency]

    if filters.transaction_type:
        where.append("transaction_type = ?")
        params.append(filters.transaction_type)
    if filters.category:
        where.append("ledger_category = ?")
        params.append(filters.category)
    if filters.account_name:
        where.append("(account_name = ? OR counter_account_name = ?)")
        params.extend([filters.account_name, filters.account_name])
    if filters.start_date:
        where.append("ledger_date >= ?")
        params.append(filters.start_date)
    if filters.end_date:
        where.append("ledger_date <= ?")
        params.append(filters.end_date)
    if filters.amount_min is not None:
        where.append(f"{_LEDGER_AMOUNT_EXPR} >= ?")
        params.append(amount_filter_cents(filters.amount_min))
    if filters.amount_max is not None:
        where.append(f"{_LEDGER_AMOUNT_EXPR} <= ?")
        params.append(amount_filter_cents(filters.amount_max))
    return where, params


def _query_scalar(conn: sqlite3.Connection, sql: str, params: SqlParams) -> float:
    """执行只返回一个数值的聚合查询。"""

    row = conn.execute(sql, params).fetchone()
    return float((row[0] if row else 0) or 0)


def _query_bounds(
    conn: sqlite3.Connection,
    where: list[str],
    params: SqlParams,
) -> tuple[str | None, str | None]:
    """读取筛选结果中有日期账目的首末日期。"""

    row = conn.execute(
        f"""SELECT MIN(ledger_date), MAX(ledger_date)
            FROM items
            WHERE {" AND ".join(where)} AND ledger_date IS NOT NULL""",
        params,
    ).fetchone()
    if not row or not row[0] or not row[1]:
        return None, None
    return str(row[0]), str(row[1])


def _previous_year_day(current: date) -> date:
    """向前平移一年，闰日落到上一年的 2 月 28 日。"""

    try:
        return current.replace(year=current.year - 1)
    except ValueError:
        return current.replace(year=current.year - 1, day=28)


def _build_period_delta(
    conn: sqlite3.Connection,
    filters: _LedgerFilters,
    current_total: float,
    compare_mode: CompareMode,
) -> float | None:
    """按上一等长周期或去年同期计算金额变化比例。"""

    if compare_mode == "none" or not filters.start_date or not filters.end_date:
        return None

    start = _parse_date(filters.start_date)
    end   = _parse_date(filters.end_date)
    if compare_mode == "previous_year_to_date":
        previous_start = _previous_year_day(start)
        previous_end   = _previous_year_day(end)
    else:
        span_days = (end - start).days + 1
        previous_end = start - timedelta(days=1)
        previous_start = previous_end - timedelta(days=span_days - 1)

    previous_filters = replace(
        filters,
        start_date = previous_start.isoformat(),
        end_date   = previous_end.isoformat(),
    )
    previous_where, previous_params = _build_ledger_where(previous_filters)
    previous_total = _query_scalar(
        conn,
        f"SELECT {_LEDGER_AMOUNT_TOTAL_EXPR} FROM items WHERE {' AND '.join(previous_where)}",
        previous_params,
    )
    if previous_total == 0:
        return None if current_total == 0 else 1.0
    return (current_total - previous_total) / previous_total


def _build_focus_details(
    conn: sqlite3.Connection,
    filters: _LedgerFilters,
    focus_total: float,
) -> tuple[BucketMode, list[JsonObject], list[JsonObject], list[JsonObject]]:
    """生成当前焦点类型的补零趋势、分类占比和 K 线数据。"""

    where, params = _build_ledger_where(filters)
    focus_start = filters.start_date
    focus_end   = filters.end_date
    if not focus_start or not focus_end:
        focus_start, focus_end = _query_bounds(conn, where, params)
    if not focus_start or not focus_end:
        return "day", [], [], []

    start                   = _parse_date(focus_start)
    end                     = _parse_date(focus_end)
    bucket_mode: BucketMode = "day" if (end - start).days <= 62 else "month"
    bucket_keys             = _iter_bucket_keys(start, end, bucket_mode)
    trend_rows              = conn.execute(
        f"""SELECT ledger_date, ROUND({_LEDGER_AMOUNT_EXPR} / 100.0, 2), created_at, id
            FROM items
            WHERE {" AND ".join(where)} AND ledger_date IS NOT NULL
            ORDER BY ledger_date, created_at, id""",
        params,
    ).fetchall()

    bucket_totals: defaultdict[str, float] = defaultdict(float)
    bucket_counts: defaultdict[str, int]   = defaultdict(int)
    candle_map: dict[str, JsonObject]      = {}
    for ledger_date, amount, _created_at, _item_id in trend_rows:
        ledger_day = _parse_date(str(ledger_date))
        key = ledger_day.isoformat() if bucket_mode == "day" else ledger_day.strftime("%Y-%m")
        numeric_amount = float(amount or 0)
        bucket_totals[key] += numeric_amount
        bucket_counts[key] += 1

        candle = candle_map.get(key)
        if candle is None:
            candle_map[key] = {
                "key": key,
                "label": _bucket_label(key, bucket_mode),
                "open": numeric_amount,
                "close": numeric_amount,
                "high": numeric_amount,
                "low": numeric_amount,
                "total": numeric_amount,
                "count": 1,
            }
            continue
        candle["high"] = max(candle["high"], numeric_amount)
        candle["low"]  = min(candle["low"], numeric_amount)
        candle["total"] += numeric_amount
        candle["count"] += 1
        candle["close"] = numeric_amount

    trend: list[JsonObject] = [
        {
            "key": key,
            "label": _bucket_label(key, bucket_mode),
            "total": round(bucket_totals.get(key, 0), 2),
            "count": bucket_counts.get(key, 0),
        }
        for key in bucket_keys
    ]
    candles: list[JsonObject] = [
        {
            "key": key,
            "label": candle["label"],
            "open": round(candle["open"], 2),
            "close": round(candle["close"], 2),
            "high": round(candle["high"], 2),
            "low": round(candle["low"], 2),
            "total": round(candle["total"], 2),
            "count": candle["count"],
        }
        for key in bucket_keys
        if (candle := candle_map.get(key)) is not None
    ]

    category_rows = conn.execute(
        f"""
        SELECT COALESCE(NULLIF(TRIM(ledger_category), ''), '未分类') AS category,
               {_LEDGER_AMOUNT_TOTAL_EXPR} AS total,
               COUNT(*) AS count
        FROM items
        WHERE {" AND ".join(where)}
        GROUP BY COALESCE(NULLIF(TRIM(ledger_category), ''), '未分类')
        ORDER BY total DESC, category ASC
        """,
        params,
    ).fetchall()
    categories: list[JsonObject] = [
        {
            "category": str(row[0] or "未分类").strip() or "未分类",
            "total": round(float(row[1] or 0), 2),
            "count": int(row[2] or 0),
            "share": round(float(row[1] or 0) / focus_total, 4) if focus_total else 0,
        }
        for row in category_rows
    ]
    return bucket_mode, trend, categories, candles


def build_ledger_insights(
    db: Database,
    owner_id: str,
    transaction_type: str | None = None,
    category: str | None         = None,
    account_name: str | None     = None,
    start_date: str | None       = None,
    end_date: str | None         = None,
    amount_min: float | None     = None,
    amount_max: float | None     = None,
    compare_mode: str            = "previous_period",
    currency: str                = "CNY",
) -> JsonObject:
    """生成账本页和 Widget 共用的筛选洞察。"""

    normalized_transaction_type = (transaction_type or "").strip() or None
    if (
        normalized_transaction_type is not None
        and normalized_transaction_type not in _TRANSACTION_TYPES
    ):
        raise ValueError("transaction_type must be expense, income, transfer, or None")
    if compare_mode not in _COMPARE_MODES:
        raise ValueError("compare_mode must be previous_period, previous_year_to_date, or none")
    if amount_min is not None and amount_min < 0:
        raise ValueError("amount_min must not be negative")
    if amount_max is not None and amount_max < 0:
        raise ValueError("amount_max must not be negative")
    if amount_min is not None and amount_max is not None and amount_min > amount_max:
        raise ValueError("amount_min must not be greater than amount_max")

    normalized_start = start_date or None
    normalized_end   = end_date or None
    start_day        = _parse_date(normalized_start) if normalized_start else None
    end_day          = _parse_date(normalized_end) if normalized_end else None
    if start_day and end_day and start_day > end_day:
        raise ValueError("start_date must not be after end_date")
    if start_day and end_day:
        today = now_in_timezone(owner_id, db).date()
        if start_day <= today <= end_day:
            normalized_end = today.isoformat()

    filters = _LedgerFilters(
        owner_id         = owner_id,
        currency         = currency_code(currency),
        transaction_type = cast(TransactionType | None, normalized_transaction_type),
        category         = (category or "").strip() or None,
        account_name     = (account_name or "").strip() or None,
        start_date       = normalized_start,
        end_date         = normalized_end,
        amount_min       = amount_min,
        amount_max       = amount_max,
    )
    normalized_compare_mode = cast(CompareMode, compare_mode)
    conn                    = db.get_connection()
    base_where, base_params = _build_ledger_where(filters)
    totals_rows = conn.execute(
        f"""SELECT transaction_type, {_LEDGER_AMOUNT_TOTAL_EXPR}, COUNT(*)
            FROM items
            WHERE {" AND ".join(base_where)}
            GROUP BY transaction_type""",
        base_params,
    ).fetchall()
    totals_by_type = {str(row[0]): float(row[1] or 0) for row in totals_rows}
    counts_by_type = {str(row[0]): int(row[2] or 0) for row in totals_rows}

    focus_transaction_type: TransactionType = filters.transaction_type or "expense"
    focus_total = round(totals_by_type.get(focus_transaction_type, 0.0), 2)
    focus_count = counts_by_type.get(focus_transaction_type, 0)
    focus_filters = replace(filters, transaction_type=focus_transaction_type)
    bucket_mode, trend, categories, candles = _build_focus_details(
        conn,
        focus_filters,
        focus_total,
    )
    delta = _build_period_delta(
        conn,
        focus_filters,
        focus_total,
        normalized_compare_mode,
    )
    peak_bucket = max(
        (point for point in trend if point["count"]),
        key     = lambda point: point["total"],
        default = None,
    )

    expense_total   = round(totals_by_type.get("expense", 0.0), 2)
    income_total    = round(totals_by_type.get("income", 0.0), 2)
    transfer_total  = round(totals_by_type.get("transfer", 0.0), 2)
    grouped_filters = {
        "type": "ledger",
        "date_field": "ledger_date",
        "start_date": normalized_start,
        "end_date": normalized_end,
        "transaction_type": normalized_transaction_type,
        "category": category,
        "account_name": account_name,
        "amount_min": amount_min,
        "amount_max": amount_max,
    }
    return {
        "currency": filters.currency,
        "by_currency": db.aggregate_ledger_by_currency(owner_id, grouped_filters),
        "summary": {
            "currency": filters.currency,
            "expense_total": expense_total,
            "income_total": income_total,
            "transfer_total": transfer_total,
            "focus_transaction_type": focus_transaction_type,
            "focus_total": focus_total,
            "focus_count": focus_count,
            "average_focus_amount": round(focus_total / focus_count, 2) if focus_count else 0,
            "bucket_mode": bucket_mode,
            "peak_bucket_label": peak_bucket["label"] if peak_bucket else "",
            "peak_bucket_total": peak_bucket["total"] if peak_bucket else 0,
            "delta_label": _DELTA_LABELS[normalized_compare_mode],
            "delta_vs_previous": round(delta, 4) if delta is not None else None,
        },
        "expense_timeline": trend,
        "expense_categories": categories,
        "expense_hotspots": categories[:5],
        "expense_candles": candles,
    }
