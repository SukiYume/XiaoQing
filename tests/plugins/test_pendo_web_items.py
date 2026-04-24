"""Regression tests for pendo web ledger category filtering."""

import importlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import types
import uuid

import pytest

from plugins.pendo.services.db import Database
from plugins.pendo.utils.validators import (
    normalize_diary_fields,
    normalize_event_fields,
    normalize_ledger_fields,
    normalize_note_fields,
    normalize_task_fields,
)
from plugins.pendo.web.analytics import ledger_insights as ledger_insights_module
from plugins.pendo.web.analytics.ledger_insights import build_ledger_insights


ROOT = Path(__file__).resolve().parents[2]


def _load_items_module():
    fastapi = types.ModuleType("fastapi")

    class _Router:
        def _decorator(self, *_args, **_kwargs):
            def decorator(fn):
                return fn

            return decorator

        def get(self, *_args, **_kwargs):
            return self._decorator(*_args, **_kwargs)

        def post(self, *_args, **_kwargs):
            return self._decorator(*_args, **_kwargs)

        def put(self, *_args, **_kwargs):
            return self._decorator(*_args, **_kwargs)

        def delete(self, *_args, **_kwargs):
            return self._decorator(*_args, **_kwargs)

    class _HTTPException(Exception):
        def __init__(self, status_code: int, detail: str):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fastapi.APIRouter = _Router
    fastapi.Depends = lambda dep=None: dep
    fastapi.Query = lambda default=None, **_kwargs: default
    fastapi.HTTPException = _HTTPException
    fastapi.Header = lambda default=None, **_kwargs: default
    fastapi.Request = type("Request", (), {})

    responses = types.ModuleType("fastapi.responses")
    responses.Response = type("Response", (), {})

    _orig_fastapi = sys.modules.get("fastapi")
    _orig_responses = sys.modules.get("fastapi.responses")

    sys.modules["fastapi"] = fastapi
    sys.modules["fastapi.responses"] = responses
    sys.modules.pop("plugins.pendo.web.api.items", None)
    mod = importlib.import_module("plugins.pendo.web.api.items")

    if _orig_fastapi is not None:
        sys.modules["fastapi"] = _orig_fastapi
    else:
        sys.modules.pop("fastapi", None)
    if _orig_responses is not None:
        sys.modules["fastapi.responses"] = _orig_responses
    else:
        sys.modules.pop("fastapi.responses", None)

    return mod


def test_database_get_items_supports_ledger_category_filter():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_items_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-ledger"

    try:
        db.insert_item({
            "id": "l1",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "午饭",
            "amount": 22.5,
            "direction": "expense",
            "ledger_category": "餐饮",
            "ledger_date": "2026-03-25",
        })
        db.insert_item({
            "id": "l2",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "地铁",
            "amount": 4,
            "direction": "expense",
            "ledger_category": "交通",
            "ledger_date": "2026-03-25",
        })

        items = db.get_items(owner_id, filters={"type": "ledger", "ledger_category": "餐饮"})

        assert len(items) == 1
        assert items[0].ledger_category == "餐饮"
        assert items[0].title == "午饭"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_items_list_applies_priority_before_pagination_and_total_count():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_items_priority_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-priority"
    items_module = _load_items_module()

    try:
        db.insert_item({
            "id": "task_nonmatch",
            "owner_id": owner_id,
            "type": "task",
            "title": "普通优先级",
            "priority": 3,
            "status": "todo",
            "created_at": "2026-03-03T09:00:00",
            "updated_at": "2026-03-03T09:00:00",
        })
        db.insert_item({
            "id": "task_match_1",
            "owner_id": owner_id,
            "type": "task",
            "title": "高优先级一",
            "priority": 1,
            "status": "todo",
            "created_at": "2026-03-02T09:00:00",
            "updated_at": "2026-03-02T09:00:00",
        })
        db.insert_item({
            "id": "task_match_2",
            "owner_id": owner_id,
            "type": "task",
            "title": "高优先级二",
            "priority": 1,
            "status": "todo",
            "created_at": "2026-03-01T09:00:00",
            "updated_at": "2026-03-01T09:00:00",
        })

        result = items_module.list_items(
            type="task",
            priority=1,
            page=1,
            page_size=1,
            owner_id=owner_id,
            db=db,
        )

        assert result["data"]["total"] == 2
        assert [item["id"] for item in result["data"]["items"]] == ["task_match_1"]
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_resolve_date_field_rejects_untrusted_field_names():
    items_module = _load_items_module()

    with pytest.raises(Exception) as exc_info:
        items_module._resolve_date_field("ledger", "created_at); DROP TABLE items; --")

    error = exc_info.value
    assert getattr(error, "status_code", None) == 422
    assert "Invalid date_field" in getattr(error, "detail", str(error))


def test_resolve_date_field_restricts_fields_by_item_type():
    items_module = _load_items_module()

    assert items_module._resolve_date_field("task", "due_time") == "due_time"
    assert items_module._resolve_date_field("task", "created_at") == "created_at"

    with pytest.raises(Exception) as exc_info:
        items_module._resolve_date_field("task", "ledger_date")

    error = exc_info.value
    assert getattr(error, "status_code", None) == 422
    assert "task" in getattr(error, "detail", str(error))


def test_database_get_items_supports_diary_date_sort_field():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_items_diary_sort_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-diary-sort"

    try:
        db.insert_item({
            "id": "d2",
            "owner_id": owner_id,
            "type": "diary",
            "title": "后一天",
            "content": "第二篇",
            "diary_date": "2026-03-20",
            "created_at": "2026-03-18T20:00:00",
            "updated_at": "2026-03-18T20:00:00",
        })
        db.insert_item({
            "id": "d1",
            "owner_id": owner_id,
            "type": "diary",
            "title": "前一天",
            "content": "第一篇",
            "diary_date": "2026-03-19",
            "created_at": "2026-03-21T20:00:00",
            "updated_at": "2026-03-21T20:00:00",
        })

        items = db.get_items(
            owner_id,
            filters={"type": "diary", "sort_field": "diary_date", "sort_order": "ASC"},
            limit=10,
        )

        assert [item.id for item in items] == ["d1", "d2"]
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_items_api_source_maps_ledger_category_for_filters():
    src = (ROOT / "plugins" / "pendo" / "web" / "api" / "items.py").read_text(encoding="utf-8")

    assert 'def _resolve_category_field(type: Optional[str]) -> str:' in src
    assert 'return "ledger_category" if type == "ledger" else "category"' in src
    assert 'filters[_resolve_category_field(type)] = category' in src
    assert 'where.append(f"{category_field} = ?")' in src
    assert "normalize_ledger_fields(item_data, partial=False)" in src
    assert "normalize_ledger_fields(updates, partial=True)" in src


def test_normalize_ledger_fields_sets_defaults_and_rejects_invalid_amount():
    result = normalize_ledger_fields({"title": "午饭", "amount": 18.5}, partial=False)

    assert result["amount"] == 18.5
    assert result["direction"] == "expense"
    assert result["ledger_category"] == "其他"
    assert result["ledger_date"]

    with pytest.raises(ValueError, match="greater than 0"):
        normalize_ledger_fields({"title": "坏数据", "amount": 0}, partial=False)


def test_normalize_ledger_fields_rejects_invalid_update_values():
    with pytest.raises(ValueError, match="Invalid ledger direction"):
        normalize_ledger_fields({"direction": "sideways"}, partial=True)

    with pytest.raises(ValueError, match="expected YYYY-MM-DD"):
        normalize_ledger_fields({"ledger_date": "2026/03/25"}, partial=True)


def test_normalize_event_fields_ignores_legacy_leaf_fields_and_deduplicates_reminders():
    result = normalize_event_fields({
        "title": "  发布准备  ",
        "category": "项目",
        "location": "  A1  ",
        "notes": "  备注  ",
        "start_time": "2026-03-13T18:00",
        "end_time": "2026-03-14T10:00",
        "rrule": "FREQ=WEEKLY",
        "parent_id": "legacy-parent",
        "remind_policy_id": "legacy-policy",
        "milestones": [{"name": "旧节点", "time": "2026-03-14T10:00"}],
        "remind_times": [
            "2026-03-13T17:00",
            "2026-03-13T17:00",
            "2026-03-14T09:00",
        ],
    }, partial=False)

    assert result["title"] == "发布准备"
    assert result["location"] == "A1"
    assert result["notes"] == "备注"
    assert result["start_time"] == "2026-03-13T18:00:00"
    assert result["end_time"] == "2026-03-14T10:00:00"
    assert "milestones" not in result
    assert "rrule" not in result
    assert "parent_id" not in result
    assert "remind_policy_id" not in result
    assert result["remind_times"] == ["2026-03-13T17:00:00", "2026-03-13T18:00:00"]

    with pytest.raises(ValueError, match="after start_time"):
        normalize_event_fields({
            "title": "坏事件",
            "start_time": "2026-03-14T10:00",
            "end_time": "2026-03-13T10:00",
        }, partial=False)

    with pytest.raises(ValueError, match="Event start_time is required"):
        normalize_event_fields({
            "title": "旧多节点 payload",
            "milestones": [
                {"name": "节点一", "time": "2026-03-13T10:00"},
                {"name": "节点二", "time": "2026-03-13T10:00"},
            ],
        }, partial=False)


def test_event_update_route_ignores_legacy_multinode_payload_fields():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_event_legacy_payload_ignore_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-event-metadata"
    items_module = _load_items_module()

    try:
        db.insert_item(normalize_event_fields({
            "id": "ev-note",
            "owner_id": owner_id,
            "type": "event",
            "title": "学术会议",
            "category": "会议",
            "start_time": "2026-04-22T12:43:00",
            "end_time": "2026-04-26T12:00:00",
            "remind_times": ["2026-04-21T12:43:00", "2026-04-25T12:00:00"],
            "notes": "全局备注",
        }, partial=False))

        body = items_module.ItemUpdate(
            title="学术会议（更新标题）",
            milestones=[
                {"name": "会议开始", "time": "2026-04-22T12:43:00"},
                {"name": "会议结束", "time": "2026-04-26T12:00:00"},
            ],
        )

        result = items_module.update_item(
            "ev-note",
            body=body,
            owner_id=owner_id,
            db=db,
        )

        assert result["ok"] is True
        updated = db.get_item("ev-note", owner_id=owner_id)
        assert updated is not None
        assert not hasattr(updated, "milestones")
        assert not hasattr(updated, "rrule")
        assert updated.notes == "全局备注"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_normalize_task_fields_accepts_priority_five_and_manages_completed_at():
    task = normalize_task_fields({
        "title": "收尾任务",
        "category": "工作",
        "priority": 5,
        "status": "done",
        "due_time": "2026-03-26T18:00",
    }, partial=False)

    assert task["priority"] == 5
    assert task["status"] == "done"
    assert task["due_time"] == "2026-03-26T18:00:00"
    assert task["completed_at"]

    reopened = normalize_task_fields({
        **task,
        "status": "in_progress",
    }, partial=False)
    assert reopened["completed_at"] is None

    with pytest.raises(ValueError, match="Invalid task status"):
        normalize_task_fields({"title": "坏任务", "status": "stuck"}, partial=False)

    with pytest.raises(ValueError, match="due_time"):
        normalize_task_fields({"title": "坏任务", "due_time": "tomorrow"}, partial=False)


def test_item_create_model_source_accepts_nullable_text_fields():
    src = (ROOT / "plugins" / "pendo" / "web" / "api" / "items.py").read_text(encoding="utf-8")

    assert 'title: Optional[str] = ""' in src
    assert 'content: Optional[str] = ""' in src
    assert 'category: Optional[str] = None' in src


def test_task_update_route_preserves_explicit_nulls_for_clearing_fields():
    src = (ROOT / "plugins" / "pendo" / "web" / "api" / "items.py").read_text(encoding="utf-8")
    assert "model_dump(exclude_unset=True)" in src

    existing = {
        "title": "清空字段",
        "content": "旧备注",
        "category": "工作",
        "status": "todo",
        "priority": 2,
        "due_time": "2026-03-26T18:00:00",
        "created_at": "2026-03-30T09:00:00",
        "updated_at": "2026-03-30T09:00:00",
    }
    merged = dict(existing)
    merged.update({"due_time": None, "category": None, "content": None})

    updated = normalize_task_fields(merged, partial=False)

    assert updated["due_time"] is None
    assert updated["category"] == "2026-03-30"
    assert updated["content"] == ""


def test_normalize_note_fields_sets_defaults_and_deduplicates_tags():
    note = normalize_note_fields({
        "title": "  读书摘录  ",
        "content": "  很长的正文  ",
        "category": "",
        "tags": ["学习", "学习", " 阅读 ", ""],
    }, partial=False)

    assert note["title"] == "读书摘录"
    assert note["content"] == "很长的正文"
    assert note["category"] == "未分类"
    assert note["tags"] == ["学习", "阅读"]


def test_normalize_note_fields_normalizes_references_and_related_items():
    note = normalize_note_fields({
        "title": "引用笔记",
        "content": "正文",
        "references": [
            {"kind": "item", "id": " task_1 ", "type": "task", "title": " 待办标题 "},
            {"kind": "item", "id": "task_1", "type": "task", "title": "重复"},
            {"id": "event_1"},
            "bad",
        ],
        "related_items": ["event_1", "note_1", "note_1"],
    }, partial=False)

    assert note["references"] == [
        {"kind": "item", "id": "task_1", "type": "task", "title": "待办标题"},
        {"kind": "item", "id": "event_1"},
    ]
    assert note["related_items"] == ["event_1", "note_1", "task_1"]


def test_database_get_items_filters_note_tags_exactly_and_keyword():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_note_exact_tag_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-note-exact-tag"

    try:
        db.insert_item(normalize_note_fields({
            "id": "note_work",
            "owner_id": owner_id,
            "type": "note",
            "title": "工作笔记",
            "content": "普通内容",
            "tags": ["工作"],
            "created_at": "2026-04-01T10:00:00",
            "updated_at": "2026-04-01T10:00:00",
        }, partial=False))
        db.insert_item(normalize_note_fields({
            "id": "note_workflow",
            "owner_id": owner_id,
            "type": "note",
            "title": "流程笔记",
            "content": "深度流程内容",
            "tags": ["工作流"],
            "created_at": "2026-04-02T10:00:00",
            "updated_at": "2026-04-02T10:00:00",
        }, partial=False))

        tagged = db.get_items(owner_id, filters={"type": "note", "tags": "工作"}, limit=10)
        keyword = db.get_items(owner_id, filters={"type": "note", "keyword": "深度"}, limit=10)

        assert [item.id for item in tagged] == ["note_work"]
        assert [item.id for item in keyword] == ["note_workflow"]
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_database_migration_adds_note_reference_columns_to_old_items_table(tmp_path):
    db_path = tmp_path / "old_note_schema.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE items (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT,
                tags TEXT,
                category TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                context TEXT,
                visibility TEXT DEFAULT 'private',
                attachments TEXT,
                ai_meta TEXT,
                deleted INTEGER DEFAULT 0,
                deleted_at TEXT,
                start_time TEXT,
                end_time TEXT,
                timezone TEXT,
                location TEXT,
                participants TEXT,
                remind_times TEXT,
                reminder_rules TEXT,
                event_role TEXT,
                event_collection_id TEXT,
                event_collection_kind TEXT,
                event_index INTEGER,
                event_node_key TEXT,
                source_item_id TEXT,
                due_time TEXT,
                priority INTEGER,
                status TEXT,
                estimate INTEGER,
                subtasks TEXT,
                dependencies TEXT,
                progress INTEGER DEFAULT 0,
                completed_at TEXT,
                mood TEXT,
                mood_score INTEGER,
                weather TEXT,
                template_id TEXT,
                diary_date TEXT,
                notes TEXT,
                amount REAL,
                direction TEXT,
                ledger_category TEXT,
                payment_method TEXT,
                ledger_date TEXT,
                remark TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    db = Database(str(db_path))
    try:
        item_id = db.insert_item(normalize_note_fields({
            "id": "note_ref_migrated",
            "owner_id": "u-note-migration",
            "type": "note",
            "title": "迁移后引用",
            "content": "正文",
            "references": [{"kind": "item", "id": "task_1"}],
            "related_items": ["task_1"],
            "created_at": "2026-04-01T10:00:00",
            "updated_at": "2026-04-01T10:00:00",
        }, partial=False))

        item = db.get_item(item_id, owner_id="u-note-migration")
        assert item.references == [{"kind": "item", "id": "task_1"}]
        assert item.related_items == ["task_1"]
    finally:
        db.cleanup()


def test_item_update_note_logs_edit_details_for_web_undo():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_note_web_edit_log_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-note-web-edit-log"
    items_module = _load_items_module()

    try:
        db.insert_item(normalize_note_fields({
            "id": "note_edit",
            "owner_id": owner_id,
            "type": "note",
            "title": "旧标题",
            "content": "旧正文",
            "category": "工作",
            "tags": ["旧"],
            "created_at": "2026-04-01T10:00:00",
            "updated_at": "2026-04-01T10:00:00",
        }, partial=False))
        db.insert_item(normalize_task_fields({
            "id": "task_1",
            "owner_id": owner_id,
            "type": "task",
            "title": "关联待办",
            "content": "",
            "status": "todo",
            "priority": 3,
            "created_at": "2026-04-01T11:00:00",
            "updated_at": "2026-04-01T11:00:00",
        }, partial=False))

        result = items_module.update_item(
            "note_edit",
            body=items_module.ItemUpdate(
                content="新正文",
                references=[{"kind": "item", "id": "task_1"}],
                related_items=["task_1"],
            ),
            owner_id=owner_id,
            db=db,
        )

        assert result["ok"] is True
        updated = db.get_item("note_edit", owner_id=owner_id)
        assert updated.content == "新正文"
        assert updated.related_items == ["task_1"]
        assert updated.references == [
            {"kind": "item", "id": "task_1", "type": "task", "title": "关联待办"}
        ]

        row = db.get_connection().execute(
            "SELECT action, details FROM operation_logs WHERE item_id = ? ORDER BY id DESC LIMIT 1",
            ("note_edit",),
        ).fetchone()
        details = json.loads(row[1])
        assert row[0] == "edit_note"
        assert details["old_values"]["content"] == "旧正文"
        assert details["updates"]["content"] == "新正文"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_normalize_diary_fields_requires_content_and_clears_optional_values():
    diary = normalize_diary_fields({
        "diary_date": "2026-03-26",
        "title": "  夜晚散步  ",
        "content": "  今天散步很舒服。  ",
        "location": "  江边  ",
        "mood": "😊",
        "weather": "☀️ 晴",
        "mood_score": "8",
        "template_id": "",
    }, partial=False)

    assert diary["diary_date"] == "2026-03-26"
    assert diary["title"] == "夜晚散步"
    assert diary["content"] == "今天散步很舒服。"
    assert diary["location"] == "江边"
    assert diary["mood"] == "😊"
    assert diary["weather"] == "☀️ 晴"
    assert diary["mood_score"] == 8
    assert diary["template_id"] is None

    cleared = normalize_diary_fields({
        **diary,
        "title": "",
        "location": None,
        "weather": "",
        "mood_score": "",
    }, partial=False)

    assert cleared["title"] == ""
    assert cleared["location"] == ""
    assert cleared["weather"] == ""
    assert cleared["mood_score"] is None

    with pytest.raises(ValueError, match="Diary content cannot be empty"):
        normalize_diary_fields({"diary_date": "2026-03-26", "content": ""}, partial=False)

    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        normalize_diary_fields({"diary_date": "2026/03/26", "content": "正文"}, partial=False)

    with pytest.raises(ValueError, match="between 1 and 10"):
        normalize_diary_fields({"diary_date": "2026-03-26", "content": "正文", "mood_score": 11}, partial=False)


def test_items_api_source_supports_note_filters_and_normalization():
    src = (ROOT / "plugins" / "pendo" / "web" / "api" / "items.py").read_text(encoding="utf-8")

    assert 'tags: Optional[str] = None' in src
    assert 'filters["tags"] = tags' in src
    assert "normalize_note_fields(item_data, partial=False)" in src
    assert "normalized = normalize_note_fields(merged, partial=False)" in src
    assert "resolve_default_category(db, owner_id)" in src
    assert 'category_field = _resolve_category_field(type)' in src
    assert 'SELECT DISTINCT {category_field}' in src


def test_items_api_source_supports_diary_normalization_and_same_day_conflict():
    src = (ROOT / "plugins" / "pendo" / "web" / "api" / "items.py").read_text(encoding="utf-8")

    assert "normalize_diary_fields(item_data, partial=False)" in src
    assert "normalized = normalize_diary_fields(merged, partial=False)" in src
    assert "Diary already exists for this date" in src
    assert "db.has_diary_for_date(owner_id, diary_date)" in src
    assert '"diary_date"' in src


def test_event_validation_rejects_invalid_merged_update_values():
    existing = normalize_event_fields({
        "title": "好事件",
        "category": "会议",
        "start_time": "2026-03-26T10:00:00",
        "end_time": "2026-03-26T11:00:00",
        "remind_times": ["2026-03-26T09:00:00"],
    }, partial=False)

    merged = dict(existing)
    merged.update({"end_time": "2026-03-26T08:00:00"})

    with pytest.raises(ValueError, match="end_time"):
        normalize_event_fields(merged, partial=False)


def test_item_create_event_accepts_reminder_rules_and_builds_cache():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_event_reminder_rules_create_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-event-rules-create"
    items_module = _load_items_module()

    try:
        body = items_module.ItemCreate(
            type="event",
            title="规则提醒",
            category="会议",
            start_time="2030-01-02T09:00:00",
            reminder_rules=[{"offset_seconds": 3600}, {"offset_seconds": 0}],
        )

        result = items_module.create_item(body=body, owner_id=owner_id, db=db)

        assert result["ok"] is True
        event = db.get_item(result["data"]["id"], owner_id=owner_id)
        assert event.reminder_rules == [
            {"offset_seconds": 3600},
            {"offset_seconds": 0},
        ]
        assert event.remind_times == [
            "2030-01-02T08:00:00",
            "2030-01-02T09:00:00",
        ]
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_item_update_event_rebuilds_stale_remind_times_from_existing_rules():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_event_reminder_rules_update_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-event-rules-update"
    items_module = _load_items_module()

    try:
        db.insert_item(
            normalize_event_fields(
                {
                    "id": "ev-rules",
                    "owner_id": owner_id,
                    "type": "event",
                    "title": "移动时间",
                    "category": "会议",
                    "start_time": "2030-01-02T09:00:00",
                    "reminder_rules": [{"offset_seconds": 3600}, {"offset_seconds": 0}],
                },
                partial=False,
            )
        )

        body = items_module.ItemUpdate(
            start_time="2030-01-03T10:00:00",
            remind_times=["2030-01-02T08:00:00", "2030-01-02T09:00:00"],
        )

        result = items_module.update_item("ev-rules", body=body, owner_id=owner_id, db=db)

        assert result["ok"] is True
        event = db.get_item("ev-rules", owner_id=owner_id)
        assert event.reminder_rules == [
            {"offset_seconds": 3600},
            {"offset_seconds": 0},
        ]
        assert event.remind_times == [
            "2030-01-03T09:00:00",
            "2030-01-03T10:00:00",
        ]
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_event_reminder_log_sync_preserves_sent_history_but_excludes_removed_reminders_from_repeat_queue():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_event_reminder_sync_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-event-sync"

    try:
        db.insert_item({
            "id": "ev1",
            "owner_id": owner_id,
            "type": "event",
            "title": "提醒同步",
            "category": "会议",
            "start_time": "2026-03-26T10:00:00",
            "end_time": "2026-03-26T11:00:00",
            "remind_times": ["2026-03-26T09:00:00", "2026-03-26T09:30:00"],
        })
        db.log_reminder("ev1", "2026-03-26T09:00:00", sent=True)
        db.log_reminder("ev1", "2026-03-26T09:30:00", sent=True)

        db.update_item("ev1", {"remind_times": ["2026-03-26T09:30:00"]}, owner_id=owner_id)

        logs = db.get_reminder_logs("ev1")
        queued = db.get_unconfirmed_sent_reminders()
        assert [row["remind_time"] for row in logs] == [
            "2026-03-26T09:00:00",
            "2026-03-26T09:30:00",
        ]
        assert [row["remind_time"] for row in queued] == ["2026-03-26T09:30:00"]

        assert db.delete_item("ev1", soft=True, owner_id=owner_id) is True
        assert db.get_reminder_logs("ev1") == []
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_build_ledger_insights_uses_filtered_ledger_category_and_builds_svg_data():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_ledger_insights_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-insights"

    try:
        db.insert_item({
            "id": "e1",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "早餐",
            "amount": 12,
            "direction": "expense",
            "ledger_category": "餐饮",
            "ledger_date": "2026-03-20",
            "created_at": "2026-03-20T08:00:00",
        })
        db.insert_item({
            "id": "e2",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "午餐",
            "amount": 24,
            "direction": "expense",
            "ledger_category": "餐饮",
            "ledger_date": "2026-03-20",
            "created_at": "2026-03-20T12:00:00",
        })
        db.insert_item({
            "id": "e3",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "地铁",
            "amount": 6,
            "direction": "expense",
            "ledger_category": "交通",
            "ledger_date": "2026-03-21",
            "created_at": "2026-03-21T09:00:00",
        })
        db.insert_item({
            "id": "i1",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "工资",
            "amount": 5000,
            "direction": "income",
            "ledger_category": "工资",
            "ledger_date": "2026-03-21",
            "created_at": "2026-03-21T10:00:00",
        })

        result = build_ledger_insights(
            db=db,
            owner_id=owner_id,
            category="餐饮",
            start_date="2026-03-20",
            end_date="2026-03-21",
        )

        assert result["summary"]["expense_total"] == 36
        assert result["summary"]["income_total"] == 0
        assert result["summary"]["focus_direction"] == "expense"
        assert result["summary"]["focus_count"] == 2
        assert len(result["expense_categories"]) == 1
        assert result["expense_categories"][0]["category"] == "餐饮"
        assert result["expense_categories"][0]["share"] == 1
        assert [point["total"] for point in result["expense_timeline"]] == [36, 0]
        assert len(result["expense_candles"]) == 1
        assert result["expense_candles"][0]["open"] == 12
        assert result["expense_candles"][0]["close"] == 24
        assert result["expense_candles"][0]["high"] == 24
        assert result["expense_candles"][0]["low"] == 12
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_build_ledger_insights_switches_focus_with_income_filter():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_ledger_income_insights_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-income-insights"

    try:
        for item in [
            {
                "id": "income-1",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "稿费",
                "amount": 500,
                "direction": "income",
                "ledger_category": "副业",
                "ledger_date": "2026-03-02",
                "created_at": "2026-03-02T09:00:00",
            },
            {
                "id": "income-2",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "奖金",
                "amount": 800,
                "direction": "income",
                "ledger_category": "奖金",
                "ledger_date": "2026-03-18",
                "created_at": "2026-03-18T09:00:00",
            },
            {
                "id": "expense-1",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "午饭",
                "amount": 30,
                "direction": "expense",
                "ledger_category": "餐饮",
                "ledger_date": "2026-03-10",
                "created_at": "2026-03-10T12:00:00",
            },
        ]:
            db.insert_item(item)

        result = build_ledger_insights(
            db=db,
            owner_id=owner_id,
            direction="income",
            start_date="2026-03-01",
            end_date="2026-03-31",
        )

        assert result["summary"]["focus_direction"] == "income"
        assert result["summary"]["focus_total"] == 1300
        assert result["summary"]["focus_count"] == 2
        assert [item["category"] for item in result["expense_categories"]] == ["奖金", "副业"]
        timeline = {point["key"]: point["total"] for point in result["expense_timeline"] if point["total"]}
        assert timeline == {
            "2026-03-02": 500,
            "2026-03-18": 800,
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_build_ledger_insights_year_mode_compares_against_last_year_to_date():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_ledger_year_compare_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-year-compare"

    try:
        db.insert_item({
            "id": "cy1",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "今年一月",
            "amount": 100,
            "direction": "expense",
            "ledger_category": "餐饮",
            "ledger_date": "2026-01-10",
            "created_at": "2026-01-10T10:00:00",
        })
        db.insert_item({
            "id": "cy2",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "今年三月",
            "amount": 50,
            "direction": "expense",
            "ledger_category": "交通",
            "ledger_date": "2026-03-10",
            "created_at": "2026-03-10T10:00:00",
        })
        db.insert_item({
            "id": "py1",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "去年同期",
            "amount": 100,
            "direction": "expense",
            "ledger_category": "餐饮",
            "ledger_date": "2025-02-10",
            "created_at": "2025-02-10T10:00:00",
        })
        db.insert_item({
            "id": "pp1",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "上一周期高支出",
            "amount": 500,
            "direction": "expense",
            "ledger_category": "服务",
            "ledger_date": "2025-11-10",
            "created_at": "2025-11-10T10:00:00",
        })

        result = build_ledger_insights(
            db=db,
            owner_id=owner_id,
            start_date="2026-01-01",
            end_date="2026-03-25",
            compare_mode="previous_year_to_date",
        )

        assert result["summary"]["expense_total"] == 150
        assert result["summary"]["delta_label"] == "较去年同期"
        assert result["summary"]["delta_vs_previous"] == 0.5
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_build_ledger_insights_month_bucket_orders_candles_by_ledger_date_not_created_at():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_ledger_month_candles_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-month-candles"

    try:
        for item in [
            {
                "id": "m-backfill",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "月初补录",
                "amount": 10,
                "direction": "expense",
                "ledger_category": "餐饮",
                "ledger_date": "2026-03-01",
                "created_at": "2026-04-01T09:00:00",
            },
            {
                "id": "m-mid",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "月中消费",
                "amount": 25,
                "direction": "expense",
                "ledger_category": "餐饮",
                "ledger_date": "2026-03-05",
                "created_at": "2026-03-05T09:00:00",
            },
            {
                "id": "m-end",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "月底消费",
                "amount": 40,
                "direction": "expense",
                "ledger_category": "交通",
                "ledger_date": "2026-03-28",
                "created_at": "2026-03-28T09:00:00",
            },
        ]:
            db.insert_item(item)

        result = build_ledger_insights(
            db=db,
            owner_id=owner_id,
            start_date="2026-01-01",
            end_date="2026-03-31",
        )

        assert result["summary"]["bucket_mode"] == "month"
        assert result["expense_timeline"][-1]["key"] == "2026-03"
        assert result["expense_timeline"][-1]["total"] == 75
        assert result["expense_candles"][-1]["label"] == "2026-03"
        assert result["expense_candles"][-1]["open"] == 10
        assert result["expense_candles"][-1]["close"] == 40
        assert result["expense_candles"][-1]["high"] == 40
        assert result["expense_candles"][-1]["low"] == 10
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_build_ledger_insights_clips_current_period_visuals_to_today():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_ledger_current_period_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-current-period"

    class _FrozenDateTime(importlib.import_module("datetime").datetime):
        @classmethod
        def now(cls):
            return cls(2026, 4, 8, 10, 0, 0)

    original_datetime = ledger_insights_module.datetime
    ledger_insights_module.datetime = _FrozenDateTime

    try:
        for item in [
            {
                "id": "apr-1",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "月初消费",
                "amount": 48,
                "direction": "expense",
                "ledger_category": "餐饮",
                "ledger_date": "2026-04-01",
                "created_at": "2026-04-01T09:00:00",
            },
            {
                "id": "apr-8",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "今天消费",
                "amount": 28,
                "direction": "expense",
                "ledger_category": "交通",
                "ledger_date": "2026-04-08",
                "created_at": "2026-04-08T09:00:00",
            },
        ]:
            db.insert_item(item)

        result = build_ledger_insights(
            db=db,
            owner_id=owner_id,
            start_date="2026-04-01",
            end_date="2026-04-30",
        )

        assert result["summary"]["focus_total"] == 76
        assert result["summary"]["peak_bucket_label"] == "4/1"
        assert result["expense_timeline"][0]["key"] == "2026-04-01"
        assert result["expense_timeline"][-1]["key"] == "2026-04-08"
        assert len(result["expense_timeline"]) == 8
        assert result["expense_candles"][-1]["key"] == "2026-04-08"
    finally:
        ledger_insights_module.datetime = original_datetime
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_ledger_page_source_requests_insights_component():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "ledger.js").read_text(encoding="utf-8")

    assert "renderLedgerInsightsPanel" in src
    assert "/stats/ledger/insights" in src
    assert "compareModeForFilter" in src
    assert "ledger-sort-amount" in src
    assert "_sortMode === 'amount' ? 'amount' : 'ledger_date'" in src
    assert "await loadAndRender(true);" in src
    assert "if (changedType && changedType !== 'ledger') return;" in src
    assert "if (_dateFilter !== 'all') {" in src


def test_ledger_page_source_uses_unified_time_presets_with_today():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "ledger.js").read_text(encoding="utf-8")

    assert "{ value: 'today',  label: '今天' }" in src
    assert "{ value: 'week',   label: '本周' }" in src
    assert "{ value: 'month',  label: '本月' }" in src
    assert "{ value: 'quarter', label: '本季' }" in src
    assert "{ value: 'year',   label: '今年' }" in src
    assert "{ value: 'last_year', label: '去年' }" in src
    assert "{ value: 'custom', label: '自定义' }" in src
    assert "{ value: 'all',    label: '全部' }" in src
    assert "import { derivePresetRange, todayRangeKey } from '../utils/date_ranges.js';" in src
    assert "const range = derivePresetRange(filter, {" in src
    assert "today: todayStr()," in src
    assert "current_month" not in src
    assert "近30天" not in src


def test_ledger_page_source_stabilizes_quick_add_and_custom_range_layout():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "ledger.js").read_text(encoding="utf-8")

    assert "box-sizing: border-box;" in src
    assert "--ledger-qa-direction-width: 128px;" in src
    assert "--ledger-qa-control-width: 176px;" in src
    assert "display: flex;" in src
    assert "flex-wrap: wrap;" in src
    assert ".ledger-quick-add .pselect { width: auto; max-width: 100%; }" in src
    assert "width: var(--ledger-qa-direction-width);" in src
    assert "width: min(100%, var(--ledger-qa-control-width));" in src
    assert "--ledger-qa-direction-width: 108px;" in src
    assert "--ledger-qa-control-width: 136px;" in src
    assert "grid-template-columns: minmax(0, 1fr);" in src
    assert "width: 100%;" in src
    assert "max-width: 100%;" in src
    assert "--ledger-qa-control-width: 100%;" not in src
    assert "if (group) group.style.display = val === 'custom' ? 'grid' : 'none';" not in src
    assert "grid-column: 1 / -1;" in src
    assert "--ledger-filter-select-width: 170px;" in src
    assert "--ledger-filter-control-width: 196px;" in src
    assert "--ledger-filter-amount-width: 312px;" in src
    assert "display: flex;" in src
    assert "width: min(100%, var(--ledger-filter-amount-width));" in src
    assert "grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);" in src
    assert ".ledger-filter-date { width: 108px; flex: 0 0 108px; }" not in src
    assert ".ledger-insight-svg-ring {" in src
    assert "max-width: min(200px, 52vw);" in src
    assert ".ledger-insight-y-label {" in src
    assert "clamp(14px, 4.2vw, 18px)" in src
    assert "clamp(13px, 3.8vw, 17px)" in src


def test_pendo_web_pages_use_unified_xl_mobile_phone_breakpoints():
    roots = [
        ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages",
        ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "components",
    ]
    legacy_tokens = [
        "BREAKPOINTS.WIDE",
        "BREAKPOINTS.NARROW",
        "BREAKPOINTS.COMPACT",
        "BREAKPOINTS.FORM",
        "BREAKPOINTS.SEARCH",
        "BREAKPOINTS.DASHBOARD",
        "BREAKPOINTS.DESKTOP",
        "BREAKPOINTS.STATS_SMALL",
    ]

    for root in roots:
        for path in root.rglob("*.js"):
            src = path.read_text(encoding="utf-8")
            for token in legacy_tokens:
                assert token not in src, f"{path} still uses legacy breakpoint {token}"


def test_search_page_source_uses_soft_icon_backgrounds():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "search.js").read_text(encoding="utf-8")

    assert "function alphaColor(hex, alpha = 0.12)" in src
    assert "const iconBg = alphaColor(cfg.color, 0.14);" in src
    assert "const iconBorder = alphaColor(cfg.color, 0.2);" not in src
    assert ".search-card-icon {" in src
    assert "border: 1px solid transparent;" not in src
    assert 'style="background:${iconBg};color:${cfg.color};"' in src


def test_app_source_uses_subtle_back_to_top_button_styles():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert "const BACK_TO_TOP_THEME = {" in src
    assert "width: 38px;" in src
    assert "height: 38px;" in src
    assert "--btt-accent: var(--color-dashboard);" in src
    assert "background: color-mix(in srgb, var(--btt-accent) 68%, transparent);" in src
    assert "-webkit-tap-highlight-color: transparent;" in src
    assert "#back-to-top:focus-visible {" in src
    assert "color-mix(in srgb, var(--btt-accent) 16%, transparent);" in src
    assert "applyTheme(getCurrentPage());" in src
    assert "onRouteChange((path) => applyTheme(path));" in src


def test_app_source_extracts_token_from_pasted_message():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "plugins" / "pendo" / "web" / "static" / "index.html").read_text(encoding="utf-8")

    assert "function extractLoginToken(rawValue)" in src
    assert "A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+" in src
    assert "const token = extractLoginToken(input.value);" in src
    assert "if (token !== input.value.trim()) {" in src
    assert "直接粘贴整条回复消息" in html


def test_ledger_insights_component_uses_time_scaled_candle_axis():
    src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "components" / "ledger_insights.js").read_text(encoding="utf-8")

    assert "function pickEvenAxisIndexes(length, maxLabels)" in src
    assert "const safeMaxLabels = Math.max(maxLabels, 2);" in src
    assert "const step = Math.max(1, Math.ceil((length - 1) / (safeMaxLabels - 1)));" in src
    assert "for (let index = 0; index < length && picked.length < maxLabels; index += step) {" in src
    assert "Math.round(index * step)" not in src
    assert "const labelIndexes = pickEvenAxisIndexes(coords.length, 5);" in src
    assert "function formatAxisLabel(key)" in src
    assert "function buildCandleSvg(candles, rangeKeys = [])" in src
    assert "const domainKeys = Array.isArray(rangeKeys) && rangeKeys.length ? rangeKeys : candles.map((item) => item.key);" in src
    assert "const domainIndex = new Map(domainKeys.map((key, index) => [key, index]));" in src
    assert "const domainStep = domainKeys.length > 1 ? innerWidth / (domainKeys.length - 1) : innerWidth;" in src
    assert "const candleDomainIndex = domainIndex.get(item.key) ?? index;" in src
    assert "const labelIndexes = pickEvenAxisIndexes(domainKeys.length, 5);" in src
    assert "buildCandleSvg(candles, timeline.map((item) => item.key))" in src
    assert "const focusDirection = summary.focus_direction === 'income' ? 'income' : 'expense';" in src
    assert "const focusLabel = focusDirection === 'income' ? '收入' : '支出';" in src


def test_items_api_source_normalizes_events_before_create_and_update():
    src = (ROOT / "plugins" / "pendo" / "web" / "api" / "items.py").read_text(encoding="utf-8")

    assert "normalize_event_fields(item_data, partial=False)" in src
    assert "normalized = normalize_event_fields(merged, partial=False)" in src


def test_items_api_source_normalizes_tasks_before_create_and_update():
    src = (ROOT / "plugins" / "pendo" / "web" / "api" / "items.py").read_text(encoding="utf-8")

    assert "normalize_task_fields(item_data, partial=False)" in src
    assert "normalized = normalize_task_fields(merged, partial=False)" in src
