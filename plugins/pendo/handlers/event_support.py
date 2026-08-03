"""日程提醒计算与消息格式化的共用函数。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..utils.formatters import ItemFormatter, MessageBuilder
from ..utils.time_utils import TimezoneHelper, parse_remind_times
from ..utils.validators import (
    build_remind_times_from_rules,
    derive_reminder_rules,
    normalize_reminder_rules,
    with_start_time_reminder_rule,
)

_DEFAULT_REMINDER_OFFSETS = (
    timedelta(days=1),
    timedelta(hours=1),
    timedelta(minutes=10),
)
_REMINDER_STATUS_LABELS: Final[dict[str, str]] = {
    "✅": "✅ 已确认",
    "📩": "📩 已发送未确认",
    "⏳": "⏳ 待发送",
}


def event_display_timezone(event: Any) -> ZoneInfo:
    """Return the explicit timezone attached to an event or collection."""
    raw = event.get("timezone") if isinstance(event, dict) else getattr(event, "timezone", None)
    try:
        return ZoneInfo(str(raw or TimezoneHelper.DEFAULT_TZ.key))
    except (ZoneInfoNotFoundError, ValueError):
        return TimezoneHelper.DEFAULT_TZ


def has_time(remind_times: list[str], target: str) -> bool:
    """将相差不足 60 秒的提醒时间视为同一时刻。"""
    try:
        target_dt = datetime.fromisoformat(target)
    except (ValueError, TypeError):
        return target in remind_times
    for current in remind_times:
        try:
            current_dt = datetime.fromisoformat(current)
            if target_dt.tzinfo is None:
                current_dt = current_dt.replace(tzinfo=None)
            elif current_dt.tzinfo is None:
                current_dt = current_dt.replace(tzinfo=target_dt.tzinfo)
            else:
                current_dt = current_dt.astimezone(target_dt.tzinfo)
            if abs((target_dt - current_dt).total_seconds()) < 60:
                return True
        except (ValueError, TypeError):
            continue
    return False


def default_reminders(start_time: str | None) -> list[str]:
    """生成提前 1 天、1 小时和 10 分钟的默认提醒。"""
    if not start_time:
        return []
    try:
        start_dt = datetime.fromisoformat(start_time)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=TimezoneHelper.DEFAULT_TZ)
        now = TimezoneHelper.now(start_dt.tzinfo)
        return [
            (start_dt - offset).isoformat()
            for offset in _DEFAULT_REMINDER_OFFSETS
            if start_dt - offset > now
        ]
    except (ValueError, TypeError):
        return []


def _shift_reminders_for_new_start(
    old_start: datetime,
    new_start: datetime,
    remind_times: list[str],
) -> list[str]:
    """保留原提醒与日程的时间差，并兼容历史无时区数据。"""
    shifted: list[str] = []
    for remind_time in remind_times:
        try:
            remind_dt = datetime.fromisoformat(remind_time)
        except (ValueError, TypeError):
            continue
        if old_start.tzinfo is None:
            remind_dt = remind_dt.replace(tzinfo=None)
        elif remind_dt.tzinfo is None:
            remind_dt = remind_dt.replace(tzinfo=old_start.tzinfo)
        else:
            remind_dt = remind_dt.astimezone(old_start.tzinfo)
        shifted.append((new_start - (old_start - remind_dt)).isoformat())
    return shifted


def ensure_event_reminders(
    parsed_data: dict[str, Any],
    *,
    build_from_offsets: Callable[[str, list[Any]], list[str]] | None = None,
) -> list[str]:
    """解析日程提醒，并保证日程开始时刻本身在其中。"""
    if parsed_data.get("remind_times"):
        remind_times = parse_remind_times(parsed_data["remind_times"])
    elif parsed_data.get("remind_offsets") and parsed_data.get("start_time") and build_from_offsets:
        remind_times = build_from_offsets(parsed_data["start_time"], parsed_data["remind_offsets"])
    else:
        remind_times = default_reminders(parsed_data.get("start_time"))

    return ensure_start_time_reminder(remind_times, parsed_data.get("start_time"))


def ensure_event_reminder_rules(
    parsed_data: dict[str, Any],
    remind_times: list[str] | None = None,
) -> list[dict[str, int]]:
    """从解析后日程中得到语义化提醒规则。"""
    if "reminder_rules" in parsed_data:
        return normalize_reminder_rules(parsed_data.get("reminder_rules"))

    start_time = parsed_data.get("start_time")
    reminders = remind_times if remind_times is not None else parsed_data.get("remind_times")
    if start_time and reminders:
        return with_start_time_reminder_rule(derive_reminder_rules(start_time, reminders))

    return [{"offset_seconds": 0}] if start_time else []


def ensure_start_time_reminder(remind_times: list[str], start_time: str | None) -> list[str]:
    """保证当前日程开始时刻在提醒列表中恰好出现一次。"""
    normalized = list(remind_times)
    if start_time and not has_time(normalized, start_time):
        normalized.append(datetime.fromisoformat(start_time).isoformat())
    normalized.sort()
    return normalized


def recalculate_event_reminders(event: Any, updates: dict[str, Any]) -> list[str]:
    """日程开始时间改变时，按原偏移量重算提醒。"""
    reminder_rules = normalize_reminder_rules(getattr(event, "reminder_rules", None) or [])
    target_start = updates.get("start_time") or getattr(event, "start_time", None)
    if reminder_rules and target_start:
        return build_remind_times_from_rules(target_start, reminder_rules)

    existing = parse_remind_times(event.remind_times)
    # 空规则和空时间表示用户已明确清空全部提醒。编辑日程时间时必须保留
    # 这个状态，不能把创建日程时使用的默认提醒重新补回来。
    if not existing:
        return []

    new_start = updates.get("start_time")
    if not new_start:
        remind_times = existing
        start = event.start_time
    else:
        if existing and event.start_time:
            old_start = datetime.fromisoformat(event.start_time)
            new_start_dt = datetime.fromisoformat(new_start)
            remind_times = _shift_reminders_for_new_start(old_start, new_start_dt, existing)
        else:
            remind_times = default_reminders(new_start)
        start = new_start

    return ensure_start_time_reminder(remind_times, start)


def format_recurring_event_created(
    title: str, instance_count: int, remind_count: int, collection_id: str
) -> str:
    lines = [
        "✅ 已创建日程",
        "",
        f"🗓️ {title}",
        f"🔄 共 {instance_count} 个实例",
    ]
    if remind_count:
        lines.append(f"⏰ 每项已设置 {remind_count} 个提醒")
    lines.append(f"\n`{collection_id}`")
    lines.append(f"\n💡 用 /pendo event reminders {collection_id} 查看所有实例提醒")
    return "\n".join(lines)


def format_milestone_event_created(event: dict[str, Any]) -> str:
    raw_milestones = event.get("milestones", [])
    milestones = (
        [milestone for milestone in raw_milestones if isinstance(milestone, dict)]
        if isinstance(raw_milestones, list)
        else []
    )
    remind_count = len(event.get("remind_times", []))
    display_timezone = event_display_timezone(event)

    lines = [
        "✅ 已创建日程",
        "",
        f"🗓️ {event.get('title', '无标题')}",
        f"🗺️ 多时间节点事件 ({len(milestones)}个节点)",
    ]
    for milestone in milestones:
        milestone_time = ItemFormatter.format_datetime(
            milestone.get("time", ""), "%m-%d %H:%M", tz=display_timezone
        )
        lines.append(f"📌 {milestone.get('name', '')}  {milestone_time}")

    if event.get("location"):
        lines.append(f"📍 {event['location']}")
    if event.get("notes"):
        lines.append(f"📝 {event['notes']}")
    if remind_count:
        lines.append(f"🔔 已设置 {remind_count} 个提醒")

    lines.append(f"\n`{event['id']}`")
    lines.append(f"\n💡 用 /pendo event reminders {event['id']} 查看提醒")
    return "\n".join(lines)


def format_event_created(event: dict[str, Any]) -> str:
    start_time = ItemFormatter.format_datetime(
        event["start_time"], tz=event_display_timezone(event)
    )
    remind_count = len(event.get("remind_times", []))

    lines = [
        "✅ 已创建日程",
        "",
        f"🗓️ {event.get('title', '无标题')}",
        "📆 单次事件",
        f"⏰ {start_time}",
    ]
    if event.get("location"):
        lines.append(f"📍 {event['location']}")
    if event.get("notes"):
        lines.append(f"📝 {event['notes']}")
    if remind_count:
        lines.append(f"🔔 已设置 {remind_count} 个提醒")
    lines.append(f"\n`{event['id']}`")
    lines.append(f"\n💡 用 /pendo event reminders {event['id']} 查看提醒")
    return "\n".join(lines)


def format_conflicts(conflicts: list[dict[str, Any]], event: dict[str, Any]) -> str:
    builder = MessageBuilder()
    builder.add_line(f"⚠️ 日程 {event.get('title', '无标题')} 与以下日程冲突:")
    builder.add_blank()
    for conflict in conflicts[:3]:
        start_str = ItemFormatter.format_datetime(
            conflict.get("start_time", ""),
            "%m-%d %H:%M",
            tz=event_display_timezone(conflict or event),
        )
        builder.add_item("•", f"{conflict.get('title', '无标题')} ({start_str})")
    builder.add_blank()
    builder.add_line("输入 yes 确认创建，no 取消")
    return builder.build()


def get_remind_status(log: dict[str, Any] | None) -> str:
    if log and log.get("confirmed_at"):
        return "✅"
    if log and log.get("sent_at"):
        return "📩"
    return "⏳"


def get_remind_status_label(log: dict[str, Any] | None) -> str:
    """返回适合详情页展示的完整提醒状态。"""
    return _REMINDER_STATUS_LABELS[get_remind_status(log)]


def format_event_reminders(event: Any, log_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
    remind_times = parse_remind_times(getattr(event, "remind_times", []))
    title = getattr(event, "title", "") or "无标题"
    start_time = getattr(event, "start_time", "") or ""
    notes = getattr(event, "notes", None) or ""
    display_timezone = event_display_timezone(event)

    if not remind_times:
        return {"status": "info", "message": f"🔔 日程: {title}\n\n未设置提醒"}

    builder = MessageBuilder()
    builder.add_line(f"🔔 **{title}** 的提醒列表")
    event_time_str = ItemFormatter.format_datetime(
        start_time, "%m月%d日 %H:%M", tz=display_timezone
    )
    builder.add_line(f"🗓️ 日程时间: {event_time_str}")
    if notes:
        builder.add_line(f"📝 {notes}")
    builder.add_line("─" * 30)
    builder.add_blank()

    for index, remind_time in enumerate(remind_times, 1):
        time_str = ItemFormatter.format_datetime(remind_time, "%m月%d日 %H:%M", tz=display_timezone)
        status = get_remind_status_label(log_map.get(remind_time))
        builder.add_line(f"⏰ **提醒 {index}**: {time_str}  {status}")

    return {"status": "success", "message": builder.build()}
