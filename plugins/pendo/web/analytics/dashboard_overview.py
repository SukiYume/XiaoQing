"""聚合 Pendo Web 看板所需的日程、任务、账本和日记摘要。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, cast

from ...models.item import EventItem
from ...services.db import Database
from ...utils.time_utils import TimezoneHelper
from ..utils import collection_payload, item_to_dict
from .event_schedule import build_event_schedule

JsonObject = dict[str, Any]
ScheduledEvent = tuple[date, JsonObject]


def _month_bounds(day: date) -> tuple[date, date]:
    """返回给定日期所在自然月的首日和末日。"""

    month_start = day.replace(day=1)
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    return month_start, next_month - timedelta(days=1)


def _display_fields(row: JsonObject, collection: JsonObject | None) -> JsonObject:
    """将多节点集合标题、地点和分类叠加到日程显示行。"""

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


def _event_entries(
    item: EventItem,
    range_start: date,
    range_end: date,
    user_timezone: Any,
    collection: JsonObject | None = None,
) -> list[ScheduledEvent]:
    """把单个日程展开为带显示日期的看板行。"""

    entries: list[ScheduledEvent] = []
    schedule = build_event_schedule(item, range_start, range_end, user_timezone)
    item_dict = item_to_dict(item)
    event_collection = collection_payload(collection)
    for day in schedule["display_days"]:
        for row in schedule["day_entries"].get(day, []):
            display = _display_fields(row, collection)
            entries.append(
                (
                    date.fromisoformat(day),
                    {
                        **item_dict,
                        "id": getattr(item, "id", ""),
                        "title": display["title"],
                        "display_title": display["display_title"],
                        "display_subtitle": display["display_subtitle"],
                        "node_title": row["title"],
                        "collection": event_collection,
                        "start_time": row["start_time"],
                        "end_time": row["end_time"],
                        "start_epoch_ms": row["start_epoch_ms"],
                        "end_epoch_ms": row["end_epoch_ms"],
                        "location": display["location"],
                        "category": display["category"],
                        "entry_kind": row["kind"],
                    },
                ),
            )
    return entries


def _local_datetime(
    db: Database,
    owner_id: str,
    value: datetime | None,
) -> datetime:
    """把当前时间统一到用户时区下的无时区墙钟。"""

    user_timezone = TimezoneHelper.get_user_timezone(owner_id, db)
    if value is None:
        return cast(datetime, TimezoneHelper.now(user_timezone)).replace(
            tzinfo=None,
            microsecond=0,
        )
    if value.tzinfo is not None:
        return value.astimezone(user_timezone).replace(tzinfo=None, microsecond=0)
    return value.replace(microsecond=0)


def _scheduled_events(
    db: Database,
    owner_id: str,
    range_start: date,
    range_end: date,
) -> list[ScheduledEvent]:
    """一次读取覆盖区间和集合头，再展开全部日程显示行。"""

    raw_events = db.get_events_for_range(
        owner_id,
        f"{range_start.isoformat()}T00:00:00",
        f"{range_end.isoformat()}T23:59:59",
    )
    collection_ids = list(
        dict.fromkeys(
            str(collection_id) for item in raw_events if (collection_id := item.event_collection_id)
        )
    )
    collections = (
        db.get_event_collections_by_ids(owner_id, collection_ids) if collection_ids else {}
    )
    user_timezone = TimezoneHelper.get_user_timezone(owner_id, db)

    entries: list[ScheduledEvent] = []
    for item in raw_events:
        collection_id = item.event_collection_id
        collection = collections.get(str(collection_id)) if collection_id else None
        entries.extend(_event_entries(item, range_start, range_end, user_timezone, collection))
    return entries


def _recent_counts(
    db: Database,
    owner_id: str,
    start_date: date,
    start_time: datetime,
    end_time: datetime,
) -> tuple[int, int]:
    """用一次聚合查询统计近三十天日记和已完成任务。"""

    user_timezone = TimezoneHelper.get_user_timezone(owner_id, db)
    start_epoch = TimezoneHelper.parse(start_time.isoformat(), user_timezone).timestamp()
    end_epoch = TimezoneHelper.parse(end_time.isoformat(), user_timezone).timestamp()

    row = (
        db.get_connection()
        .execute(
            """
        SELECT
          COUNT(CASE
            WHEN type = 'diary' AND diary_date BETWEEN ? AND ? THEN 1
          END) AS recent_diary_count,
          COUNT(CASE
            WHEN type = 'task'
             AND status = 'done'
             AND pendo_utc_epoch(
                   COALESCE(NULLIF(completed_at, ''), NULLIF(updated_at, '')), ?
                 ) BETWEEN ? AND ?
            THEN 1
          END) AS recent_completed_count
        FROM items
        WHERE owner_id = ? AND deleted = 0
        """,
            (
                start_date.isoformat(),
                end_time.date().isoformat(),
                user_timezone.key,
                start_epoch,
                end_epoch,
                owner_id,
            ),
        )
        .fetchone()
    )
    return (int(row[0]), int(row[1])) if row else (0, 0)


def build_dashboard_overview(
    db: Database,
    owner_id: str,
    now: datetime | None = None,
) -> JsonObject:
    """构建当前用户的完整看板响应。"""

    current = _local_datetime(db, owner_id, now)
    today_date = current.date()
    month_start_date, month_end_date = _month_bounds(today_date)
    agenda_end_date = (current + timedelta(days=21)).date()
    coverage_end_date = max(month_end_date, agenda_end_date)
    today = today_date.isoformat()
    month_ago_date = today_date - timedelta(days=30)
    month_ago_time = current - timedelta(days=30)

    active_task_filters = {"type": "task", "status": "open"}
    active_task_count = db.count_items(owner_id, active_task_filters)
    active_tasks = db.get_active_task_preview(owner_id, limit=8)
    tasks_completed = db.get_items(
        owner_id,
        filters={
            "type": "task",
            "status": "done",
            "sort_field": "updated_at",
            "sort_order": "DESC",
        },
        limit=16,
        use_cache=True,
    )

    recent_ledger = db.get_items(
        owner_id,
        filters={
            "type": "ledger",
            "date_field": "ledger_date",
            "start_date": month_start_date.isoformat(),
            "end_date": today,
            "sort_field": "ledger_date",
            "sort_order": "DESC",
        },
        limit=8,
        use_cache=True,
    )

    month_ledger = db.aggregate_ledger_amounts_by_day(
        owner_id,
        {
            "type": "ledger",
            "date_field": "ledger_date",
            "start_date": month_start_date.isoformat(),
            "end_date": today,
        },
    )

    spending_by_day = {
        day: expense_cents / 100 for day, (expense_cents, _income_cents) in month_ledger.items()
    }
    month_expense = sum(expense_cents for expense_cents, _ in month_ledger.values()) / 100
    month_income = sum(income_cents for _, income_cents in month_ledger.values()) / 100

    spending_trend: list[JsonObject] = []
    for offset in range((today_date - month_start_date).days + 1):
        day = (month_start_date + timedelta(days=offset)).isoformat()
        spending_trend.append({"date": day, "amount": round(spending_by_day.get(day, 0.0), 2)})

    scheduled_events = _scheduled_events(db, owner_id, month_start_date, coverage_end_date)

    events_month = [
        payload
        for display_day, payload in scheduled_events
        if month_start_date <= display_day <= month_end_date
    ]
    events_month.sort(key=lambda event: event.get("start_time") or "")

    events_agenda = [
        payload
        for display_day, payload in scheduled_events
        if month_start_date <= display_day <= agenda_end_date
    ]
    events_agenda.sort(key=lambda event: event.get("start_time") or "")

    recent_diary_count, recent_completed_count = _recent_counts(
        db,
        owner_id,
        month_ago_date,
        month_ago_time,
        current,
    )

    user_timezone = TimezoneHelper.get_user_timezone(owner_id, db)

    def completed_sort_key(item: object) -> float:
        value = getattr(item, "completed_at", "") or getattr(item, "updated_at", "") or ""
        try:
            return cast(datetime, TimezoneHelper.parse(str(value), user_timezone)).timestamp()
        except (OverflowError, TypeError, ValueError):
            return float("-inf")

    tasks_completed.sort(key=completed_sort_key, reverse=True)

    return {
        "summary": {
            "events_month": len(events_month),
            "tasks_pending": active_task_count,
            "tasks_done_recent": recent_completed_count,
            "ledger_month_expense": round(month_expense, 2),
            "diary_month": recent_diary_count,
        },
        "events_month": events_month,
        "events_agenda": events_agenda,
        "tasks": {
            "active": [item_to_dict(task) for task in active_tasks],
            "completed": [item_to_dict(task) for task in tasks_completed[:4]],
        },
        "recent_ledger": [item_to_dict(item) for item in recent_ledger],
        "spending_trend": spending_trend,
        "month_summary": {
            "income": round(month_income, 2),
            "expense": round(month_expense, 2),
            "balance": round(month_income - month_expense, 2),
        },
    }
