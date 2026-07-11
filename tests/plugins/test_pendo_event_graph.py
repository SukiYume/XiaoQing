from __future__ import annotations

import sqlite3
from pathlib import Path

from plugins.pendo.handlers.event import EventHandler
from plugins.pendo.models.item import EventItem
from plugins.pendo.services.db import Database
from plugins.pendo.services.reminder import ReminderService
from plugins.pendo.utils.validators import (
    build_remind_times_from_rules,
    derive_reminder_rules,
    normalize_event_fields,
)


def test_event_graph_schema_and_event_item_roundtrip(tmp_path: Path):
    db = Database(str(tmp_path / "pendo_event_graph_schema.db"))
    try:
        conn = db.get_connection()
        item_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(items)").fetchall()
        }
        assert {
            "event_role",
            "event_collection_id",
            "event_collection_kind",
            "event_index",
            "event_node_key",
            "source_item_id",
            "reminder_rules",
        }.issubset(item_columns)
        assert {"rrule", "parent_id", "remind_policy_id", "milestones"}.isdisjoint(item_columns)

        collection_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(event_collections)").fetchall()
        }
        assert {
            "id",
            "owner_id",
            "kind",
            "title",
            "rrule",
            "reminder_rules",
            "deleted",
        }.issubset(collection_columns)

        event = EventItem(
            owner_id="u1",
            title="节点",
            start_time="2030-01-01T10:00:00",
            event_role="multi_node_child",
            event_collection_id="col12345",
            event_collection_kind="multi_node",
            event_index=2,
            event_node_key="m02",
            source_item_id="legacy01",
            reminder_rules=[{"offset_seconds": 3600}, {"offset_seconds": 0}],
        )
        db.insert_item(event, "node0002")

        loaded = db.get_item("node0002", owner_id="u1")
        assert isinstance(loaded, EventItem)
        assert loaded.event_role == "multi_node_child"
        assert loaded.event_collection_id == "col12345"
        assert loaded.event_collection_kind == "multi_node"
        assert loaded.event_index == 2
        assert loaded.event_node_key == "m02"
        assert loaded.source_item_id == "legacy01"
        assert loaded.reminder_rules == [
            {"offset_seconds": 3600},
            {"offset_seconds": 0},
        ]
    finally:
        db.cleanup()


def test_reminder_rules_derive_and_rebuild_remind_times():
    start_time = "2030-01-02T09:00:00"
    rules = derive_reminder_rules(
        start_time,
        [
            "2030-01-01T09:00:00",
            "2030-01-02T08:00:00",
            "2030-01-02T09:00:00",
        ],
    )

    assert rules == [
        {"offset_seconds": 86400},
        {"offset_seconds": 3600},
        {"offset_seconds": 0},
    ]
    assert build_remind_times_from_rules(start_time, rules) == [
        "2030-01-01T09:00:00",
        "2030-01-02T08:00:00",
        "2030-01-02T09:00:00",
    ]


def test_normalize_event_fields_keeps_rules_and_cache_in_sync():
    normalized = normalize_event_fields(
        {
            "title": "会议",
            "category": "工作",
            "start_time": "2030-01-02T09:00:00",
            "reminder_rules": [{"offset_seconds": 3600}, {"offset_seconds": 0}],
        }
    )

    assert normalized["reminder_rules"] == [
        {"offset_seconds": 3600},
        {"offset_seconds": 0},
    ]
    assert normalized["remind_times"] == [
        "2030-01-02T08:00:00",
        "2030-01-02T09:00:00",
    ]

    normalized_from_times = normalize_event_fields(
        {
            "title": "会议",
            "category": "工作",
            "start_time": "2030-01-02T09:00:00",
            "remind_times": ["2030-01-01T09:00:00", "2030-01-02T09:00:00"],
        }
    )

    assert normalized_from_times["reminder_rules"] == [
        {"offset_seconds": 86400},
        {"offset_seconds": 0},
    ]


def test_normalize_event_fields_defaults_leaf_to_start_time_reminder():
    normalized = normalize_event_fields(
        {
            "title": "无显式提醒",
            "category": "工作",
            "start_time": "2030-01-02T09:00:00",
        }
    )

    assert normalized["reminder_rules"] == [{"offset_seconds": 0}]
    assert normalized["remind_times"] == ["2030-01-02T09:00:00"]


def test_normalize_event_fields_allows_explicit_reminder_clear():
    normalized = normalize_event_fields(
        {
            "title": "清空提醒",
            "category": "工作",
            "start_time": "2030-01-02T09:00:00",
            "reminder_rules": [],
        }
    )

    assert normalized["reminder_rules"] == []
    assert normalized["remind_times"] == []


def test_event_collection_store_and_graph_service(tmp_path: Path):
    from plugins.pendo.services.event_graph import EventGraphService

    db = Database(str(tmp_path / "pendo_event_graph_store.db"))
    try:
        collection_id = db.create_event_collection(
            {
                "id": "col00001",
                "owner_id": "u1",
                "kind": "multi_node",
                "title": "大会",
                "category": "工作",
                "start_time": "2030-01-01T09:00:00",
                "end_time": "2030-01-03T18:00:00",
                "reminder_rules": [{"offset_seconds": 3600}, {"offset_seconds": 0}],
            }
        )
        assert collection_id == "col00001"

        first = EventItem(
            owner_id="u1",
            title="注册截止",
            start_time="2030-01-01T09:00:00",
            event_role="multi_node_child",
            event_collection_id=collection_id,
            event_collection_kind="multi_node",
            event_index=1,
            reminder_rules=[{"offset_seconds": 0}],
            remind_times=["2030-01-01T09:00:00"],
        )
        second = EventItem(
            owner_id="u1",
            title="会议开始",
            start_time="2030-01-03T09:00:00",
            event_role="multi_node_child",
            event_collection_id=collection_id,
            event_collection_kind="multi_node",
            event_index=2,
            reminder_rules=[{"offset_seconds": 0}],
            remind_times=["2030-01-03T09:00:00"],
        )
        db.insert_item(second, "node0002")
        db.insert_item(first, "node0001")

        collection = db.get_event_collection(collection_id, "u1")
        assert collection is not None
        assert collection["title"] == "大会"
        assert collection["reminder_rules"] == [
            {"offset_seconds": 3600},
            {"offset_seconds": 0},
        ]

        children = db.get_collection_events(collection_id, "u1")
        assert [child.id for child in children] == ["node0001", "node0002"]

        service = EventGraphService(db)
        leaf_family = service.load_by_id("u1", "node0002")
        assert leaf_family.kind == "multi_node"
        assert leaf_family.leaf is not None
        assert leaf_family.leaf.title == "会议开始"
        assert leaf_family.collection is not None
        assert leaf_family.collection["title"] == "大会"
        assert [child.id for child in leaf_family.children] == ["node0001", "node0002"]

        collection_family = service.load_by_id("u1", collection_id)
        assert collection_family.kind == "multi_node"
        assert collection_family.leaf is None
        assert [child.title for child in collection_family.children] == [
            "注册截止",
            "会议开始",
        ]

        assert db.update_event_collection(collection_id, {"title": "新大会"}, "u1")
        assert db.get_event_collection(collection_id, "u1")["title"] == "新大会"

        assert db.delete_event_collection(collection_id, "u1", cascade=True)
        assert db.get_event_collection(collection_id, "u1") is None
        assert db.get_item("node0001", "u1") is None
        assert db.get_item("node0002", "u1") is None
    finally:
        db.cleanup()


def test_batch_soft_delete_removes_child_reminder_logs(tmp_path: Path):
    db = Database(str(tmp_path / "pendo_batch_delete_reminder_logs.db"))
    try:
        first = EventItem(
            owner_id="u1",
            title="节点一",
            start_time="2030-01-01T09:00:00",
            remind_times=["2030-01-01T08:00:00"],
        )
        second = EventItem(
            owner_id="u1",
            title="节点二",
            start_time="2030-01-02T09:00:00",
            remind_times=["2030-01-02T08:00:00"],
        )
        db.insert_item(first, "node-log1")
        db.insert_item(second, "node-log2")
        db.log_reminder("node-log1", "2030-01-01T08:00:00", sent=True)
        db.log_reminder("node-log2", "2030-01-02T08:00:00", sent=True)

        assert db.get_reminder_logs("node-log1")
        assert db.get_reminder_logs("node-log2")

        db.batch_soft_delete(["node-log1", "node-log2"], "u1")

        assert db.get_reminder_logs("node-log1") == []
        assert db.get_reminder_logs("node-log2") == []
    finally:
        db.cleanup()


class _NoConflictReminderService:
    def detect_conflict(self, user_id: str, start_time: str, end_time: str | None = None):
        return []


class _UnusedAiParser:
    def build_remind_times_from_offsets(self, start_time: str, offsets: list[str]):
        raise AssertionError("not used")

    def build_reminder_rules_from_description(self, description: str):
        if "1小时" in description:
            return [{"offset_seconds": 3600}, {"offset_seconds": 0}]
        return [{"offset_seconds": 0}]


def test_create_multi_node_event_writes_collection_and_leaf_events(tmp_path: Path):
    import asyncio

    db = Database(str(tmp_path / "pendo_event_graph_create_multi.db"))
    handler = EventHandler(
        db=db,
        ai_parser=_UnusedAiParser(),
        reminder_service=_NoConflictReminderService(),
    )

    try:
        result = asyncio.run(
            handler.create_event(
                "u1",
                {
                    "title": "学术会议",
                    "category": "会议",
                    "location": "北京",
                    "notes": "全局备注",
                    "milestones": [
                        {"name": "注册截止", "time": "2030-04-01T09:00:00"},
                        {"name": "会议开始", "time": "2030-04-03T09:00:00", "notes": "带证件"},
                    ],
                    "reminder_rules": [{"offset_seconds": 3600}, {"offset_seconds": 0}],
                },
                {},
            )
        )

        assert result["status"] == "success"
        collection_id = result["item_id"]
        collection = db.get_event_collection(collection_id, "u1")
        assert collection is not None
        assert collection["kind"] == "multi_node"
        assert collection["title"] == "学术会议"
        assert collection["notes"] == "全局备注"
        assert collection["start_time"] == "2030-04-01T09:00:00"
        assert collection["end_time"] == "2030-04-03T09:00:00"

        children = db.get_collection_events(collection_id, "u1")
        assert [child.id for child in children] == [
            f"{collection_id}_m01",
            f"{collection_id}_m02",
        ]
        assert [child.title for child in children] == ["注册截止", "会议开始"]
        assert children[0].event_role == "multi_node_child"
        assert children[0].event_collection_id == collection_id
        assert children[0].remind_times == [
            "2030-04-01T08:00:00",
            "2030-04-01T09:00:00",
        ]
        assert children[1].notes == "带证件"
        assert children[1].remind_times == [
            "2030-04-03T08:00:00",
            "2030-04-03T09:00:00",
        ]
    finally:
        db.cleanup()


def test_atomic_collection_create_rolls_back_header_and_children_on_failure(tmp_path: Path):
    db = Database(str(tmp_path / "pendo_event_graph_atomic_rollback.db"))
    collection_id = "c" * 32
    child = {
        "owner_id": "u1",
        "type": "event",
        "title": "节点",
        "category": "工作",
        "start_time": "2030-04-01T09:00:00",
        "event_role": "multi_node_child",
        "event_collection_id": collection_id,
        "event_collection_kind": "multi_node",
    }
    try:
        try:
            db.create_event_collection_with_children(
                {
                    "id": collection_id,
                    "owner_id": "u1",
                    "kind": "multi_node",
                    "title": "原子集合",
                },
                [("duplicate-child", child), ("duplicate-child", child)],
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("duplicate child ID must fail the transaction")

        assert db.get_event_collection(collection_id, "u1") is None
        assert db.get_item("duplicate-child", "u1") is None
    finally:
        db.cleanup()


def test_create_recurring_event_writes_collection_and_occurrence_leaves(tmp_path: Path):
    import asyncio

    db = Database(str(tmp_path / "pendo_event_graph_create_recurring.db"))
    handler = EventHandler(
        db=db,
        ai_parser=_UnusedAiParser(),
        reminder_service=_NoConflictReminderService(),
    )

    try:
        result = asyncio.run(
            handler.create_event(
                "u1",
                {
                    "title": "晨会",
                    "category": "工作",
                    "start_time": "2030-01-01T09:00:00",
                    "end_time": "2030-01-01T10:00:00",
                    "rrule": "FREQ=DAILY;COUNT=2",
                    "reminder_rules": [{"offset_seconds": 3600}, {"offset_seconds": 0}],
                },
                {},
            )
        )

        assert result["status"] == "success"
        collection_id = result["item_id"]
        collection = db.get_event_collection(collection_id, "u1")
        assert collection is not None
        assert collection["kind"] == "recurring"
        assert collection["rrule"] == "FREQ=DAILY;COUNT=2"

        children = db.get_collection_events(collection_id, "u1")
        assert [child.event_role for child in children] == [
            "recurring_occurrence",
            "recurring_occurrence",
        ]
        assert [child.start_time for child in children] == [
            "2030-01-01T09:00:00",
            "2030-01-02T09:00:00",
        ]
        assert [child.end_time for child in children] == [
            "2030-01-01T10:00:00",
            "2030-01-02T10:00:00",
        ]
        assert children[1].remind_times == [
            "2030-01-02T08:00:00",
            "2030-01-02T09:00:00",
        ]
    finally:
        db.cleanup()


def test_cli_view_edit_delete_and_reminders_support_event_graph(tmp_path: Path):
    import asyncio

    db = Database(str(tmp_path / "pendo_event_graph_cli_crud.db"))
    handler = EventHandler(
        db=db,
        ai_parser=_UnusedAiParser(),
        reminder_service=_NoConflictReminderService(),
    )

    try:
        create = asyncio.run(
            handler.create_event(
                "u1",
                {
                    "title": "项目发布",
                    "category": "工作",
                    "milestones": [
                        {"name": "提审", "time": "2030-05-01T10:00:00"},
                        {"name": "上线", "time": "2030-05-02T18:00:00"},
                    ],
                    "reminder_rules": [{"offset_seconds": 0}],
                },
                {},
            )
        )
        collection_id = create["item_id"]
        first_id = f"{collection_id}_m01"
        second_id = f"{collection_id}_m02"

        leaf_view = asyncio.run(handler.view_event("u1", first_id, {}))
        assert leaf_view["status"] == "success"
        assert "所属: 项目发布" in leaf_view["message"]
        assert "提审" in leaf_view["message"]
        assert second_id in leaf_view["message"]

        collection_view = asyncio.run(handler.view_event("u1", collection_id, {}))
        assert collection_view["status"] == "success"
        assert "项目发布" in collection_view["message"]
        assert first_id in collection_view["message"]
        assert second_id in collection_view["message"]

        reminders = asyncio.run(handler.list_reminders("u1", collection_id, {}))
        assert reminders["status"] == "success"
        assert "项目发布" in reminders["message"]
        assert first_id in reminders["message"]
        assert second_id in reminders["message"]

        set_result = asyncio.run(
            handler.set_reminders("u1", f"{collection_id} 提前1小时提醒", {})
        )
        assert set_result["status"] == "success"
        assert db.get_item(first_id, "u1").remind_times == [
            "2030-05-01T09:00:00",
            "2030-05-01T10:00:00",
        ]

        async def fake_parse_updates(changes, current_event):
            return {"title": "提审截止"}

        handler._parse_updates = fake_parse_updates
        edit_leaf = asyncio.run(handler.edit_event("u1", f"{first_id} 改名", {}))
        assert edit_leaf["status"] == "success"
        assert db.get_item(first_id, "u1").title == "提审截止"
        assert db.get_item(second_id, "u1").title == "上线"

        delete_leaf = asyncio.run(handler.delete_event("u1", first_id, {}))
        assert delete_leaf["status"] == "success"
        assert db.get_item(first_id, "u1") is None
        assert db.get_item(second_id, "u1") is not None
        assert db.get_event_collection(collection_id, "u1") is not None

        delete_collection = asyncio.run(handler.delete_event("u1", collection_id, {}))
        assert delete_collection["status"] == "success"
        assert db.get_item(second_id, "u1") is None
        assert db.get_event_collection(collection_id, "u1") is None
    finally:
        db.cleanup()


def test_cli_delete_single_reminder_removes_it_from_event_and_logs(tmp_path: Path):
    import asyncio

    db = Database(str(tmp_path / "pendo_event_reminder_delete.db"))
    handler = EventHandler(
        db=db,
        ai_parser=_UnusedAiParser(),
        reminder_service=_NoConflictReminderService(),
    )

    try:
        db.insert_item(
            EventItem(
                id="evt-del",
                owner_id="u1",
                title="可删除提醒",
                start_time="2030-06-01T10:00:00",
                end_time="2030-06-01T11:00:00",
                reminder_rules=[{"offset_seconds": 3600}, {"offset_seconds": 0}],
                remind_times=["2030-06-01T09:00:00", "2030-06-01T10:00:00"],
            ),
            "evt-del",
        )
        db.confirm_reminder(
            "evt-del",
            user_action="preconfirmed",
            owner_id="u1",
            remind_time="2030-06-01T09:00:00",
            allow_future=True,
        )

        result = asyncio.run(
            handler.handle_reminders("u1", "delete evt-del 2030-06-01 09:00", {})
        )

        assert result["status"] == "success"
        event = db.get_item("evt-del", owner_id="u1")
        assert event.remind_times == ["2030-06-01T10:00:00"]
        assert event.reminder_rules == [{"offset_seconds": 0}]
        assert db.get_reminder_logs("evt-del") == []
    finally:
        db.cleanup()


def test_grouped_leaf_reminder_message_uses_collection_context(tmp_path: Path):
    db = Database(str(tmp_path / "pendo_event_graph_reminder_message.db"))
    try:
        collection_id = db.create_event_collection(
            {
                "id": "conf2030",
                "owner_id": "u1",
                "kind": "multi_node",
                "title": "学术会议",
                "category": "工作",
                "start_time": "2030-04-01T09:00:00",
                "end_time": "2030-04-03T18:00:00",
            }
        )
        leaf = EventItem(
            owner_id="u1",
            title="报告提交截止",
            start_time="2030-04-02T12:00:00",
            event_role="multi_node_child",
            event_collection_id=collection_id,
            event_collection_kind="multi_node",
            event_index=2,
            reminder_rules=[{"offset_seconds": 3600}, {"offset_seconds": 0}],
            remind_times=["2030-04-02T11:00:00", "2030-04-02T12:00:00"],
            notes="提交 PDF",
        )
        db.insert_item(leaf, "conf2030_m02")

        loaded = db.get_item("conf2030_m02", owner_id="u1")
        message = ReminderService(db)._build_reminder_message(
            loaded,
            "2030-04-02T11:00:00",
        )

        assert "🗓️ 学术会议" in message
        assert "📌 报告提交截止" in message
        assert "🎯 节点时间: 04月02日 12:00" in message
        assert "📝 提交 PDF" in message
        assert "/pendo confirm conf2030_m02" in message
    finally:
        db.cleanup()
