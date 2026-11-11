"""日程编辑、集合修改和结构化写入之间保持一致的回归测试。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from plugins.pendo.handlers.event import EventHandler, _split_force_flag
from plugins.pendo.services.ai_parser import AIParser, RateLimiter
from plugins.pendo.services.reminder import ReminderService

USER = "10001"


@pytest.fixture
def events(db):
    parser = AIParser(context=None, db=db)
    parser._rate_limiter = RateLimiter(max_calls=1000)
    parser._call_llm = AsyncMock()
    reminder         = ReminderService(db)
    handler = EventHandler(db=db, ai_parser=parser, reminder_service=reminder)

    def say(payload: dict) -> None:
        parser._call_llm.return_value = json.dumps(payload, ensure_ascii=False)

    return db, handler, reminder, say


def _add(handler, text: str) -> dict:
    return asyncio.run(handler.add_event(USER, text, {}))


def _edit(handler, item_id: str, changes: str) -> dict:
    return asyncio.run(handler.edit_event(USER, f"{item_id} {changes}", {}))


@pytest.mark.parametrize(
    "changes,llm",
    [
        ("标题改为产品说明会", {"title": "产品说明会"}),
        ("地点改到备注栏旁边的会议室", {"location": "备注栏旁边的会议室"}),
    ],
)
def test_edit_keeps_notes_when_words_only_look_like_note_directive(events, changes, llm):
    db, handler, _reminder, say = events
    say({"title": "周会", "start_time": "2031-05-01T10:00:00", "notes": "带电脑"})
    item_id = _add(handler, "2031年5月1日10点周会 备注带电脑")["item_id"]

    say(llm)
    assert _edit(handler, item_id, changes)["status"] == "success"

    assert db.get_item(item_id, USER).notes == "带电脑"


@pytest.mark.parametrize(
    "changes,expected",
    [
        ("备注为从北京南坐G123去会场", "从北京南坐G123去会场"),
        ("把备注改成记得带护照", "记得带护照"),
        ("改到2031-05-02 10:00，备注：带合同", "带合同"),
        ("说明改为先签到再入场", "先签到再入场"),
    ],
)
def test_edit_still_applies_real_note_directive(events, changes, expected):
    db, handler, _reminder, say = events
    say({"title": "周会", "start_time": "2031-05-01T10:00:00"})
    item_id = _add(handler, "2031年5月1日10点周会")["item_id"]

    say({})
    assert _edit(handler, item_id, changes)["status"] == "success"

    assert db.get_item(item_id, USER).notes == expected


def test_recurring_collection_edit_reaches_occurrences_and_undo_restores_them(events):
    db, handler, reminder, say = events
    say(
        {
            "title"     : "周会",
            "start_time": "2031-05-05T10:00:00",
            "location"  : "A101",
            "rrule"     : "FREQ=WEEKLY;COUNT=3",
        }
    )
    collection_id = _add(handler, "每周一10点周会 A101 重复3次")["item_id"]
    # 单独改过地点的实例不跟着集合走。
    moved = db.get_collection_events(collection_id, USER)[2]
    say({"location": "线上"})
    assert _edit(handler, moved.id, "地点改到线上")["status"] == "success"

    say({"title": "例会", "location": "B202"})
    assert _edit(handler, collection_id, "标题改为例会，地点改到B202")["status"] == "success"

    children = db.get_collection_events(collection_id, USER)
    assert [(child.title, child.location) for child in children] == [
        ("例会", "B202"),
        ("例会", "B202"),
        ("例会", "线上"),
    ]
    message = reminder._build_reminder_message(children[0], children[0].remind_times[0])
    assert "📍 B202" in message
    assert "A101" not in message and "周会" not in message
    listing = asyncio.run(handler.list_events(USER, "2031-05", {}))["message"]
    assert "例会 🔄 @ B202" in listing
    assert "例会 · 例会" not in listing

    undone = db.undo_edit(USER)
    assert undone["status"] == "success"
    restored = db.get_collection_events(collection_id, USER)
    assert [(child.title, child.location) for child in restored] == [
        ("周会", "A101"),
        ("周会", "A101"),
        ("周会", "线上"),
    ]


def test_multi_node_collection_edit_keeps_node_names_and_notes(events):
    db, handler, _reminder, say = events
    say(
        {
            "title"     : "出差",
            "location"  : "杭州",
            "milestones": [
                {"name": "出发", "time": "2031-05-01T09:00:00"},
                {"name": "返程", "time": "2031-05-03T18:00:00"},
            ],
        }
    )
    collection_id = _add(handler, "出差")["item_id"]

    say({"title": "团建", "location": "苏州", "notes": "带身份证"})
    assert (
        _edit(handler, collection_id, "标题改为团建，地点改到苏州，备注：带身份证")["status"]
        == "success"
    )

    children = db.get_collection_events(collection_id, USER)
    assert [(child.title, child.location, child.notes or "") for child in children] == [
        ("出发", "苏州", ""),
        ("返程", "苏州", ""),
    ]


def test_edit_with_new_offsets_updates_rules_so_later_moves_keep_them(events):
    db, handler, _reminder, say = events
    say({"title": "评审", "start_time": "2031-05-01T10:00:00", "remind_offsets": ["提前1天"]})
    item_id = _add(handler, "评审 提前1天提醒")["item_id"]

    say({"start_time": "2031-05-02T15:00:00", "remind_offsets": ["提前2小时"]})
    assert _edit(handler, item_id, "改到5月2日15点，提前2小时提醒")["status"] == "success"
    item = db.get_item(item_id, USER)
    assert item.reminder_rules == [{"offset_seconds": 7200}, {"offset_seconds": 0}]
    assert item.remind_times == ["2031-05-02T05:00:00+00:00", "2031-05-02T07:00:00+00:00"]

    say({"start_time": "2031-05-03T15:00:00"})
    assert _edit(handler, item_id, "改到5月3日15点")["status"] == "success"
    assert db.get_item(item_id, USER).remind_times == [
        "2031-05-03T05:00:00+00:00",
        "2031-05-03T07:00:00+00:00",
    ]


def test_force_flag_removal_keeps_text_whitespace():
    payload = '{"kind":"single","title":"a  b","notes":"x   y","start_time":"2031-05-01T10:00"}'

    assert _split_force_flag(f"{payload} --force") == (payload, True)
    assert _split_force_flag("明天9点开会\n备注  带电脑 --force") == (
        "明天9点开会\n备注  带电脑",
        True,
    )
    assert _split_force_flag("--force 明天9点开会") == ("明天9点开会", True)
    assert _split_force_flag('学习 "git push --force 用法"') == (
        '学习 "git push --force 用法"',
        False,
    )


def test_record_offsets_are_not_replaced_by_content_wording(events):
    db, handler, _reminder, _say = events
    payload = {
        "kind"          : "single",
        "title"         : "给王总打电话",
        "content"       : "提前1天提醒王总准备材料",
        "start_time"    : "2031-05-01T10:00",
        "remind_offsets": ["提前2小时"],
    }

    result = asyncio.run(handler.record_event(USER, json.dumps(payload, ensure_ascii=False), {}))

    assert db.get_item(result["item_id"], USER).reminder_rules == [
        {"offset_seconds": 7200},
        {"offset_seconds": 0},
    ]


def test_rrule_wins_over_milestones_without_stretching_each_occurrence(events):
    db, handler, _reminder, say = events
    say(
        {
            "title"     : "晨跑",
            "rrule"     : "FREQ=DAILY;COUNT=2",
            "milestones": [
                {"name": "第一天", "time": "2031-05-01T07:00:00"},
                {"name": "第二天", "time": "2031-05-02T07:00:00"},
            ],
        }
    )

    result = _add(handler, "每天7点晨跑两次")

    collection = db.get_event_collection(result["item_id"], USER)
    children   = db.get_collection_events(result["item_id"], USER)
    assert collection["kind"] == "recurring"
    assert [(child.start_time, child.end_time) for child in children] == [
        ("2031-04-30T23:00:00+00:00", None),
        ("2031-05-01T23:00:00+00:00", None),
    ]


def test_multi_node_conflict_checks_each_node_not_the_whole_span(events):
    _db, handler, _reminder, say = events
    say({"title": "牙医", "start_time": "2031-05-15T15:00:00"})
    _add(handler, "牙医")
    nodes = {
        "title"     : "基金申请",
        "milestones": [
            {"name": "注册截止", "time": "2031-05-01T09:00:00"},
            {"name": "材料提交", "time": "2031-06-01T09:00:00"},
        ],
    }

    say(nodes)
    assert _add(handler, "基金申请 5月1日注册截止 6月1日材料提交")["status"] == "success"

    nodes["title"]                 = "复查"
    nodes["milestones"][0]["time"] = "2031-05-15T15:30:00"
    say(nodes)
    assert _add(handler, "复查")["status"] == "need_confirm"


def test_invalid_category_reports_the_reason_instead_of_generic_failure(events):
    db, handler, _reminder, say = events
    say({"title": "评审", "start_time": "2031-05-01T10:00:00", "category": "工作/研发"})

    created = asyncio.run(handler.handle(USER, "add 评审", {}))

    assert created["status"] == "error"
    assert "分类名只能包含" in created["message"]

    say({"title": "评审", "start_time": "2031-05-01T10:00:00"})
    item_id = _add(handler, "评审")["item_id"]
    say({"category": "工作/研发"})
    edited = asyncio.run(handler.handle(USER, f"edit {item_id} 分类改为工作/研发", {}))
    assert edited["status"] == "error"
    assert "分类名只能包含" in edited["message"]
    assert db.get_item(item_id, USER).category != "工作/研发"


def test_recurrence_node_keys_stay_unique_within_a_day(events):
    db, handler, _reminder, say = events
    say(
        {
            "title"     : "吃药",
            "start_time": "2031-05-01T08:00:00",
            "rrule"     : "FREQ=DAILY;BYHOUR=8,20;COUNT=4",
        }
    )

    result = _add(handler, "每天8点和20点吃药")

    keys = [child.event_node_key for child in db.get_collection_events(result["item_id"], USER)]
    assert keys == ["20310501T0800", "20310501T2000", "20310502T0800", "20310502T2000"]


def test_recurring_conflict_check_reads_existing_events_once(events, monkeypatch):
    db, handler, _reminder, say = events
    say({"title": "牙医", "start_time": "2031-05-03T07:30:00"})
    _add(handler, "牙医")
    calls: list[tuple[str, str]] = []
    original                     = db.get_events_for_range

    def counted(user_id: str, start_date: str, end_date: str):
        calls.append((start_date, end_date))
        return original(user_id, start_date, end_date)

    monkeypatch.setattr(db, "get_events_for_range", counted)
    recurring = {
        "title"     : "晨跑",
        "start_time": "2031-05-01T07:00:00",
        "rrule"     : "FREQ=DAILY;COUNT=60",
    }

    say(recurring)
    blocked = _add(handler, "每天7点晨跑60次")

    assert blocked["status"] == "need_confirm"
    assert "牙医" in blocked["message"]
    assert len(calls) == 1

    calls.clear()
    recurring["start_time"] = "2031-05-01T09:00:00"
    say(recurring)
    assert _add(handler, "每天9点晨跑60次")["status"] == "success"
    assert len(calls) == 1
