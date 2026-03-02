"""
通用操作命令模块
处理确认、延后、撤销等操作
"""
import logging
from typing import Optional
from core.plugin_base import run_sync
from core.args import parse
from ..services.db import Database
from ..services.reminder import ReminderService
from ..utils.time_utils import parse_delay_time

logger = logging.getLogger(__name__)

async def handle_confirm(user_id: str, args: str, reminder_service: ReminderService, db=None) -> dict[str, str]:
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
        return {'status': 'error', 'message': '❌ 请指定要确认的条目ID\n\n例如: /pendo confirm 3F2A'}

    item_id = parse(args).first

    try:
        # 获取条目信息（如果提供了db）
        item = None
        if db:
            item = await run_sync(db.items.get_item, item_id, user_id)

        result = await run_sync(reminder_service.confirm_reminder, item_id, 'confirmed')
        if result.get('status') == 'success':
            # 构建友好的确认消息
            if item:
                title = item.title or '无标题'
                item_type = item.type.value if hasattr(item.type, 'value') else 'item'

                type_icons = {
                    'event': '🗓️',
                    'task': '📝',
                    'note': '📒',
                    'diary': '📔',
                }
                icon = type_icons.get(item_type, '📌')

                message = f"✅ 已确认{icon} {title}\n\n"
                message += f"`{item_id}`\n\n"
                # 检查是否还有后续提醒
                remind_times = item.remind_times or []
                from datetime import datetime
                now = datetime.now()
                future_reminders = []
                for rt in remind_times:
                    try:
                        if datetime.fromisoformat(rt) > now:
                            future_reminders.append(rt)
                    except (ValueError, TypeError):
                        pass
                if future_reminders:
                    message += f"💡 此次提醒已确认，后续还有 {len(future_reminders)} 个提醒"
                else:
                    message += f"💡 此次提醒已确认，没有更多提醒了"
                return {'status': 'success', 'message': message}
            else:
                return {'status': 'success', 'message': f"✅ 已确认提醒 `{item_id}`"}
        return {'status': result.get('status', 'error'), 'message': result.get('message', '确认成功')}
    except Exception as e:
        logger.exception("确认提醒失败: %s", e)
        return {'status': 'error', 'message': f"❌ 确认失败: {str(e)}"}

async def handle_snooze(user_id: str, args: str, reminder_service: ReminderService) -> str:
    """处理延后提醒命令
    
    Args:
        user_id: 用户ID
        args: 命令参数（条目ID和时间）
        reminder_service: 提醒服务实例
        
    Returns:
        延后结果消息
    """
    if not args:
        return "请指定要延后的条目ID和时间，例如: /pendo snooze 3F2A 10min 或 /pendo snooze 3F2A 19:00"
    
    parsed = parse(args)
    item_id = parsed.first
    time_arg = parsed.rest(1)
    
    if not time_arg:
        return "请指定延后时间，例如: 10min, 1h, 19:00"
    
    try:
        from datetime import datetime
        # 获取条目信息（添加owner_id检查）
        db = reminder_service.db
        item = await run_sync(db.items.get_item, item_id, user_id)

        if not item:
            return f"未找到条目: {item_id}"

        remind_times = item.remind_times or []
        if not remind_times:
            return "该条目没有设置提醒"

        # S-2修复：以当前时间为基准，避免用户延迟 snooze 导致新提醒时间落在过去
        new_remind_time = _parse_snooze_time(time_arg)

        # S-3修复：只移除刚触发的那个 remind_time，保留其他所有提醒时间（包括未来的）
        # 旧逻辑仅保留过去时间，会丢失事件的后续提醒点（如 T-1h snooze 后丢失 T、T+1h）
        now = datetime.now()
        snoozed_remind_time = await _get_last_sent_remind_time(db, item_id)
        if snoozed_remind_time and snoozed_remind_time in remind_times:
            other_times = [rt for rt in remind_times if rt != snoozed_remind_time]
        else:
            # 无法定位触发的提醒时间时，保留所有未来提醒时间
            other_times = [rt for rt in remind_times if datetime.fromisoformat(rt) > now]
        new_remind_times = other_times + [new_remind_time]

        await run_sync(db.items.update_item, item_id, {'remind_times': new_remind_times}, user_id)

        # 记录用户操作
        await run_sync(reminder_service.confirm_reminder, item_id, 'delayed')

        return f"已将提醒延后到: {new_remind_time}"
            
    except Exception as e:
        logger.exception("延后提醒失败: %s", e)
        return f"延后失败: {str(e)}"

async def handle_undo(user_id: str, args: str, db: Database) -> str:
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
        op_type = latest.get('type')

        if not op_type:
            return f"未找到{minutes}分钟内可撤销的操作（删除或编辑）"

        if op_type == 'delete':
            result = await run_sync(db.items.undo_delete, user_id, minutes)
            if result.get('status') == 'success':
                item = result.get('item')
                item_type = item.type.value if hasattr(item.type, 'value') else item.type
                type_name = {
                    'event': '日程',
                    'task': '待办',
                    'note': '笔记',
                    'idea': '想法',
                    'diary': '日记'
                }.get(item_type, '条目')
                return f"✅ 已恢复{type_name}: {item.title or '无标题'} ({item.id})"
            return result.get('message', '撤销失败')

        if op_type == 'edit':
            result = await run_sync(db.items.undo_edit, user_id, minutes)
            if result.get('status') == 'success':
                return result['message']
            return result.get('message', '撤销编辑失败')

        return "未找到可撤销的操作"
            
    except Exception as e:
        logger.exception("撤销失败: %s", e)
        return f"撤销失败: {str(e)}"

def _parse_snooze_time(time_arg: str, base_time: Optional[str] = None) -> str:
    """解析延后时间参数

    Args:
        time_arg: 时间参数（如 "10m", "1h", "1d", "19:00"）
        base_time: 基准时间（ISO格式），相对延迟以此为起点而非当前时间

    Returns:
        解析后的时间字符串（ISO格式）

    Raises:
        ValueError: 如果无法解析时间参数
    """
    new_time = parse_delay_time(time_arg, current_due=base_time)
    if not new_time:
        raise ValueError(f"无法解析时间参数: {time_arg}")
    return new_time

async def _get_last_sent_remind_time(db: Database, item_id: str) -> Optional[str]:
    """获取最近已发送的提醒时间
    
    Args:
        db: 数据库实例
        item_id: 条目ID
        
    Returns:
        最近已发送的提醒时间字符串，如果没有则返回None
    """
    def _fetch():
        conn = db.conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT remind_time FROM reminder_logs
            WHERE item_id = ? AND sent_at IS NOT NULL
            ORDER BY sent_at DESC LIMIT 1
        """, (item_id,))
        row = cursor.fetchone()
        return row[0] if row else None
    
    return await run_sync(_fetch)
