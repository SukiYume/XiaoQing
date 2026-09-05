# 验证稍后提醒按用户时区计算并保留真实时刻。
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from plugins.pendo.commands.operations import _parse_snooze_time


def test_relative_snooze_uses_user_aware_instant_not_server_timezone() -> None:
    user_now = datetime(2030, 7, 1, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = datetime.fromisoformat(_parse_snooze_time("10m", now=user_now))
    assert result.tzinfo is not None
    assert result.astimezone(UTC) - user_now.astimezone(UTC) == (result - user_now)
    assert (result - user_now).total_seconds() == 600


def test_absolute_snooze_uses_user_wall_clock_across_dst_server_zone() -> None:
    # The host timezone is irrelevant: the injected user instant carries the
    # user's zone and DST rules.
    user_now = datetime(2030, 3, 10, 18, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = datetime.fromisoformat(_parse_snooze_time("19:00", now=user_now))
    assert result.hour == 19 and result.minute == 0
    assert result.utcoffset() == user_now.utcoffset()
    assert (result - user_now).total_seconds() == 30 * 60
