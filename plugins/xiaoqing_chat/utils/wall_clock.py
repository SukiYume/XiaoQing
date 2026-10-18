"""按 Core 配置的时区格式化聊天时间，避免宿主机时区进入人物的时间感。"""

from __future__ import annotations

import time
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.clock import DEFAULT_CONFIG_TIMEZONE

_WEEKDAYS     = "一二三四五六日"
_zone: tzinfo = ZoneInfo(DEFAULT_CONFIG_TIMEZONE)


def configure_wall_clock(timezone_name: object) -> None:
    """运行配置加载时更新时区；无效名称沿用 Core 默认时区。"""

    global _zone
    name = str(timezone_name or "").strip() or DEFAULT_CONFIG_TIMEZONE
    try:
        _zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        _zone = ZoneInfo(DEFAULT_CONFIG_TIMEZONE)


def local_datetime(ts: float | None = None) -> datetime:
    return datetime.fromtimestamp(time.time() if ts is None else float(ts), _zone)


def format_local(fmt: str, ts: float | None = None) -> str:
    return local_datetime(ts).strftime(fmt)


def day_period(hour: int) -> str:
    if hour < 5:
        return "凌晨"
    if hour < 9:
        return "早上"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 23:
        return "晚上"
    return "深夜"


def describe_now(ts: float | None = None) -> str:
    """给模型的当前时间：日期、星期和时段一起出现，减少对时段的误判。"""

    moment = local_datetime(ts)
    return f"{moment:%Y-%m-%d %H:%M}（周{_WEEKDAYS[moment.weekday()]}{day_period(moment.hour)}）"
