# 时钟与随机源接口（分发器注入，测试可替换）和按配置时区取当前时间的函数。
"""Clock and random interfaces for testability."""

from __future__ import annotations

import random
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

T                       = TypeVar("T")
DEFAULT_CONFIG_TIMEZONE = "Asia/Shanghai"
# 按 ``datetime.weekday()`` 取：“周” / “星期”后面的那个字。
WEEKDAY_NAMES = "一二三四五六日"


class IClock(Protocol):
    def now(self) -> float: ...


class IRandom(Protocol):
    def random(self) -> float: ...

    def choice(self, seq: Sequence[T]) -> T: ...


class SystemClock:
    def now(self) -> float:
        return time.time()


class SystemRandom:
    def random(self) -> float:
        return random.random()

    def choice(self, seq: Sequence[T]) -> T:
        return random.choice(seq)


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
