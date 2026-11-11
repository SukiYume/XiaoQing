"""她的“一天”：从起床到睡觉，凌晨 5 点前还算前一天。

按日历切的话，零点后写的日记只看得到零点后的事，前一晚刷的都不算“今天”；零点前后没上网的晚上就
整个没有睡前整理。预算计数、睡前整理、日记日期、“昨天 / 前天”都按这里的日子算。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, tzinfo
from typing import Any

DAY               = 86400.0
DAY_ROLLOVER_HOUR = 5


def life_date(moment: datetime) -> date:
    return (moment - timedelta(hours=DAY_ROLLOVER_HOUR)).date()


def day_key(moment: datetime) -> str:
    return life_date(moment).strftime("%Y-%m-%d")


def episode_day(episode: dict[str, Any], tz: tzinfo | None) -> str:
    """经历属于她的哪一天：按时间戳现算（经历上不另存日子，免得和时间戳对不上）。"""

    return day_key(datetime.fromtimestamp(float(episode["at"]), tz))
