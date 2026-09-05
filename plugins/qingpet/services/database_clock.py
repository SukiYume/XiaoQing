"""Qingpet 全部数据库仓储共享的可替换 UTC 时钟。"""

from datetime import datetime
from typing import cast

from ..utils.time import business_date as _business_date
from ..utils.time import utc_now


def now() -> datetime:
    """返回数据库业务规则使用的当前 UTC 时间。"""

    return cast(datetime, utc_now())


def business_date(value: datetime | None = None) -> str:
    """每日配额与零点调度统一采用上海业务日期，存储时间戳保持 UTC。"""
    return _business_date(value or now())
