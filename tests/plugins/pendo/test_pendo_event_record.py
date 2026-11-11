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
    # 创建回报直接给出各节点 ID，之后改某个节点不必先查看集合。
    assert all(
        f"{item.title}  " in result["message"] and item.display_id in result["message"]
        for item in items
    )
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
    from plugins.pendo.main import _format_result

    continuation = _format_result(asked).command_outcome
    assert continuation.state == "incomplete"
    confirmed_payload = json.loads(
        continuation.confirmation_command.removeprefix("/pendo event record ")
    )
    assert confirmed_payload == {**payload, "title": "乙", "force": True}
    assert len(db.get_all_items(USER, {"type": "event"})) == 1

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


@pytest.mark.asyncio
async def test_list_marks_collection_ids_and_cross_day_end_dates(db):
    """列表行直接给出节点所属集合 ID，跨天结束带日期：整组删除不必逐个 view 找集合。"""
    from plugins.pendo.utils.identifiers import public_id

    handler = _handler(db)
    await _record(
        handler,
        {
            "kind" : "multi_node",
            "title": "研讨会",
            "nodes": [
                {"name": "会议开始", "time": "2030-11-14T09:00"},
                {"name": "会议结束", "time": "2030-11-15T09:00"},
            ],
        },
    )
    await _record(
        handler,
        {
            "kind"      : "single",
            "title"     : "跨天会议",
            "start_time": "2030-11-16T09:00",
            "end_time"  : "2030-11-17T09:00",
        },
    )
    nodes = [item for item in db.get_all_items(USER, {"type": "event"}) if item.event_collection_id]
    listing = await handler.handle(USER, "list 2030-11-14..2030-11-17", _Context())
    message = listing["message"]
    collection = public_id(nodes[0].event_collection_id)
    assert message.count(f"（集合 {collection}）") == len(nodes) == 2
    assert all(f"{node.display_id}（集合 {collection}）" in message for node in nodes)
    assert "09:00 - 11-17 09:00 跨天会议" in message


def _ai_parsed(handler: EventHandler, text: str, fields: dict) -> dict:
    """模拟 event add 的 AI 解析结果：字段经解析器的统一整理，来源为 ai。"""
    return handler.ai_parser.build_event_result(
        {**fields, "parse_source": "ai"}, text, USER, partial=False
    )


@pytest.mark.parametrize(
    "text,fields,kind",
    [
        (
            "后天上午九点组会，提前一天和一小时提醒",
            {
                "title"         : "组会",
                "start_time"    : "2030-01-02T09:00:00",
                "remind_offsets": ["提前1天", "提前1小时"],
            },
            "single",
        ),
        (
            "多节点事件，11月14日会议开始，15日会议结束",
            {
                "title"     : "研讨会",
                "milestones": [
                    {"name": "会议开始", "time": "2030-11-14T09:00:00"},
                    {"name": "会议结束", "time": "2030-11-15T09:00:00"},
                ],
            },
            "multi_node",
        ),
        (
            "每周二下午四点讨论，重复三次",
            {
                "title"     : "讨论",
                "start_time": "2030-01-01T16:00:00",
                "rrule"     : "FREQ=WEEKLY;BYDAY=TU;COUNT=3",
            },
            "recurring",
        ),
    ],
)
def test_parsed_add_result_round_trips_into_record(db, text, fields, kind):
    """冲突确认用结构化记录原样重建 add 的解析结果：时间、节点和提醒规则一致。"""
    handler = _handler(db)
    parsed  = _ai_parsed(handler, text, fields)
    record  = EventRecord.from_parsed(parsed)
    assert record is not None and record.kind == kind
    rebuilt = handler.ai_parser.build_event_result(
        record.parsed_fields(), record.content or record.title, USER, partial=False
    )
    for key in (
        "title",
        "start_time",
        "end_time",
        "rrule",
        "milestones",
        "reminder_rules",
        "content",
    ):
        assert rebuilt.get(key) == parsed.get(key), key


def test_rule_fallback_results_are_not_turned_into_records(db):
    handler = _handler(db)
    parsed  = handler.ai_parser.parse_natural_language("明天上午九点开会", USER)
    assert EventRecord.from_parsed({**parsed, "parse_source": "rule"}) is None


@pytest.mark.asyncio
async def test_add_conflict_offers_exact_record_confirmation(db, monkeypatch):
    """add 冲突时给出按本次解析结果原样创建的命令：确认时不再调用解析模型。"""
    from plugins.pendo.main import _format_result

    handler = _handler(db)
    await _record(handler, {"kind": "single", "title": "甲", "start_time": "2030-11-14T09:00"})
    text   = "添加一个多节点事件，11月14日会议开始，15日会议结束，提前一天提醒"
    parsed = _ai_parsed(
        handler,
        text,
        {
            "title"         : "研讨会",
            "remind_offsets": ["提前1天"],
            "milestones"    : [
                {"name": "会议开始", "time": "2030-11-14T09:00:00"},
                {"name": "会议结束", "time": "2030-11-15T09:00:00"},
            ],
        },
    )
    calls: list[str] = []

    async def fake_parse(source, user_id, **_kwargs):
        calls.append(source)
        return dict(parsed)

    monkeypatch.setattr(handler.ai_parser, "parse_event_with_ai", fake_parse)
    asked = await handler.handle(USER, "add " + text, _Context())
    assert asked["status"] == "need_confirm"
    command = _format_result(asked).command_outcome.confirmation_command
    assert command.startswith("/pendo event record ")
    confirmed = await handler.handle(USER, command.removeprefix("/pendo event "), _Context())
    assert confirmed["status"] == "success" and calls == [text]
    nodes = [item for item in db.get_all_items(USER, {"type": "event"}) if item.event_collection_id]
    assert sorted(item.title for item in nodes) == ["会议开始", "会议结束"]
    assert all(item.reminder_rules == parsed["reminder_rules"] for item in nodes)


@pytest.mark.asyncio
async def test_natural_language_edit_renames_one_node_in_place(db):
    """节点改名走自然语言 edit：按节点 ID 原地修改，集合、时间、提醒和另一个节点都不变。"""
    handler = _handler(db)
    await _record(
        handler,
        {"kind": "multi_node", "title": "研讨会", "nodes": _NODES, "remind_offsets": ["提前1天"]},
    )
    before = {item.id: item for item in db.get_all_items(USER, {"type": "event"})}
    first = min(before.values(), key=lambda item: item.start_time)
    result = await handler.handle(USER, f"edit {first.display_id} 标题改为会议开始", _Context())
    assert result["status"] == "success"
    after = {item.id: item for item in db.get_all_items(USER, {"type": "event"})}
    assert after.keys() == before.keys()
    assert after[first.id].title == "会议开始"
    for item_id, item in after.items():
        old = before[item_id]
        assert (item.event_collection_id, item.start_time, item.remind_times) == (
            old.event_collection_id,
            old.start_time,
            old.remind_times,
        )
        if item_id != first.id:
            assert item.title == old.title
