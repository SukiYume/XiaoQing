"""统一生成兼容现有数据库列的 UTC 时间与周期键。"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def utc_now() -> datetime:
    """返回不受主机时区影响、兼容旧列的无时区 UTC 时间。"""
    return datetime.now(UTC).replace(tzinfo=None)


def business_date(value: datetime | None = None) -> str:
    """返回上海业务日期；兼容数据库中保存的无时区 UTC 时间。"""
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()


def business_week() -> str:
    """返回定时任务使用的 ISO 周键。"""
    return utc_now().replace(tzinfo=UTC).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%G-W%V")
