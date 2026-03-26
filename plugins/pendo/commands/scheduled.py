"""
定时任务模块
处理所有定时检查和推送任务
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from core.plugin_base import run_sync, segments
from ..services.db import Database
from ..services.reminder import ReminderService
from ..services.ai_parser import AIParser
from ..utils.time_utils import get_user_now_from_settings
from ..utils.settings_utils import save_user_setting
from ..utils.db_ops import (
    get_database,
    get_user_custom_settings as _get_user_custom_settings,
    get_user_settings_bundle_map,
)
from ..models.item import ItemType

logger = logging.getLogger(__name__)

# 缓存 reminder_service 单例
_reminder_service_singleton: ReminderService | None = None


async def check_reminders(context) -> list[dict[str, Any]]:
    """检查并发送提醒

    Args:
        context: 上下文对象

    Returns:
        消息列表
    """
    global _reminder_service_singleton
    db = get_database(context)
    messages = []

    if _reminder_service_singleton is None:
        _reminder_service_singleton = ReminderService(db)
    reminder_service = _reminder_service_singleton

    # 检查并发送提醒
    result = await run_sync(reminder_service.check_and_send_reminders, context)

    # 直接通过 WS 私聊发送提醒（不走 HTTP，不发群）
    for msg in result.get("messages", []):
        user_id = msg.get("user_id")
        message = msg.get("message")
        item_id = msg.get("item_id")
        remind_time = msg.get("remind_time")
        if not user_id:
            continue
        action = {
            "action": "send_private_msg",
            "params": {"user_id": int(user_id), "message": segments(message)},
        }
        try:
            if hasattr(context, "send_action"):
                await context.send_action(action)
                if item_id and remind_time:
                    db.log_reminder(item_id, remind_time, sent=True)
            else:
                messages.append(action)
        except Exception as e:
            # 发送失败不记录，下个周期会重试
            logger.warning("发送提醒失败 item=%s: %s", item_id, e)

    return messages


async def send_daily_briefings(context, db: Database) -> list[dict[str, Any]]:
    """发送每日简报

    Args:
        context: 上下文对象
        db: 数据库实例

    Returns:
        消息列表
    """
    messages = []
    current_utc = datetime.now(timezone.utc)

    try:
        user_ids = await _get_active_user_ids(db)
        settings_bundle_map = await get_user_settings_bundle_map(user_ids, db)
        ai_parser = AIParser(context)

        for user_id in user_ids:
            try:
                settings_bundle = settings_bundle_map[user_id]
                custom_settings = settings_bundle["custom_settings"]
                user_settings = settings_bundle["settings"]
                user_now = get_user_now_from_settings(user_settings, current_utc)
                current_date = user_now.date().isoformat()
                target_time = user_settings.get("daily_report_time", "08:00")

                # 检查是否今天已发送
                if custom_settings.get("last_daily_briefing_date") == current_date:
                    continue

                # 检查是否启用每日简报
                if not custom_settings.get("daily_briefing_enabled", True):
                    continue

                if not _is_time_reached(user_now, target_time):
                    continue

                # 生成并发送简报
                briefing_msg = await _generate_briefing_content(user_id, db, ai_parser)

                action = {
                    "action": "send_private_msg",
                    "params": {"user_id": int(user_id), "message": segments(briefing_msg)},
                }
                if hasattr(context, "send_action"):
                    await context.send_action(action)
                else:
                    messages.append(action)

                # 更新最后发送日期
                await run_sync(
                    save_user_setting, user_id, "last_daily_briefing_date", current_date, db
                )
                logger.info("Sent daily briefing to user %s", user_id)

            except Exception as e:
                logger.exception("为用户 %s 发送每日简报失败: %s", user_id, e)

    except Exception as e:
        logger.exception("发送每日简报时出错: %s", e)

    return messages


async def check_diary_reminders(context, db: Database) -> list[dict[str, Any]]:
    """检查日记提醒

    Args:
        context: 上下文对象
        db: 数据库实例

    Returns:
        消息列表
    """
    messages = []
    current_utc = datetime.now(timezone.utc)

    try:
        user_ids = await _get_active_user_ids(db)
        settings_bundle_map = await get_user_settings_bundle_map(user_ids, db)

        for user_id in user_ids:
            try:
                settings_bundle = settings_bundle_map[user_id]
                custom_settings = settings_bundle["custom_settings"]
                user_settings = settings_bundle["settings"]
                user_now = get_user_now_from_settings(user_settings, current_utc)
                current_date = user_now.date().isoformat()
                target_time = user_settings.get("diary_remind_time", "21:30")

                # 检查是否今天已提醒
                if custom_settings.get("last_diary_remind_date") == current_date:
                    continue

                if not _is_time_reached(user_now, target_time):
                    continue

                # 检查今天是否已写日记
                if await _has_diary_for_date(db, user_id, current_date):
                    # 已写，更新状态不提醒
                    await run_sync(
                        save_user_setting, user_id, "last_diary_remind_date", current_date, db
                    )
                    continue

                # 发送提醒
                action = {
                    "action": "send_private_msg",
                    "params": {
                        "user_id": int(user_id),
                        "message": segments(
                            "📔 今天还没有写日记哦，记录一下美好的今天吧？\n发送 /pendo diary 开始"
                        ),
                    },
                }
                if hasattr(context, "send_action"):
                    await context.send_action(action)
                else:
                    messages.append(action)

                await run_sync(
                    save_user_setting, user_id, "last_diary_remind_date", current_date, db
                )

            except Exception as e:
                # L-1修复：记录日志，避免静默吞掉异常
                logger.warning("为用户 %s 处理日记提醒失败: %s", user_id, e)
    except Exception as e:
        logger.exception("检查日记提醒时出错: %s", e)

    return messages


async def send_weekly_finance_summaries(context, db: Database) -> list[dict[str, Any]]:
    """发送每周财务总结。

    由调度器按周触发，这里只做幂等保护和内容发送。
    """
    messages = []
    current_utc = datetime.now(timezone.utc)

    try:
        user_ids = await _get_active_user_ids(db)
        settings_bundle_map = await get_user_settings_bundle_map(user_ids, db)

        for user_id in user_ids:
            try:
                settings_bundle = settings_bundle_map[user_id]
                user_settings = settings_bundle["settings"]
                custom_settings = settings_bundle["custom_settings"]
                user_now = get_user_now_from_settings(user_settings, current_utc)
                current_date = user_now.date().isoformat()

                if custom_settings.get("last_weekly_finance_summary_date") == current_date:
                    continue

                start_dt = user_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
                    days=user_now.weekday()
                )
                end_dt = user_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
                    hours=23, minutes=59, seconds=59
                )
                summary = await _generate_finance_summary_content(
                    db,
                    user_id,
                    start_dt.strftime("%Y-%m-%d"),
                    end_dt.strftime("%Y-%m-%d"),
                    f"{start_dt.strftime('%m/%d')} - {end_dt.strftime('%m/%d')}",
                    "📆 本周财务总结",
                )
                if not summary:
                    await run_sync(
                        save_user_setting,
                        user_id,
                        "last_weekly_finance_summary_date",
                        current_date,
                        db,
                    )
                    continue

                await _send_private_or_collect(context, messages, user_id, summary)
                await run_sync(
                    save_user_setting,
                    user_id,
                    "last_weekly_finance_summary_date",
                    current_date,
                    db,
                )
            except Exception as e:
                logger.exception("为用户 %s 发送每周财务总结失败: %s", user_id, e)
    except Exception as e:
        logger.exception("发送每周财务总结时出错: %s", e)

    return messages


async def send_month_end_finance_summaries(context, db: Database) -> list[dict[str, Any]]:
    """发送月底财务总结。

    由调度器按月触发，这里只做幂等保护和内容发送。
    """
    messages = []
    current_utc = datetime.now(timezone.utc)

    try:
        user_ids = await _get_active_user_ids(db)
        settings_bundle_map = await get_user_settings_bundle_map(user_ids, db)

        for user_id in user_ids:
            try:
                settings_bundle = settings_bundle_map[user_id]
                user_settings = settings_bundle["settings"]
                custom_settings = settings_bundle["custom_settings"]
                user_now = get_user_now_from_settings(user_settings, current_utc)
                current_date = user_now.date().isoformat()

                if custom_settings.get("last_month_end_finance_summary_date") == current_date:
                    continue

                start_dt = user_now.replace(
                    day=1, hour=0, minute=0, second=0, microsecond=0
                )
                end_dt = user_now.replace(hour=23, minute=59, second=59, microsecond=0)
                summary = await _generate_finance_summary_content(
                    db,
                    user_id,
                    start_dt.strftime("%Y-%m-%d"),
                    end_dt.strftime("%Y-%m-%d"),
                    f"{start_dt.strftime('%Y/%m/%d')} - {end_dt.strftime('%Y/%m/%d')}",
                    "🧾 月底财务总结",
                )
                if not summary:
                    await run_sync(
                        save_user_setting,
                        user_id,
                        "last_month_end_finance_summary_date",
                        current_date,
                        db,
                    )
                    continue

                await _send_private_or_collect(context, messages, user_id, summary)
                await run_sync(
                    save_user_setting,
                    user_id,
                    "last_month_end_finance_summary_date",
                    current_date,
                    db,
                )
            except Exception as e:
                logger.exception("为用户 %s 发送月底财务总结失败: %s", user_id, e)
    except Exception as e:
        logger.exception("发送月底财务总结时出错: %s", e)

    return messages


# ============================================================
# 辅助函数
# ============================================================


async def _get_active_user_ids(db: Database) -> list[str]:
    """获取活跃用户ID列表"""
    return await run_sync(db.items.get_active_user_ids)


async def _send_private_or_collect(
    context, messages: list[dict[str, Any]], user_id: str, message: str
) -> None:
    action = {
        "action": "send_private_msg",
        "params": {"user_id": int(user_id), "message": segments(message)},
    }
    if hasattr(context, "send_action"):
        await context.send_action(action)
    else:
        messages.append(action)


def _is_time_reached(current_time: datetime, target_time_str: str) -> bool:
    """检查是否到达目标时间

    Args:
        current_time: 当前时间
        target_time_str: 目标时间字符串（HH:MM格式）

    Returns:
        是否已到达或超过目标时间
    """
    try:
        target_hour, target_minute = map(int, target_time_str.split(":"))
        return current_time.hour == target_hour and current_time.minute == target_minute
    except ValueError:
        return False


async def _generate_finance_summary_content(
    db: Database,
    user_id: str,
    start_date: str,
    end_date: str,
    range_label: str,
    title: str,
) -> str:
    items = await run_sync(
        db.items.query_items_by_date_range,
        user_id,
        ItemType.LEDGER.value,
        "ledger_date",
        start_date,
        end_date,
    )
    if not items:
        return ""

    income_items = [item for item in items if getattr(item, "direction", "") == "income"]
    expense_items = [item for item in items if getattr(item, "direction", "") == "expense"]
    total_income = sum(float(getattr(item, "amount", 0) or 0) for item in income_items)
    total_expense = sum(float(getattr(item, "amount", 0) or 0) for item in expense_items)
    balance = total_income - total_expense

    expense_by_cat: dict[str, float] = {}
    for item in expense_items:
        category = getattr(item, "ledger_category", "") or "其他"
        expense_by_cat[category] = expense_by_cat.get(category, 0.0) + float(item.amount or 0)

    income_by_cat: dict[str, float] = {}
    for item in income_items:
        category = getattr(item, "ledger_category", "") or "其他"
        income_by_cat[category] = income_by_cat.get(category, 0.0) + float(item.amount or 0)

    top_expense = max(expense_items, key=lambda item: float(getattr(item, "amount", 0) or 0), default=None)
    top_expense_category = max(expense_by_cat.items(), key=lambda pair: pair[1], default=None)
    top_income_category = max(income_by_cat.items(), key=lambda pair: pair[1], default=None)

    balance_prefix = "+" if balance >= 0 else ""
    lines = [
        title,
        f"📅 范围: {range_label}",
        f"🧾 共 {len(items)} 笔流水",
        "",
        f"💰 收入: ¥{total_income:.2f}",
        f"💸 支出: ¥{total_expense:.2f}",
        f"📊 结余: {balance_prefix}¥{balance:.2f}",
    ]

    if top_expense_category:
        lines.append(f"📂 最大支出分类: {top_expense_category[0]} ¥{top_expense_category[1]:.2f}")
    if top_income_category:
        lines.append(f"📥 主要收入来源: {top_income_category[0]} ¥{top_income_category[1]:.2f}")
    if top_expense is not None:
        title_text = getattr(top_expense, "title", "") or "未命名支出"
        ledger_date = getattr(top_expense, "ledger_date", "") or start_date
        lines.append(
            f"🔥 最大单笔支出: {title_text} ¥{float(top_expense.amount or 0):.2f} ({ledger_date})"
        )

    if expense_by_cat:
        lines.append("")
        lines.append("支出前 3 分类:")
        for index, (category, amount) in enumerate(
            sorted(expense_by_cat.items(), key=lambda pair: pair[1], reverse=True)[:3], 1
        ):
            lines.append(f"  {index}. {category} ¥{amount:.2f}")

    return "\n".join(lines)


async def _generate_briefing_content(user_id: str, db: Database, ai_parser: AIParser) -> str:
    """生成每日简报内容"""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)

    events, tasks, overdue_tasks = await _fetch_briefing_items(
        db, user_id, today.isoformat(), tomorrow.isoformat()
    )

    # 生成简报
    briefing = _format_daily_briefing(events, tasks)

    # 添加逾期提醒
    if overdue_tasks:
        briefing += f"\n\n⚠️ 逾期待办 ({len(overdue_tasks)}项):"
        for task in overdue_tasks[:3]:
            due_time = getattr(task, "due_time", "") or ""
            if due_time:
                dt = datetime.fromisoformat(due_time)
                time_str = dt.strftime("%m-%d")
                briefing += f"\n  - {task.title or '无标题'} (截止: {time_str})"

    return briefing


def _format_daily_briefing(events: list[Any], tasks: list[Any]) -> str:
    """格式化每日简报文本（纯格式化，不涉及 AI）"""
    current_date = datetime.now().strftime("%Y年%m月%d日")
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    weekday = weekday_names[datetime.now().weekday()]

    lines = [f"☀️ 早上好！今天是{current_date} {weekday}", ""]

    if events:
        lines.append("🗓️ **今日日程**")
        for evt in events[:5]:
            start_time = evt.start_time or ""
            time_str = start_time[11:16] if len(start_time) > 11 else ""
            title = evt.title or "无标题"
            location = f" @{evt.location}" if evt.location else ""
            lines.append(f"  • {time_str} {title}{location}")
        if len(events) > 5:
            lines.append(f"  ...还有 {len(events) - 5} 项")
        lines.append("")
    else:
        lines.append("🗓️ 今日暂无日程安排")
        lines.append("")

    if tasks:
        lines.append("✅ **今日待办**")
        for task in tasks[:5]:
            title = task.title or "无标题"
            raw_priority = (
                task.priority if hasattr(task, "priority") and task.priority is not None else 3
            )
            priority_value = getattr(raw_priority, "value", raw_priority)
            priority = priority_value if isinstance(priority_value, int) else 3
            priority_mark = "🔴" if priority <= 2 else "🟡" if priority == 3 else "⚪"
            lines.append(f"  {priority_mark} {title}")
        if len(tasks) > 5:
            lines.append(f"  ...还有 {len(tasks) - 5} 项")
        lines.append("")
    else:
        lines.append("✅ 今日暂无待办事项")
        lines.append("")

    lines.append("🌟 祝你今天工作顺利！")
    return "\n".join(lines)


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
    yesterday = (current_time - timedelta(days=1)).strftime("%Y-%m-%d")
    today = current_time.strftime("%Y-%m-%d")

    try:
        user_ids = await _get_active_user_ids(db)

        for user_id in user_ids:
            try:
                # 检查是否今天已执行过迁移
                custom_settings = await _get_user_custom_settings(user_id, db)
                if custom_settings.get("last_todo_migrate_date") == today:
                    continue

                # 查询前一天所有未完成的待办
                undone_tasks = await _get_undone_tasks_for_date(db, user_id, yesterday)

                if not undone_tasks:
                    # 没有未完成的待办，更新标记
                    await run_sync(save_user_setting, user_id, "last_todo_migrate_date", today, db)
                    continue

                # M-5修复：单事务批量迁移，替代逐条提交
                migrated_count = await _batch_migrate_tasks_to_date(
                    db, undone_tasks, today, user_id
                )

                # 如果有迁移，发送通知
                if migrated_count > 0:
                    action = {
                        "action": "send_private_msg",
                        "params": {
                            "user_id": int(user_id),
                            "message": segments(
                                f"📋 已将昨天的 {migrated_count} 个未完成待办迁移到今天\n\n💡 使用 /pendo todo list today 查看"
                            ),
                        },
                    }
                    if hasattr(context, "send_action"):
                        await context.send_action(action)
                    else:
                        messages.append(action)

                # 更新最后迁移日期
                await run_sync(save_user_setting, user_id, "last_todo_migrate_date", today, db)
                logger.info(
                    "Migrated %s undone todos for user %s from %s to %s",
                    migrated_count,
                    user_id,
                    yesterday,
                    today,
                )

            except Exception as e:
                logger.exception("为用户 %s 迁移待办失败: %s", user_id, e)

    except Exception as e:
        logger.exception("迁移待办时出错: %s", e)

    return messages


async def _get_undone_tasks_for_date(db: Database, user_id: str, date_str: str) -> list[Any]:
    """获取指定日期未完成的待办"""
    return await run_sync(db.items.get_undone_tasks_for_date, user_id, date_str)


async def _batch_migrate_tasks_to_date(
    db: Database, tasks: list[Any], target_date: str, user_id: str
) -> int:
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
        placeholders = ",".join(["?" for _ in task_ids])
        with conn:
            cursor.execute(
                f"UPDATE items SET category = ?, updated_at = ? WHERE id IN ({placeholders}) AND owner_id = ?",
                [target_date, now] + task_ids + [user_id],
            )
            return cursor.rowcount

    return await run_sync(_update)


def cleanup_reminder_singleton() -> None:
    """L-5修复：清除 _reminder_service_singleton，在插件 cleanup 时调用"""
    global _reminder_service_singleton
    _reminder_service_singleton = None
