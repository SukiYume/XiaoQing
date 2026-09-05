"""为 Pendo Web 笔记页和 Widget 生成新增趋势与分类概览。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal, cast

from ...services.db import Database
from ...utils.identifiers import public_id
from ...utils.time_utils import TimezoneHelper, now_in_timezone
from ..utils import parse_iso_date

JsonObject         = dict[str, Any]
CadenceGranularity = Literal["day", "week", "month", "year"]

_NOTE_CATEGORY_SQL    = "COALESCE(NULLIF(TRIM(i.category), ''), '未分类')"
_NOTE_CREATED_DAY_SQL = "pendo_local_date(i.created_at, :timezone)"
_NOTE_TAGS_SQL        = "CASE WHEN json_valid(i.tags) THEN i.tags ELSE '[]' END"


@dataclass(frozen=True, slots=True)
class _NotePeriod:
    """请求范围及其实际统计尾端。"""

    today: date
    range_start: date | None
    range_end: date | None
    cadence_start: date
    cadence_end: date
    granularity: CadenceGranularity


def _parse_argument_day(value: str | None, field_name: str) -> date | None:
    """解析可选日期参数；显式非法值不能静默退回默认日期。"""

    if value is None or not value.strip():
        return None
    parsed = parse_iso_date(value)
    if parsed is None:
        raise ValueError(f"{field_name} must be a valid ISO date")
    return cast(date, parsed)


def _resolve_period(
    db: Database,
    owner_id: str,
    today: str | None,
    start_date: str | None,
    end_date: str | None,
) -> _NotePeriod:
    """校验请求范围，并为当前周期裁掉尚未发生的未来日期。"""

    today_day   = _parse_argument_day(today, "today") or now_in_timezone(owner_id, db).date()
    range_start = _parse_argument_day(start_date, "start_date")
    range_end   = _parse_argument_day(end_date, "end_date")
    if (range_start is None) != (range_end is None):
        raise ValueError("start_date and end_date must be provided together")
    if range_start is None or range_end is None:
        return _NotePeriod(
            today       = today_day,
            range_start = None,
            range_end   = None,
            cadence_start=today_day - timedelta(days=13),
            cadence_end = today_day,
            granularity = "day",
        )
    if range_start > range_end:
        raise ValueError("start_date must not be after end_date")

    cadence_end = today_day if range_start <= today_day <= range_end else range_end
    span_days   = (range_end - range_start).days
    if range_start.year != range_end.year:
        granularity: CadenceGranularity = "year"
    elif span_days > 62:
        granularity = "month"
    elif span_days > 7:
        granularity = "week"
    else:
        granularity = "day"
    return _NotePeriod(
        today         = today_day,
        range_start   = range_start,
        range_end     = range_end,
        cadence_start = range_start,
        cadence_end   = cadence_end,
        granularity   = granularity,
    )


def _cadence_key(day: date, granularity: CadenceGranularity) -> str:
    """把新增日期映射到日、ISO 周、月或年桶。"""

    if granularity == "year":
        return str(day.year)
    if granularity == "month":
        return day.strftime("%Y-%m")
    if granularity == "week":
        return (day - timedelta(days=day.weekday())).isoformat()
    return day.isoformat()


def _cadence_slots(
    start_day: date,
    end_day: date,
    granularity: CadenceGranularity,
) -> list[tuple[str, JsonObject]]:
    """生成图表轴槽；即使某个周期没有新增也保留零值位置。"""

    slots: list[tuple[str, JsonObject]] = []
    if granularity == "year":
        for year in range(start_day.year, end_day.year + 1):
            key = str(year)
            slots.append((key, {"date": f"{year}-01-01", "label": key, "year": key}))
        return slots

    if granularity == "month":
        cursor = start_day.replace(day=1)
        final = end_day.replace(day=1)
        while cursor <= final:
            key = cursor.strftime("%Y-%m")
            slots.append((key, {"date": cursor.isoformat(), "label": key, "month": key}))
            cursor = date(cursor.year + cursor.month // 12, cursor.month % 12 + 1, 1)
        return slots

    if granularity == "week":
        cursor = start_day - timedelta(days=start_day.weekday())
        while cursor <= end_day:
            iso = cursor.isocalendar()
            key = cursor.isoformat()
            slots.append(
                (
                    key,
                    {
                        "date": key,
                        "label": f"{cursor.month}/{cursor.day}",
                        "week": f"{iso.year}-W{iso.week:02d}",
                    },
                )
            )
            cursor += timedelta(days=7)
        return slots

    for offset in range((end_day - start_day).days + 1):
        current = start_day + timedelta(days=offset)
        key = current.isoformat()
        slots.append((key, {"date": key, "label": f"{current.month}/{current.day}"}))
    return slots


def _build_cadence(
    counter: Counter[str],
    start_day: date,
    end_day: date,
    granularity: CadenceGranularity,
) -> list[JsonObject]:
    """把数据库返回的创建日计数合并到完整轴槽。"""

    return [
        {**payload, "count": counter.get(key, 0)}
        for key, payload in _cadence_slots(start_day, end_day, granularity)
    ]


def _note_where(
    period: _NotePeriod,
    owner_id: str,
    category: str | None,
    tag_query: str,
) -> tuple[str, dict[str, Any]]:
    """生成 overview 各条聚合查询共用的笔记过滤条件。"""

    where                  = ["i.owner_id = :owner_id", "i.type = 'note'", "i.deleted = 0"]
    params: dict[str, Any] = {"owner_id": owner_id}
    if period.range_start is not None:
        where.extend(
            [
                f"{_NOTE_CREATED_DAY_SQL} >= :range_start",
                f"{_NOTE_CREATED_DAY_SQL} <= :range_end",
            ]
        )
        params.update(
            range_start = period.cadence_start.isoformat(),
            range_end   = period.cadence_end.isoformat(),
        )
    if category is not None:
        where.append(f"{_NOTE_CATEGORY_SQL} = :category")
        params["category"] = category
    if tag_query:
        where.append(
            f"""
            EXISTS (
              SELECT 1 FROM json_each({_NOTE_TAGS_SQL}) AS requested_tag
              WHERE pendo_casefold(TRIM(CAST(requested_tag.value AS TEXT))) = :tag_query
            )
            """
        )
        params["tag_query"] = tag_query
    return " AND ".join(where), params


def build_notes_overview(
    db: Database,
    owner_id: str,
    today: str | None      = None,
    start_date: str | None = None,
    end_date: str | None   = None,
    category: str | None   = None,
    tags: str | None       = None,
) -> JsonObject:
    """生成指定范围内的笔记数量、分类、标签、趋势和近期预览。"""

    period              = _resolve_period(db, owner_id, today, start_date, end_date)
    normalized_category = (category or "").strip() or None
    tag_query           = (tags or "").strip().casefold()
    where, params = _note_where(period, owner_id, normalized_category, tag_query)
    params["timezone"] = TimezoneHelper.get_user_timezone(owner_id, db).key
    week_start = period.today - timedelta(days=6)
    conn    = db.get_connection()
    summary = conn.execute(
        f"""
        SELECT COUNT(*) AS total_count,
               COALESCE(SUM(LENGTH(TRIM(COALESCE(i.content, '')))), 0) AS total_length,
               COALESCE(SUM(CASE WHEN json_array_length({_NOTE_TAGS_SQL}) > 0
                                 THEN 1 ELSE 0 END), 0) AS tagged_count,
               COALESCE(SUM(CASE WHEN {_NOTE_CREATED_DAY_SQL} BETWEEN :week_start AND :today
                                 THEN 1 ELSE 0 END), 0) AS week_new_count
        FROM items AS i
        WHERE {where}
        """,
        {**params, "week_start": week_start.isoformat(), "today": period.today.isoformat()},
    ).fetchone()
    total_count  = int(summary["total_count"] or 0)
    total_length = int(summary["total_length"] or 0)
    tagged_count = int(summary["tagged_count"] or 0)

    category_rows = conn.execute(
        f"""
        SELECT {_NOTE_CATEGORY_SQL} AS category_name, COUNT(*) AS item_count
        FROM items AS i
        WHERE {where}
        GROUP BY category_name
        ORDER BY item_count DESC, category_name
        LIMIT 6
        """,
        params,
    ).fetchall()
    categories = [
        {
            "category": str(row["category_name"]),
            "count": int(row["item_count"]),
            "share": int(row["item_count"]) / total_count if total_count else 0,
        }
        for row in category_rows
    ]

    tag_rows = conn.execute(
        f"""
        SELECT MIN(TRIM(CAST(note_tag.value AS TEXT))) AS tag_label,
               COUNT(DISTINCT i.id) AS item_count
        FROM items AS i
        JOIN json_each({_NOTE_TAGS_SQL}) AS note_tag
        WHERE {where}
          AND note_tag.type IN ('text', 'integer', 'real')
          AND TRIM(CAST(note_tag.value AS TEXT)) != ''
        GROUP BY pendo_casefold(TRIM(CAST(note_tag.value AS TEXT)))
        ORDER BY item_count DESC, pendo_casefold(tag_label)
        LIMIT 8
        """,
        params,
    ).fetchall()
    hot_tags = [{"tag": str(row["tag_label"]), "count": int(row["item_count"])} for row in tag_rows]

    cadence_rows = conn.execute(
        f"""
        SELECT {_NOTE_CREATED_DAY_SQL} AS created_day, COUNT(*) AS item_count
        FROM items AS i
        WHERE {where}
          AND {_NOTE_CREATED_DAY_SQL} BETWEEN :cadence_start AND :cadence_end
        GROUP BY created_day
        """,
        {
            **params,
            "cadence_start": period.cadence_start.isoformat(),
            "cadence_end": period.cadence_end.isoformat(),
        },
    ).fetchall()
    cadence_counter: Counter[str] = Counter()
    for row in cadence_rows:
        created_day = parse_iso_date(str(row["created_day"] or ""))
        if created_day is not None:
            cadence_counter[_cadence_key(created_day, period.granularity)] += int(row["item_count"])

    cadence = _build_cadence(
        cadence_counter,
        period.cadence_start,
        period.cadence_end,
        period.granularity,
    )
    recent_notes = [
        {**dict(row), "display_id": public_id(row["id"])}
        for row in conn.execute(
            f"""
            SELECT i.id, i.title, i.content, {_NOTE_CATEGORY_SQL} AS category,
                   i.created_at, i.updated_at
            FROM items AS i
            WHERE {where}
            ORDER BY pendo_utc_epoch(
                       COALESCE(NULLIF(i.updated_at, ''), i.created_at), :timezone
                     ) DESC,
                     i.id DESC
            LIMIT 6
            """,
            params,
        ).fetchall()
    ]
    all_categories = [
        str(row["category_name"])
        for row in conn.execute(
            f"""
            SELECT DISTINCT {_NOTE_CATEGORY_SQL} AS category_name
            FROM items AS i
            WHERE i.owner_id = ? AND i.type = 'note' AND i.deleted = 0
            ORDER BY category_name
            """,
            (owner_id,),
        ).fetchall()
    ]

    return {
        "summary": {
            "total_count": total_count,
            "week_new_count": int(summary["week_new_count"] or 0),
            "average_length": round(total_length / total_count, 1) if total_count else 0,
            "tagged_rate": round(tagged_count / total_count, 4) if total_count else 0,
            "range_start": period.range_start.isoformat() if period.range_start else None,
            "range_end": period.range_end.isoformat() if period.range_end else None,
        },
        "categories": categories,
        "hot_tags": hot_tags,
        "cadence": cadence,
        "cadence_granularity": period.granularity,
        "recent_notes": recent_notes,
        "all_categories": all_categories,
    }


def build_notes_widget_overview(
    db: Database,
    owner_id: str,
    today: str | None = None,
    *,
    limit: int = 5,
) -> JsonObject:
    """用计数与有界预览生成 Widget 笔记摘要。"""

    if limit <= 0:
        raise ValueError("limit must be positive")
    period = _resolve_period(db, owner_id, today, None, None)
    week_start = period.today - timedelta(days=6)
    timezone_name = TimezoneHelper.get_user_timezone(owner_id, db).key
    conn          = db.get_connection()
    summary       = conn.execute(
        f"""
        SELECT COUNT(*) AS total_count,
               COALESCE(SUM(CASE WHEN {_NOTE_CREATED_DAY_SQL} BETWEEN :week_start AND :today
                                 THEN 1 ELSE 0 END), 0) AS week_new_count
        FROM items AS i
        WHERE i.owner_id = :owner_id AND i.type = 'note' AND i.deleted = 0
        """,
        {
            "week_start": week_start.isoformat(),
            "today": period.today.isoformat(),
            "owner_id": owner_id,
            "timezone": timezone_name,
        },
    ).fetchone()
    recent_notes = [
        {**dict(row), "display_id": public_id(row["id"])}
        for row in conn.execute(
            f"""
            SELECT i.id, i.title, i.content, {_NOTE_CATEGORY_SQL} AS category
            FROM items AS i
            WHERE i.owner_id = :owner_id AND i.type = 'note' AND i.deleted = 0
            ORDER BY pendo_utc_epoch(
                       COALESCE(NULLIF(i.updated_at, ''), i.created_at), :timezone
                     ) DESC,
                     i.id DESC
            LIMIT :limit
            """,
            {"owner_id": owner_id, "timezone": timezone_name, "limit": limit},
        ).fetchall()
    ]
    return {
        "summary": {
            "total_count": int(summary["total_count"] or 0),
            "week_new_count": int(summary["week_new_count"] or 0),
        },
        "recent_notes": recent_notes,
    }
