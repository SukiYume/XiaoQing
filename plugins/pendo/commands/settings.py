"""解析、校验并持久化 Pendo 聊天端用户设置。"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, TypeAlias, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.args import parse
from core.plugin_base import run_sync

from ..services.db import Database
from ..utils.settings_utils import (
    format_plugin_settings_message,
    parse_toggle_value,
    save_user_setting,
)

Validator: TypeAlias = Callable[[str], tuple[bool, Any]]
Formatter: TypeAlias = Callable[[Any], str]


@dataclass(frozen=True, slots=True)
class _SettingSpec:
    """单值设置的存储位置、校验器和成功文案。"""

    key: str
    validator: Validator
    formatter: Formatter
    custom: bool = True


_HHMM_RE: Final = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


def _validate_hhmm(value: str) -> tuple[bool, str]:
    """校验 HH:MM 时间格式，返回 (is_valid, value_or_error_msg)"""
    v = value.strip()
    if not v:
        return False, "请指定时间，格式: HH:MM，例如: 08:00"
    if _HHMM_RE.fullmatch(v) is None:
        return False, f"❌ 无效的时间格式: {v}\n请使用 HH:MM 格式（00:00 ~ 23:59），例如: 08:00"
    return True, v


def _validate_timezone(value: str) -> tuple[bool, str]:
    """校验 IANA 时区键，失败时不允许写入不可用配置。"""

    timezone_name = value.strip()
    if not timezone_name:
        return False, "请指定时区"
    try:
        ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError):
        return (
            False,
            f"❌ 无效的时区: {timezone_name}\n"
            "请使用 IANA 时区标识符，例如: Asia/Shanghai, America/New_York",
        )
    return True, timezone_name


async def _show_settings(user_id: str, db: Database) -> str:
    """读取并格式化当前用户设置。"""

    settings = cast(dict[str, Any], await run_sync(db.get_user_settings, user_id))
    return cast(str, format_plugin_settings_message(settings))


async def _update_setting(
    user_id: str,
    value: str,
    db: Database,
    spec: _SettingSpec,
) -> str:
    """校验单值设置，并写入自定义 JSON 或顶层设置列。"""

    is_valid, processed_value = spec.validator(value)
    if not is_valid:
        return processed_value if isinstance(processed_value, str) else f"无效的设置值: {value}"

    if spec.custom:
        await run_sync(save_user_setting, user_id, spec.key, processed_value, db)
    else:
        settings = cast(dict[str, Any], await run_sync(db.get_user_settings, user_id))
        settings[spec.key] = processed_value
        await run_sync(db.update_user_settings, user_id, settings)

    return spec.formatter(processed_value)


async def _set_quiet_hours(user_id: str, value: str, db: Database) -> str:
    """校验并一次写入静默时段的开始、结束时间。"""

    if value.count("-") != 1:
        return "请指定静默时段，格式: <开始时间>-<结束时间>，例如: 23:00-07:00"

    raw_start, raw_end = value.split("-", 1)
    start_time = raw_start.strip()
    end_time = raw_end.strip()

    ok, msg = _validate_hhmm(start_time)
    if not ok:
        return f"开始时间格式错误: {msg}"
    ok, msg = _validate_hhmm(end_time)
    if not ok:
        return f"结束时间格式错误: {msg}"

    settings = cast(dict[str, Any], await run_sync(db.get_user_settings, user_id))
    settings["quiet_hours_start"] = start_time
    settings["quiet_hours_end"] = end_time
    await run_sync(db.update_user_settings, user_id, settings)

    return f"⚙️ 设置已更新\n🔕 静默时段: {start_time} - {end_time}\n💡 这段时间内提醒会更克制"


_AI_CONSENT_SPEC: Final = _SettingSpec(
    "ai_sensitive_data_consent",
    parse_toggle_value,
    lambda allowed: (
        "⚙️ AI 日记数据共享设置已更新\n"
        + (
            "✅ 已同意：日记正文可发送给当前配置的第三方 AI 服务进行情绪分析。"
            "可随时用 `ai_consent off` 撤回。"
            if allowed
            else "🔒 已关闭：日记正文不会发送给外部 AI，系统仅使用本地规则分析。"
        )
    ),
)

_SETTING_SPECS: Final[dict[str, _SettingSpec]] = {
    "reminder": _SettingSpec(
        "reminder_enabled",
        parse_toggle_value,
        lambda enabled: (
            "⚙️ 设置已更新\n"
            f"🔔 提醒通知: {'开启' if enabled else '关闭'}\n"
            "💡 事件提醒和待办提醒都会按这个总开关生效"
        ),
    ),
    "timezone": _SettingSpec(
        "timezone",
        _validate_timezone,
        lambda timezone_name: (
            f"⚙️ 设置已更新\n🌍 时区: {timezone_name}\n💡 后续提醒、简报和日记提醒都会按这个时区计算"
        ),
        custom=False,
    ),
    "daily_report": _SettingSpec(
        "daily_report_time",
        _validate_hhmm,
        lambda report_time: (
            f"⚙️ 设置已更新\n🗓️ 每日简报时间: {report_time}\n💡 每日简报会在这个时间点准备发送"
        ),
        custom=False,
    ),
    "daily_briefing": _SettingSpec(
        "daily_briefing_enabled",
        parse_toggle_value,
        lambda enabled: f"⚙️ 设置已更新\n🗓️ 每日简报: {'开启' if enabled else '关闭'}",
    ),
    "diary_remind": _SettingSpec(
        "diary_remind_time",
        _validate_hhmm,
        lambda remind_time: f"⚙️ 设置已更新\n📝 日记提醒时间: {remind_time}",
        custom=False,
    ),
    "ai_consent": _AI_CONSENT_SPEC,
    "ai_privacy": _AI_CONSENT_SPEC,
}


async def handle_settings(user_id: str, args: str, db: Database) -> str:
    """分发查看、静默时段和单值设置命令。"""

    if not args.strip():
        return await _show_settings(user_id, db)

    parsed = parse(args)
    action = parsed.first.lower()
    value = parsed.rest(1)

    if action in {"view", "show"}:
        if value:
            return "❌ 查看设置不接受额外参数\n用法: /pendo settings view"
        return await _show_settings(user_id, db)
    if action == "quiet_hours":
        return await _set_quiet_hours(user_id, value, db)
    spec = _SETTING_SPECS.get(action)
    if spec is not None:
        return await _update_setting(user_id, value, db, spec)
    return (
        f"❌ 未知的设置项: {action}\n\n"
        "可用设置:\n"
        "• reminder - 开关提醒\n"
        "• timezone - 时区\n"
        "• quiet_hours - 静默时段\n"
        "• daily_report - 每日简报时间\n"
        "• daily_briefing - 开关每日简报\n"
        "• diary_remind - 日记提醒时间\n"
        "• ai_consent - 是否允许将日记正文发送到配置的 AI 服务"
    )
