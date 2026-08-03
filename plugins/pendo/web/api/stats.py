"""Pendo Web 统计聚合接口。"""

from __future__ import annotations

from calendar import monthrange
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query

from ...models.item import EventItem
from ...services.db import Database
from ...utils.time_utils import TimezoneHelper, now_in_timezone
from ..analytics.diary_overview import build_diary_overview
from ..analytics.ledger_insights import build_ledger_insights
from ..analytics.notes_overview import build_notes_overview
from ..analytics.task_overview import build_task_overview
from ..deps import get_current_user, get_db

router = APIRouter()
JsonObject = dict[str, Any]
StatsRange = Annotated[str | None, Query(alias="range", max_length=64)]
DateQuery = Annotated[str | None, Query(max_length=10)]
TextQuery = Annotated[str | None, Query(max_length=120)]
LedgerTransactionType = Literal["expense", "income", "transfer"]
LedgerCompareMode = Literal["previous_period", "previous_year_to_date", "none"]

LEDGER_AMOUNT_EXPR = Database._LEDGER_AMOUNT_CENTS_EXPR
LEDGER_AMOUNT_TOTAL_EXPR = f"ROUND(COALESCE(SUM({LEDGER_AMOUNT_EXPR}), 0) / 100.0, 2)"

LEDGER_HISTOGRAM_BUCKETS: tuple[tuple[str, float, float | None], ...] = (
    ("0-20", 0, 20),
    ("20-50", 20, 50),
    ("50-100", 50, 100),
    ("100-300", 100, 300),
    ("300-1000", 300, 1000),
    ("1000+", 1000, None),
)

_WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _today(db: Database | None = None, owner_id: str | None = None) -> date:
    """集中提供当前日期，保证同次统计使用一致时钟并便于确定性测试。"""

    if db is not None and owner_id is not None:
        return now_in_timezone(owner_id, db).date()
    return datetime.now().date()


def _local_datetime(value: object, user_timezone: ZoneInfo) -> datetime | None:
    """把混合 ISO 时间转换到用户时区；损坏值不进入统计。"""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        return TimezoneHelper.parse(text, user_timezone)
    except (OverflowError, TypeError, ValueError):
        return None


def _day_in_range(value: date | None, start: date, end: date) -> bool:
    return value is not None and start <= value <= end


def _coarse_timestamp_dates(start: date, end: date) -> tuple[str, str]:
    """给混合偏移时间保留全球最大时差所需的两日粗筛余量。"""

    return (start - timedelta(days=2)).isoformat(), (end + timedelta(days=2)).isoformat()


def _event_time_slot(hour: int) -> str:
    """映射到前端稳定的六个时段桶。"""

    if 6 <= hour <= 8:
        return "06-09"
    if 9 <= hour <= 11:
        return "09-12"
    if 12 <= hour <= 13:
        return "12-14"
    if 14 <= hour <= 17:
        return "14-18"
    if 18 <= hour <= 20:
        return "18-21"
    return "21-24"


def _aggregate_ledger_periods(
    rows: Iterable[Sequence[Any]],
    key_name: Literal["month", "date"],
) -> list[JsonObject]:
    """把账本的分类型 SQL 行合并为按月或按日的收入/支出序列。"""

    periods: dict[str, JsonObject] = {}
    for row in rows:
        period = str(row[0])
        transaction_type = str(row[1])
        total = float(row[2] or 0)
        if period not in periods:
            periods[period] = {key_name: period, "income": 0, "expense": 0}
        if transaction_type == "income":
            periods[period]["income"] = total
        elif transaction_type == "expense":
            periods[period]["expense"] = total
    return list(periods.values())


def _validate_date_bounds(start: str, end: str) -> tuple[str, str]:
    """校验并规范化闭区间日期，拒绝模糊日期和反向区间。"""

    try:
        start_day = date.fromisoformat(start.strip())
        end_day = date.fromisoformat(end.strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError("日期范围必须使用 YYYY-MM-DD 格式") from exc
    if start_day > end_day:
        raise ValueError("开始日期不能晚于结束日期")
    return start_day.isoformat(), end_day.isoformat()


def _parse_range(range_str: str | None, *, today: date | None = None) -> tuple[str, str]:
    """把预设、月份或显式区间解析为规范化的闭区间日期。"""

    current_day = today or _today()
    value = str(range_str or "month").strip() or "month"
    quarter_month = ((current_day.month - 1) // 3) * 3 + 1
    preset_bounds = {
        "month": (current_day.replace(day=1), current_day),
        "week": (current_day - timedelta(days=current_day.weekday()), current_day),
        "quarter": (date(current_day.year, quarter_month, 1), current_day),
        "year": (date(current_day.year, 1, 1), current_day),
        "last_year": (
            date(current_day.year - 1, 1, 1),
            date(current_day.year - 1, 12, 31),
        ),
        "all": (date(1970, 1, 1), current_day),
    }
    if value in preset_bounds:
        start_day, end_day = preset_bounds[value]
    elif ".." in value:
        parts = value.split("..")
        if len(parts) != 2:
            raise ValueError("日期范围只能包含一个 '..' 分隔符")
        return _validate_date_bounds(parts[0], parts[1])
    elif len(value) == 7 and value[4] == "-":
        try:
            start_day = date.fromisoformat(f"{value}-01")
        except ValueError as exc:
            raise ValueError("月份范围必须使用 YYYY-MM 格式") from exc
        end_day = start_day.replace(day=monthrange(start_day.year, start_day.month)[1])
    else:
        raise ValueError("不支持的统计日期范围")
    return start_day.isoformat(), end_day.isoformat()


def _resolve_stats_range(
    range_str: str | None,
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    today: date | None = None,
) -> tuple[str, str]:
    """解析路由日期参数，并统一转换为可读的 422 错误。"""

    try:
        if (start_date is None) != (end_date is None):
            raise ValueError("start_date 与 end_date 必须同时提供")
        if start_date is not None and end_date is not None:
            return _validate_date_bounds(start_date, end_date)
        return _parse_range(range_str, today=today)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _build_amount_histogram(amounts: Sequence[float]) -> list[JsonObject]:
    """按固定半开区间生成支出金额直方图。"""

    histogram: list[JsonObject] = []
    for label, lower, upper in LEDGER_HISTOGRAM_BUCKETS:
        if upper is None:
            count = sum(1 for amount in amounts if amount >= lower)
        else:
            count = sum(1 for amount in amounts if lower <= amount < upper)
        histogram.append({"bucket": label, "count": count})
    return histogram


def _month_floor(value: date | datetime) -> date:
    """返回日期所在月份的第一天。"""
    return date(value.year, value.month, 1)


def _shift_months(value: date, delta: int) -> date:
    """在月初日期上平移指定月数。"""
    month_index = (value.year * 12 + value.month - 1) + delta
    year, month = divmod(month_index, 12)
    return date(year, month + 1, 1)


@router.get("/stats/ledger")  # type: ignore[untyped-decorator]
def ledger_stats(
    range_str: StatsRange = None,
    start_date: DateQuery = None,
    end_date: DateQuery = None,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JsonObject:
    """返回指定日期范围内的账本趋势、分类和金额分布。"""

    start, end = _resolve_stats_range(
        range_str,
        start_date,
        end_date,
        today=_today(db, owner_id),
    )
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
        SELECT COALESCE(NULLIF(TRIM(ledger_category), ''), '未分类') AS category,
               transaction_type AS ledger_kind,
               {LEDGER_AMOUNT_TOTAL_EXPR} AS total,
               COUNT(*) AS count
        FROM items WHERE type='ledger' AND owner_id=? AND deleted=0
        AND ledger_date BETWEEN ? AND ?
        GROUP BY category, ledger_kind
        ORDER BY total DESC, category
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

    category_totals: dict[str, list[JsonObject]] = {
        "expense": [],
        "income": [],
        "transfer": [],
    }
    for category, transaction_type, total, _count in by_category:
        if transaction_type in category_totals:
            category_totals[transaction_type].append(
                {"category": category, "total": float(total or 0)}
            )

    return {
        "ok": True,
        "data": {
            "monthly": _aggregate_ledger_periods(monthly, "month"),
            "expense_by_category": category_totals["expense"],
            "income_by_category": category_totals["income"],
            "transfer_by_category": category_totals["transfer"],
            "daily": _aggregate_ledger_periods(daily, "date"),
            "expense_amount_histogram": _build_amount_histogram(
                [float(r[0] or 0) for r in expense_amounts]
            ),
        },
        "message": "",
    }


@router.get("/stats/ledger/insights")  # type: ignore[untyped-decorator]
def ledger_visual_insights(
    transaction_type: Annotated[LedgerTransactionType | None, Query()] = None,
    category: TextQuery = None,
    account_name: TextQuery = None,
    start_date: DateQuery = None,
    end_date: DateQuery = None,
    amount_min: Annotated[float | None, Query(ge=0)] = None,
    amount_max: Annotated[float | None, Query(ge=0)] = None,
    compare_mode: LedgerCompareMode = "previous_period",
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JsonObject:
    """返回账本页视觉卡片所需的紧凑洞察。"""

    if (start_date is None) != (end_date is None):
        raise HTTPException(status_code=422, detail="start_date 与 end_date 必须同时提供")
    if start_date is not None and end_date is not None:
        try:
            start_date, end_date = _validate_date_bounds(start_date, end_date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if amount_min is not None and amount_max is not None and amount_min > amount_max:
        raise HTTPException(status_code=422, detail="amount_min 不能大于 amount_max")

    return {
        "ok": True,
        "data": build_ledger_insights(
            db=db,
            owner_id=owner_id,
            transaction_type=transaction_type,
            category=(category or "").strip() or None,
            account_name=(account_name or "").strip() or None,
            start_date=start_date,
            end_date=end_date,
            amount_min=amount_min,
            amount_max=amount_max,
            compare_mode=compare_mode,
        ),
        "message": "",
    }


@router.get("/stats/tasks")  # type: ignore[untyped-decorator]
def task_stats(
    range_str: StatsRange = None,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JsonObject:
    """返回任务状态、创建/关闭趋势及未完成任务分布。"""

    start, end = _resolve_stats_range(range_str, today=_today(db, owner_id))
    start_day = date.fromisoformat(start)
    end_day = date.fromisoformat(end)
    lower_date, upper_date = _coarse_timestamp_dates(start_day, end_day)
    user_timezone = TimezoneHelper.get_user_timezone(owner_id, db)
    task_rows = (
        db.get_connection()
        .execute(
            """
        SELECT status, category, priority, plan_date, deadline_at,
               created_at, completed_at, cancelled_at
        FROM items
        WHERE type='task' AND owner_id=? AND deleted=0
          AND (
            plan_date BETWEEN ? AND ?
            OR substr(created_at, 1, 10) BETWEEN ? AND ?
            OR substr(COALESCE(completed_at, ''), 1, 10) BETWEEN ? AND ?
            OR substr(COALESCE(cancelled_at, ''), 1, 10) BETWEEN ? AND ?
          )
        ORDER BY id
        """,
            (
                owner_id,
                start,
                end,
                lower_date,
                upper_date,
                lower_date,
                upper_date,
                lower_date,
                upper_date,
            ),
        )
        .fetchall()
    )

    status_counter: Counter[str] = Counter()
    priority_counter: Counter[Any] = Counter()
    plan_counter: Counter[str] = Counter()
    text_category_counter: Counter[str] = Counter()
    weekly_counter: Counter[tuple[str, str]] = Counter()
    for row in task_rows:
        created_at = _local_datetime(row["created_at"], user_timezone)
        completed_at = _local_datetime(row["completed_at"], user_timezone)
        cancelled_at = _local_datetime(row["cancelled_at"], user_timezone)
        try:
            plan_day = date.fromisoformat(str(row["plan_date"] or ""))
        except ValueError:
            plan_day = None
        if not any(
            (
                _day_in_range(created_at.date() if created_at else None, start_day, end_day),
                _day_in_range(completed_at.date() if completed_at else None, start_day, end_day),
                _day_in_range(cancelled_at.date() if cancelled_at else None, start_day, end_day),
                _day_in_range(plan_day, start_day, end_day),
            )
        ):
            continue

        status_key = str(row["status"] or "")
        status_counter[status_key] += 1
        # 优先级原样返回，保持既有 API 的整数类型；只在排序时转成文本。
        priority_counter[row["priority"]] += 1
        for activity, timestamp in (
            ("created", created_at),
            ("done", completed_at if status_key == "done" else None),
            ("cancelled", cancelled_at if status_key == "cancelled" else None),
        ):
            if timestamp is not None and _day_in_range(timestamp.date(), start_day, end_day):
                weekly_counter[(timestamp.strftime("%Y-W%W"), activity)] += 1
        if status_key != "open":
            continue

        # 日期计划优先；没有计划日期时，截止日仍可反映任务压力。
        deadline = _local_datetime(row["deadline_at"], user_timezone)
        plan_key = str(row["plan_date"] or "").strip() or (
            deadline.date().isoformat() if deadline else ""
        )
        category_key = str(row["category"] or "").strip()
        if plan_key:
            plan_counter[plan_key] += 1
        if category_key and category_key != "未分类":
            text_category_counter[category_key] += 1

    totals = {
        "open": status_counter["open"],
        "done": status_counter["done"],
        "cancelled": status_counter["cancelled"],
    }
    totals["closed"] = totals["done"] + totals["cancelled"]

    weekly_map: dict[str, JsonObject] = {}
    for (week_key, activity), count in sorted(weekly_counter.items()):
        entry = weekly_map.setdefault(
            week_key,
            {"week": week_key, "created": 0, "done": 0, "cancelled": 0},
        )
        entry[activity] = count
    weekly = [weekly_map[key] for key in sorted(weekly_map)]
    new_this_week = sum(int(item["created"]) for item in weekly)

    category_counts = sorted(
        text_category_counter.items(),
        key=lambda item: (-item[1], item[0]),
    )[:8]

    return {
        "ok": True,
        "data": {
            "totals": totals,
            "weekly": weekly,
            "by_plan": [
                {"plan": key, "count": count} for key, count in sorted(plan_counter.items())
            ],
            "by_category": [{"category": key, "count": count} for key, count in category_counts],
            "by_priority": [
                {"priority": priority, "count": count}
                for priority, count in sorted(
                    priority_counter.items(),
                    key=lambda item: str(item[0] or ""),
                )
            ],
            "new_this_week": new_this_week,
        },
        "message": "",
    }


@router.get("/stats/tasks/overview")  # type: ignore[untyped-decorator]
def task_overview(
    today: DateQuery = None,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JsonObject:
    """返回新版任务页所需的紧凑概览。"""

    try:
        data = build_task_overview(db=db, owner_id=owner_id, today=today)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "data": data,
        "message": "",
    }


@router.get("/stats/notes/overview")  # type: ignore[untyped-decorator]
def notes_overview(
    today: DateQuery = None,
    start_date: DateQuery = None,
    end_date: DateQuery = None,
    category: TextQuery = None,
    tags: Annotated[str | None, Query(max_length=500)] = None,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JsonObject:
    """返回新版笔记页所需的紧凑概览。"""

    try:
        data = build_notes_overview(
            db=db,
            owner_id=owner_id,
            today=today,
            start_date=start_date,
            end_date=end_date,
            category=(category or "").strip() or None,
            tags=(tags or "").strip() or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "data": data,
        "message": "",
    }


@router.get("/stats/diary/overview")  # type: ignore[untyped-decorator]
def diary_overview(
    year: Annotated[int | None, Query(ge=1970, le=9999)] = None,
    month: Annotated[int | None, Query(ge=1, le=12)] = None,
    start_date: DateQuery = None,
    end_date: DateQuery = None,
    cadence_granularity: Literal["day", "week", "month", "year", "auto"] = "day",
    today: DateQuery = None,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JsonObject:
    """返回新版日记页和统计范围卡片所需的紧凑概览。"""

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


@router.get("/stats/events")  # type: ignore[untyped-decorator]
def event_stats(
    range_str: StatsRange = None,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JsonObject:
    """返回日程的周趋势、时段矩阵和分类分布。"""

    start, end = _resolve_stats_range(range_str, today=_today(db, owner_id))
    start_day = date.fromisoformat(start)
    end_day = date.fromisoformat(end)
    user_timezone = TimezoneHelper.get_user_timezone(owner_id, db)
    events = db.get_events_for_range(
        owner_id,
        f"{start}T00:00:00",
        f"{end}T23:59:59",
    )
    collection_ids = [
        event.event_collection_id
        for event in events
        if isinstance(event, EventItem) and event.event_collection_id
    ]
    collections = db.get_event_collections_by_ids(owner_id, collection_ids)

    weekly_counter: Counter[str] = Counter()
    slot_counter: Counter[str] = Counter()
    weekday_slot_counter: Counter[tuple[int, str]] = Counter()
    category_counter: Counter[str] = Counter()
    for event in events:
        start_at = _local_datetime(event.start_time, user_timezone)
        if start_at is None or not _day_in_range(start_at.date(), start_day, end_day):
            continue
        week = start_at.strftime("%Y-W%W")
        slot = _event_time_slot(start_at.hour)
        weekly_counter[week] += 1
        slot_counter[slot] += 1
        weekday_slot_counter[(start_at.weekday(), slot)] += 1

        category = str(event.category or "").strip()
        if not category or category == "未分类":
            collection = collections.get(str(event.event_collection_id or ""))
            category = str((collection or {}).get("category") or "").strip()
        category_counter[category if category and category != "未分类" else "未分类"] += 1

    weekly = [{"week": week, "count": weekly_counter[week]} for week in sorted(weekly_counter)]
    time_slots = [{"slot": slot, "count": slot_counter[slot]} for slot in sorted(slot_counter)]
    weekday_slots = [
        {
            "weekday": _WEEKDAY_LABELS[weekday],
            "slot": slot,
            "count": count,
        }
        for (weekday, slot), count in sorted(weekday_slot_counter.items())
    ]
    by_category = [
        {"category": category, "count": count}
        for category, count in sorted(
            category_counter.items(), key=lambda item: (-item[1], item[0])
        )
    ]

    return {
        "ok": True,
        "data": {
            "weekly": weekly,
            "time_slots": time_slots,
            "weekday_slots": weekday_slots,
            "by_category": by_category,
        },
        "message": "",
    }


@router.get("/stats/ledger/comparison")  # type: ignore[untyped-decorator]
def ledger_comparison(
    months: Annotated[int, Query(ge=3, le=12)] = 6,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JsonObject:
    """返回连续月份的收支，以及环比和同比基线。"""

    # HTTP 请求由 Query 约束；显式校验同时覆盖单元测试和内部直接调用。
    if not 3 <= months <= 12:
        raise HTTPException(status_code=422, detail="months 必须在 3 到 12 之间")

    current_month = _month_floor(_today(db, owner_id))
    conn = db.get_connection()
    month_window = [_shift_months(current_month, offset) for offset in range(-(months - 1), 1)]
    query_start = _shift_months(month_window[0], -12).isoformat()
    query_end = _shift_months(current_month, 1).isoformat()

    # 同比窗口已经覆盖当前窗口和环比基线，一次查询即可满足三种比较。
    monthly_rows = conn.execute(
        f"""
        SELECT strftime('%Y-%m', ledger_date) AS month,
               transaction_type AS ledger_kind,
               {LEDGER_AMOUNT_TOTAL_EXPR} AS total
        FROM items WHERE type='ledger' AND owner_id=? AND deleted=0
        AND ledger_date >= ? AND ledger_date < ?
        GROUP BY month, ledger_kind ORDER BY month
    """,
        (owner_id, query_start, query_end),
    ).fetchall()

    monthly_map: dict[str, JsonObject] = {}
    for month, transaction_type, total in monthly_rows:
        month_key = str(month)
        values = monthly_map.setdefault(month_key, {"expense": 0.0, "income": 0.0})
        if transaction_type == "income":
            values["income"] = round(float(total or 0), 2)
        elif transaction_type == "expense":
            values["expense"] = round(float(total or 0), 2)

    result_months: list[JsonObject] = []
    for month_start in month_window:
        month_key = month_start.strftime("%Y-%m")
        current_values = monthly_map.get(month_key, {"expense": 0.0, "income": 0.0})
        prev_key = _shift_months(month_start, -1).strftime("%Y-%m")
        previous_values = monthly_map.get(prev_key, {"expense": 0.0, "income": 0.0})
        yoy_key = _shift_months(month_start, -12).strftime("%Y-%m")
        yoy_values = monthly_map.get(yoy_key, {"expense": 0.0, "income": 0.0})
        result_months.append(
            {
                "month": month_key,
                "expense": current_values["expense"],
                "income": current_values["income"],
                "prev_expense": previous_values["expense"],
                "prev_income": previous_values["income"],
                "yoy_expense": yoy_values["expense"],
                "yoy_income": yoy_values["income"],
            }
        )

    return {
        "ok": True,
        "data": {"months": result_months},
        "message": "",
    }


@router.get("/stats/activity-heatmap")  # type: ignore[untyped-decorator]
def activity_heatmap(
    year: Annotated[int | None, Query(ge=1970, le=9999)] = None,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JsonObject:
    """按天返回全年五类条目活动，用于贡献式热力图。"""

    if year is not None and not 1970 <= year <= 9999:
        raise HTTPException(status_code=422, detail="year 必须在 1970 到 9999 之间")
    target_year = year or _today(db, owner_id).year
    start_date = date(target_year, 1, 1)
    end_date = date(target_year, 12, 31)
    conn = db.get_connection()

    day_count = (end_date - start_date).days + 1
    all_days = [(start_date + timedelta(days=offset)).isoformat() for offset in range(day_count)]

    activity_counts: dict[str, Counter[str]] = {
        item_type: Counter() for item_type in ("ledger", "task", "event", "note", "diary")
    }

    # 账本和日记存的是业务自然日，可以继续直接按日期列聚合。
    calendar_rows = conn.execute(
        """
        SELECT type,
               CASE WHEN type = 'ledger' THEN ledger_date ELSE diary_date END AS activity_date,
               COUNT(*) AS count
        FROM items
        WHERE owner_id=? AND deleted=0 AND type IN ('ledger', 'diary')
          AND CASE WHEN type = 'ledger' THEN ledger_date ELSE diary_date END BETWEEN ? AND ?
        GROUP BY type, activity_date
        ORDER BY activity_date, type
        """,
        (owner_id, start_date.isoformat(), end_date.isoformat()),
    ).fetchall()
    for item_type, activity_date, count in calendar_rows:
        activity_counts[str(item_type)][str(activity_date)] = int(count or 0)

    # 真实时间戳先用日期前缀粗筛，再按用户时区精确换日。
    lower_date, upper_date = _coarse_timestamp_dates(start_date, end_date)
    timestamp_rows = conn.execute(
        """
        SELECT type, created_at, start_time
        FROM items
        WHERE owner_id=? AND deleted=0
          AND (
            (type='event' AND substr(COALESCE(start_time, ''), 1, 10) BETWEEN ? AND ?)
            OR (
              type IN ('task', 'note')
              AND substr(created_at, 1, 10) BETWEEN ? AND ?
            )
          )
        """,
        (owner_id, lower_date, upper_date, lower_date, upper_date),
    ).fetchall()
    user_timezone = TimezoneHelper.get_user_timezone(owner_id, db)
    for row in timestamp_rows:
        item_type = str(row["type"])
        value = row["start_time"] if item_type == "event" else row["created_at"]
        timestamp = _local_datetime(value, user_timezone)
        if timestamp is not None and _day_in_range(timestamp.date(), start_date, end_date):
            activity_counts[item_type][timestamp.date().isoformat()] += 1

    days: list[JsonObject] = []
    for day_key in all_days:
        ledger_count = activity_counts["ledger"][day_key]
        task_count = activity_counts["task"][day_key]
        event_count = activity_counts["event"][day_key]
        note_count = activity_counts["note"][day_key]
        diary_count = activity_counts["diary"][day_key]
        days.append(
            {
                "date": day_key,
                "count": ledger_count + task_count + event_count + note_count + diary_count,
                "ledger": ledger_count,
                "task": task_count,
                "event": event_count,
                "note": note_count,
                "diary": diary_count,
            }
        )

    return {
        "ok": True,
        "data": {"year": target_year, "days": days},
        "message": "",
    }
