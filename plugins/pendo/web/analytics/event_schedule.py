"""为看板、日程页和 Widget 提供统一的日程展示时间轴。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from ...models.item import EventItem

JsonObject = dict[str, Any]


def _resolve_datetime(
    value: str | None,
    timezone_info: tzinfo,
    *,
    is_end: bool = False,
) -> tuple[datetime, int] | None:
    """返回用户墙钟和对应的真实时间轴毫秒值。"""

    if value is None or not value.strip():
        return None
    text = value.strip()
    if len(text) == 10:
        text = f"{text}{'T23:59:59' if is_end else 'T00:00:00'}"
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        local = parsed
        instant = parsed.replace(tzinfo=timezone_info)
    else:
        instant = parsed
        local = parsed.astimezone(timezone_info).replace(tzinfo=None)
    return local, round(instant.timestamp() * 1000)


def ensure_datetime(
    value: str | None,
    timezone_info: tzinfo,
    *,
    is_end: bool = False,
) -> datetime | None:
    """解析 ISO 日期/时间，并转换为调用方明确指定的墙钟时间。"""

    resolved = _resolve_datetime(value, timezone_info, is_end=is_end)
    return resolved[0] if resolved is not None else None


def daterange(start_day: date, end_day: date) -> list[str]:
    """生成闭区间内的 ISO 自然日；反向区间返回空列表。"""

    return [
        (start_day + timedelta(days=offset)).isoformat()
        for offset in range((end_day - start_day).days + 1)
    ]


def event_kind(event: EventItem) -> str:
    """把持久化集合类型收敛为三个公开展示类型。"""

    collection_kind = str(event.event_collection_kind or "")
    if collection_kind in {"multi_node", "recurring"}:
        return collection_kind
    return "single"


def build_event_schedule(
    event: EventItem,
    range_start_day: date,
    range_end_day: date,
    timezone_info: tzinfo,
) -> JsonObject:
    """把单个日程展开为范围内每天可直接渲染的时间轴条目。"""

    kind           = event_kind(event)
    start_resolved = _resolve_datetime(event.start_time, timezone_info)
    end_resolved = _resolve_datetime(event.end_time, timezone_info, is_end=True)
    if start_resolved is None:
        return {
            "kind": kind,
            "display_days": [],
            "day_entries": {},
            "time_summary": "未设置时间",
            "start_epoch_ms": None,
            "end_epoch_ms": None,
        }
    start_dt, start_epoch_ms = start_resolved
    end_dt, end_epoch_ms = end_resolved if end_resolved is not None else (None, None)

    display_start = max(start_dt.date(), range_start_day)
    display_end   = min((end_dt or start_dt).date(), range_end_day)
    display_days  = daterange(display_start, display_end) if display_start <= display_end else []
    start_time = start_dt.isoformat(timespec="seconds")
    end_time = end_dt.isoformat(timespec="seconds") if end_dt else ""
    start_day    = start_dt.date().isoformat()
    end_day      = end_dt.date().isoformat() if end_dt else start_day
    time_summary = start_dt.strftime("%H:%M")
    if end_dt is not None:
        time_summary = f"{time_summary} - {end_dt.strftime('%H:%M')}"

    day_entries: dict[str, list[JsonObject]] = {}
    for day in display_days:
        if day == start_day:
            row_time   = start_time
            time_label = start_dt.strftime("%H:%M")
        elif end_dt is not None and day == end_day:
            row_time   = f"{day}T00:00:00"
            time_label = f"至 {end_dt.strftime('%H:%M')}"
        else:
            row_time   = f"{day}T00:00:00"
            time_label = "跨天"
        day_entries[day] = [
            {
                "day": day,
                "kind": kind,
                "title": event.title or "无标题",
                "subtitle": time_summary,
                "time": row_time,
                "time_label": time_label,
                "start_time": start_time,
                "end_time": end_time,
                "start_epoch_ms": start_epoch_ms,
                "end_epoch_ms": end_epoch_ms,
                "location": event.location or "",
                "category": event.category or "",
            }
        ]

    return {
        "kind": kind,
        "display_days": display_days,
        "day_entries": day_entries,
        "time_summary": time_summary,
        "start_epoch_ms": start_epoch_ms,
        "end_epoch_ms": end_epoch_ms,
    }
