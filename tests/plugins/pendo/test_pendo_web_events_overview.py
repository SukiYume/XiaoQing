"""Pendo Web 日程概览的最小响应与批量查询。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from plugins.pendo.services.db import Database
from plugins.pendo.web.analytics.events_overview import (
    build_event_detail,
    build_events_overview,
)
from tests.helpers.sqlite_test_support import trace_sql_statements


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(str(tmp_path / "pendo.db"))
    yield database
    database.cleanup()


def test_overview_and_detail_only_publish_fields_used_by_the_page(db: Database) -> None:
    """概览与详情不得泄漏所有者、上下文或服务端中间字段。"""

    db.insert_item(
        {
            "id"          : "minimal-event",
            "owner_id"    : "owner-a",
            "type"        : "event",
            "title"       : "发布复盘",
            "content"     : "可搜索但不公开的正文",
            "category"    : "工作",
            "start_time"  : "2030-01-03T10:00:00",
            "end_time"    : "2030-01-03T11:00:00",
            "location"    : "会议室",
            "notes"       : "详情备注",
            "remind_times": ["2030-01-03T09:30:00"],
            "context"     : {"group_id": "private-group"},
            "ai_meta"     : {"summary": "private-summary"},
        }
    )
    db.log_reminder("minimal-event", "2030-01-03T01:30:00+00:00", sent=True)

    overview = build_events_overview(
        db,
        "owner-a",
        start_date = "2030-01-01",
        end_date   = "2030-01-31",
        keyword    = "可搜索",
    )

    assert set(overview) == {
        "summary",
        "categories",
        "calendar_days",
        "timeline_days",
        "events",
    }
    assert set(overview["summary"]) == {
        "event_count",
        "multi_node_count",
        "reminder_count",
    }
    assert overview["events"] == [
        {
            "id"        : "minimal-event",
            "display_id": "minimal-event",
            "title"     : "发布复盘",
            "category"  : "工作",
            "kind"      : "single",
            "collection": None,
        }
    ]

    detail = build_event_detail(db, "owner-a", "minimal-event")
    assert detail is not None
    assert set(detail) == {"event", "related_instances"}
    assert set(detail["event"]) == {
        "id",
        "display_id",
        "title",
        "category",
        "start_time",
        "end_time",
        "location",
        "notes",
        "event_role",
        "event_collection_kind",
        "kind",
        "collection",
        "reminders",
        "series_id",
        "version",
    }
    assert detail["event"]["reminders"][0]["status"] == "sent"


def test_overview_batches_collection_reads(db: Database) -> None:
    """多个集合叶子必须经批量入口读取，不逐条查询集合。"""

    for collection_id, event_day in (("group-a", "03"), ("group-b", "04")):
        db.create_event_collection(
            {
                "id"        : collection_id,
                "owner_id"  : "owner-a",
                "kind"      : "multi_node",
                "title"     : collection_id,
                "start_time": f"2030-01-{event_day}T10:00:00",
                "end_time"  : f"2030-01-{event_day}T10:00:00",
            }
        )
        db.insert_item(
            {
                "id"                   : f"{collection_id}_m01",
                "owner_id"             : "owner-a",
                "type"                 : "event",
                "title"                : "节点",
                "start_time"           : f"2030-01-{event_day}T10:00:00",
                "event_role"           : "multi_node_child",
                "event_collection_id"  : collection_id,
                "event_collection_kind": "multi_node",
            }
        )

    with trace_sql_statements(db.get_connection()) as statements:
        overview = build_events_overview(
            db,
            "owner-a",
            start_date = "2030-01-01",
            end_date   = "2030-01-31",
        )

    assert overview["summary"]["event_count"] == 2
    collection_reads = [sql for sql in statements if "FROM event_collections" in sql]
    assert len(collection_reads) == 1
    assert "group-a" in collection_reads[0] and "group-b" in collection_reads[0]
