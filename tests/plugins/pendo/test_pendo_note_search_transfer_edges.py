"""笔记标题、搜索折叠重复日程、导入请求头和极端日期的回归测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import tests.helpers.pendo_web_transfer_test_support as _fixture_support
from plugins.pendo.handlers.diary import DiaryHandler
from plugins.pendo.handlers.event import EventHandler
from plugins.pendo.handlers.note import NoteHandler
from plugins.pendo.handlers.search import SearchHandler
from plugins.pendo.services.ai_parser import AIParser, RateLimiter
from plugins.pendo.services.db import Database
from plugins.pendo.services.reminder import ReminderService
from tests.helpers.pendo_web_transfer_test_support import (
    OWNER_ID,
    _build_sample_bundle_bytes,
    transfer_api,
)

auth_headers = _fixture_support.auth_headers

USER    = "10001"
CONTEXT = SimpleNamespace()


def _note(db: Database, args: str) -> dict[str, Any]:
    return asyncio.run(NoteHandler(db).handle(USER, args, CONTEXT))


def test_multiline_note_without_title_uses_first_line_as_title(db):
    created = _note(db, "add 购物清单\n- 牛奶\n- 鸡蛋")

    note = db.get_item(created["item_id"], USER)
    assert note.title == "购物清单"
    assert note.content == "购物清单\n- 牛奶\n- 鸡蛋"


def test_plain_edit_keeps_explicit_title_and_replaces_only_content(db):
    note_id = _note(db, "add title:周会纪要 content 第一版正文")["item_id"]

    assert _note(db, f"edit {note_id} 第二版正文，补充了结论")["status"] == "success"

    note = db.get_item(note_id, USER)
    assert (note.title, note.content) == ("周会纪要", "第二版正文，补充了结论")

    assert _note(db, f"edit {note_id} title:周会结论 content 第三版")["status"] == "success"
    note = db.get_item(note_id, USER)
    assert (note.title, note.content) == ("周会结论", "第三版")


def test_plain_edit_still_renames_note_whose_title_came_from_content(db):
    note_id = _note(db, "add 买牛奶")["item_id"]

    assert _note(db, f"edit {note_id} 买牛奶和鸡蛋")["status"] == "success"

    note = db.get_item(note_id, USER)
    assert (note.title, note.content) == ("买牛奶和鸡蛋", "买牛奶和鸡蛋")


def test_note_view_still_shows_note_when_last_viewed_write_loses_a_race(db, monkeypatch):
    note_id  = _note(db, "add title:配置说明 content 正文内容")["item_id"]
    original = db.update_item

    def racing_update(item_id, updates, owner_id=None, **options):
        # 另一处修改先提交，查看时带的旧版本号对不上。
        original(item_id, {"category": "资料"}, owner_id)
        return original(item_id, updates, owner_id, **options)

    monkeypatch.setattr(db, "update_item", racing_update)

    viewed = _note(db, f"view {note_id}")

    assert viewed["status"] == "success"
    assert "正文内容" in viewed["message"]


def _events(db: Database):
    parser = AIParser(context=None, db=db)
    parser._rate_limiter = RateLimiter(max_calls=1000)
    parser._call_llm = AsyncMock()
    handler = EventHandler(db=db, ai_parser=parser, reminder_service=ReminderService(db))

    def add(payload: dict[str, Any]) -> str:
        parser._call_llm.return_value = json.dumps(payload, ensure_ascii=False)
        return str(asyncio.run(handler.add_event(USER, payload["title"], {}))["item_id"])

    return add


def test_chat_search_lists_a_recurring_series_once(db):
    add           = _events(db)
    collection_id = add(
        {"title": "周会", "start_time": "2031-05-05T10:00:00", "rrule": "FREQ=WEEKLY;COUNT=30"}
    )
    add({"title": "周会材料准备", "start_time": "2031-05-04T10:00:00"})
    _note(db, "add title:周会纪要 content 第一版正文")

    result  = asyncio.run(SearchHandler(db).search(USER, "周会", CONTEXT))
    message = result["message"]

    assert "命中: 3 条" in message
    assert "周会材料准备" in message and "周会纪要" in message
    assert "周会 · 周会" not in message
    first_occurrence = db.get_collection_events(collection_id, USER)[0]
    assert first_occurrence.display_id in message
    assert "🔄重复日程 集合" in message

    # 页面搜索仍返回每一次实例，由日历视图自己分组。
    _items, total = db.search_items_page(USER, "周会")
    assert total == 32


def test_collapsed_series_prefers_next_upcoming_occurrence(db, monkeypatch):
    add           = _events(db)
    collection_id = add(
        {"title": "晨跑", "start_time": "2031-05-01T07:00:00", "rrule": "FREQ=DAILY;COUNT=5"}
    )
    children = db.get_collection_events(collection_id, USER)
    monkeypatch.setattr(
        "plugins.pendo.services.db.utc_now_iso", lambda: "2031-05-02T12:00:00+00:00"
    )

    items, total = db.search_items_page(USER, "晨跑", collapse_recurring=True)

    assert total == 1
    assert items[0].id == children[2].id

    monkeypatch.setattr(
        "plugins.pendo.services.db.utc_now_iso", lambda: "2032-01-01T00:00:00+00:00"
    )
    items, _total = db.search_items_page(USER, "晨跑", collapse_recurring=True)
    assert items[0].id == children[-1].id


def _task_bundle() -> bytes:
    return _build_sample_bundle_bytes(
        {
            "task": [
                {
                    "_type"  : "task",
                    "_schema": 2,
                    "id"     : "edge-task",
                    "title"  : "任务",
                    "status" : "open",
                }
            ]
        }
    )


@pytest.mark.parametrize(
    "filename",
    ["%ff%fe.zip", "a" * 300, "bad%0Aname.zip"],
    ids=["invalid-utf8", "too-long", "control-character"],
)
def test_import_rejects_malformed_filename_header_before_touching_data(
    client: Any, temp_db: Database, auth_headers: dict, filename: str
):
    response = client.post(
        "/api/transfer/import/execute",
        headers = {**auth_headers, "X-Transfer-Filename": filename},
        content = _task_bundle(),
    )

    assert response.status_code == 422
    assert temp_db.count_items(OWNER_ID, {"type": "task"}) == 0


def test_import_keeps_percent_encoded_unicode_filename(
    client: Any, temp_db: Database, auth_headers: dict
):
    response = client.post(
        "/api/transfer/import/execute",
        headers = {**auth_headers, "X-Transfer-Filename": "%E5%A4%87%E4%BB%BD.pendo.zip"},
        content = _task_bundle(),
    )

    assert response.status_code == 200
    assert temp_db.get_transfer_logs(OWNER_ID, limit=1)[0]["filename"] == "备份.pendo.zip"


def test_preview_cache_keeps_one_entry_per_owner(monkeypatch):
    monkeypatch.setattr(transfer_api, "_PREVIEWS", type(transfer_api._PREVIEWS)())

    first  = transfer_api._remember_preview("owner-a", [{"type": "task", "id": "1", "title": "a"}])
    other  = transfer_api._remember_preview("owner-b", [{"type": "task", "id": "2", "title": "b"}])
    second = transfer_api._remember_preview("owner-a", [{"type": "task", "id": "3", "title": "c"}])

    assert transfer_api._recall_preview("owner-a", first) is None
    assert transfer_api._recall_preview("owner-a", second) is not None
    assert transfer_api._recall_preview("owner-b", other) is not None
    assert len(transfer_api._PREVIEWS) == 2


def test_stats_and_widget_handle_dates_at_the_edge_of_the_calendar(client: Any, auth_headers: dict):
    assert client.get("/api/stats/activity-heatmap?year=9999").status_code == 200
    assert client.get("/api/stats/tasks?range_str=9999-12-01..9999-12-31").status_code in {200, 422}
    calendar = client.get("/api/widget/calendar?start_date=9999-12-30&end_date=9999-12-31")
    assert calendar.status_code == 422
    assert client.get("/api/widget/summary?now=9999-12-31T23:00:00").status_code == 422


def _export_all(client: Any, auth_headers: dict) -> bytes:
    response = client.post(
        "/api/transfer/export/download",
        headers = auth_headers,
        json    = {"selection": {"types": ["event", "note"], "preset": "all"}},
    )
    assert response.status_code == 200
    return bytes(response.content)


def _import(client: Any, auth_headers: dict, bundle: bytes, policy: str) -> dict[str, int]:
    response = client.post(
        "/api/transfer/import/execute",
        headers={
            **auth_headers,
            "X-Transfer-Options": json.dumps({"conflict_policy": policy, "force": True}),
        },
        content=bundle,
    )
    assert response.status_code == 200
    return dict(response.json()["data"]["results"])


def test_skip_policy_restores_own_backup_without_duplicates(
    client: Any, temp_db: Database, auth_headers: dict
):
    temp_db.insert_item(
        {
            "id"      : "own-note",
            "owner_id": OWNER_ID,
            "type"    : "note",
            "title"   : "原笔记",
            "content" : "正文",
        }
    )
    collection_id = temp_db.create_event_collection(
        {
            "id"        : "own-collection",
            "owner_id"  : OWNER_ID,
            "kind"      : "multi_node",
            "title"     : "出差",
            "timezone"  : "Asia/Shanghai",
            "start_time": "2031-05-01T09:00:00",
            "end_time"  : "2031-05-03T18:00:00",
        },
        [
            (
                f"own-node-{index}",
                {
                    "owner_id"  : OWNER_ID,
                    "type"      : "event",
                    "title"     : title,
                    "timezone"  : "Asia/Shanghai",
                    "start_time": start,
                },
            )
            for index, (title, start) in enumerate(
                [("出发", "2031-05-01T09:00:00"), ("返程", "2031-05-03T18:00:00")], 1
            )
        ],
    )
    bundle = _export_all(client, auth_headers)
    temp_db.delete_item("own-note", owner_id=OWNER_ID)

    results = _import(client, auth_headers, bundle, "skip")

    # 仍在的日程和集合跳过；删掉的那条笔记从备份里补回来。
    assert results == {"inserted": 1, "updated": 0, "skipped": 3, "failed": 0}
    assert temp_db.count_items(OWNER_ID, {"type": "event"}) == 2
    assert temp_db.count_items(OWNER_ID, {"type": "note"}) == 1
    assert len(temp_db.get_collection_events(collection_id, OWNER_ID)) == 2

    # 覆盖不认现有条目的 ID：外部 ID 不能指定覆盖哪条原记录。
    overwrite = _import(client, auth_headers, bundle, "overwrite")
    assert overwrite["updated"] == 1 and overwrite["inserted"] == 3
    assert temp_db.get_item("own-node-1", OWNER_ID).title == "出发"


def test_diary_add_treats_leading_relative_future_word_as_content(db):
    handler = DiaryHandler(db)
    created = asyncio.run(handler.handle(USER, "add 明天 要考试了，有点紧张 mood:anxious", CONTEXT))
    diary   = db.get_item(created["item_id"], USER)
    zone    = asyncio.run(handler._user_zone(USER))

    assert diary.diary_date == datetime.now(zone).strftime("%Y-%m-%d")
    assert diary.content == "明天 要考试了，有点紧张"

    dated = asyncio.run(handler.handle(USER, "add 昨天 补一篇 mood:calm", CONTEXT))
    assert db.get_item(dated["item_id"], USER).content == "补一篇"


def test_diary_template_keeps_answers_when_saving_fails(db):
    handler           = DiaryHandler(db)
    ended: list[bool] = []

    async def end_session() -> None:
        ended.append(True)

    context = SimpleNamespace(end_session=end_session)
    prompts = ["第一件好事:", "第二件好事:"]
    session = _Session(
        {
            "type"       : "diary_template",
            "template_id": "three_good",
            "diary_date" : "2031-05-01",
            "prompts"    : prompts,
            "step"       : 1,
            "answers"    : ["早起跑步"],
        }
    )

    failed = asyncio.run(handler.handle_session_message(USER, "长" * 50_001, context, session))

    assert failed["status"] == "info"
    assert "前面的回答已保留" in failed["message"]
    assert ended == []
    assert (session.get("step"), session.get("answers")) == (1, ["早起跑步"])

    saved = asyncio.run(handler.handle_session_message(USER, "读完一本书", context, session))
    assert saved["status"] == "success"
    assert ended == [True]
    assert "读完一本书" in db.get_item(saved["item_id"], USER).content


class _Session:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = dict(data)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
