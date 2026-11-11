"""追加节点经过 AI 解析、原子保存、提醒和撤销的完整链路。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.pendo.core.exceptions import ItemVersionConflictException
from plugins.pendo.handlers.event import EventHandler
from plugins.pendo.models.item import EventItem
from plugins.pendo.services.ai_parser import AIParser, RateLimiter
from plugins.pendo.services.db import Database


@pytest.fixture
def event_collection(tmp_path):
    db = Database(str(tmp_path / "append.db"))
    parser = AIParser(context=None, db=db)
    parser._rate_limiter = RateLimiter(max_calls=100)
    parser._call_llm = AsyncMock()
    handler = EventHandler(db=db, ai_parser=parser, reminder_service=Mock())
    collection_id = db.create_event_collection(
        {
            "id"            : "collection123",
            "owner_id"      : "u1",
            "kind"          : "multi_node",
            "title"         : "项目发布",
            "category"      : "工作",
            "timezone"      : "Asia/Shanghai",
            "start_time"    : "2030-05-01T10:00:00",
            "end_time"      : "2030-05-02T18:00:00",
            "reminder_rules": [{"offset_seconds": 3600}, {"offset_seconds": 0}],
        },
        [
            (
                "node1",
                EventItem(
                    owner_id       = "u1",
                    title          = "提审",
                    start_time     = "2030-05-01T10:00:00",
                    timezone       = "Asia/Shanghai",
                    event_index    = 1,
                    event_node_key = "m01",
                ),
            ),
            (
                "node2",
                EventItem(
                    owner_id       = "u1",
                    title          = "上线",
                    start_time     = "2030-05-02T18:00:00",
                    timezone       = "Asia/Shanghai",
                    event_index    = 2,
                    event_node_key = "m02",
                ),
            ),
        ],
    )
    yield db, parser, handler, collection_id
    db.cleanup()


@pytest.mark.parametrize("count,explicit", [(1, False), (2, True)])
def test_append_through_ai_preserves_nodes_and_can_undo(event_collection, count, explicit):
    db, parser, handler, collection_id = event_collection
    before   = [node.to_dict() for node in db.get_collection_events(collection_id, "u1")]
    original = db.get_event_collection(collection_id, "u1")
    payload  = {
        "add_nodes": [
            {"name": f"验收{i}", "time": f"2030-05-0{3 + i}T15:00:00", "notes": "带材料"}
            for i in range(count)
        ]
    }
    if explicit:
        payload["remind_offsets"] = ["提前30分钟"]
    parser._call_llm.return_value = json.dumps(payload)
    result = asyncio.run(handler.edit_event("u1", f"{collection_id} 添加验收节点", {}))
    assert result["status"] == "success"
    nodes = db.get_collection_events(collection_id, "u1")
    assert len(nodes) == 2 + count
    assert [node.to_dict() for node in nodes[:2]] == before
    for index, node in enumerate(nodes[2:], 3):
        assert node.event_index == index
        assert node.event_node_key == f"m{index:02d}"
        assert node.event_role == "multi_node_child"
        assert node.event_collection_id == collection_id
        assert node.category == "工作"
        assert node.notes == "带材料"
        assert node.start_time == f"2030-05-0{index}T07:00:00+00:00"
        offset_time = "06:30" if explicit else "06:00"
        assert node.remind_times == [
            f"2030-05-0{index}T{offset_time}:00+00:00",
            node.start_time,
        ]
        assert node.display_id in result["message"]
        assert (
            db.get_connection()
            .execute(
                "SELECT COUNT(*) FROM reminder_logs WHERE item_id = ?",
                (node.id,),
            )
            .fetchone()[0]
            == 2
        )
    collection = db.get_event_collection(collection_id, "u1")
    assert collection["title"] == original["title"]
    assert collection["end_time"] == nodes[-1].start_time
    assert collection["reminder_rules"] == original["reminder_rules"]
    assert db.undo_edit("u1")["status"] == "success"
    assert [node.to_dict() for node in db.get_collection_events(collection_id, "u1")] == before
    assert db.get_event_collection(collection_id, "u1")["end_time"] == original["end_time"]
    for node in nodes[2:]:
        assert db.get_item(node.id, "u1") is None
        assert (
            db.get_connection()
            .execute(
                "SELECT COUNT(*) FROM reminder_logs WHERE item_id = ?",
                (node.id,),
            )
            .fetchone()[0]
            == 0
        )
        assert (
            db.get_connection()
            .execute(
                "SELECT COUNT(*) FROM items_fts WHERE id = ?",
                (node.id,),
            )
            .fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "bad_node",
    [
        {"name": "缺时间"},
        {"name": "坏时间", "time": "invalid"},
        {"name": "", "time": "2030-05-03T15:00:00"},
        {"name": "倒序", "time": "2030-05-03T15:00:00", "end_time": "2030-05-03T14:00:00"},
    ],
)
def test_invalid_append_is_all_or_nothing(event_collection, bad_node):
    db, parser, handler, collection_id = event_collection
    before                        = db.get_event_collection(collection_id, "u1")
    parser._call_llm.return_value = json.dumps(
        {
            "add_nodes": [
                {"name": "有效", "time": "2030-05-03T15:00:00"},
                bad_node,
            ]
        }
    )
    result = asyncio.run(handler.edit_event("u1", f"{collection_id} 添加两个节点", {}))
    assert result["status"] == "error"
    assert len(db.get_collection_events(collection_id, "u1")) == 2
    assert db.get_event_collection(collection_id, "u1") == before


def test_append_target_and_owner_are_checked(event_collection):
    db, parser, handler, collection_id = event_collection
    parser._call_llm.return_value = json.dumps(
        {
            "add_nodes": [
                {"name": "验收", "time": "2030-05-03T15:00:00"},
            ]
        }
    )
    result = asyncio.run(handler.edit_event("u2", f"{collection_id} 添加节点", {}))
    assert result["status"] == "error"
    parser._call_llm.assert_not_awaited()
    result = asyncio.run(handler.edit_event("u1", "node1 添加节点", {}))
    assert result["status"] == "warning"
    assert len(db.get_collection_events(collection_id, "u1")) == 2


def test_append_transaction_rolls_back_and_checks_version(event_collection):
    db, _parser, _handler, collection_id = event_collection
    before = db.get_event_collection(collection_id, "u1")
    nodes  = [
        EventItem(id="new1", owner_id="u1", title="新节点", start_time="2030-05-03T15:00:00"),
        EventItem(id="new2", owner_id="u1", title="无效时间", start_time="invalid"),
    ]
    with pytest.raises(ValueError):
        db.update_event_collection(collection_id, {"title": "新标题"}, "u1", new_nodes=nodes)
    assert db.get_item("new1", "u1") is None
    assert db.get_event_collection(collection_id, "u1") == before
    with pytest.raises(ItemVersionConflictException):
        db.update_event_collection(
            collection_id, {}, "u1", new_nodes=nodes[:1], expected_version=99
        )
    assert len(db.get_collection_events(collection_id, "u1")) == 2


def test_undo_append_preserves_later_node_edit(event_collection):
    db, parser, handler, collection_id = event_collection
    parser._call_llm.return_value = json.dumps(
        {
            "add_nodes": [
                {"name": "验收", "time": "2030-05-03T15:00:00"},
            ]
        }
    )
    asyncio.run(handler.edit_event("u1", f"{collection_id} 添加节点", {}))
    node = db.get_collection_events(collection_id, "u1")[-1]
    db.update_item(node.id, {"title": "验收更新"}, owner_id="u1")
    assert db.undo_edit("u1")["status"] == "error"
    assert db.get_item(node.id, "u1").title == "验收更新"


def test_append_node_notes_and_time_stay_local_to_the_new_node(event_collection):
    db, parser, handler, collection_id = event_collection
    parser._call_llm.return_value = json.dumps(
        {
            "add_nodes" : [{"name": "验收", "time": "2030-05-03T15:00:00Z", "notes": "带材料"}],
            "start_time": "2030-05-03T15:00:00",
            "end_time"  : "2030-05-03T16:00:00",
        }
    )
    result = asyncio.run(handler.edit_event("u1", f"{collection_id} 添加节点，备注带材料", {}))
    assert result["status"] == "success"
    assert "15:00" in result["message"]
    collection = db.get_event_collection(collection_id, "u1")
    assert collection["notes"] == ""
    assert collection["start_time"] == "2030-05-01T02:00:00+00:00"
    node = db.get_collection_events(collection_id, "u1")[-1]
    assert node.notes == "带材料"
    assert node.start_time == "2030-05-03T07:00:00+00:00"


def test_append_and_metadata_change_undo_together(event_collection):
    db, parser, handler, collection_id = event_collection
    original                      = db.get_event_collection(collection_id, "u1")
    parser._call_llm.return_value = json.dumps(
        {
            "add_nodes": [{"name": "验收", "time": "2030-05-03T15:00:00"}],
            "title"    : "发布计划",
            "category" : "学习",
        }
    )
    result = asyncio.run(
        handler.edit_event(
            "u1",
            f"{collection_id} 标题改为发布计划，分类改为学习，添加验收节点",
            {},
        )
    )
    assert result["status"] == "success"
    assert db.get_event_collection(collection_id, "u1")["title"] == "发布计划"
    assert all(node.category == "学习" for node in db.get_collection_events(collection_id, "u1"))
    assert db.undo_edit("u1")["status"] == "success"
    assert db.get_event_collection(collection_id, "u1")["title"] == original["title"]
    assert len(db.get_collection_events(collection_id, "u1")) == 2


def test_multiple_appends_keep_unique_indices_and_undo_in_order(event_collection):
    db, parser, handler, collection_id = event_collection
    parser._call_llm.return_value = json.dumps(
        {
            "add_nodes": [{"name": "验收", "time": "2030-05-03T15:00:00"}],
        }
    )
    for _ in range(2):
        assert (
            asyncio.run(handler.edit_event("u1", f"{collection_id} 添加节点", {}))["status"]
            == "success"
        )
    assert [node.event_index for node in db.get_collection_events(collection_id, "u1")] == [
        1,
        2,
        3,
        4,
    ]
    assert db.undo_edit("u1")["status"] == "success"
    assert db.undo_edit("u1")["status"] == "success"
    assert len(db.get_collection_events(collection_id, "u1")) == 2


def test_recurring_collection_rejects_append(event_collection):
    db, parser, handler, _collection_id = event_collection
    db.create_event_collection(
        {"id": "recurring123", "owner_id": "u1", "kind": "recurring", "title": "例会"}
    )
    parser._call_llm.return_value = json.dumps(
        {
            "add_nodes": [{"name": "验收", "time": "2030-05-03T15:00:00"}],
        }
    )
    result = asyncio.run(handler.edit_event("u1", "recurring123 添加节点", {}))
    assert result["status"] == "warning"
    assert db.get_collection_events("recurring123", "u1") == []


def test_append_reminder_offset_crosses_dst_as_elapsed_time(event_collection):
    db, parser, handler, collection_id = event_collection
    db.update_event_collection(collection_id, {"timezone": "America/Los_Angeles"}, "u1")
    parser._call_llm.return_value = json.dumps(
        {
            "add_nodes"     : [{"name": "验收", "time": "2030-03-10T03:30:00"}],
            "remind_offsets": ["提前1小时"],
        }
    )
    result = asyncio.run(handler.edit_event("u1", f"{collection_id} 添加节点，提前1小时提醒", {}))
    assert result["status"] == "success"
    node = db.get_collection_events(collection_id, "u1")[-1]
    assert node.start_time == "2030-03-10T10:30:00+00:00"
    assert node.remind_times == ["2030-03-10T09:30:00+00:00", "2030-03-10T10:30:00+00:00"]
