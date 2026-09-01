"""Pendo Web 日记统计聚合器的跨模块回归。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from plugins.pendo.services.db import Database
from plugins.pendo.web.analytics import diary_overview as diary_overview_module
from plugins.pendo.web.analytics.diary_overview import build_diary_overview


def _insert(db: Database, owner_id: str, **values: Any) -> None:
    """为当前所有者写入一条测试日记。"""

    db.insert_item({"owner_id": owner_id, "type": "diary", **values})


def test_build_diary_overview_tracks_fill_rate_streaks_and_moods(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """月概览应一次生成活跃度、连续记录、情绪和近期条目。"""

    owner_id = "u-diary-overview"
    _insert(
        db,
        owner_id,
        id="d1",
        title="春天",
        content="今天写了很多很多字。",
        diary_date="2026-03-20",
        mood="happy",
        weather="☀️ 晴",
        created_at="2026-03-20T22:00:00",
        updated_at="2026-03-20T22:00:00",
    )
    _insert(
        db,
        owner_id,
        id="d2",
        title="散步",
        content="今天继续写日记。",
        diary_date="2026-03-21",
        mood="happy",
        created_at="2026-03-21T22:00:00",
        updated_at="2026-03-21T22:00:00",
    )
    _insert(
        db,
        owner_id,
        id="d3",
        title="雨夜",
        content="这一天有些疲惫。",
        diary_date="2026-03-23",
        mood="tired",
        template_id="night_review",
        created_at="2026-03-23T22:00:00",
        updated_at="2026-03-23T22:00:00",
    )
    _insert(
        db,
        owner_id,
        id="d4",
        title="四月第一天",
        content="下一月的记录不应算进三月。",
        diary_date="2026-04-01",
        mood="neutral",
        created_at="2026-04-01T22:00:00",
        updated_at="2026-04-01T22:00:00",
    )

    # 全局 streak 只应读取日期列，不应分页装载完整历史条目。
    monkeypatch.setattr(
        db,
        "get_items",
        lambda *_args, **_kwargs: pytest.fail("日记概览不应装载完整历史条目"),
    )
    result = build_diary_overview(
        db=db,
        owner_id=owner_id,
        year=2026,
        month=3,
        today="2026-03-23",
    )

    assert result["summary"] == {
        "entry_count": 3,
        "range_start": "2026-03-01",
        "range_end": "2026-03-31",
        "range_days": 31,
        "active_days": 3,
        "average_length": round(
            (len("今天写了很多很多字。") + len("今天继续写日记。") + len("这一天有些疲惫。")) / 3,
            1,
        ),
        "fill_rate": 3 / 31,
        "current_streak": 1,
        "longest_streak": 2,
        "period_longest_streak": 2,
        "total_words": len("今天写了很多很多字。今天继续写日记。这一天有些疲惫。"),
        "busiest_day": {
            "date": "2026-03-20",
            "count": 1,
            "words": len("今天写了很多很多字。"),
        },
    }
    assert result["cadence_granularity"] == "day"
    assert result["mood_breakdown"][0] == {"mood": "happy", "count": 2, "share": 2 / 3}
    assert result["template_usage"] == [{"template_id": "night_review", "count": 1}]
    assert result["cadence"][19]["count"] == 1
    assert result["cadence"][19]["words"] == len("今天写了很多很多字。")
    assert result["cadence"][20]["count"] == 1
    assert result["cadence"][22]["count"] == 1
    assert result["recent_entries"][0]["id"] == "d3"
    assert result["recent_entries"][0]["entry_label"] == "22:00"


def test_build_diary_overview_supports_range_based_weekly_cadence(db: Database) -> None:
    """同一年内的中等日期范围应自动聚合为 ISO 周。"""

    owner_id = "u-diary-range"
    for item_id, diary_date, mood, template_id, content in (
        ("d1", "2026-01-05", "calm", "night_review", "第一周的记录。"),
        ("d2", "2026-01-12", "calm", "", "第二周的记录更长一点。"),
        ("d3", "2026-01-27", "happy", "free_write", "第三周没有连写。"),
        ("d4", "2026-02-08", "happy", "", "二月开始继续补记。"),
    ):
        _insert(
            db,
            owner_id,
            id=item_id,
            title=item_id,
            content=content,
            diary_date=diary_date,
            mood=mood,
            template_id=template_id,
            created_at=f"{diary_date}T21:00:00",
            updated_at=f"{diary_date}T21:00:00",
        )

    result = build_diary_overview(
        db=db,
        owner_id=owner_id,
        start_date="2026-01-01",
        end_date="2026-02-15",
        today="2026-02-15",
        cadence_granularity="auto",
    )

    assert result["summary"]["entry_count"] == 4
    assert result["summary"]["range_days"] == 46
    assert result["summary"]["period_longest_streak"] == 1
    assert result["cadence_granularity"] == "week"
    assert result["cadence"][0]["label"] == "2026-W01"
    assert result["cadence"][-1]["label"] == "2026-W07"
    assert sum(item["count"] for item in result["cadence"]) == 4
    assert result["mood_breakdown"][0]["mood"] == "calm"
    assert {item["template_id"] for item in result["template_usage"]} == {
        "free_write",
        "night_review",
    }


def test_build_diary_overview_supports_cross_year_yearly_cadence(db: Database) -> None:
    """跨年范围应按年汇总，并保留没有日记的年份桶。"""

    owner_id = "u-diary-year"
    for item_id, diary_date, content in (
        ("d1", "2024-03-08", "2024 年记录"),
        ("d2", "2025-06-10", "2025 年记录更长一点"),
        ("d3", "2026-02-15", "2026 年记录"),
    ):
        _insert(
            db,
            owner_id,
            id=item_id,
            title=item_id,
            content=content,
            diary_date=diary_date,
            created_at=f"{diary_date}T21:00:00",
            updated_at=f"{diary_date}T21:00:00",
        )

    result = build_diary_overview(
        db=db,
        owner_id=owner_id,
        start_date="2024-01-01",
        end_date="2026-12-31",
        today="2026-12-31",
        cadence_granularity="auto",
    )

    assert result["cadence_granularity"] == "year"
    assert [item["label"] for item in result["cadence"]] == ["2024", "2025", "2026"]
    assert [item["count"] for item in result["cadence"]] == [1, 1, 1]


def test_build_diary_overview_uses_user_clock_when_today_is_omitted(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未显式传 today 时，连续天数必须基于用户时区而非服务器日期。"""

    owner_id = "u-diary-user-clock"
    _insert(
        db,
        owner_id,
        id="d1",
        title="昨天",
        content="仍应算作当前连续记录。",
        diary_date="2030-01-01",
    )

    def user_now(user_id: str, database: Database) -> datetime:
        assert user_id == owner_id
        assert database is db
        return datetime.fromisoformat("2030-01-02T00:30:00-12:00")

    monkeypatch.setattr(diary_overview_module, "now_in_timezone", user_now)
    result = build_diary_overview(db=db, owner_id=owner_id, year=2030, month=1)

    assert result["summary"]["current_streak"] == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"today": "not-a-date"}, "today must be a valid YYYY-MM-DD string"),
        (
            {"cadence_granularity": "quarter"},
            "cadence_granularity must be one of: auto, day, month, week, year",
        ),
    ],
)
def test_build_diary_overview_rejects_invalid_direct_arguments(
    db: Database,
    kwargs: dict[str, Any],
    message: str,
) -> None:
    """服务函数也要校验绕过 FastAPI 类型层的直接调用。"""

    with pytest.raises(ValueError, match=message):
        build_diary_overview(
            db=db,
            owner_id="u-diary-invalid",
            year=2026,
            month=3,
            **kwargs,
        )
