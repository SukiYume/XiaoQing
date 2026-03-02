"""
命令模块
将main.py中的命令处理逻辑拆分到独立模块
"""
from .settings import handle_settings
from .operations import handle_confirm, handle_snooze, handle_undo
from .scheduled import check_reminders, send_daily_briefings, send_evening_briefings, check_diary_reminders, migrate_undone_todos, cleanup_reminder_singleton
from .session import handle_session_message, handle_diary_template_session, handle_event_conflict_session, handle_event_info_session


__all__ = [
    'handle_settings',
    'handle_confirm',
    'handle_snooze',
    'handle_undo',
    'check_reminders',
    'send_daily_briefings',
    'send_evening_briefings',
    'check_diary_reminders',
    'migrate_undone_todos',
    'cleanup_reminder_singleton',
    'handle_session_message',
    'handle_diary_template_session',
    'handle_event_conflict_session',
    'handle_event_info_session',
]
