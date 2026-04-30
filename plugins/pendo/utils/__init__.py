"""
工具模块（精简版）
"""
from .db_ops import DbOpsMixin, get_database
from .error_handlers import handle_command_errors, handle_command_errors_with_segments
from .formatters import (
    TYPE_NAMES,
    ItemFormatter,
    MessageBuilder,
    format_error_message,
    format_success_message,
    format_warning_message,
)
from .settings_utils import (
    parse_custom_settings,
    save_user_setting,
)
from .time_utils import (
    # 时区辅助
    TimezoneHelper,
    now_in_timezone,
    parse_and_localize,
    # 日期解析
    parse_date_optional,
    parse_date_required,
    parse_delay_time,
    parse_diary_range,
    parse_event_time_range,
    parse_hhmm_to_minutes,
    parse_remind_times,
    parse_search_date_range,
    parse_time_offset,
)
from .validators import (
    sanitize_search_keyword,
    validate_category,
    validate_diary_content,
    validate_item_data,
    validate_location,
    validate_priority,
    validate_tag,
    validate_title,
)

__all__ = [
    # 时区辅助
    'TimezoneHelper',
    'now_in_timezone',
    'parse_and_localize',
    # 日期解析
    'parse_date_optional',
    'parse_date_required',
    'parse_event_time_range',
    'parse_search_date_range',
    'parse_diary_range',
    'parse_delay_time',
    'parse_hhmm_to_minutes',
    'parse_time_offset',
    'parse_remind_times',
    # 验证
    'sanitize_search_keyword',
    'validate_category',
    'validate_diary_content',
    'validate_item_data',
    'validate_location',
    'validate_priority',
    'validate_tag',
    'validate_title',
    # 设置
    'parse_custom_settings',
    'save_user_setting',
    # 数据库操作
    'DbOpsMixin',
    'get_database',
    # 错误处理
    'handle_command_errors',
    'handle_command_errors_with_segments',
    # 消息格式化
    'ItemFormatter',
    'MessageBuilder',
    'format_success_message',
    'format_error_message',
    'format_warning_message',
    'TYPE_NAMES',
]
