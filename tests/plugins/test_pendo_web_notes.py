"""Pendo Web 笔记概览聚合器的跨模块回归。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from plugins.pendo.services.db import Database
from plugins.pendo.web.analytics import notes_overview as notes_overview_module
from plugins.pendo.web.analytics.notes_overview import build_notes_overview


def _insert(db: Database, owner_id: str, **values: Any) -> None:
    """为当前所有者写入一条测试笔记。"""

    db.insert_item({"owner_id": owner_id, "type": "note", **values})


def test_build_notes_overview_tracks_categories_tags_and_creation_cadence(
    db: Database,
) -> None:
    """概览按创建日统计新增，并只公开 Widget 需要的近期字段。"""

    owner_id = "u-note-overview"
    _insert(
        db,
        owner_id,
        id="n1",
        title="项目复盘",
        content="这是第一条笔记",
        category="工作",
        tags=["复盘", "工作", "复盘"],
        created_at="2026-03-24T10:00:00",
        updated_at="2026-03-25T08:00:00",
    )
    _insert(
        db,
        owner_id,
        id="n2",
        title="阅读摘录",
        content="第二条笔记更长一些",
        category="学习",
        tags=["阅读"],
        created_at="2026-03-26T09:00:00",
        updated_at="2026-03-26T09:30:00",
    )

    result = build_notes_overview(db=db, owner_id=owner_id, today="2026-03-26")
    cadence = {item["date"]: item["count"] for item in result["cadence"]}

    assert result["summary"]["total_count"] == 2
    assert result["summary"]["week_new_count"] == 2
    assert result["summary"]["tagged_rate"] == 1
    assert {item["category"] for item in result["categories"]} == {"工作", "学习"}
    assert {item["tag"]: item["count"] for item in result["hot_tags"]} == {
        "复盘": 1,
        "工作": 1,
        "阅读": 1,
    }
    assert cadence["2026-03-24"] == 1
    assert cadence["2026-03-25"] == 0
    assert cadence["2026-03-26"] == 1
    assert len(result["cadence"]) == 14
    assert result["recent_notes"][0] == {
        "id": "n2",
        "title": "阅读摘录",
        "content": "第二条笔记更长一些",
        "category": "学习",
        "created_at": "2026-03-26T01:00:00+00:00",
        "updated_at": "2026-03-26T01:30:00+00:00",
    }
    assert result["all_categories"] == ["学习", "工作"]


def test_build_notes_overview_clips_current_period_cadence_to_today(db: Database) -> None:
    """包含今天的范围只展示已经发生的周桶，同时保留请求原始边界。"""

    owner_id = "u-note-current-period"
    _insert(
        db,
        owner_id,
        id="n1",
        title="本月笔记",
        content="本月内容",
        category="工作",
        created_at="2026-04-06T09:00:00",
        updated_at="2026-04-06T09:00:00",
    )

    result = build_notes_overview(
        db=db,
        owner_id=owner_id,
        today="2026-04-08",
        start_date="2026-04-01",
        end_date="2026-04-30",
    )

    assert result["summary"]["range_start"] == "2026-04-01"
    assert result["summary"]["range_end"] == "2026-04-30"
    assert result["cadence_granularity"] == "week"
    assert [item["date"] for item in result["cadence"]] == ["2026-03-30", "2026-04-06"]
    assert [item["count"] for item in result["cadence"]] == [0, 1]


def test_build_notes_overview_filters_tags_by_trimmed_exact_match(db: Database) -> None:
    """标签筛选忽略大小写和外围空白，但不做子串匹配。"""

    owner_id = "u-note-overview-exact-tag"
    _insert(
        db,
        owner_id,
        id="n_work",
        title="工作",
        content="工作内容",
        category="工作",
        tags=["工作"],
        created_at="2026-03-24T10:00:00",
        updated_at="2026-03-24T10:00:00",
    )
    _insert(
        db,
        owner_id,
        id="n_workflow",
        title="工作流",
        content="工作流内容",
        category="工作",
        tags=["工作流"],
        created_at="2026-03-25T10:00:00",
        updated_at="2026-03-25T10:00:00",
    )

    result = build_notes_overview(
        db=db,
        owner_id=owner_id,
        today="2026-03-26",
        tags="  工作  ",
    )

    assert result["summary"]["total_count"] == 1
    assert result["recent_notes"][0]["id"] == "n_work"


def test_build_notes_overview_preserves_unicode_casefold_tag_matching(db: Database) -> None:
    owner_id = "u-note-unicode-tag"
    _insert(
        db,
        owner_id,
        id="unicode-tag",
        title="Unicode 标签",
        content="正文",
        tags=["legacy"],
        created_at="2026-03-26T09:00:00",
        updated_at="2026-03-26T09:00:00",
    )
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE items SET tags = ? WHERE id = ?",
            ('["Straße"]', "unicode-tag"),
        )

    result = build_notes_overview(
        db=db,
        owner_id=owner_id,
        today="2026-03-26",
        tags="STRASSE",
    )

    assert result["summary"]["total_count"] == 1
    assert result["recent_notes"][0]["id"] == "unicode-tag"


def test_build_notes_overview_uses_user_clock_and_excludes_future_week_new(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """省略 today 时使用用户时区，未来创建时间不能计入近七日。"""

    owner_id = "u-note-user-clock"
    for item_id, created_at in (
        ("past", "2030-01-01T10:00:00"),
        ("future", "2030-01-03T10:00:00"),
    ):
        _insert(
            db,
            owner_id,
            id=item_id,
            title=item_id,
            content=item_id,
            created_at=created_at,
            updated_at=created_at,
        )

    def user_now(user_id: str, database: Database) -> datetime:
        assert user_id == owner_id
        assert database is db
        return datetime.fromisoformat("2030-01-02T00:30:00-12:00")

    monkeypatch.setattr(notes_overview_module, "now_in_timezone", user_now)
    result = build_notes_overview(db=db, owner_id=owner_id)

    assert result["summary"]["total_count"] == 2
    assert result["summary"]["week_new_count"] == 1
    assert result["cadence"][-1]["date"] == "2030-01-02"
    assert sum(item["count"] for item in result["cadence"]) == 1


def test_build_notes_overview_reads_all_pages(db: Database) -> None:
    """总数、分类和趋势不能在首个 500 条批次处截断。"""

    owner_id = "u-note-pagination"
    for index in range(505):
        _insert(
            db,
            owner_id,
            id=f"note-{index:03d}",
            title=f"笔记 {index}",
            content="内容",
            category="批量",
            created_at="2026-03-26T09:00:00",
            updated_at=f"2026-03-26T09:{index % 60:02d}:00",
        )

    result = build_notes_overview(db=db, owner_id=owner_id, today="2026-03-26")

    assert result["summary"]["total_count"] == 505
    assert result["categories"] == [{"category": "批量", "count": 505, "share": 1}]
    assert result["cadence"][-1]["count"] == 505
    assert len(result["recent_notes"]) == 6


def test_notes_overview_groups_aware_creation_in_user_timezone(db: Database) -> None:
    owner_id = "u-note-aware-user-zone"
    db.update_user_settings(owner_id, {"timezone": "Asia/Shanghai"})
    _insert(
        db,
        owner_id,
        id="aware-note",
        title="跨 UTC 日期",
        content="内容",
        created_at="2026-03-30T23:30:00+00:00",
        updated_at="2026-03-30T23:30:00+00:00",
    )

    result = build_notes_overview(
        db=db,
        owner_id=owner_id,
        today="2026-03-31",
        start_date="2026-03-31",
        end_date="2026-03-31",
    )

    assert result["summary"]["week_new_count"] == 1
    assert result["cadence"] == [{"date": "2026-03-31", "label": "3/31", "count": 1}]


def test_build_notes_overview_uses_aggregates_instead_of_item_materialization(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id = "u-note-sql-overview"
    _insert(
        db,
        owner_id,
        id="sql-note",
        title="SQL 聚合",
        content="正文",
        created_at="2026-03-26T09:00:00",
        updated_at="2026-03-26T09:00:00",
    )
    monkeypatch.setattr(
        db,
        "get_items",
        lambda *_args, **_kwargs: pytest.fail("overview must not materialize item pages"),
    )
    monkeypatch.setattr(
        db,
        "get_all_items",
        lambda *_args, **_kwargs: pytest.fail("overview must not load all notes"),
    )

    result = build_notes_overview(db=db, owner_id=owner_id, today="2026-03-26")

    assert result["summary"]["total_count"] == 1
    assert result["recent_notes"][0]["id"] == "sql-note"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"today": "not-a-date"}, "today must be a valid ISO date"),
        (
            {"start_date": "2026-03-01"},
            "start_date and end_date must be provided together",
        ),
        (
            {"start_date": "2026-03-31", "end_date": "2026-03-01"},
            "start_date must not be after end_date",
        ),
    ],
)
def test_build_notes_overview_rejects_invalid_direct_arguments(
    db: Database,
    kwargs: dict[str, Any],
    message: str,
) -> None:
    """服务函数必须校验绕过 FastAPI 参数层的直接调用。"""

    with pytest.raises(ValueError, match=message):
        build_notes_overview(db=db, owner_id="u-note-invalid", **kwargs)
