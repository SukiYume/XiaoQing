"""为 Pendo Web 日程页构造概览、单条详情和集合详情。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final, Literal, cast
from zoneinfo import ZoneInfo

from ...config import PendoConfig
from ...models.item import EventItem
from ...services.db import Database
from ...services.event_graph import EventGraphService
from ...utils.identifiers import public_id
from ...utils.time_utils import TimezoneHelper
from ..utils import collection_payload
from .event_schedule import build_event_schedule, daterange, ensure_datetime, event_kind

JsonObject          = dict[str, Any]
ReminderStatus      = Literal["pending", "sent", "confirmed"]
EventWithCollection = tuple[EventItem, JsonObject | None]

_EVENT_KINDS: Final       = frozenset({"", "all", "single", "multi_node", "recurring"})
_REMINDER_FILTERS: Final  = frozenset({"", "all", "with", "none", "pending", "sent", "confirmed"})
_SQLITE_BATCH_SIZE: Final = 500


@dataclass(frozen=True, slots=True)
class _EventFilters:
    """已规范化并通过白名单校验的概览筛选条件。"""

    keyword: str
    category: str
    kind: str
    reminder: str


@dataclass(frozen=True, slots=True)
class _ReminderSummary:
    """提醒状态计数；仅在服务端筛选和汇总时使用。"""

    total: int
    pending: int
    sent: int
    confirmed: int


@dataclass(frozen=True, slots=True)
class _OverviewEvent:
    """概览构造过程中的内部事件，避免把计算字段泄漏到 API。"""

    id: str
    title: str
    category: str
    kind: str
    collection: JsonObject | None
    display_days: list[str]
    day_entries: dict[str, list[JsonObject]]
    reminder_summary: _ReminderSummary


def _parse_range(
    start_date: str,
    end_date: str,
    user_timezone: ZoneInfo,
) -> tuple[datetime, datetime]:
    """解析必填闭区间，不用主机当前时间掩盖空值或非法值。"""

    try:
        range_start = ensure_datetime(start_date, user_timezone)
        range_end = ensure_datetime(end_date, user_timezone, is_end=True)
    except ValueError as exc:
        raise ValueError("start_date and end_date must be valid ISO dates") from exc
    if range_start is None or range_end is None:
        raise ValueError("start_date and end_date are required")
    if range_end < range_start:
        raise ValueError("end_date must not precede start_date")
    return range_start, range_end


def _normalize_filters(
    keyword: str,
    category: str,
    kind: str,
    reminder: str,
) -> _EventFilters:
    """清理文本筛选，并拒绝前端约定之外的枚举值。"""

    normalized_kind     = kind.strip().lower()
    normalized_reminder = reminder.strip().lower()
    if normalized_kind not in _EVENT_KINDS:
        raise ValueError(f"unsupported event kind: {kind}")
    if normalized_reminder not in _REMINDER_FILTERS:
        raise ValueError(f"unsupported reminder filter: {reminder}")
    return _EventFilters(
        keyword  = keyword.strip().casefold(),
        category = category.strip(),
        kind     = normalized_kind,
        reminder = normalized_reminder,
    )


def list_event_categories(db: Database, owner_id: str) -> list[str]:
    """直接查询当前用户的日程分类，避免实例化全部历史条目。"""

    rows = (
        db.get_connection()
        .execute(
            """
        SELECT DISTINCT
            CASE
                WHEN category IS NULL OR TRIM(category) = '' THEN ?
                ELSE TRIM(category)
            END AS category
        FROM items
        WHERE owner_id = ? AND type = 'event' AND deleted = 0
        ORDER BY category
        """,
            (PendoConfig.DEFAULT_CATEGORY, owner_id),
        )
        .fetchall()
    )
    return [str(row["category"]) for row in rows]


def _event_matches_keyword(
    event: EventItem,
    keyword: str,
    collection: JsonObject | None,
) -> bool:
    """在叶子日程和所属集合的可搜索文本中匹配关键词。"""

    if not keyword:
        return True
    values = [event.title, event.content, event.location, event.notes, event.category]
    if collection is not None:
        values.extend((str(collection.get("title") or ""), str(collection.get("notes") or "")))
    return keyword in "\n".join(value or "" for value in values).casefold()


def _select_events(
    events: list[EventItem],
    collections_by_id: dict[str, JsonObject],
    filters: _EventFilters,
) -> list[EventWithCollection]:
    """先应用不依赖提醒日志的廉价筛选，减少后续批量读取量。"""

    selected: list[EventWithCollection] = []
    for event in events:
        collection  = collections_by_id.get(event.event_collection_id or "")
        actual_kind = event_kind(event)
        if filters.category and event.category != filters.category:
            continue
        if filters.kind not in {"", "all", actual_kind}:
            continue
        if not _event_matches_keyword(event, filters.keyword, collection):
            continue
        selected.append((event, collection))
    return selected


def _build_reminder_rows(event: EventItem, reminder_logs: list[JsonObject]) -> list[JsonObject]:
    """合并计划提醒和发送日志；未写日志的提醒保持待发送状态。"""

    log_map = {str(log["remind_time"]): log for log in reminder_logs if log.get("remind_time")}
    rows: list[JsonObject] = []
    for remind_time in sorted(str(value) for value in event.remind_times if value):
        log                    = log_map.get(remind_time)
        status: ReminderStatus = "pending"
        if log is not None and log.get("confirmed_at"):
            status = "confirmed"
        elif log is not None and log.get("sent_at"):
            status = "sent"
        rows.append(
            {
                "time": remind_time,
                "status": status,
                "sent_at": log.get("sent_at") if log else None,
                "confirmed_at": log.get("confirmed_at") if log else None,
                "repeat_count": int(log.get("repeat_count") or 0) if log else 0,
            }
        )
    return rows


def _summarize_reminders_in_range(
    reminder_rows: list[JsonObject],
    range_start_day: date,
    range_end_day: date,
    user_timezone: ZoneInfo,
) -> _ReminderSummary:
    """统计范围内提醒；坏的导入时间不会拖垮整个概览。"""

    counts: dict[ReminderStatus, int] = {"pending": 0, "sent": 0, "confirmed": 0}
    for row in reminder_rows:
        remind_time = row.get("time")
        if not remind_time:
            continue
        try:
            remind_at = ensure_datetime(str(remind_time), user_timezone)
        except ValueError:
            continue
        status = row.get("status")
        if (
            remind_at is not None
            and range_start_day <= remind_at.date() <= range_end_day
            and status in counts
        ):
            counts[cast(ReminderStatus, status)] += 1
    return _ReminderSummary(
        total     = sum(counts.values()),
        pending   = counts["pending"],
        sent      = counts["sent"],
        confirmed = counts["confirmed"],
    )


def _event_matches_reminder(summary: _ReminderSummary, reminder: str) -> bool:
    if reminder in {"", "all"}:
        return True
    if reminder == "with":
        return summary.total > 0
    if reminder == "none":
        return summary.total == 0
    return {
        "pending": summary.pending,
        "sent": summary.sent,
        "confirmed": summary.confirmed,
    }.get(reminder, 0) > 0


def _fetch_reminder_logs_by_event_ids(
    db: Database,
    event_ids: list[str],
) -> dict[str, list[JsonObject]]:
    """分批读取多条日程的提醒日志，避免 N+1 查询和 SQLite 变量上限。"""

    unique_ids = [event_id for event_id in dict.fromkeys(event_ids) if event_id]
    logs_by_event: dict[str, list[JsonObject]] = {event_id: [] for event_id in unique_ids}
    conn = db.get_connection()
    for offset in range(0, len(unique_ids), _SQLITE_BATCH_SIZE):
        batch        = unique_ids[offset : offset + _SQLITE_BATCH_SIZE]
        placeholders = ",".join("?" for _ in batch)
        rows         = conn.execute(
            f"""
            SELECT item_id, remind_time, sent_at, confirmed_at, user_action,
                   repeat_count, last_sent_at
            FROM reminder_logs
            WHERE item_id IN ({placeholders})
            ORDER BY item_id, remind_time
            """,
            batch,
        ).fetchall()
        for row in rows:
            log     = dict(row)
            item_id = str(log.pop("item_id"))
            logs_by_event[item_id].append(log)
    return logs_by_event


def _prepare_overview_events(
    db: Database,
    selected: list[EventWithCollection],
    range_start_day: date,
    range_end_day: date,
    reminder_filter: str,
    user_timezone: ZoneInfo,
) -> list[_OverviewEvent]:
    """批量读取日志后，完成提醒筛选和时间轴展开。"""

    logs_by_event = _fetch_reminder_logs_by_event_ids(
        db,
        [event.id for event, _collection in selected],
    )
    prepared: list[_OverviewEvent] = []
    for event, collection in selected:
        schedule     = build_event_schedule(event, range_start_day, range_end_day, user_timezone)
        display_days = cast(list[str], schedule["display_days"])
        if not display_days:
            continue
        reminder_summary = _summarize_reminders_in_range(
            _build_reminder_rows(event, logs_by_event.get(event.id, [])),
            range_start_day,
            range_end_day,
            user_timezone,
        )
        if not _event_matches_reminder(reminder_summary, reminder_filter):
            continue
        prepared.append(
            _OverviewEvent(
                id               = event.id,
                title            = event.title or "",
                category         = event.category or "",
                kind             = str(schedule["kind"]),
                collection       = collection_payload(collection),
                display_days     = display_days,
                day_entries      = cast(dict[str, list[JsonObject]], schedule["day_entries"]),
                reminder_summary = reminder_summary,
            )
        )
    return prepared


def _build_calendar_views(
    events: list[_OverviewEvent],
    range_start_day: date,
    range_end_day: date,
) -> tuple[dict[str, JsonObject], list[JsonObject]]:
    """一次遍历生成日历格和有内容的时间线日期。"""

    day_keys                             = daterange(range_start_day, range_end_day)
    calendar_days: dict[str, JsonObject] = {
        day: {"date": day, "count": 0, "items": [], "has_events": False} for day in day_keys
    }
    timeline_days: dict[str, list[JsonObject]] = {day: [] for day in day_keys}

    for event in events:
        for day in event.display_days:
            calendar = calendar_days.get(day)
            if calendar is None:
                continue
            calendar["count"]      = int(calendar["count"]) + 1
            calendar["has_events"] = True
            items                  = cast(list[JsonObject], calendar["items"])
            if len(items) < 3:
                items.append(
                    {
                        "event_id": event.id,
                        "kind": event.kind,
                        "label": event.title or "无标题",
                    }
                )
            timeline_days[day].extend(
                {
                    "event_id": event.id,
                    "event_display_id": public_id(event.id),
                    "collection": event.collection,
                    "kind": row["kind"],
                    "day": day,
                    "time": row["time"],
                    "time_label": row["time_label"],
                    "title": row["title"],
                    "subtitle": row["subtitle"],
                    "location": row["location"],
                    "category": row["category"],
                    "reminder_total": event.reminder_summary.total,
                }
                for row in event.day_entries.get(day, [])
            )

    timeline_list: list[JsonObject] = []
    for day in day_keys:
        items = timeline_days[day]
        if not items:
            continue
        items.sort(key=lambda item: str(item.get("time") or ""))
        timeline_list.append({"date": day, "items": items})
    return calendar_days, timeline_list


def build_events_overview(  # noqa: PLR0913 - 参数对应稳定的 HTTP 查询契约。
    db: Database,
    owner_id: str,
    *,
    start_date: str,
    end_date: str,
    keyword: str  = "",
    category: str = "",
    kind: str     = "all",
    reminder: str = "all",
) -> JsonObject:
    """返回指定范围的最小日程概览响应。"""

    user_timezone = TimezoneHelper.get_user_timezone(owner_id, db)
    range_start, range_end = _parse_range(start_date, end_date, user_timezone)
    filters      = _normalize_filters(keyword, category, kind, reminder)
    range_events = db.get_events_for_range(
        owner_id,
        range_start.isoformat(timespec="seconds"),
        range_end.isoformat(timespec="seconds"),
    )
    collection_ids = [
        event.event_collection_id for event in range_events if event.event_collection_id is not None
    ]
    collections_by_id = db.get_event_collections_by_ids(owner_id, collection_ids)
    selected          = _select_events(range_events, collections_by_id, filters)
    events            = _prepare_overview_events(
        db,
        selected,
        range_start.date(),
        range_end.date(),
        filters.reminder,
        user_timezone,
    )
    calendar_days, timeline_days = _build_calendar_views(
        events,
        range_start.date(),
        range_end.date(),
    )

    return {
        "summary": {
            "event_count": len(events),
            "multi_node_count": sum(event.kind == "multi_node" for event in events),
            "reminder_count": sum(event.reminder_summary.total for event in events),
        },
        "categories": list_event_categories(db, owner_id),
        "calendar_days": calendar_days,
        "timeline_days": timeline_days,
        # 页面只用这些字段补全时间线卡片；详情按需从独立端点读取。
        "events": [
            {
                "id": event.id,
                "display_id": public_id(event.id),
                "title": event.title,
                "category": event.category,
                "kind": event.kind,
                "collection": event.collection,
            }
            for event in events
        ],
    }


def build_event_detail(db: Database, owner_id: str, event_id: str) -> JsonObject | None:
    """返回叶子日程详情；传入集合头编号时转交集合详情构造器。"""

    family = EventGraphService(db).load_by_id(owner_id, event_id)
    if family.collection and family.leaf is None:
        return build_event_collection_detail(db, owner_id, event_id)

    event = db.get_item(event_id, owner_id=owner_id)
    if not isinstance(event, EventItem):
        return None
    collection = (
        db.get_event_collection(event.event_collection_id, owner_id)
        if event.event_collection_id
        else None
    )
    related_instances: list[JsonObject] = []
    if collection is not None:
        related_instances = [
            {
                "id": child.id,
                "display_id": child.display_id,
                "title": child.title,
                "start_time": child.start_time,
                "end_time": child.end_time,
            }
            for child in db.get_collection_events(str(collection["id"]), owner_id)
            if child.id != event.id
        ][:12]
    return {
        # 详情只公开展示与单条编辑器实际使用的叶子字段。
        "event": {
            "id": event.id,
            "display_id": event.display_id,
            "title": event.title or "",
            "category": event.category or "",
            "start_time": event.start_time,
            "end_time": event.end_time,
            "location": event.location or "",
            "notes": event.notes or "",
            "event_role": event.event_role,
            "event_collection_kind": event.event_collection_kind,
            "kind": event_kind(event),
            "collection": collection_payload(collection),
            "reminders": _build_reminder_rows(event, db.get_reminder_logs(event.id)),
            "series_id": event.event_collection_id or None,
        },
        "related_instances": related_instances,
    }


def build_event_collection_detail(
    db: Database,
    owner_id: str,
    collection_id: str,
) -> JsonObject | None:
    """返回集合头及节点列表，不为列表页计算未使用的提醒和时间轴。"""

    collection = db.get_event_collection(collection_id, owner_id)
    if collection is None:
        return None
    children = db.get_collection_events(collection_id, owner_id)
    return {
        "collection": collection_payload(collection),
        "children": [
            {
                "id": child.id,
                "display_id": child.display_id,
                "title": child.title,
                "start_time": child.start_time,
            }
            for child in children
        ],
    }
