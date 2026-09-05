"""为 Pendo Web 日记页生成紧凑、可直接渲染的统计数据。"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, cast

from ...models.item import DiaryItem, ItemType
from ...services.db import Database
from ...utils.identifiers import public_id
from ...utils.time_utils import TimezoneHelper, now_in_timezone

_ALLOWED_CADENCE_GRANULARITIES = frozenset({"day", "week", "month", "year", "auto"})


def _parse_day(value: str | None) -> date | None:
    """解析持久化日期；损坏的历史值由调用方决定忽略或报错。"""

    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _resolve_period(
    year: int | None       = None,
    month: int | None      = None,
    start_date: str | None = None,
    end_date: str | None   = None,
) -> tuple[date, date]:
    """把自然月或显式日期范围归一为闭区间。"""

    if start_date is not None or end_date is not None:
        start = _parse_day(start_date)
        end   = _parse_day(end_date)
        if start is None or end is None:
            raise ValueError("start_date and end_date must be valid YYYY-MM-DD strings")
        if start > end:
            raise ValueError("start_date must not be after end_date")
        return start, end

    if year is None or month is None:
        raise ValueError("year and month are required when start_date/end_date are not provided")
    try:
        start = date(year, month, 1)
    except ValueError as exc:
        raise ValueError("year and month must form a valid calendar month") from exc
    return start, date(year, month, monthrange(year, month)[1])


def _load_diary_days(db: Database, owner_id: str) -> set[date]:
    """只读取历史日记日期，避免为连续记录统计反序列化完整条目。"""

    rows = db.get_connection().execute(
        """
        SELECT DISTINCT diary_date
        FROM items
        WHERE owner_id = ? AND type = ? AND deleted = 0 AND diary_date IS NOT NULL
        """,
        (owner_id, ItemType.DIARY.value),
    )
    days: set[date] = set()
    for row in rows:
        value  = row[0]
        parsed = _parse_day(value if isinstance(value, str) else None)
        if parsed is not None:
            days.add(parsed)
    return days


def _entry_sort_key(item: DiaryItem) -> str:
    """优先按日记发生时间排序，旧数据再回退到通用时间字段。"""

    return item.entry_time or item.created_at or item.updated_at or item.diary_date or ""


def _entry_label(item: DiaryItem, user_timezone: tzinfo) -> str:
    """提取列表使用的 HH:MM 标签；没有具体时间的条目标记为全天。"""

    raw = _entry_sort_key(item)
    if len(raw) < 16 or raw[10] not in {"T", " "}:
        return "全天"
    try:
        return cast(datetime, TimezoneHelper.parse(raw, user_timezone)).strftime("%H:%M")
    except (OverflowError, TypeError, ValueError):
        return "全天"


def _current_streak(days: set[date], today: date) -> int:
    """计算截至今天的连续天数；今天未写时允许从昨天延续。"""

    cursor = today if today in days else today - timedelta(days=1)
    streak = 0
    while cursor in days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def _longest_streak(days: set[date]) -> int:
    """计算一组日期中的最长连续区间。"""

    longest               = 0
    current               = 0
    previous: date | None = None
    for current_day in sorted(days):
        current = current + 1 if previous and current_day - previous == timedelta(days=1) else 1
        longest  = max(longest, current)
        previous = current_day
    return longest


def _resolve_cadence_granularity(start: date, end: date, granularity: str) -> str:
    """校验粒度，并为自动模式选择不会过密的时间桶。"""

    if granularity not in _ALLOWED_CADENCE_GRANULARITIES:
        allowed = ", ".join(sorted(_ALLOWED_CADENCE_GRANULARITIES))
        raise ValueError(f"cadence_granularity must be one of: {allowed}")
    if granularity != "auto":
        return granularity

    span_days = (end - start).days + 1
    if start.year != end.year:
        return "year"
    if span_days > 62:
        return "month"
    if span_days > 7:
        return "week"
    return "day"


def _build_cadence(
    start: date,
    end: date,
    day_counts: dict[str, int],
    day_words: dict[str, int],
    cadence_granularity: str,
) -> tuple[str, list[dict[str, Any]]]:
    """按指定粒度补齐范围内的空桶并汇总篇数和字数。"""

    resolved = _resolve_cadence_granularity(start, end, cadence_granularity)
    buckets: dict[str, dict[str, Any]] = {}
    ordered_keys: list[str] = []

    for offset in range((end - start).days + 1):
        current = start + timedelta(days=offset)
        date_key = current.isoformat()
        if resolved == "year":
            bucket_key = current.strftime("%Y")
            label      = bucket_key
        elif resolved == "month":
            bucket_key = current.strftime("%Y-%m")
            label      = bucket_key
        elif resolved == "week":
            iso        = current.isocalendar()
            bucket_key = f"{iso.year}-W{iso.week:02d}"
            label      = bucket_key
        else:
            bucket_key = date_key
            label      = (
                str(current.day)
                if start.year == end.year and start.month == end.month
                else f"{current.month}/{current.day}"
            )

        if bucket_key not in buckets:
            buckets[bucket_key] = {
                "bucket": bucket_key,
                "date": date_key,
                "label": label,
                "count": 0,
                "words": 0,
            }
            ordered_keys.append(bucket_key)
        buckets[bucket_key]["count"] += day_counts.get(date_key, 0)
        buckets[bucket_key]["words"] += day_words.get(date_key, 0)

    return resolved, [buckets[key] for key in ordered_keys]


def build_diary_overview(
    db: Database,
    owner_id: str,
    year: int | None         = None,
    month: int | None        = None,
    start_date: str | None   = None,
    end_date: str | None     = None,
    today: str | None        = None,
    cadence_granularity: str = "day",
) -> dict[str, Any]:
    """生成指定自然月或显式日期范围的日记概览。"""

    start, end = _resolve_period(year=year, month=month, start_date=start_date, end_date=end_date)
    if today is None:
        today_day = now_in_timezone(owner_id, db).date()
    else:
        parsed_today = _parse_day(today)
        if parsed_today is None:
            raise ValueError("today must be a valid YYYY-MM-DD string")
        today_day = parsed_today

    queried_items = db.query_items_by_date_range(
        owner_id,
        ItemType.DIARY.value,
        "diary_date",
        start.isoformat(),
        end.isoformat(),
    )
    window_items = sorted(
        (item for item in queried_items if isinstance(item, DiaryItem)),
        key     = _entry_sort_key,
        reverse = True,
    )
    user_timezone = TimezoneHelper.get_user_timezone(owner_id, db)

    day_counts: dict[str, int]      = {}
    day_words: dict[str, int]       = {}
    mood_counts: dict[str, int]     = {}
    template_counts: dict[str, int] = {}
    period_days: set[date]          = set()
    total_words                     = 0

    for item in window_items:
        diary_day = _parse_day(item.diary_date)
        if diary_day is None:
            continue
        date_key = diary_day.isoformat()
        period_days.add(diary_day)
        day_counts[date_key] = day_counts.get(date_key, 0) + 1

        word_count = len(item.content.strip())
        total_words += word_count
        day_words[date_key] = day_words.get(date_key, 0) + word_count

        mood = (item.mood or "").strip()
        if mood:
            mood_counts[mood] = mood_counts.get(mood, 0) + 1

        template_id = (item.template_id or "").strip()
        if template_id:
            template_counts[template_id] = template_counts.get(template_id, 0) + 1

    all_days              = _load_diary_days(db, owner_id)
    current_streak        = _current_streak(all_days, today_day)
    longest_streak        = _longest_streak(all_days)
    period_longest_streak = _longest_streak(period_days)
    total_days            = (end - start).days + 1
    active_days           = len(period_days)

    resolved_cadence_granularity, cadence = _build_cadence(
        start               = start,
        end                 = end,
        day_counts          = day_counts,
        day_words           = day_words,
        cadence_granularity = cadence_granularity,
    )

    mood_breakdown = [
        {
            "mood": mood,
            "count": count,
            "share": count / len(window_items) if window_items else 0,
        }
        for mood, count in sorted(mood_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    template_usage = [
        {"template_id": template_id, "count": count}
        for template_id, count in sorted(
            template_counts.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    recent_entries = [
        {
            "id": item.id,
            "display_id": public_id(item.id),
            "title": item.title,
            "diary_date": item.diary_date,
            "entry_time": item.entry_time,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "entry_label": _entry_label(item, user_timezone),
            "mood": item.mood,
            "mood_score": item.mood_score,
            "weather": item.weather,
            "is_favorite": item.is_favorite,
            "content_preview": item.content.strip()[:80],
            "word_count": len(item.content.strip()),
        }
        for item in window_items[:6]
    ]

    busiest_day = None
    if day_words:
        # 字数和篇数相同时选择较新的日期，保证结果稳定且更贴近近期回顾。
        busiest_date = max(
            day_words,
            key=lambda day: (day_words[day], day_counts.get(day, 0), day),
        )
        busiest_day = {
            "date": busiest_date,
            "count": day_counts.get(busiest_date, 0),
            "words": day_words[busiest_date],
        }

    return {
        "summary": {
            "entry_count": len(window_items),
            "range_start": start.isoformat(),
            "range_end": end.isoformat(),
            "range_days": total_days,
            "active_days": active_days,
            "average_length": round(total_words / len(window_items), 1) if window_items else 0,
            "fill_rate": active_days / total_days,
            "current_streak": current_streak,
            "longest_streak": longest_streak,
            "period_longest_streak": period_longest_streak,
            "total_words": total_words,
            "busiest_day": busiest_day,
        },
        "cadence_granularity": resolved_cadence_granularity,
        "mood_breakdown": mood_breakdown,
        "cadence": cadence,
        "template_usage": template_usage,
        "recent_entries": recent_entries,
    }
