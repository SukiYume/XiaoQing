"""
设置管理命令模块
处理用户设置相关的所有命令
"""
import logging
from typing import Callable, Optional, Any
from core.plugin_base import run_sync
from core.args import parse
from ..services.db import Database
from ..utils.time_utils import parse_custom_settings, save_user_setting

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
    
    if action == "reminder":
        return await _set_reminder_settings(user_id, value, db)
    elif action == "timezone":
        return await _set_timezone(user_id, value, db)
    elif action == "quiet_hours":
        return await _set_quiet_hours(user_id, value, db)
    elif action == "default_view":
        return await _set_default_view(user_id, value, db)
    elif action == "daily_report":
        return await _set_daily_report_time(user_id, value, db)
    elif action == "diary_remind":
        return await _set_diary_remind_time(user_id, value, db)
    elif action == "privacy":
        return await _set_privacy_mode(user_id, value, db)
    else:
        return f"未知的设置项: {action}\n可用设置: reminder, timezone, quiet_hours, default_view, daily_report, diary_remind, privacy"

async def _show_settings(user_id: str, db: Database) -> str:
    """显示当前设置"""
    try:
        logger.debug("Showing settings for user %s", user_id)
        settings = await run_sync(db.settings.get_user_settings, user_id)
        custom = parse_custom_settings(settings)
        
        lines = ["⚙️ **当前设置**"]
        lines.append(f"\n🌍 时区: {settings.get('timezone', 'Asia/Shanghai')}")
        lines.append(f"🔕 静默时段: {settings.get('quiet_hours_start', '23:00')} - {settings.get('quiet_hours_end', '07:00')}")
        lines.append(f"📊 默认视图: {custom.get('default_view', 'today')}")
        lines.append(f"🔔 提醒: {'开启' if custom.get('reminder_enabled', True) else '关闭'}")
        lines.append(f"🗓️ 每日简报时间: {settings.get('daily_report_time', '08:00')}")
        lines.append(f"📝 日记提醒时间: {settings.get('diary_remind_time', '21:30')}")
        
        lines.append("\n**修改设置:**")
        lines.append("• /pendo settings reminder on/off - 开关提醒")
        lines.append("• /pendo settings timezone <时区> - 设置时区")
        lines.append("• /pendo settings quiet_hours <开始>-<结束> - 设置静默时段")
        lines.append("• /pendo settings default_view <视图> - 设置默认视图")
        lines.append("• /pendo settings daily_report <时间> - 设置每日简报时间")
        lines.append("• /pendo settings diary_remind <时间> - 设置日记提醒时间")
        
        return '\n'.join(lines)
    except Exception as e:
        logger.exception("Error showing settings for user %s: %s", user_id, e)
        return f"获取设置失败: {str(e)}"

async def _update_setting(
    user_id: str, 
    setting_key: str, 
    value: str, 
    db: Database,
    validator: Optional[Callable[[str], tuple[bool, Any]]] = None,
    formatter: Optional[Callable[[Any], str]] = None,
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
    return f"{setting_key}已设置为: {processed_value}"

async def _set_reminder_settings(user_id: str, value: str, db: Database) -> str:
    """设置提醒开关"""
    def validator(v):
        if not v: return False, "请指定 on 或 off"
        return True, v.lower() in ['on', 'true', '1', 'yes', '是']
    
    return await _update_setting(
        user_id, "reminder_enabled", value, db, 
        validator=validator, 
        formatter=lambda v: f"提醒已{'开启' if v else '关闭'}",
        is_custom=True
    )

async def _set_timezone(user_id: str, value: str, db: Database) -> str:
    """设置时区"""
    def validator(v):
        if not v:
            return False, "请指定时区"
        
        available_zones = _get_available_timezones()
        if available_zones and v not in available_zones:
            return False, f"无效的时区: {v}\n请使用 IANA 时区标识符，例如: Asia/Shanghai, America/New_York"
        
        return True, v
    
    return await _update_setting(
        user_id, "timezone", value, db, 
        validator=validator,
        is_custom=False
    )

async def _set_quiet_hours(user_id: str, value: str, db: Database) -> str:
    """设置静默时段"""
    if not value or '-' not in value:
        return "请指定静默时段，格式: <开始时间>-<结束时间>，例如: 23:00-07:00"
    
    parts = value.split('-')
    start_time = parts[0].strip()
    end_time = parts[1].strip()
    
    settings = await run_sync(db.settings.get_user_settings, user_id)
    settings['quiet_hours_start'] = start_time
    settings['quiet_hours_end'] = end_time
    await run_sync(db.settings.update_user_settings, user_id, settings)
    
    return f"静默时段已设置为: {start_time} - {end_time}"

async def _set_default_view(user_id: str, value: str, db: Database) -> str:
    """设置默认视图"""
    valid_views = ['today', 'tomorrow', 'week', 'month']
    def validator(v):
        if not v or v.lower() not in valid_views:
            return False, f"请指定有效的视图: {', '.join(valid_views)}"
        return True, v.lower()

    return await _update_setting(
        user_id, "default_view", value, db, 
        validator=validator, 
        formatter=lambda v: f"默认视图已设置为: {v}",
        is_custom=True
    )

async def _set_daily_report_time(user_id: str, value: str, db: Database) -> str:
    """设置每日简报时间"""
    return await _update_setting(
        user_id, "daily_report_time", value, db,
        validator=lambda v: (False, "请指定时间，例如: 08:00") if not v else (True, v),
        is_custom=False
    )

async def _set_diary_remind_time(user_id: str, value: str, db: Database) -> str:
    """设置日记提醒时间"""
    return await _update_setting(
        user_id, "diary_remind_time", value, db,
        validator=lambda v: (False, "请指定时间，例如: 21:30") if not v else (True, v),
        is_custom=False
    )

async def _set_privacy_mode(user_id: str, value: str, db: Database) -> str:
    """设置隐私模式"""
    def validator(v):
        if not v: return False, "请指定 on 或 off"
        return True, v.lower() in ['on', 'true', '1', 'yes', '是']
        
    return await _update_setting(
        user_id, "privacy_mode", value, db, 
        validator=validator, 
        formatter=lambda v: f"隐私模式已{'开启' if v else '关闭'}\n\n开启后，在群聊中创建条目时，详情将通过私聊发送。",
        is_custom=True
    )
