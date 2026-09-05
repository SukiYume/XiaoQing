"""Pendo Web 共享日程时间轴的边界回归。"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from plugins.pendo.models.item import EventItem
from plugins.pendo.web.analytics.event_schedule import (
    build_event_schedule,
    daterange,
    ensure_datetime,
    event_kind,
)


def test_ensure_datetime_accepts_date_space_time_and_offset() -> None:
    """纯日期、空格分隔时间和带偏移时间都应得到一致墙钟值。"""

    zone = ZoneInfo("Asia/Shanghai")
    assert ensure_datetime("2026-04-29", zone).isoformat() == "2026-04-29T00:00:00"
    assert ensure_datetime("2026-04-29", zone, is_end=True).isoformat() == ("2026-04-29T23:59:59")
    assert ensure_datetime("2026-04-29 10:30:00", zone).isoformat() == ("2026-04-29T10:30:00")
    assert ensure_datetime("2026-04-29T00:30:00+00:00", zone).isoformat() == ("2026-04-29T08:30:00")


def test_ensure_datetime_rejects_invalid_explicit_value() -> None:
    """显式损坏时间不能被悄悄解释为缺失。"""

    with pytest.raises(ValueError):
        ensure_datetime("not-a-datetime", ZoneInfo("Asia/Shanghai"))


def test_ensure_datetime_uses_requested_user_timezone_not_default_timezone() -> None:
    """同一 UTC 时刻必须按当前用户时区换日，不能固定按上海时间展示。"""

    parsed = ensure_datetime(
        "2026-04-29T00:30:00+00:00",
        ZoneInfo("America/Los_Angeles"),
    )

    assert parsed is not None
    assert parsed.isoformat() == "2026-04-28T17:30:00"


def test_daterange_handles_closed_and_reverse_ranges() -> None:
    """自然日生成器包含首尾，反向范围安全返回空。"""

    assert daterange(date(2026, 3, 30), date(2026, 4, 1)) == [
        "2026-03-30",
        "2026-03-31",
        "2026-04-01",
    ]
    assert daterange(date(2026, 4, 1), date(2026, 3, 30)) == []


def test_build_event_schedule_expands_multi_day_event_once_per_day() -> None:
    """跨天日程应保留首日时间、中间日标记和末日结束时间。"""

    event = EventItem(
        title                 = "跨天出差",
        category              = "工作",
        location              = "上海",
        start_time            = "2026-03-10T09:00:00",
        end_time              = "2026-03-12T03:00:00",
        event_collection_kind = "multi_node",
    )

    result = build_event_schedule(
        event,
        date(2026, 3, 10),
        date(2026, 3, 12),
        ZoneInfo("Asia/Shanghai"),
    )

    assert event_kind(event) == "multi_node"
    assert result["display_days"] == ["2026-03-10", "2026-03-11", "2026-03-12"]
    assert result["day_entries"]["2026-03-10"][0]["time_label"] == "09:00"
    assert result["day_entries"]["2026-03-11"][0]["time_label"] == "跨天"
    assert result["day_entries"]["2026-03-12"][0]["time_label"] == "至 03:00"
    assert result["start_epoch_ms"] == round(
        datetime(2026, 3, 10, 9, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
    )
    assert result["end_epoch_ms"] == round(
        datetime(2026, 3, 12, 3, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
    )


def test_build_event_schedule_handles_missing_start_time() -> None:
    """缺少开始时间时返回稳定空时间轴。"""

    result = build_event_schedule(
        EventItem(title="待定"),
        date(2026, 3, 1),
        date(2026, 3, 31),
        ZoneInfo("Asia/Shanghai"),
    )

    assert result == {
        "kind": "single",
        "display_days": [],
        "day_entries": {},
        "time_summary": "未设置时间",
        "start_epoch_ms": None,
        "end_epoch_ms": None,
    }
