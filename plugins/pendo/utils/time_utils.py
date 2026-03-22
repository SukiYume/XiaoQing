"""
时间解析工具
提供时区辅助、日期/时间范围解析、提醒时间解析等核心功能
"""

import json
import re
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Any
from zoneinfo import ZoneInfo

from ..config import PendoConfig

logger = logging.getLogger(__name__)

# ==================== 时区辅助 ====================


class TimezoneHelper:
    """时区辅助类"""

    DEFAULT_TZ = ZoneInfo(PendoConfig.DEFAULT_TIMEZONE)

    @staticmethod
    def get_user_timezone(user_id: str, db=None) -> ZoneInfo:
        """获取用户时区"""
        if db:
            try:
                settings = db.settings.get_user_settings(user_id)
                tz_str = settings.get("timezone", PendoConfig.DEFAULT_TIMEZONE)
                return ZoneInfo(tz_str)
            except Exception as e:
                logger.warning("Failed to get user timezone: %s", e)
        return TimezoneHelper.DEFAULT_TZ

    @staticmethod
    def now(tz: Optional[ZoneInfo] = None) -> datetime:
        """获取带时区的当前时间"""
        return datetime.now(tz or TimezoneHelper.DEFAULT_TZ)

    @staticmethod
    def parse(dt_str: str, tz: Optional[ZoneInfo] = None) -> datetime:
        """解析日期时间字符串并附加时区"""
        if not dt_str:
            raise ValueError("Empty datetime string")

        try:
            # M-7修复：直接解析后检查 tzinfo，不依赖字符串计数
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                return dt.astimezone(tz) if tz else dt
            return dt.replace(tzinfo=tz or TimezoneHelper.DEFAULT_TZ)
        except ValueError as e:
            logger.error("Failed to parse datetime: %s, error: %s", dt_str, e)
            raise

    @staticmethod
    def format_for_storage(dt: datetime) -> str:
        """格式化datetime用于存储"""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TimezoneHelper.DEFAULT_TZ)
        return dt.astimezone(timezone.utc).isoformat()


def now_in_timezone(user_id: str | None = None, db=None) -> datetime:
    """获取用户时区的当前时间"""
    if user_id and db:
        tz = TimezoneHelper.get_user_timezone(user_id, db)
    else:
        tz = TimezoneHelper.DEFAULT_TZ
    return TimezoneHelper.now(tz)


def get_user_now_from_settings(settings: dict[str, Any], current_utc: datetime) -> datetime:
    """Resolve a user's local current time from persisted settings."""
    tz_name = settings.get("timezone", PendoConfig.DEFAULT_TIMEZONE)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(PendoConfig.DEFAULT_TIMEZONE)
    return current_utc.astimezone(tz)


def parse_and_localize(dt_str: str, user_id: str | None = None, db=None) -> datetime:
    """解析时间字符串并本地化"""
    if user_id and db:
        tz = TimezoneHelper.get_user_timezone(user_id, db)
    else:
        tz = TimezoneHelper.DEFAULT_TZ
    return TimezoneHelper.parse(dt_str, tz)


# ==================== 日期解析 ====================


def parse_date_optional(date_str: str, now: Optional[datetime] = None) -> Optional[str]:
    """解析日期字符串为 YYYY-MM-DD，失败返回 None"""
    if not date_str or not str(date_str).strip():
        return None

    now = now or datetime.now()
    text = str(date_str).strip()
    lowered = text.lower()

    # 相对日期
    relative = {
        "今天": 0,
        "today": 0,
        "昨天": -1,
        "yesterday": -1,
        "明天": 1,
        "tomorrow": 1,
        "后天": 2,
        "前天": -2,
    }
    if lowered in relative:
        return (now + timedelta(days=relative[lowered])).strftime("%Y-%m-%d")

    # YYYY-MM-DD
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        pass

    # MM-DD
    try:
        return datetime.strptime(f"{now.year}-{text}", "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        pass

    return None


def parse_date_required(date_str: str, now: Optional[datetime] = None) -> str:
    """解析日期字符串，失败抛出 ValueError"""
    parsed = parse_date_optional(date_str, now)
    if not parsed:
        raise ValueError(f"无法解析日期: {date_str}")
    return parsed


def _parse_ym_range(ym_str: str) -> tuple[datetime, datetime]:
    """解析 YYYY-MM 字符串为该月第一天和最后一天的 datetime"""
    dt = datetime.strptime(ym_str, "%Y-%m")
    start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if dt.month == 12:
        end = dt.replace(year=dt.year + 1, month=1, day=1) - timedelta(seconds=1)
    else:
        end = dt.replace(month=dt.month + 1, day=1) - timedelta(seconds=1)
    return start, end


def _parse_time_range_core(
    time_range: str, now: Optional[datetime] = None, default: str = "today"
) -> tuple[datetime, datetime]:
    """核心时间范围解析，返回 (start_dt, end_dt)。

    支持: today, tomorrow, week, month, year, 今天, 本周, 本月, 今年,
          lastNd, YYYY, YYYY-MM, YYYY-MM-DD..YYYY-MM-DD
    """
    now = now or datetime.now()
    tr = (time_range or default).strip().lower()
    tr = re.sub(r"\.{3,}", "..", tr)

    _sod = now.replace(hour=0, minute=0, second=0, microsecond=0)
    _eod = now.replace(hour=23, minute=59, second=59, microsecond=0)

    # lastNd
    match = re.search(r"last(\d+)d", tr)
    if match:
        return now - timedelta(days=int(match.group(1))), now

    # 关键字映射
    kw = {
        "today": (_sod, _eod),
        "今天": (_sod, _eod),
        "tomorrow": ((_sod + timedelta(days=1)), (_eod + timedelta(days=1))),
        "week": (_sod, now + timedelta(days=7)),
        "本周": (
            _sod - timedelta(days=now.weekday()),
            _sod
            - timedelta(days=now.weekday())
            + timedelta(days=6, hours=23, minutes=59, seconds=59),
        ),
        "month": (_sod, now + timedelta(days=30)),
        "本月": (
            now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
            (_parse_ym_range(now.strftime("%Y-%m")))[1],
        ),
        "year": (
            now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0),
            now.replace(month=12, day=31, hour=23, minute=59, second=59, microsecond=0),
        ),
        "今年": (
            now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0),
            now.replace(month=12, day=31, hour=23, minute=59, second=59, microsecond=0),
        ),
    }
    if tr in kw:
        return kw[tr]

    # YYYY
    if re.fullmatch(r"\d{4}", tr):
        year = int(tr)
        return datetime(year, 1, 1), datetime(year, 12, 31, 23, 59, 59)

    # YYYY-MM
    if re.fullmatch(r"\d{4}-\d{2}", tr):
        return _parse_ym_range(tr)

    # start..end
    if ".." in tr:
        try:
            s, e = tr.split("..", 1)
            s, e = s.strip(), e.strip()
            try:
                start = datetime.strptime(s, "%Y-%m-%d")
            except ValueError:
                start, _ = _parse_ym_range(s)
            try:
                end = datetime.strptime(e, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            except ValueError:
                _, end = _parse_ym_range(e)
            return start, end
        except (ValueError, AttributeError) as exc:
            logger.warning("Failed to parse time range '%s': %s", time_range, exc)

    # 默认
    return _sod, _eod


def parse_event_time_range(time_range: str, now: Optional[datetime] = None) -> tuple[str, str]:
    """解析事件时间范围，返回 ISO start/end"""
    start, end = _parse_time_range_core(time_range, now)
    return start.isoformat(), end.isoformat()


def parse_search_date_range(
    range_str: str, now: Optional[datetime] = None
) -> tuple[Optional[str], Optional[str]]:
    """解析搜索日期范围，返回 ISO start/end 或 (None, None)"""
    if not range_str or not range_str.strip():
        return None, None
    try:
        start, end = _parse_time_range_core(range_str, now)
        return start.isoformat(), end.isoformat()
    except Exception:
        return None, None


def parse_diary_range(range_str: str, now: Optional[datetime] = None) -> tuple[str, str]:
    """解析日记范围，返回 YYYY-MM-DD start/end"""
    now = now or datetime.now()
    default = "today"
    # diary 特有默认：无匹配时返回最近30天
    tr = (range_str or default).strip().lower()
    if not re.search(r"last\d+d|today|week|month|year|今天|本周|本月|今年|\d{4}|\.\.", tr):
        return (now - timedelta(days=30)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")
    start, end = _parse_time_range_core(range_str, now)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def parse_delay_time(
    delay_str: str, current_due: Optional[str] = None, now: Optional[datetime] = None
) -> Optional[str]:
    """解析延后时间，返回 ISO 时间字符串"""
    if not delay_str:
        return None

    now = now or datetime.now()
    text = str(delay_str).strip().lower()

    # 相对时间: 1h, 30m, 2d
    match = re.match(r"^(\d+)([hmd])$", text)
    if match:
        num = int(match.group(1))
        unit = match.group(2)
        base = now
        if current_due:
            try:
                base = datetime.fromisoformat(current_due)
            except (ValueError, TypeError):
                # 解析失败，使用当前时间作为基准
                pass
        deltas = {"h": timedelta(hours=num), "m": timedelta(minutes=num), "d": timedelta(days=num)}
        return (base + deltas[unit]).isoformat()

    # 绝对时间: HH:MM
    if ":" in text:
        try:
            hour, minute = map(int, text.split(":"))
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate < now:
                candidate += timedelta(days=1)
            return candidate.isoformat()
        except (ValueError, AttributeError):
            # 时间格式解析失败
            pass

    return None


def parse_hhmm_to_minutes(time_str: str) -> Optional[int]:
    """解析 HH:MM 到分钟数"""
    if not time_str:
        return None
    try:
        hour, minute = map(int, str(time_str).strip().split(":"))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return hour * 60 + minute
    except (ValueError, AttributeError):
        # 时间格式解析失败
        pass
    return None


def parse_time_offset(offset_str: str) -> int:
    """解析时间偏移为分钟数"""
    match = re.match(r"^(\d+)([mhd])$", offset_str.lower())
    if not match:
        return 0
    value = int(match.group(1))
    unit = match.group(2)
    return {"m": value, "h": value * 60, "d": value * 1440}.get(unit, 0)


def parse_remind_times(raw: Any) -> list[str]:
    """解析提醒时间（list、JSON字符串 → list，失败返回空列表）"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass
    return []
