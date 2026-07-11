"""
设置管理命令模块
处理用户设置相关的所有命令
"""
import logging
import re
from collections.abc import Callable
from typing import Any

from core.args import parse
from core.plugin_base import run_sync

from ..services.db import Database
from ..utils.settings_utils import (
    format_plugin_settings_message,
    parse_toggle_value,
    save_user_setting,
)

# 缓存可用时区列表
_available_timezones = None

def _get_available_timezones() -> set[str]:
    """获取可用时区列表（带缓存）"""
    global _available_timezones
    if _available_timezones is None:
        try:
            from zoneinfo import available_timezones
            _available_timezones = set(available_timezones())
        except ImportError:
            _available_timezones = set()
    return _available_timezones

logger = logging.getLogger(__name__)

# HH:MM 格式校验
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

def _validate_hhmm(value: str) -> tuple[bool, str]:
    """校验 HH:MM 时间格式，返回 (is_valid, value_or_error_msg)"""
    v = value.strip()
    if not v:
        return False, "请指定时间，格式: HH:MM，例如: 08:00"
    if not _HHMM_RE.match(v):
        return False, f"❌ 无效的时间格式: {v}\n请使用 HH:MM 格式（00:00 ~ 23:59），例如: 08:00"
    return True, v


async def handle_settings(user_id: str, args: str, db: Database) -> str:
    """处理设置命令

    Args:
        user_id: 用户ID
        args: 命令参数
        db: 数据库实例

    Returns:
        设置结果消息
    """
    if not args:
        return await _show_settings(user_id, db)

    parsed = parse(args)
    action = parsed.first.lower()
    value = parsed.rest(1)

    if action in ("view", "show"):
        return await _show_settings(user_id, db)
    elif action == "reminder":
        return await _set_reminder_settings(user_id, value, db)
    elif action == "timezone":
        return await _set_timezone(user_id, value, db)
    elif action == "quiet_hours":
        return await _set_quiet_hours(user_id, value, db)
    elif action == "daily_report":
        return await _set_daily_report_time(user_id, value, db)
    elif action == "daily_briefing":
        return await _set_daily_briefing_enabled(user_id, value, db)
    elif action == "diary_remind":
        return await _set_diary_remind_time(user_id, value, db)
    elif action == "privacy":
        return await _set_privacy_mode(user_id, value, db)
    elif action in {"ai_consent", "ai_privacy"}:
        return await _set_ai_sensitive_data_consent(user_id, value, db)
    else:
        return (
            f"❌ 未知的设置项: {action}\n\n"
            "可用设置:\n"
            "• reminder - 开关提醒\n"
            "• timezone - 时区\n"
            "• quiet_hours - 静默时段\n"
            "• daily_report - 每日简报时间\n"
            "• daily_briefing - 开关每日简报\n"
            "• diary_remind - 日记提醒时间\n"
            "• privacy - 隐私模式\n"
            "• ai_consent - 是否允许将日记正文发送到配置的 AI 服务"
        )

async def _show_settings(user_id: str, db: Database) -> str:
    """显示当前设置"""
    try:
        logger.debug("Showing settings for user %s", user_id)
        settings = await run_sync(db.settings.get_user_settings, user_id)
        return format_plugin_settings_message(settings)
    except Exception as e:
        logger.exception("Error showing settings for user %s: %s", user_id, e)
        return f"获取设置失败: {str(e)}"

async def _update_setting(
    user_id: str,
    setting_key: str,
    value: str,
    db: Database,
    validator: Callable[[str], tuple[bool, Any]] | None = None,
    formatter: Callable[[Any], str] | None = None,
    is_custom: bool = True
) -> str:
    """通用设置更新函数

    Args:
        user_id: 用户ID
        setting_key: 设置键名
        value: 设置值
        db: 数据库实例
        validator: 验证函数，返回(is_valid, processed_value)
        formatter: 格式化函数，用于生成返回消息
        is_custom: 是否为自定义设置（存储在custom_settings字段）

    Returns:
        设置结果消息
    """
    processed_value = value
    if validator:
        is_valid, val_or_msg = validator(value)
        if not is_valid:
            return val_or_msg if isinstance(val_or_msg, str) else f"无效的设置值: {value}"
        processed_value = val_or_msg

    if is_custom:
        await run_sync(save_user_setting, user_id, setting_key, processed_value, db)
    else:
        settings = await run_sync(db.settings.get_user_settings, user_id)
        settings[setting_key] = processed_value
        await run_sync(db.settings.update_user_settings, user_id, settings)

    if formatter:
        return formatter(processed_value)
    return f"✅ {setting_key}已设置为: {processed_value}"

async def _set_reminder_settings(user_id: str, value: str, db: Database) -> str:
    """设置提醒开关"""
    return await _update_setting(
        user_id, "reminder_enabled", value, db,
        validator=parse_toggle_value,
        formatter=lambda v: (
            "⚙️ 设置已更新\n"
            f"🔔 提醒通知: {'开启' if v else '关闭'}\n"
            "💡 事件提醒和待办提醒都会按这个总开关生效"
        ),
        is_custom=True
    )

async def _set_timezone(user_id: str, value: str, db: Database) -> str:
    """设置时区"""
    def validator(v):
        if not v:
            return False, "请指定时区"

        available_zones = _get_available_timezones()
        if available_zones and v not in available_zones:
            return False, f"❌ 无效的时区: {v}\n请使用 IANA 时区标识符，例如: Asia/Shanghai, America/New_York"

        return True, v

    return await _update_setting(
        user_id, "timezone", value, db,
        validator=validator,
        formatter=lambda v: (
            "⚙️ 设置已更新\n"
            f"🌍 时区: {v}\n"
            "💡 后续提醒、简报和日记提醒都会按这个时区计算"
        ),
        is_custom=False
    )

async def _set_quiet_hours(user_id: str, value: str, db: Database) -> str:
    """设置静默时段"""
    if not value or '-' not in value:
        return "请指定静默时段，格式: <开始时间>-<结束时间>，例如: 23:00-07:00"

    parts = value.split('-')
    start_time = parts[0].strip()
    end_time = parts[1].strip()

    # 校验 HH:MM 格式
    ok, msg = _validate_hhmm(start_time)
    if not ok:
        return f"开始时间格式错误: {msg}"
    ok, msg = _validate_hhmm(end_time)
    if not ok:
        return f"结束时间格式错误: {msg}"

    settings = await run_sync(db.settings.get_user_settings, user_id)
    settings['quiet_hours_start'] = start_time
    settings['quiet_hours_end'] = end_time
    await run_sync(db.settings.update_user_settings, user_id, settings)

    return (
        "⚙️ 设置已更新\n"
        f"🔕 静默时段: {start_time} - {end_time}\n"
        "💡 这段时间内提醒会更克制"
    )

async def _set_daily_report_time(user_id: str, value: str, db: Database) -> str:
    """设置每日简报时间"""
    return await _update_setting(
        user_id, "daily_report_time", value, db,
        validator=_validate_hhmm,
        formatter=lambda v: (
            "⚙️ 设置已更新\n"
            f"🗓️ 每日简报时间: {v}\n"
            "💡 每日简报会在这个时间点准备发送"
        ),
        is_custom=False
    )

async def _set_daily_briefing_enabled(user_id: str, value: str, db: Database) -> str:
    """开关每日简报"""
    return await _update_setting(
        user_id, "daily_briefing_enabled", value, db,
        validator=parse_toggle_value,
        formatter=lambda v: (
            "⚙️ 设置已更新\n"
            f"🗓️ 每日简报: {'开启' if v else '关闭'}"
        ),
        is_custom=True
    )

async def _set_diary_remind_time(user_id: str, value: str, db: Database) -> str:
    """设置日记提醒时间"""
    return await _update_setting(
        user_id, "diary_remind_time", value, db,
        validator=_validate_hhmm,
        formatter=lambda v: (
            "⚙️ 设置已更新\n"
            f"📝 日记提醒时间: {v}"
        ),
        is_custom=False
    )

async def _set_privacy_mode(user_id: str, value: str, db: Database) -> str:
    """设置隐私模式"""
    return await _update_setting(
        user_id, "privacy_mode", value, db,
        validator=parse_toggle_value,
        formatter=lambda v: (
            "⚙️ 设置已更新\n"
            f"🔒 隐私模式: {'开启' if v else '关闭'}\n"
            "💡 开启后，群聊中的非公开详情会改为私聊发送"
        ),
        is_custom=True
    )


async def _set_ai_sensitive_data_consent(user_id: str, value: str, db: Database) -> str:
    """Record explicit, revocable consent before diary text may leave Pendo."""
    return await _update_setting(
        user_id,
        "ai_sensitive_data_consent",
        value,
        db,
        validator=parse_toggle_value,
        formatter=lambda allowed: (
            "⚙️ AI 日记数据共享设置已更新\n"
            + (
                "✅ 已同意：日记正文可发送给当前配置的第三方 AI 服务进行情绪分析。可随时用 `ai_consent off` 撤回。"
                if allowed
                else "🔒 已关闭：日记正文不会发送给外部 AI，系统仅使用本地规则分析。"
            )
        ),
        is_custom=True,
    )
