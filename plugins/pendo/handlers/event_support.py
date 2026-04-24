"""Shared support helpers for event reminders and message formatting."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from ..utils.formatters import ItemFormatter, MessageBuilder
from ..utils.time_utils import parse_remind_times
from ..utils.validators import (
    build_remind_times_from_rules,
    derive_reminder_rules,
    normalize_reminder_rules,
    with_start_time_reminder_rule,
)


def group_reminders_by_milestone(
    remind_times: list[str], milestones: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], list[str]]]:
    """Group reminders by nearest following milestone."""
    milestone_map = group_reminders_by_milestone_index(remind_times, milestones)
    return [
        (milestones[idx], milestone_map[idx])
        for idx in range(len(milestones))
        if milestone_map[idx]
    ]


def group_reminders_by_milestone_index(
    remind_times: list[str], milestones: list[dict[str, Any]]
) -> dict[int, list[str]]:
    """Group reminders by milestone index."""
    milestone_map: dict[int, list[str]] = {i: [] for i in range(len(milestones))}
    for rt in remind_times:
        try:
            remind_dt = datetime.fromisoformat(rt)
        except (ValueError, TypeError):
            continue
        best_idx: int | None = None
        best_delta = None
        for idx, milestone in enumerate(milestones):
            try:
                milestone_dt = datetime.fromisoformat(milestone.get("time", ""))
            except (ValueError, TypeError):
                continue
            if milestone_dt >= remind_dt:
                delta = milestone_dt - remind_dt
                if best_delta is None or delta < best_delta:
                    best_delta = delta
                    best_idx = idx
        if best_idx is not None:
            milestone_map[best_idx].append(rt)
    return milestone_map


def normalize_iso(time_str: str) -> str:
    return datetime.fromisoformat(time_str).isoformat()


def has_time(remind_times: list[str], target: str) -> bool:
    """Treat values within 60 seconds as equivalent."""
    try:
        target_dt = datetime.fromisoformat(target)
    except (ValueError, TypeError):
        return target in remind_times
    for current in remind_times:
        try:
            if abs((target_dt - datetime.fromisoformat(current)).total_seconds()) < 60:
                return True
        except (ValueError, TypeError):
            continue
    return False


def default_reminders(start_time: str | None) -> list[str]:
    """Default reminders: 1 day, 1 hour, 10 minutes before."""
    if not start_time:
        return []
    try:
        start_dt = datetime.fromisoformat(start_time)
        now = datetime.now()
        offsets = [timedelta(days=1), timedelta(hours=1), timedelta(minutes=10)]
        return [(start_dt - offset).isoformat() for offset in offsets if start_dt - offset > now]
    except (ValueError, TypeError):
        return []


def calculate_remind_offsets(start_dt: datetime, remind_times: list[str]) -> list[timedelta]:
    offsets = []
    for remind_time in remind_times:
        try:
            remind_dt = datetime.fromisoformat(remind_time).replace(tzinfo=None)
        except (ValueError, TypeError):
            continue
        offsets.append(start_dt - remind_dt)
    return offsets


def apply_offsets(start_dt: datetime, offsets: list[timedelta]) -> list[str]:
    return [(start_dt - offset).isoformat() for offset in offsets]


def ensure_event_reminders(
    parsed_data: dict[str, Any],
    *,
    build_from_offsets: Callable[[str, list[Any]], list[str]] | None = None,
) -> list[str]:
    """Guarantee a start-time reminder exists for the event."""
    if parsed_data.get("remind_times"):
        remind_times = list(parsed_data["remind_times"])
    elif parsed_data.get("remind_offsets") and parsed_data.get("start_time") and build_from_offsets:
        remind_times = build_from_offsets(parsed_data["start_time"], parsed_data["remind_offsets"])
    else:
        remind_times = default_reminders(parsed_data.get("start_time"))

    return ensure_start_time_reminder(remind_times, parsed_data.get("start_time"))


def ensure_event_reminder_rules(
    parsed_data: dict[str, Any],
    remind_times: list[str] | None = None,
) -> list[dict[str, int]]:
    """Resolve semantic reminder rules for a parsed event payload."""
    if "reminder_rules" in parsed_data:
        return normalize_reminder_rules(parsed_data.get("reminder_rules"))

    start_time = parsed_data.get("start_time")
    reminders = remind_times if remind_times is not None else parsed_data.get("remind_times")
    if start_time and reminders:
        return with_start_time_reminder_rule(derive_reminder_rules(start_time, reminders))

    return [{"offset_seconds": 0}] if start_time else []


def ensure_start_time_reminder(remind_times: list[str], start_time: str | None) -> list[str]:
    """Ensure the current event start_time exists exactly once in remind_times."""
    normalized = list(remind_times)
    if start_time and not has_time(normalized, start_time):
        normalized.append(normalize_iso(start_time))
    normalized.sort()
    return normalized


def recalculate_milestone_reminders(
    old_milestones: list[dict[str, Any]],
    new_milestones: list[dict[str, Any]],
    existing_reminders: list[str],
) -> list[str]:
    """Shift reminder groups milestone-by-milestone, preserving unaffected nodes."""
    if not old_milestones or not new_milestones:
        return sorted(existing_reminders)

    grouped = group_reminders_by_milestone_index(existing_reminders, old_milestones)
    old_index_by_name = {
        str(milestone.get("name", "")).strip(): idx
        for idx, milestone in enumerate(old_milestones)
        if str(milestone.get("name", "")).strip()
    }

    recalculated: list[str] = []
    for new_idx, new_milestone in enumerate(new_milestones):
        old_idx = old_index_by_name.get(str(new_milestone.get("name", "")).strip(), new_idx)
        if old_idx >= len(old_milestones):
            continue

        old_milestone = old_milestones[old_idx]
        cluster = grouped.get(old_idx, [])
        if not cluster:
            continue

        old_time = str(old_milestone.get("time", "")).strip()
        new_time = str(new_milestone.get("time", "")).strip()
        if not old_time or not new_time:
            recalculated.extend(cluster)
            continue

        if old_time == new_time:
            recalculated.extend(cluster)
            continue

        try:
            offsets = calculate_remind_offsets(datetime.fromisoformat(old_time), cluster)
            recalculated.extend(apply_offsets(datetime.fromisoformat(new_time), offsets))
        except (ValueError, TypeError):
            recalculated.extend(cluster)

    return sorted(set(recalculated))


def recalculate_event_reminders(event: Any, updates: dict[str, Any]) -> list[str]:
    """Shift reminder schedule when event start_time changes."""
    reminder_rules = normalize_reminder_rules(getattr(event, "reminder_rules", None) or [])
    target_start = updates.get("start_time") or getattr(event, "start_time", None)
    if reminder_rules and target_start and not (getattr(event, "milestones", None) or []):
        return build_remind_times_from_rules(target_start, reminder_rules)

    existing = parse_remind_times(event.remind_times)
    old_milestones = getattr(event, "milestones", None) or []
    new_milestones = updates.get("milestones") or []
    if old_milestones and new_milestones:
        remind_times = recalculate_milestone_reminders(old_milestones, new_milestones, existing)
        start = updates.get("start_time") or event.start_time
        return ensure_start_time_reminder(remind_times, start)

    new_start = updates.get("start_time")
    if not new_start:
        remind_times = existing
        start = event.start_time
    else:
        if existing and event.start_time:
            old_start = datetime.fromisoformat(event.start_time)
            new_start_dt = datetime.fromisoformat(new_start)
            offsets = calculate_remind_offsets(old_start, existing)
            remind_times = apply_offsets(new_start_dt, offsets)
        else:
            remind_times = default_reminders(new_start)
        start = new_start

    return ensure_start_time_reminder(remind_times, start)


def format_recurring_event_created(
    title: str, instance_count: int, remind_count: int, parent_id: str
) -> str:
    lines = [
        "✅ 已创建日程",
        "",
        f"🗓️ {title}",
        f"🔄 共 {instance_count} 个实例",
    ]
    if remind_count:
        lines.append(f"⏰ 每项已设置 {remind_count} 个提醒")
    lines.append(f"\n`{parent_id}`")
    lines.append(f"\n💡 用 /pendo event reminders {parent_id} 查看所有实例提醒")
    return "\n".join(lines)


def format_milestone_event_created(event: dict[str, Any]) -> str:
    milestones = event.get("milestones", [])
    remind_count = len(event.get("remind_times", []))

    lines = [
        "✅ 已创建日程",
        "",
        f"🗓️ {event.get('title', '无标题')}",
        f"🗺️ 多时间节点事件 ({len(milestones)}个节点)",
    ]
    for milestone in milestones:
        milestone_time = ItemFormatter.format_datetime(milestone.get("time", ""), "%m-%d %H:%M")
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
    start_time = ItemFormatter.format_datetime(event["start_time"])
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
        start_str = ItemFormatter.format_datetime(conflict.get("start_time", ""), "%m-%d %H:%M")
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


def format_event_reminders(event: Any, log_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
    remind_times = parse_remind_times(event.remind_times)
    title = event.title or "无标题"
    start_time = event.start_time or ""
    milestones = getattr(event, "milestones", None) or []
    notes = getattr(event, "notes", None) or ""

    if not remind_times:
        return {"status": "info", "message": f"🔔 日程: {title}\n\n未设置提醒"}

    builder = MessageBuilder()
    builder.add_line(f"🔔 **{title}** 的提醒列表")
    event_time_str = ItemFormatter.format_datetime(start_time, "%m月%d日 %H:%M")
    builder.add_line(f"🗓️ 日程时间: {event_time_str}")
    if milestones:
        builder.add_line(f"🗺️ 多时间节点 ({len(milestones)}个)")
        for milestone in milestones:
            milestone_time = ItemFormatter.format_datetime(
                milestone.get("time", ""), "%m月%d日 %H:%M"
            )
            builder.add_line(f"  📌 {milestone.get('name', '')}  {milestone_time}")
            milestone_notes = str(milestone.get("notes", "") or "").strip()
            if milestone_notes:
                builder.add_line(f"     📝 {milestone_notes}")
    if notes:
        builder.add_line(f"📝 全局备注: {notes}" if milestones else f"📝 {notes}")
    builder.add_line("─" * 30)
    builder.add_blank()

    status_labels = {"✅": "✅ 已确认", "📩": "📩 已发送未确认", "⏳": "⏳ 待发送"}
    for index, remind_time in enumerate(remind_times, 1):
        time_str = ItemFormatter.format_datetime(remind_time, "%m月%d日 %H:%M")
        status = status_labels[get_remind_status(log_map.get(remind_time))]
        builder.add_line(f"⏰ **提醒 {index}**: {time_str}  {status}")

    return {"status": "success", "message": builder.build()}
