"""Pendo 的时区、日期范围和提醒时间解析工具。"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, Any, Final, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.plugin_base import run_sync

from ..config import PendoConfig

if TYPE_CHECKING:
    from ..services.db import Database

logger = logging.getLogger(__name__)

_COMMON_STORAGE_DATETIME_FIELDS: Final = (
    "created_at",
    "updated_at",
    "deleted_at",
)
_ITEM_STORAGE_DATETIME_FIELDS: Final = {
    "event": ("start_time", "end_time"),
    "task": ("deadline_at", "completed_at", "cancelled_at"),
    "note": ("last_viewed",),
    "diary": ("entry_time",),
    "ledger": (),
}
_ALL_STORAGE_DATETIME_FIELDS: Final = frozenset(
    (
        *_COMMON_STORAGE_DATETIME_FIELDS,
        *(field_name for fields in _ITEM_STORAGE_DATETIME_FIELDS.values() for field_name in fields),
    )
)


class TimezoneHelper:
    """集中处理 Pendo 持久化和展示使用的时区规则。"""

    DEFAULT_TZ = ZoneInfo(PendoConfig.DEFAULT_TIMEZONE)

    @staticmethod
    def get_user_timezone(user_id: str, db: Database | None = None) -> ZoneInfo:
        """获取用户时区"""
        if db:
            try:
                settings = db.get_user_settings(user_id)
                tz_str = settings.get("timezone", PendoConfig.DEFAULT_TIMEZONE)
                return ZoneInfo(tz_str)
            except Exception as exc:
                logger.warning(
                    "Failed to get user timezone error_type=%s",
                    type(exc).__name__,
                )
        return TimezoneHelper.DEFAULT_TZ

    @staticmethod
    def now(tz: tzinfo | None = None) -> datetime:
        """获取带时区的当前时间"""
        return datetime.now(tz or TimezoneHelper.DEFAULT_TZ)

    @staticmethod
    def parse(dt_str: str, tz: tzinfo | None = None) -> datetime:
        """解析日期时间字符串并附加时区"""
        if not dt_str:
            raise ValueError("Empty datetime string")

        # 解析后检查 tzinfo；字符串分隔符数量不能可靠表示时区信息。
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            return dt.astimezone(tz) if tz else dt
        return dt.replace(tzinfo=tz or TimezoneHelper.DEFAULT_TZ)

    @staticmethod
    def format_for_storage(dt: datetime) -> str:
        """Format an explicitly timezone-aware datetime as UTC for storage."""
        if dt.tzinfo is None:
            raise ValueError(
                "naive datetime is not accepted for storage; supply an explicit timezone"
            )
        return dt.astimezone(timezone.utc).isoformat()


def now_in_timezone(user_id: str | None = None, db: Database | None = None) -> datetime:
    """获取用户时区的当前时间"""
    if user_id and db:
        tz = TimezoneHelper.get_user_timezone(user_id, db)
    else:
        tz = TimezoneHelper.DEFAULT_TZ
    return TimezoneHelper.now(tz)


def resolve_source_wall_time(
    parsed: datetime,
    field_name: str,
    source_zone: ZoneInfo,
) -> datetime:
    """Attach a source zone only when a wall time maps to one real instant.

    A naive value during a DST gap has no real instant; one during a fold has
    two.  Silently choosing either case would make an irreversible migration
    guess, so both are rejected.
    """

    if parsed.tzinfo is not None:
        raise ValueError(f"{field_name} must be a naive source wall time")
    candidates: dict[tuple[timedelta | None, timedelta | None], datetime] = {}
    for fold in (0, 1):
        aware = parsed.replace(tzinfo=source_zone, fold=fold)
        try:
            roundtrip = aware.astimezone(timezone.utc).astimezone(source_zone).replace(tzinfo=None)
        except (OverflowError, ValueError) as exc:
            raise ValueError(f"Invalid {field_name}, outside supported datetime range") from exc
        if roundtrip == parsed:
            candidates[(aware.utcoffset(), aware.dst())] = aware
    if not candidates:
        raise ValueError(f"Nonexistent local time for {field_name} in source timezone")
    if len(candidates) > 1:
        raise ValueError(f"Ambiguous local time for {field_name} in source timezone")
    return next(iter(candidates.values()))


def normalize_datetime_for_storage(
    value: object,
    field_name: str,
    source_zone: ZoneInfo,
    *,
    timespec: str = "seconds",
) -> str:
    """Convert an ISO datetime or explicit wall time to a UTC-aware ISO value."""

    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{field_name} is required")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Invalid {field_name}, expected ISO datetime") from exc
    try:
        if parsed.utcoffset() is None:
            parsed = resolve_source_wall_time(parsed, field_name, source_zone)
        return parsed.astimezone(timezone.utc).isoformat(timespec=timespec)
    except (OverflowError, ValueError) as exc:
        if str(exc).startswith(("Ambiguous ", "Nonexistent ", "Invalid ")):
            raise
        raise ValueError(f"Invalid {field_name}, outside supported datetime range") from exc


def utc_now_iso(*, timespec: str = "seconds") -> str:
    """Return one canonical UTC-aware timestamp for persistence."""

    return datetime.now(timezone.utc).isoformat(timespec=timespec)


def require_canonical_utc_timestamp(value: object, field_name: str) -> None:
    """Require the one timestamp shape accepted by Pendo persistence code."""

    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a canonical UTC ISO string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a canonical UTC ISO string") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must use UTC +00:00")
    canonical = parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    if value != canonical:
        raise ValueError(f"{field_name} must use canonical second-precision UTC ISO")


def require_canonical_utc_storage(payload: dict[str, Any]) -> None:
    """Reject any timestamp that reached a SQL producer outside the UTC contract."""

    for field_name in _ALL_STORAGE_DATETIME_FIELDS:
        value = payload.get(field_name)
        if value not in (None, ""):
            require_canonical_utc_timestamp(value, field_name)

    if "remind_times" not in payload:
        return
    remind_times = payload["remind_times"]
    if not isinstance(remind_times, list):
        raise ValueError("remind_times must be a list before SQL serialization")
    for index, remind_time in enumerate(remind_times):
        require_canonical_utc_timestamp(remind_time, f"remind_times[{index}]")


def normalize_item_datetimes_for_storage(
    payload: dict[str, Any],
    user_timezone: ZoneInfo,
) -> dict[str, Any]:
    """Normalize every datetime in one item payload to canonical UTC text.

    Naive schedule values are an input contract: they are user wall times (or,
    for events with an explicit ``timezone``, wall times in that event zone).
    Aware values retain their instant.  Date-only business fields are not
    touched.
    """

    normalized = dict(payload)
    item_type = str(normalized.get("type") or "").strip()
    source_zone = user_timezone
    if item_type == "event":
        timezone_name = str(normalized.get("timezone") or user_timezone.key).strip()
        try:
            source_zone = ZoneInfo(timezone_name)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid event timezone") from exc

    for field_name in _COMMON_STORAGE_DATETIME_FIELDS:
        value = normalized.get(field_name)
        if value not in (None, ""):
            normalized[field_name] = normalize_datetime_for_storage(
                value,
                field_name,
                user_timezone,
            )

    for field_name in _ITEM_STORAGE_DATETIME_FIELDS.get(item_type, ()):
        value = normalized.get(field_name)
        if value not in (None, ""):
            normalized[field_name] = normalize_datetime_for_storage(
                value,
                field_name,
                source_zone,
            )

    if "remind_times" in normalized:
        values = normalized.get("remind_times")
        if not isinstance(values, list):
            raise ValueError("remind_times must be a list")
        normalized["remind_times"] = [
            normalize_datetime_for_storage(value, "remind_times", source_zone)
            for value in values
            if value not in (None, "")
        ]
    return normalized


def normalize_event_collection_datetimes_for_storage(
    payload: dict[str, Any],
    user_timezone: ZoneInfo,
) -> dict[str, Any]:
    """Apply the event timestamp contract to a collection header."""

    had_type = "type" in payload
    normalized = normalize_item_datetimes_for_storage(
        {**payload, "type": "event"},
        user_timezone,
    )
    if not had_type:
        normalized.pop("type", None)
    return normalized


async def get_user_local_wall_time(user_id: str, db: Database) -> datetime:
    """在线程池读取用户时区，并返回供日期规则使用的朴素墙钟时间。"""

    current = cast(datetime, await run_sync(now_in_timezone, user_id, db))
    return current.replace(tzinfo=None)


def get_user_now_from_settings(settings: dict[str, Any], current_utc: datetime) -> datetime:
    """Resolve a user's local current time from persisted settings."""
    tz_name = settings.get("timezone", PendoConfig.DEFAULT_TIMEZONE)
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        tz = ZoneInfo(PendoConfig.DEFAULT_TIMEZONE)
    return current_utc.astimezone(tz)


def parse_and_localize(
    dt_str: str, user_id: str | None = None, db: Database | None = None
) -> datetime:
    """解析时间字符串并本地化"""
    if user_id and db:
        tz = TimezoneHelper.get_user_timezone(user_id, db)
    else:
        tz = TimezoneHelper.DEFAULT_TZ
    return TimezoneHelper.parse(dt_str, tz)


def parse_date_optional(date_str: str, now: datetime | None = None) -> str | None:
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


def _parse_ym_range(ym_str: str) -> tuple[datetime, datetime]:
    """解析 YYYY-MM 字符串为该月第一天和最后一天的 datetime"""
    dt = datetime.strptime(ym_str, "%Y-%m")
    start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if dt.month == 12:
        end = dt.replace(year=dt.year + 1, month=1, day=1) - timedelta(seconds=1)
    else:
        end = dt.replace(month=dt.month + 1, day=1) - timedelta(seconds=1)
    return start, end


def _day_range(day: datetime) -> tuple[datetime, datetime]:
    """返回指定日期从 00:00:00 到 23:59:59 的闭区间。"""
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start.replace(hour=23, minute=59, second=59)


def _calendar_keyword_ranges(now: datetime) -> dict[str, tuple[datetime, datetime]]:
    """构造相对当前时间的日、周、月、年关键字范围。"""
    day_start, day_end = _day_range(now)
    week_start = day_start - timedelta(days=now.weekday())
    week_end = week_start + timedelta(days=6, hours=23, minutes=59, seconds=59)
    month_start, month_end = _parse_ym_range(now.strftime("%Y-%m"))
    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    year_end = now.replace(month=12, day=31, hour=23, minute=59, second=59, microsecond=0)
    tomorrow_start = day_start + timedelta(days=1)
    tomorrow_end = day_end + timedelta(days=1)
    return {
        "today": (day_start, day_end),
        "今天": (day_start, day_end),
        "tomorrow": (tomorrow_start, tomorrow_end),
        "明天": (tomorrow_start, tomorrow_end),
        "week": (week_start, week_end),
        "本周": (week_start, week_end),
        "month": (month_start, month_end),
        "本月": (month_start, month_end),
        "year": (year_start, year_end),
        "今年": (year_start, year_end),
    }


def _parse_range_boundary(text: str, *, end: bool) -> datetime:
    """解析日期或月份边界；结束边界扩展到对应周期末尾。"""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        parsed = datetime.strptime(text, "%Y-%m-%d")
        return parsed.replace(hour=23, minute=59, second=59) if end else parsed
    month_start, month_end = _parse_ym_range(text)
    return month_end if end else month_start


def _parse_structured_time_range(text: str) -> tuple[datetime, datetime] | None:
    """解析年份、月份、日期或显式的起止范围。"""
    try:
        if re.fullmatch(r"\d{4}", text):
            year = int(text)
            return datetime(year, 1, 1), datetime(year, 12, 31, 23, 59, 59)
        if re.fullmatch(r"\d{4}-\d{2}", text):
            return _parse_ym_range(text)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return _day_range(datetime.strptime(text, "%Y-%m-%d"))
        if ".." in text:
            start_text, end_text = (part.strip() for part in text.split("..", 1))
            return (
                _parse_range_boundary(start_text, end=False),
                _parse_range_boundary(end_text, end=True),
            )
    except (ValueError, AttributeError):
        return None
    return None


def _parse_time_range_core(
    time_range: str,
    now: datetime | None = None,
    default: str = "today",
    strict: bool = False,
) -> tuple[datetime, datetime]:
    """核心时间范围解析，返回 (start_dt, end_dt)。

    支持: today, tomorrow, week, month, year, 今天, 本周, 本月, 今年,
          lastNd, YYYY, YYYY-MM, YYYY-MM-DD..YYYY-MM-DD
    """
    now = now or datetime.now()
    tr = (time_range or default).strip().lower()
    tr = re.sub(r"\.{3,}", "..", tr)
    today = _day_range(now)

    match = re.fullmatch(r"last(\d+)d", tr)
    if match:
        return now - timedelta(days=int(match.group(1))), now

    keyword_range = _calendar_keyword_ranges(now).get(tr)
    if keyword_range:
        return keyword_range

    structured_range = _parse_structured_time_range(tr)
    if structured_range:
        return structured_range

    if strict:
        raise ValueError(f"无法解析时间范围: {time_range}")
    return today


def parse_event_time_range(
    time_range: str, now: datetime | None = None, strict: bool = False
) -> tuple[str, str]:
    """解析事件时间范围，返回 ISO start/end"""
    start, end = _parse_time_range_core(time_range, now, strict=strict)
    return start.isoformat(), end.isoformat()


def parse_search_date_range(
    range_str: str, now: datetime | None = None, strict: bool = False
) -> tuple[str | None, str | None]:
    """解析搜索日期范围，返回 ISO start/end 或 (None, None)"""
    if not range_str or not range_str.strip():
        return None, None
    try:
        start, end = _parse_time_range_core(range_str, now, strict=strict)
        return start.isoformat(), end.isoformat()
    except (ValueError, AttributeError):
        if strict:
            raise
        return None, None


def parse_diary_range(
    range_str: str, now: datetime | None = None, strict: bool = False
) -> tuple[str, str]:
    """解析日记范围，返回 YYYY-MM-DD start/end"""
    now = now or datetime.now()
    try:
        # 日记的空范围仍表示今天；只有非空且无法识别的输入才回退最近 30 天。
        start, end = _parse_time_range_core(range_str, now, strict=True)
    except (ValueError, AttributeError):
        if strict:
            raise
        return (now - timedelta(days=30)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def parse_delay_time(
    delay_str: str, current_due: str | None = None, now: datetime | None = None
) -> str | None:
    """解析延后时间，返回 ISO 时间字符串"""
    if not delay_str:
        return None

    now = now or TimezoneHelper.now()
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


def parse_hhmm_to_minutes(time_str: str) -> int | None:
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


def parse_remind_times(raw: Any) -> list[str]:
    """把列表或 JSON 列表规范成非空字符串列表。"""
    parsed = raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(parsed, list):
        return []
    return [value for value in parsed if isinstance(value, str) and value]
