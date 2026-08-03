"""统一生成兼容现有数据库列的 UTC 时间与周期键。"""

from datetime import datetime, timezone


def utc_now() -> datetime:
    """返回不受主机时区影响、兼容旧列的无时区 UTC 时间。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def business_date() -> str:
    """返回定时任务使用的 UTC 日期键。"""
    return utc_now().strftime("%Y-%m-%d")


def business_week() -> str:
    """返回定时任务使用的 ISO 周键。"""
    return utc_now().strftime("%G-W%V")
