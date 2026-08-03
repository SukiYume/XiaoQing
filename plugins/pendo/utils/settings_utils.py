"""聊天命令与 Web 端共享的用户设置规范化逻辑。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from ..config import PendoConfig

if TYPE_CHECKING:
    from ..services.db import Database

logger = logging.getLogger(__name__)


DEFAULT_SETTINGS_JSON: Final[dict[str, bool]] = {
    "reminder_enabled": True,
    "daily_briefing_enabled": True,
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
    "• /pendo settings ai_consent on/off - 是否允许把日记正文发送给已配置的外部 AI",
    "  - 例: /pendo settings ai_consent off",
)

# 统一的布尔值解析字符串集合，供 _coerce_setting_bool 和 parse_toggle_value 共用
_TRUTHY = frozenset({"on", "true", "1", "yes", "是", "开启", "开"})
_FALSY = frozenset({"off", "false", "0", "no", "否", "关闭", "关"})


def _coerce_setting_bool(value: Any, default: bool) -> bool:
    """把持久化设置转为布尔值，避免把字符串 ``false`` 误判为真。"""
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
    """规范自定义设置，并保留其他插件扩展键。"""
    normalized = dict(settings_json) if isinstance(settings_json, dict) else {}
    normalized.pop("privacy_mode", None)

    for key in ("reminder_enabled", "ai_sensitive_data_consent"):
        if key in normalized or not partial:
            normalized[key] = _coerce_setting_bool(
                normalized.get(key),
                DEFAULT_SETTINGS_JSON[key],
            )

    briefing_present = (
        "daily_briefing_enabled" in normalized or "daily_report_enabled" in normalized
    )
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
    """解析中英文开关文本，返回是否有效及对应结果。"""
    normalized = value.strip().lower()
    if not normalized:
        return False, "请指定 on 或 off"

    if normalized in _TRUTHY:
        return True, True
    if normalized in _FALSY:
        return True, False
    return False, "请指定 on 或 off"


def format_plugin_settings_message(settings: dict[str, Any]) -> str:
    """从统一规范化结果生成聊天端设置说明。"""
    custom = parse_custom_settings(settings)

    reminder_on = custom.get("reminder_enabled", DEFAULT_SETTINGS_JSON["reminder_enabled"])
    briefing_on = custom.get(
        "daily_briefing_enabled", DEFAULT_SETTINGS_JSON["daily_briefing_enabled"]
    )
    ai_consent_on = custom.get(
        "ai_sensitive_data_consent",
        DEFAULT_SETTINGS_JSON["ai_sensitive_data_consent"],
    )

    lines = ["⚙️ **当前设置**"]
    lines.append(f"\n🌍 时区: {settings.get('timezone', PendoConfig.DEFAULT_TIMEZONE)}")
    lines.append(
        f"🔕 静默时段: {settings.get('quiet_hours_start', PendoConfig.DEFAULT_QUIET_HOURS_START)} - "
        f"{settings.get('quiet_hours_end', PendoConfig.DEFAULT_QUIET_HOURS_END)}"
    )
    lines.append(f"🔔 提醒: {'开启' if reminder_on else '关闭'}")
    lines.append(
        f"🗓️ 每日简报: {'开启' if briefing_on else '关闭'} "
        f"({settings.get('daily_report_time', PendoConfig.DEFAULT_DAILY_REPORT_TIME)})"
    )
    lines.append(
        f"📝 日记提醒: {settings.get('diary_remind_time', PendoConfig.DEFAULT_DIARY_REMIND_TIME)}"
    )
    lines.append(f"🤖 日记 AI 数据共享: {'允许' if ai_consent_on else '禁止'}")

    lines.append("\n**修改设置:**")
    lines.extend(PLUGIN_SETTINGS_HELP_LINES[1:])
    return "\n".join(lines)


def save_user_setting(user_id: str, key: str, value: Any, db: Database) -> None:
    """以单键补丁保存用户设置，避免并发写入覆盖其他自定义键。"""
    custom_patch = normalize_settings_json({key: value}, partial=True)
    db.update_user_settings(user_id, {"settings_json": custom_patch})


def resolve_default_category(
    db: Database,
    user_id: str,
    fallback: str = PendoConfig.DEFAULT_CATEGORY,
) -> str:
    """优先返回用户设置的默认分类，读取失败时使用插件默认值。"""
    try:
        settings = db.get_user_settings(user_id)
    except Exception as exc:
        logger.warning(
            "Failed to load default category error_type=%s",
            type(exc).__name__,
        )
        return fallback

    category = str(settings.get("default_category") or "").strip()
    return category or fallback
