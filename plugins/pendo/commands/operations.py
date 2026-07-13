"""
通用操作命令模块
处理确认、延后、撤销等操作
"""

from core.args import parse
from core.plugin_base import run_sync

from ..models.item import get_item_type_value
from ..services.db import Database
from ..services.reminder import ReminderService
from ..utils.error_handlers import error_result, success_result
from ..utils.time_utils import TimezoneHelper, now_in_timezone, parse_delay_time


def _parse_remind_time_for_compare(remind_time: str):
    """将提醒时间统一到带时区的 datetime，避免 aware/naive 混用。"""
    try:
        return TimezoneHelper.parse(remind_time, TimezoneHelper.DEFAULT_TZ)
    except (ValueError, TypeError):
        return None


async def handle_confirm(
    user_id: str, args: str, reminder_service: ReminderService, db=None
) -> dict[str, str]:
    """处理确认提醒命令

    Args:
        user_id: 用户ID
        args: 命令参数（条目ID）
        reminder_service: 提醒服务实例
        db: 数据库实例（可选）

    Returns:
        M-7修复：返回 {'status': 'success'|'error', 'message': ...} 字典，
        而非裸字符串，避免调用方用 startswith('❌') 推断状态。
    """
    if not args:
        return error_result("❌ 请指定要确认的条目ID\n\n例如: /pendo confirm 3F2A")

    item_id = parse(args).first

    try:
        db = db or getattr(reminder_service, "db", None)

        # 获取条目信息并校验所有权
        item = None
        if db:
            item = await run_sync(db.items.get_item, item_id, user_id)
            if item is None:
                return error_result(f"❌ 未找到条目: {item_id}")

        target_remind_time = None
        if db:
            last_sent_remind_time = await _get_last_sent_remind_time(db, item_id)
            if isinstance(last_sent_remind_time, str) and last_sent_remind_time.strip():
                target_remind_time = last_sent_remind_time

        confirm_args = [item_id, "confirmed", user_id]
        if target_remind_time:
            confirm_args.append(target_remind_time)

        result = await run_sync(reminder_service.confirm_reminder, *confirm_args)
        if result.get("status") == "success":
            # 构建友好的确认消息
            if item:
                title = item.title or "无标题"
                item_type = get_item_type_value(item.type)

                type_icons = {
                    "event": "🗓️",
                    "task": "📝",
                    "note": "📒",
                    "diary": "📔",
                }
                icon = type_icons.get(item_type, "📌")

                message = f"✅ 已确认{icon} {title}\n\n"
                message += f"`{item_id}`\n\n"
                # 检查是否还有后续提醒
                remind_times = item.remind_times or []
                now = TimezoneHelper.now()
                future_reminders = []
                for rt in remind_times:
                    remind_dt = _parse_remind_time_for_compare(rt)
                    if remind_dt and remind_dt > now:
                        future_reminders.append(rt)
                if future_reminders:
                    message += f"💡 此次提醒已确认，后续还有 {len(future_reminders)} 个提醒"
                else:
                    message += "💡 此次提醒已确认，没有更多提醒了"
                return success_result(message)
            else:
                return success_result(f"✅ 已确认提醒 `{item_id}`")
        return {
            "status": result.get("status", "error"),
            "message": result.get("message", "确认成功"),
        }
    except Exception:
        raise


async def handle_snooze(
    user_id: str, args: str, reminder_service: ReminderService
) -> dict[str, str]:
    """处理延后提醒命令

    Args:
        user_id: 用户ID
        args: 命令参数（条目ID和时间）
        reminder_service: 提醒服务实例

    Returns:
        延后结果消息
    """
    if not args:
        return error_result(
            "请指定要延后的条目ID和时间，例如: /pendo snooze 3F2A 10min 或 /pendo snooze 3F2A 19:00"
        )

    parsed = parse(args)
    item_id = parsed.first
    time_arg = parsed.rest(1)

    if not time_arg:
        return error_result("请指定延后时间，例如: 10min, 1h, 19:00")

    try:
        # 获取条目信息（添加owner_id检查）
        db = reminder_service.db
        item = await run_sync(db.items.get_item, item_id, user_id)

        if not item:
            return error_result(f"未找到条目: {item_id}")

        remind_times = item.remind_times or []
        if not remind_times:
            return error_result("该条目没有设置提醒")

        # S-2修复：以当前时间为基准，避免用户延迟 snooze 导致新提醒时间落在过去
        user_now = now_in_timezone(user_id, db)
        try:
            new_remind_time = _parse_snooze_time(time_arg, now=user_now)
        except ValueError:
            return error_result("无法解析延后时间，请使用 10m、1h、1d 或 19:00 等格式")

        # S-3修复：只移除刚触发的那个 remind_time，保留其他所有提醒时间（包括未来的）
        # 旧逻辑仅保留过去时间，会丢失事件的后续提醒点（如 T-1h snooze 后丢失 T、T+1h）
        now = user_now
        snoozed_remind_time = await _get_last_sent_remind_time(db, item_id)
        if snoozed_remind_time and snoozed_remind_time in remind_times:
            other_times = [rt for rt in remind_times if rt != snoozed_remind_time]
        else:
            # 无法定位触发的提醒时间时，保留所有未来提醒时间
            other_times = [
                rt
                for rt in remind_times
                if (remind_dt := _parse_remind_time_for_compare(rt)) and remind_dt > now
            ]
        new_remind_times = other_times + [new_remind_time]

        await run_sync(db.items.update_item, item_id, {"remind_times": new_remind_times}, user_id)

        # 记录用户操作
        await run_sync(
            reminder_service.confirm_reminder, item_id, "delayed", user_id, snoozed_remind_time
        )

        return success_result(f"已将提醒延后到: {new_remind_time}")

    except Exception:
        raise


async def handle_undo(user_id: str, args: str, db: Database) -> dict[str, str]:
    """处理撤销命令（支持撤销删除和编辑）

    Args:
        user_id: 用户ID
        args: 命令参数（可选的时间范围，单位分钟）
        db: 数据库实例

    Returns:
        撤销结果消息
    """
    try:
        # 解析时间参数
        minutes = 5  # 默认5分钟
        if args:
            parsed = parse(args)
            time_arg = parsed.first
            if time_arg and time_arg.isdigit():
                minutes = int(time_arg)

        # 查找最近可撤销的操作（删除 或 编辑）
        latest = await run_sync(db.items.get_latest_undoable_operation, user_id, minutes)
        op_type = latest.get("type")

        if not op_type:
            return error_result(f"未找到{minutes}分钟内可撤销的操作（删除或编辑）")

        if op_type == "delete":
            result = await run_sync(db.items.undo_delete, user_id, minutes)
            if result.get("status") == "success":
                item = result.get("item")
                if item is None:
                    return success_result(result.get("message", "✅ 已恢复条目"))
                item_type = get_item_type_value(item.type, default="task")
                type_name = {
                    "event": "日程",
                    "task": "待办",
                    "note": "笔记",
                    "idea": "想法",
                    "diary": "日记",
                }.get(item_type, "条目")
                return success_result(f"✅ 已恢复{type_name}: {item.title or '无标题'} ({item.id})")
            return error_result(result.get("message", "撤销失败"))

        if op_type == "edit":
            result = await run_sync(db.items.undo_edit, user_id, minutes)
            if result.get("status") == "success":
                type_name = {
                    "edit_event": "日程", "edit_task": "待办",
                    "edit_note": "笔记", "edit_diary": "日记",
                }.get(result.get("action", ""), "条目")
                item = await run_sync(db.items.get_item, result["item_id"], user_id)
                title = item.title if item else "未知"
                msg = f"✅ 已撤销{type_name}编辑: {title}"
                if result.get("instance_count", 1) > 1:
                    msg += f"\n📊 共恢复 {result['affected']} 个实例"
                return success_result(msg)
            return error_result(result.get("message", "撤销编辑失败"))

        return error_result("未找到可撤销的操作")

    except Exception:
        raise


def _parse_snooze_time(
    time_arg: str,
    base_time: str | None = None,
    *,
    now=None,
) -> str:
    """解析延后时间参数

    Args:
        time_arg: 时间参数（如 "10m", "1h", "1d", "19:00"）
        base_time: 基准时间（ISO格式），相对延迟以此为起点而非当前时间

    Returns:
        解析后的时间字符串（ISO格式）

    Raises:
        ValueError: 如果无法解析时间参数
    """
    new_time = parse_delay_time(time_arg, current_due=base_time, now=now)
    if not new_time:
        raise ValueError(f"无法解析时间参数: {time_arg}")
    return new_time


async def _get_last_sent_remind_time(db: Database, item_id: str) -> str | None:
    """获取最近已发送的提醒时间

    Args:
        db: 数据库实例
        item_id: 条目ID

    Returns:
        最近已发送的提醒时间字符串，如果没有则返回None
    """

    return await run_sync(db.items.get_last_unconfirmed_remind_time, item_id)
