"""``/pendo event record``：结构化 JSON 直接写入单次、多节点和重复日程。"""

from __future__ import annotations

import json

import pytest

from plugins.pendo.handlers.event import EventHandler
from plugins.pendo.models.event_record import EventRecord
from plugins.pendo.services.ai_parser import AIParser
from plugins.pendo.services.db import Database
from plugins.pendo.services.reminder import ReminderService

USER = "1001"


class _Context:
    def __init__(self) -> None:
        self.sessions: list[dict] = []

    async def create_session(self, initial_data=None, timeout=300.0):
        self.sessions.append(initial_data)


def _handler(db: Database) -> EventHandler:
    return EventHandler(db, AIParser(db=db), ReminderService(db))


async def _record(handler: EventHandler, payload: dict, context=None, suffix: str = "") -> dict:
    text = "record " + json.dumps(payload, ensure_ascii=False) + suffix
    return await handler.handle(USER, text, context or _Context())


@pytest.mark.asyncio
async def test_single_event_is_created_with_local_wall_time_and_offset_reminder(db):
    result = await _record(
        _handler(db),
        {
            "kind"          : "single",
            "title"         : "组会",
            "start_time"    : "2030-01-02T09:00",
            "end_time"      : "2030-01-02T10:00",
            "location"      : "A101",
            "remind_offsets": ["提前30分钟"],
        },
    )
    assert result["status"] == "success"
    (item,) = db.get_all_items(USER, {"type": "event"})
    assert (item.title, item.location, item.content) == ("组会", "A101", "组会")
    assert item.start_time.startswith("2030-01-02T01:00:00")  # 当地 09:00（+08:00）的 UTC 存储
    assert item.remind_times == ["2030-01-02T00:30:00+00:00", "2030-01-02T01:00:00+00:00"]


@pytest.mark.asyncio
async def test_multi_node_event_creates_a_collection_with_one_child_per_node(db):
    result = await _record(
        _handler(db),
        {
            "kind" : "multi_node",
            "title": "研讨会",
            "nodes": [
                {"name": "第一天", "time": "2030-11-14T09:00"},
                {"name": "第二天", "time": "2030-11-15T09:00", "end_time": "2030-11-15T12:00"},
            ],
            "remind_offsets": ["提前1天"],
        },
    )
    assert result["status"] == "success"
    items = db.get_all_items(USER, {"type": "event"})
    assert len(items) == 2
    assert {item.event_collection_kind for item in items} == {"multi_node"}
    assert len({item.event_collection_id for item in items}) == 1
    assert all(item.remind_times for item in items)


@pytest.mark.asyncio
async def test_recurring_event_expands_the_rrule(db):
    result = await _record(
        _handler(db),
        {
            "kind"      : "recurring",
            "title"     : "晨会",
            "start_time": "2030-01-02T09:00",
            "rrule"     : "FREQ=DAILY;COUNT=3",
        },
    )
    assert result["status"] == "success"
    assert len(db.get_all_items(USER, {"type": "event"})) == 3


@pytest.mark.asyncio
async def test_conflict_asks_for_confirmation_and_force_creates(db):
    handler = _handler(db)
    payload = {"kind": "single", "title": "甲", "start_time": "2030-01-02T09:00"}
    assert (await _record(handler, payload))["status"] == "success"

    context = _Context()
    asked   = await _record(handler, {**payload, "title": "乙"}, context)
    assert asked["status"] == "need_confirm" and len(context.sessions) == 1

    forced = await _record(handler, {**payload, "title": "丙"}, context, " --force")
    assert forced["status"] == "success" and len(context.sessions) == 1
    assert len(db.get_all_items(USER, {"type": "event"})) == 2


_SINGLE = {"kind": "single", "title": "x", "start_time": "2030-01-02T09:00"}
_NODES  = [
    {"name": "a", "time": "2030-01-02T09:00"},
    {"name": "b", "time": "2030-01-03T09:00"},
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"kind": "single", "title": "x"}, "需要 start_time"),
        ({**_SINGLE, "start_time": "2030-01-02 09:00"}, "YYYY-MM-DDTHH:MM"),
        ({**_SINGLE, "nodes": _NODES[:1]}, "不能带 nodes"),
        ({**_SINGLE, "rrule": "FREQ=DAILY"}, "不能带 rrule"),
        ({**_SINGLE, "end_time": "2030-01-02T08:00"}, "end_time 须晚于 start_time"),
        ({"kind": "multi_node", "title": "x", "nodes": _NODES[:1]}, "至少 2 个"),
        (
            {"kind": "multi_node", "title": "x", "nodes": _NODES, "rrule": "FREQ=DAILY"},
            "不能带 rrule",
        ),
        ({"kind": "recurring", "title": "x", "start_time": "2030-01-02T09:00"}, "需要 rrule"),
        (
            {"kind": "recurring", "title": "x", "start_time": "2030-01-02T09:00", "rrule": "NOPE"},
            "不是有效的 RFC5545",
        ),
        ({**_SINGLE, "remind_offsets": ["一天前"]}, "提前正整数"),
        ({"kind": "other", "title": "x"}, "kind"),
    ],
)
async def test_invalid_records_report_the_field_and_save_nothing(db, payload, message):
    result = await _record(_handler(db), payload)
    assert result["status"] == "error"
    assert "日程字段校验失败" in result["message"] and message in result["message"]
    assert db.get_all_items(USER, {"type": "event"}) == []


def test_record_maps_nodes_to_parser_milestones():
    record = EventRecord.model_validate(
        {
            "kind" : "multi_node",
            "title": "t",
            "nodes": [
                {"name": "a", "time": "2030-01-02T09:00"},
                {"name": "b", "time": "2030-01-03T09:00", "end_time": "2030-01-03T10:00"},
            ],
        }
    )
    assert record.parsed_fields()["milestones"] == [
        {"name": "a", "time": "2030-01-02T09:00"},
        {"name": "b", "time": "2030-01-03T09:00", "end_time": "2030-01-03T10:00"},
    ]
