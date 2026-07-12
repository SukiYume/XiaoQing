"""Settings normalization helpers shared by plugin and Web."""

import logging
from typing import Any

from ..config import PendoConfig

logger = logging.getLogger(__name__)


DEFAULT_SETTINGS_JSON = {
    "reminder_enabled": True,
    "daily_briefing_enabled": True,
    "privacy_mode": True,
    "ai_sensitive_data_consent": False,
}

PLUGIN_SETTINGS_HELP_LINES = (
    "• /pendo settings [view|show] - 查看当前设置",
    "• /pendo settings reminder on/off - 开关提醒(也支持 开/关)",
    "  - 例: /pendo settings reminder on",
    "• /pendo settings timezone <IANA时区> - 设置时区",
    "  - 例: /pendo settings timezone Asia/Shanghai",
    "• /pendo settings quiet_hours <开始>-<结束> - 静默时段",
    "  - 例: /pendo settings quiet_hours 23:00-07:00",
    "• /pendo settings daily_report <HH:MM> - 每日简报时间",
    "  - 例: /pendo settings daily_report 08:30",
    "• /pendo settings daily_briefing on/off - 开关每日简报",
    "  - 例: /pendo settings daily_briefing off",
    "• /pendo settings diary_remind <HH:MM> - 日记提醒时间",
    "  - 例: /pendo settings diary_remind 21:30",
    "• /pendo settings privacy on/off - 隐私模式",
    "  - 例: /pendo settings privacy on",
)

# 统一的布尔值解析字符串集合，供 _coerce_setting_bool 和 parse_toggle_value 共用
_TRUTHY = frozenset({"on", "true", "1", "yes", "是", "开启", "开"})
_FALSY = frozenset({"off", "false", "0", "no", "否", "关闭", "关"})


def _coerce_setting_bool(value: Any, default: bool) -> bool:
    """Coerce persisted setting values to bool without misreading strings like 'false'."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if not normalized:
            return default
        if normalized in _TRUTHY:
            return True
        if normalized in _FALSY:
            return False
    return bool(value)


def normalize_settings_json(settings_json: Any, *, partial: bool = False) -> dict[str, Any]:
    """Normalize custom settings while preserving unknown keys."""
    normalized = dict(settings_json) if isinstance(settings_json, dict) else {}

    if "reminder_enabled" in normalized or not partial:
        normalized["reminder_enabled"] = _coerce_setting_bool(
            normalized.get("reminder_enabled"),
            DEFAULT_SETTINGS_JSON["reminder_enabled"],
        )

    briefing_present = "daily_briefing_enabled" in normalized or "daily_report_enabled" in normalized
    if briefing_present or not partial:
        briefing_enabled = normalized.get("daily_briefing_enabled")
        if briefing_enabled is None:
            briefing_enabled = normalized.get(
                "daily_report_enabled",
                DEFAULT_SETTINGS_JSON["daily_briefing_enabled"],
            )
        normalized["daily_briefing_enabled"] = _coerce_setting_bool(
            briefing_enabled,
            DEFAULT_SETTINGS_JSON["daily_briefing_enabled"],
        )

    normalized.pop("daily_report_enabled", None)

    if "privacy_mode" in normalized or not partial:
        normalized["privacy_mode"] = _coerce_setting_bool(
            normalized.get("privacy_mode"),
            DEFAULT_SETTINGS_JSON["privacy_mode"],
        )

    if "ai_sensitive_data_consent" in normalized or not partial:
        normalized["ai_sensitive_data_consent"] = _coerce_setting_bool(
            normalized.get("ai_sensitive_data_consent"),
            DEFAULT_SETTINGS_JSON["ai_sensitive_data_consent"],
        )

    return normalized


def parse_custom_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """读取数据库层已规范化的自定义设置。"""
    raw_settings = settings.get("settings_json")
    if isinstance(raw_settings, dict):
        return normalize_settings_json(raw_settings)
    if raw_settings not in (None, ""):
        logger.warning("Unexpected settings_json type: %s", type(raw_settings).__name__)
    return normalize_settings_json({})


def parse_toggle_value(value: str) -> tuple[bool, bool | str]:
    """Parse on/off style text to a boolean."""
    normalized = value.strip().lower()
    if not normalized:
        return False, "请指定 on 或 off"

    if normalized in _TRUTHY:
        return True, True
    if normalized in _FALSY:
        return True, False
    return False, "请指定 on 或 off"


def format_plugin_settings_message(settings: dict[str, Any]) -> str:
    """Format plugin-side settings output from one normalized source."""
    custom = parse_custom_settings(settings)

    reminder_on = custom.get("reminder_enabled", DEFAULT_SETTINGS_JSON["reminder_enabled"])
    briefing_on = custom.get(
        "daily_briefing_enabled", DEFAULT_SETTINGS_JSON["daily_briefing_enabled"]
    )
    privacy_on = custom.get("privacy_mode", PendoConfig.MESSAGE_PRIVACY_MODE_DEFAULT)

    lines = ["⚙️ **当前设置**"]
    lines.append(f"\n🌍 时区: {settings.get('timezone', 'Asia/Shanghai')}")
    lines.append(
        f"🔕 静默时段: {settings.get('quiet_hours_start', '23:00')} - {settings.get('quiet_hours_end', '07:00')}"
    )
    lines.append(f"🔔 提醒: {'开启' if reminder_on else '关闭'}")
    lines.append(
        f"🗓️ 每日简报: {'开启' if briefing_on else '关闭'} ({settings.get('daily_report_time', '08:00')})"
    )
    lines.append(f"📝 日记提醒: {settings.get('diary_remind_time', '21:30')}")
    lines.append(f"🔒 隐私模式: {'开启' if privacy_on else '关闭'}")

    lines.append("\n**修改设置:**")
    lines.extend(PLUGIN_SETTINGS_HELP_LINES[1:])
    return "\n".join(lines)


def save_user_setting(user_id: str, key: str, value: Any, db):
    """保存用户设置"""
    try:
        settings = db.settings.get_user_settings(user_id)
        custom = parse_custom_settings(settings)
        custom[key] = value
        settings["settings_json"] = normalize_settings_json(custom, partial=True)
        db.settings.update_user_settings(user_id, settings)
    except Exception:
        raise


def resolve_default_category(db, user_id: str, fallback: str = "未分类") -> str:
    """Return the user's preferred default category when available."""
    try:
        settings = db.settings.get_user_settings(user_id)
    except Exception as exc:
        logger.warning(
            "Failed to load default category error_type=%s",
            type(exc).__name__,
        )
        return fallback

    category = str(settings.get("default_category") or "").strip()
    return category or fallback
