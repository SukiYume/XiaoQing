# 按配置时区取当前时间的函数与相关常量。
"""Configured-timezone wall clock."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_CONFIG_TIMEZONE = "Asia/Shanghai"
# 按 ``datetime.weekday()`` 取：“周” / “星期”后面的那个字。
WEEKDAY_NAMES = "一二三四五六日"


def now_in_configured_timezone(context: Any) -> datetime:
    """Return a timezone-aware wall-clock value from the core config contract."""

    timezone_name = DEFAULT_CONFIG_TIMEZONE
    try:
        settings   = context.get_settings_snapshot()
        config     = getattr(settings, "config", {})
        configured = config.get("timezone") if hasattr(config, "get") else None
        if isinstance(configured, str) and configured.strip():
            timezone_name = configured.strip()
        zone = ZoneInfo(timezone_name)
    except (AttributeError, TypeError, ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo(DEFAULT_CONFIG_TIMEZONE)
    return datetime.now(zone)
