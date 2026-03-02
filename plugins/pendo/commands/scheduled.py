"""
定时任务模块
处理所有定时检查和推送任务
"""
import logging
from datetime import datetime, timedelta
from typing import Any
from core.plugin_base import run_sync, segments
from ..services.db import Database
from ..services.reminder import ReminderService
from ..services.ai_parser import AIParser
from ..utils.time_utils import parse_custom_settings, save_user_setting
from ..utils.db_ops import get_database
from ..models.item import ItemType

logger = logging.getLogger(__name__)

# 缓存 reminder_service 单例
_reminder_service_singleton: ReminderService = None

async def check_reminders(context) -> list[dict[str, Any]]:
    """检查并发送提醒
    
    Args:
        context: 上下文对象
        
    Returns:
        消息列表
    """
    global _reminder_service_singleton
    db = get_database(context)
    
    if _reminder_service_singleton is None:
        _reminder_service_singleton = ReminderService(db)
    reminder_service = _reminder_service_singleton
    
    # 检查并发送提醒
    result = await run_sync(reminder_service.check_and_send_reminders, context)
    
    # 直接通过 WS 私聊发送提醒（不走 HTTP，不发群）
    for msg in result.get("messages", []):
        user_id = msg.get("user_id")
        message = msg.get("message")
        if not user_id:
            continue
        action = {
            "action": "send_private_msg",
            "params": {"user_id": int(user_id), "message": segments(message)}
        }
        if hasattr(context, "send_action"):
            await context.send_action(action)
    
    return []

async def send_daily_briefings(context, db: Database) -> list[dict[str, Any]]:
    """发送每日简报
    
    Args:
        context: 上下文对象
        db: 数据库实例
        
    Returns:
        消息列表
    """
    messages = []
    current_time = datetime.now()
    current_date = current_time.date().isoformat()
    
    try:
        user_ids = await _get_active_user_ids(db)
        ai_parser = AIParser(context)
        
        for user_id in user_ids:
            try:
                custom_settings = await _get_user_custom_settings(user_id, db)
                
                # 检查是否今天已发送
                if custom_settings.get('last_daily_briefing_date') == current_date:
                    continue
                
                # 检查是否启用每日简报
                if not custom_settings.get('daily_briefing_enabled', True):
                    continue

                # M-2修复：移除 _is_time_reached 检查。
                # cron 已在 plugin.json 中固定为 08:00 触发，对所有用户使用同一时间点，
                # 该检查导致 daily_report_time != "08:00" 的用户永远收不到简报。

                # 生成并发送简报
                briefing_msg = await _generate_briefing_content(user_id, db, ai_parser)
                
                action = {
                    "action": "send_private_msg",
                    "params": {"user_id": int(user_id), "message": segments(briefing_msg)}
                }
                if hasattr(context, "send_action"):
                    await context.send_action(action)
                else:
                    messages.append(action)
                
                # 更新最后发送日期
                await run_sync(save_user_setting, user_id, 'last_daily_briefing_date', current_date, db)
                logger.info("Sent daily briefing to user %s", user_id)

            except Exception as e:
                logger.exception("为用户 %s 发送每日简报失败: %s", user_id, e)

    except Exception as e:
        logger.exception("发送每日简报时出错: %s", e)
    
    return []

async def send_evening_briefings(context, db: Database) -> list[dict[str, Any]]:
    """发送晚间推送 (暂未实现)"""
    return []

async def check_diary_reminders(context, db: Database) -> list[dict[str, Any]]:
    """检查日记提醒
    
    Args:
        context: 上下文对象
        db: 数据库实例
        
    Returns:
        消息列表
    """
    messages = []
    current_time = datetime.now()
    current_date = current_time.date().isoformat()
    
    try:
        user_ids = await _get_active_user_ids(db)
        
        for user_id in user_ids:
            try:
                custom_settings = await _get_user_custom_settings(user_id, db)
                
                # 检查是否今天已提醒
                if custom_settings.get('last_diary_remind_date') == current_date:
                    continue
                
                # M-2修复：移除 _is_time_reached 检查（原因同 send_daily_briefings）

                # 检查今天是否已写日记
                if await _has_diary_for_date(db, user_id, current_date):
                    # 已写，更新状态不提醒
                    await run_sync(save_user_setting, user_id, 'last_diary_remind_date', current_date, db)
                    continue

                # 发送提醒
                action = {
                    "action": "send_private_msg",
                    "params": {"user_id": int(user_id), "message": segments("📔 今天还没有写日记哦，记录一下美好的今天吧？\n发送 /pendo diary 开始")}
                }
                if hasattr(context, "send_action"):
                    await context.send_action(action)
                else:
                    messages.append(action)
                
                await run_sync(save_user_setting, user_id, 'last_diary_remind_date', current_date, db)
                
            except Exception as e:
                # L-1修复：记录日志，避免静默吞掉异常
                logger.warning("为用户 %s 处理日记提醒失败: %s", user_id, e)
    except Exception as e:
        logger.exception("检查日记提醒时出错: %s", e)

    return []

# ============================================================
# 辅助函数
# ============================================================

async def _get_active_user_ids(db: Database) -> list[str]:
    """获取活跃用户ID列表"""
    def _fetch():
        conn = db.conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT owner_id FROM items WHERE deleted = 0")
        return [row[0] for row in cursor.fetchall()]

    return await run_sync(_fetch)

async def _get_user_custom_settings(user_id: str, db: Database) -> dict[str, Any]:
    """获取用户自定义设置"""
    settings = await run_sync(db.settings.get_user_settings, user_id)
    return parse_custom_settings(settings)

def _is_time_reached(current_time: datetime, target_time_str: str) -> bool:
    """检查是否到达目标时间
    
    Args:
        current_time: 当前时间
        target_time_str: 目标时间字符串（HH:MM格式）
        
    Returns:
        是否已到达或超过目标时间
    """
    try:
        target_hour, target_minute = map(int, target_time_str.split(':'))
        return current_time.hour > target_hour or (
            current_time.hour == target_hour and current_time.minute >= target_minute
        )
    except ValueError:
        return False

async def _generate_briefing_content(user_id: str, db: Database, ai_parser: AIParser) -> str:
    """生成每日简报内容"""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    
    events, tasks, overdue_tasks = await _fetch_briefing_items(db, user_id, today.isoformat(), tomorrow.isoformat())
    
    # 生成简报
    items = events + tasks
    briefing = await ai_parser.generate_daily_briefing(user_id, items)
    
    # 添加逾期提醒
    if overdue_tasks:
        briefing += f"\n\n⚠️ 逾期待办 ({len(overdue_tasks)}项):"
        for task in overdue_tasks[:3]:
            due_time = task.due_time or ''
            if due_time:
                dt = datetime.fromisoformat(due_time)
                time_str = dt.strftime('%m-%d')
                briefing += f"\n  - {task.title or '无标题'} (截止: {time_str})"
    
    return briefing

async def _fetch_briefing_items(db: Database, user_id: str, today_iso: str, tomorrow_iso: str):
    """获取简报相关的条目"""
    return await run_sync(db.items.get_briefing_items, user_id, today_iso, tomorrow_iso)

async def _has_diary_for_date(db: Database, user_id: str, diary_date: str) -> bool:
    """检查指定日期是否已有日记"""
    return await run_sync(db.items.has_diary_for_date, user_id, diary_date)

async def migrate_undone_todos(context, db: Database) -> list[dict[str, Any]]:
    """迁移前一天未完成的待办到今天
    
    每晚12:05执行，检查前一天所有分类下未完成的待办，将它们迁移到当天的分类
    
    Args:
        context: 上下文对象
        db: 数据库实例
        
    Returns:
        消息列表
    """
    messages = []
    current_time = datetime.now()
    yesterday = (current_time - timedelta(days=1)).strftime('%Y-%m-%d')
    today = current_time.strftime('%Y-%m-%d')
    
    try:
        user_ids = await _get_active_user_ids(db)
        
        for user_id in user_ids:
            try:
                # 检查是否今天已执行过迁移
                custom_settings = await _get_user_custom_settings(user_id, db)
                if custom_settings.get('last_todo_migrate_date') == today:
                    continue
                
                # 查询前一天所有未完成的待办
                undone_tasks = await _get_undone_tasks_for_date(db, user_id, yesterday)
                
                if not undone_tasks:
                    # 没有未完成的待办，更新标记
                    await run_sync(save_user_setting, user_id, 'last_todo_migrate_date', today, db)
                    continue
                
                # M-5修复：单事务批量迁移，替代逐条提交
                migrated_count = await _batch_migrate_tasks_to_date(db, undone_tasks, today, user_id)
                
                # 如果有迁移，发送通知
                if migrated_count > 0:
                    action = {
                        "action": "send_private_msg",
                        "params": {"user_id": int(user_id), "message": segments(f"📋 已将昨天的 {migrated_count} 个未完成待办迁移到今天\n\n💡 使用 /pendo todo list today 查看")}
                    }
                    if hasattr(context, "send_action"):
                        await context.send_action(action)
                    else:
                        messages.append(action)
                
                # 更新最后迁移日期
                await run_sync(save_user_setting, user_id, 'last_todo_migrate_date', today, db)
                logger.info("Migrated %s undone todos for user %s from %s to %s", migrated_count, user_id, yesterday, today)

            except Exception as e:
                logger.exception("为用户 %s 迁移待办失败: %s", user_id, e)

    except Exception as e:
        logger.exception("迁移待办时出错: %s", e)
    
    return []

async def _get_undone_tasks_for_date(db: Database, user_id: str, date_str: str) -> list:
    """获取指定日期未完成的待办"""
    def _fetch():
        conn = db.conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT * FROM items WHERE owner_id = ? AND type = '{ItemType.TASK.value}' AND category = ? AND status = 'todo' AND deleted = 0",
            (user_id, date_str)
        )
        return [item for row in cursor.fetchall() if (item := db.items._row_to_item(row)) is not None]
    
    return await run_sync(_fetch)

async def _batch_migrate_tasks_to_date(db: Database, tasks: list, target_date: str, user_id: str) -> int:
    """M-5修复：单事务批量迁移待办到指定日期，替代逐条提交

    Returns:
        成功迁移的数量
    """
    if not tasks:
        return 0

    def _update():
        conn = db.conn_manager.get_connection()
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        task_ids = [task.id for task in tasks]
        placeholders = ','.join(['?' for _ in task_ids])
        with conn:
            cursor.execute(
                f"UPDATE items SET category = ?, updated_at = ? WHERE id IN ({placeholders}) AND owner_id = ?",
                [target_date, now] + task_ids + [user_id]
            )
            return cursor.rowcount

    return await run_sync(_update)


def cleanup_reminder_singleton() -> None:
    """L-5修复：清除 _reminder_service_singleton，在插件 cleanup 时调用"""
    global _reminder_service_singleton
    _reminder_service_singleton = None
