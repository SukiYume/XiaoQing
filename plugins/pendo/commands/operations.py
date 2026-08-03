"""处理提醒确认、延后和最近一次写操作撤销。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, tzinfo
from typing import Any, Final, cast

from core.args import parse, parse_int
from core.plugin_base import run_sync

from ..config import PendoConfig
from ..models.item import EventItem, Item, TaskItem, get_item_type_value
from ..services.db import Database
from ..services.reminder import ReminderService
from ..utils.error_handlers import error_result, success_result
from ..utils.time_utils import TimezoneHelper, now_in_timezone, parse_delay_time

JsonObject = dict[str, Any]
CommandResult = JsonObject
ResultBuilder = Callable[[str], CommandResult]

# 依赖模块在局部严格 Mypy 中按边界处理；一次标注后避免每个返回点重复 cast。
_error_result: Final[ResultBuilder] = cast(ResultBuilder, error_result)
_success_result: Final[ResultBuilder] = cast(ResultBuilder, success_result)

_CONFIRM_ICONS: Final = {
    "event": "🗓️",
    "task": "📝",
    "note": "📒",
    "diary": "📔",
}
_ITEM_TYPE_NAMES: Final = {
    "event": "日程",
    "task": "待办",
    "note": "笔记",
    "diary": "日记",
    "ledger": "账目",
}
_EDIT_ACTION_NAMES: Final = {
    "edit_event": "日程",
    "edit_task": "待办",
    "edit_note": "笔记",
    "edit_diary": "日记",
    "edit_ledger": "账目",
}


def _parse_remind_time_for_compare(
    remind_time: str,
    timezone: tzinfo,
) -> datetime | None:
    """按用户时区解析提醒；损坏的旧值由调用方忽略。"""

    try:
        return cast(datetime, TimezoneHelper.parse(remind_time, timezone))
    except (TypeError, ValueError):
        return None


def _confirmed_item_message(item: Item, item_id: str, now: datetime) -> str:
    """构造确认成功消息，并统计同一条目后续仍有效的提醒。"""

    item_type = get_item_type_value(item.type)
    icon = _CONFIRM_ICONS.get(item_type, "📌")
    remind_times = item.remind_times if isinstance(item, (EventItem, TaskItem)) else []
    timezone = now.tzinfo or TimezoneHelper.DEFAULT_TZ
    future_count = 0
    for remind_time in remind_times:
        remind_at = _parse_remind_time_for_compare(remind_time, timezone)
        if remind_at is not None and remind_at > now:
            future_count += 1
    reminder_note = f"后续还有 {future_count} 个提醒" if future_count else "没有更多提醒了"
    return (
        f"✅ 已确认{icon} {item.title or '无标题'}\n\n"
        f"`{item_id}`\n\n💡 此次提醒已确认，{reminder_note}"
    )


async def handle_confirm(
    user_id: str,
    args: str,
    reminder_service: ReminderService,
    db: Database | None = None,
) -> CommandResult:
    """确认当前用户指定条目的最近一条已发送提醒。"""

    parsed = parse(args)
    if len(parsed) != 1 or parsed.options:
        return _error_result("❌ 请指定一个要确认的条目ID\n\n例如: /pendo confirm 3F2A")
    item_id = parsed.first
    database = db or reminder_service.db
    item = cast(Item | None, await run_sync(database.get_item, item_id, user_id))
    if item is None:
        return _error_result(f"❌ 未找到条目: {item_id}")

    target_remind_time = cast(
        str | None,
        await run_sync(
            database.get_last_unconfirmed_remind_time,
            item_id,
        ),
    )
    if target_remind_time is None:
        return _error_result("未找到待确认的已发送提醒")
    result = cast(
        JsonObject,
        await run_sync(
            reminder_service.confirm_reminder,
            item_id,
            "confirmed",
            user_id,
            target_remind_time,
        ),
    )
    if result.get("status") != "success":
        return _error_result(str(result.get("message") or "确认失败"))

    return _success_result(
        _confirmed_item_message(
            item,
            item_id,
            cast(datetime, now_in_timezone(user_id, database)),
        )
    )


async def _apply_snooze(
    reminder_service: ReminderService,
    user_id: str,
    item: EventItem | TaskItem,
    new_remind_time: str,
    user_now: datetime,
) -> CommandResult:
    """写入新的提醒集合，并只确认被延后的具体提醒点。"""

    database = reminder_service.db
    snoozed_remind_time = cast(
        str | None,
        await run_sync(
            database.get_last_unconfirmed_remind_time,
            item.id,
        ),
    )
    timezone = user_now.tzinfo or TimezoneHelper.DEFAULT_TZ
    if snoozed_remind_time is not None and snoozed_remind_time in item.remind_times:
        other_times = [value for value in item.remind_times if value != snoozed_remind_time]
    else:
        other_times = []
        for remind_time in item.remind_times:
            remind_at = _parse_remind_time_for_compare(remind_time, timezone)
            if remind_at is not None and remind_at > user_now:
                other_times.append(remind_time)
    new_remind_times = list(dict.fromkeys([*other_times, new_remind_time]))
    latest = datetime.max.replace(tzinfo=timezone)
    new_remind_times.sort(
        key=lambda value: _parse_remind_time_for_compare(value, timezone) or latest
    )

    updated = cast(
        bool,
        await run_sync(
            database.update_item,
            item.id,
            {"remind_times": new_remind_times},
            user_id,
            expected_version=item.version,
        ),
    )
    if not updated:
        return _error_result("条目已变化或不存在，请刷新后重试")

    # 没有已发送日志时不能传 None，否则数据库会确认该条目的全部未确认提醒。
    if snoozed_remind_time:
        confirm_result = cast(
            JsonObject,
            await run_sync(
                reminder_service.confirm_reminder,
                item.id,
                "delayed",
                user_id,
                snoozed_remind_time,
            ),
        )
        if confirm_result.get("status") != "success":
            return _error_result("提醒时间已更新，但原提醒状态记录失败")
    return _success_result(f"已将提醒延后到: {new_remind_time}")


async def handle_snooze(
    user_id: str,
    args: str,
    reminder_service: ReminderService,
) -> CommandResult:
    """把当前提醒延后，同时保留同一条目的其他有效提醒。"""

    parsed = parse(args)
    if not parsed:
        return _error_result(
            "请指定要延后的条目ID和时间，例如: /pendo snooze 3F2A 10m 或 /pendo snooze 3F2A 19:00"
        )
    if len(parsed) != 2 or parsed.options:
        return _error_result("请指定一个条目ID和延后时间，例如: 3F2A 10m")
    item_id = parsed.first
    time_arg = parsed.second

    database = reminder_service.db
    item = cast(Item | None, await run_sync(database.get_item, item_id, user_id))
    if item is None:
        return _error_result(f"未找到条目: {item_id}")
    if not isinstance(item, (EventItem, TaskItem)) or not item.remind_times:
        return _error_result("该条目没有可延后的提醒")

    user_now = cast(datetime, now_in_timezone(user_id, database))
    try:
        new_remind_time = _parse_snooze_time(time_arg, now=user_now)
    except ValueError:
        return _error_result("无法解析延后时间，请使用 10m、1h、1d 或 19:00 等格式")
    return await _apply_snooze(
        reminder_service,
        user_id,
        item,
        new_remind_time,
        user_now,
    )


async def _undo_delete(user_id: str, minutes: int, db: Database) -> CommandResult:
    """撤销最近删除，并把恢复条目格式化为用户消息。"""

    result = cast(JsonObject, await run_sync(db.undo_delete, user_id, minutes))
    if result.get("status") != "success":
        return _error_result(str(result.get("message") or "撤销失败"))
    item = result.get("item")
    if item is None:
        return _success_result(str(result.get("message") or "✅ 已恢复条目"))
    if not isinstance(item, Item):
        return _error_result("撤销结果缺少有效条目")
    type_name = _ITEM_TYPE_NAMES.get(
        get_item_type_value(item.type, default=""),
        "条目",
    )
    return _success_result(f"✅ 已恢复{type_name}: {item.title or '无标题'} ({item.id})")


async def _undo_edit(user_id: str, minutes: int, db: Database) -> CommandResult:
    """撤销最近编辑，并报告单条或批量恢复结果。"""

    result = cast(JsonObject, await run_sync(db.undo_edit, user_id, minutes))
    if result.get("status") != "success":
        return _error_result(str(result.get("message") or "撤销编辑失败"))
    item_id = str(result.get("item_id") or "")
    if not item_id:
        return _error_result("撤销结果缺少条目ID")
    item = cast(Item | None, await run_sync(db.get_item, item_id, user_id))
    message = (
        f"✅ 已撤销{_EDIT_ACTION_NAMES.get(str(result.get('action') or ''), '条目')}编辑: "
        f"{item.title if item else '未知'}"
    )
    instance_count = result.get("instance_count", 1)
    affected = result.get("affected", 0)
    if isinstance(instance_count, int) and instance_count > 1:
        message += f"\n📊 共恢复 {affected if isinstance(affected, int) else 0} 个实例"
    return _success_result(message)


async def handle_undo(user_id: str, args: str, db: Database) -> CommandResult:
    """撤销指定分钟范围内最近一次删除或编辑。"""

    parsed = parse(args)
    if parsed.options or len(parsed) > 1:
        return _error_result("正确用法: /pendo undo [分钟]")
    minutes = PendoConfig.UNDO_WINDOW_MINUTES
    if parsed:
        parsed_minutes = parse_int(
            parsed.first,
            minimum=1,
            maximum=PendoConfig.UNDO_WINDOW_MINUTES,
        )
        if parsed_minutes is None:
            return _error_result(f"分钟数必须是 1～{PendoConfig.UNDO_WINDOW_MINUTES} 的整数")
        minutes = parsed_minutes

    latest = cast(
        JsonObject,
        await run_sync(db.get_latest_undoable_operation, user_id, minutes),
    )
    operation_type = latest.get("type")
    if operation_type == "delete":
        return await _undo_delete(user_id, minutes, db)
    if operation_type == "edit":
        return await _undo_edit(user_id, minutes, db)
    return _error_result(f"未找到{minutes}分钟内可撤销的操作（删除或编辑）")


def _parse_snooze_time(
    time_arg: str,
    *,
    now: datetime | None = None,
) -> str:
    """把相对时长或当天时刻解析成 ISO 时间，非法值明确失败。"""

    new_time = cast(str | None, parse_delay_time(time_arg, now=now))
    if new_time is None:
        raise ValueError(f"无法解析时间参数: {time_arg}")
    return new_time
