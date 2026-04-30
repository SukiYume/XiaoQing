"""Regression tests for the pendo review findings."""

import asyncio
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from plugins.pendo.handlers.note import NoteHandler
from plugins.pendo.handlers.task import TaskHandler
from plugins.pendo.services.ai_parser import AIParser
from plugins.pendo.services.db import Database
from plugins.pendo.services.reminder import ReminderService

ROOT = Path(__file__).resolve().parents[2]


def _make_temp_db(prefix: str) -> tuple[Path, Database]:
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"{prefix}_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir, Database(str(temp_dir / "pendo.db"))


def test_search_items_applies_date_range_filters():
    temp_dir, db = _make_temp_db("pendo_review_search_range")
    owner_id = "u-search-range"

    try:
        db.insert_item(
            {
                "id": "note_april",
                "owner_id": owner_id,
                "type": "note",
                "title": "会议纪要",
                "content": "四月版本",
                "created_at": "2026-04-10T09:00:00",
                "updated_at": "2026-04-10T09:00:00",
            }
        )
        db.insert_item(
            {
                "id": "note_january",
                "owner_id": owner_id,
                "type": "note",
                "title": "会议纪要",
                "content": "一月版本",
                "created_at": "2026-01-10T09:00:00",
                "updated_at": "2026-01-10T09:00:00",
            }
        )

        results = db.search_items(
            owner_id,
            "会议",
            filters={
                "type": "note",
                "date_field": "created_at",
                "start_date": "2026-04-01T00:00:00",
                "end_date": "2026-04-30T23:59:59",
            },
            limit=10,
        )

        assert [item.id for item in results] == ["note_april"]
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_ai_parser_build_remind_times_uses_user_timezone(monkeypatch):
    from plugins.pendo.services import ai_parser as ai_parser_module

    la_tz = ZoneInfo("America/Los_Angeles")

    class _ServerDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = cls(2030, 1, 2, 0, 30, 0)
            return base if tz is None else base.replace(tzinfo=tz)

    monkeypatch.setattr(ai_parser_module, "datetime", _ServerDatetime)
    monkeypatch.setattr(
        ai_parser_module,
        "now_in_timezone",
        lambda user_id=None, db=None: datetime(2030, 1, 1, 8, 30, 0, tzinfo=la_tz),
    )
    monkeypatch.setattr(
        ai_parser_module,
        "parse_and_localize",
        lambda dt_str, user_id=None, db=None: datetime.fromisoformat(dt_str).replace(tzinfo=la_tz),
    )

    parser = AIParser()

    remind_times = parser.build_remind_times_from_offsets(
        "2030-01-01T10:00:00",
        ["提前1小时"],
        user_id="u-la",
    )

    assert remind_times == ["2030-01-01T09:00:00-08:00"]


def test_ai_parser_builds_semantic_reminder_rules_from_description():
    parser = AIParser()

    rules = parser.build_reminder_rules_from_description("提前1天和2小时提醒")

    assert rules == [
        {"offset_seconds": 86400},
        {"offset_seconds": 7200},
        {"offset_seconds": 0},
    ]


def test_reminder_dispatch_uses_owner_timezone(monkeypatch):
    from plugins.pendo.services import reminder as reminder_module

    la_tz = ZoneInfo("America/Los_Angeles")
    shanghai_tz = ZoneInfo("Asia/Shanghai")

    monkeypatch.setattr(
        reminder_module,
        "now_in_timezone",
        lambda user_id=None, db=None: (
            datetime(2030, 1, 1, 9, 0, 0, tzinfo=la_tz)
            if user_id == "u-la"
            else datetime(2030, 1, 2, 1, 0, 0, tzinfo=shanghai_tz)
        ),
    )
    monkeypatch.setattr(
        reminder_module,
        "parse_and_localize",
        lambda dt_str, user_id=None, db=None: datetime.fromisoformat(dt_str).replace(
            tzinfo=la_tz if user_id == "u-la" else shanghai_tz
        ),
    )

    item = SimpleNamespace(
        id="evt-la",
        owner_id="u-la",
        title="Morning sync",
        start_time="2030-01-01T10:00:00",
        end_time="2030-01-01T11:00:00",
        remind_times=["2030-01-01T09:00:00"],
        context={},
        location="Room 1",
        notes="",
        tags=[],
    )

    class _FakeDb:
        def get_all_events_with_reminders(self, future_hours=0):
            return [item]

        def get_user_settings(self, user_id):
            assert user_id == "u-la"
            return {
                "timezone": "America/Los_Angeles",
                "quiet_hours_start": "23:00",
                "quiet_hours_end": "07:00",
                "settings_json": {"reminder_enabled": True},
            }

        def get_reminder_logs(self, item_id):
            assert item_id == "evt-la"
            return []

        def is_reminder_sent(self, item_id, remind_time):
            return False

        def get_unconfirmed_sent_reminders(self):
            return []

        def get_item(self, item_id):
            return item

    result = ReminderService(db=_FakeDb()).check_and_send_reminders()

    assert result["sent"] == 1
    assert result["messages"][0]["user_id"] == "u-la"
    assert result["messages"][0]["item_id"] == "evt-la"


def test_reminder_service_skips_closed_tasks():
    done_task = SimpleNamespace(
        id="task-done",
        owner_id="u-task",
        type="task",
        status="done",
        title="已完成任务",
        remind_times=["2030-01-01T09:00:00"],
        context={},
    )

    class _FakeDb:
        def get_all_events_with_reminders(self, future_hours=0):
            return [done_task]

        def get_user_settings(self, user_id):
            raise AssertionError("closed task should not load reminder settings")

        def get_unconfirmed_sent_reminders(self):
            return []

    result = ReminderService(db=_FakeDb()).check_and_send_reminders()

    assert result["sent"] == 0
    assert result["messages"] == []


def test_reminder_repeats_skip_closed_tasks():
    cancelled_task = SimpleNamespace(
        id="task-cancelled",
        owner_id="u-task",
        type="task",
        status="cancelled",
        title="已取消任务",
        remind_times=["2030-01-01T09:00:00"],
        context={},
    )

    class _FakeDb:
        def get_unconfirmed_sent_reminders(self):
            return [{
                "item_id": "task-cancelled",
                "remind_time": "2030-01-01T09:00:00",
                "repeat_count": 1,
                "last_sent_at": "2030-01-01T09:00:00",
            }]

        def get_item(self, item_id):
            return cancelled_task

    messages = ReminderService(db=_FakeDb())._check_unconfirmed_repeats(
        current_time=datetime(2030, 1, 1, 9, 10, 0),
    )

    assert messages == []


def test_ledger_cli_edit_recomputes_amount_cents_when_amount_changes():
    from plugins.pendo.handlers.ledger import LedgerHandler

    temp_dir, db = _make_temp_db("pendo_review_ledger_amount_edit")
    owner_id = "u-ledger-cli"

    try:
        db.insert_item({
            "id": "ledger_amount",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "午饭",
            "amount": 12.34,
            "amount_cents": 1234,
            "transaction_type": "expense",
            "currency": "CNY",
            "ledger_category": "餐饮",
            "ledger_date": "2026-04-29",
            "account_name": "现金",
            "created_at": "2026-04-29T12:00:00",
            "updated_at": "2026-04-29T12:00:00",
        })

        result = asyncio.run(
            LedgerHandler(db=db).edit_ledger(
                owner_id,
                "ledger_amount amount:56.78",
                SimpleNamespace(),
            )
        )
        item = db.get_item("ledger_amount", owner_id=owner_id)

        assert result["status"] == "success"
        assert item.amount == 56.78
        assert item.amount_cents == 5678
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_daily_briefing_orders_urgent_tasks_first():
    temp_dir, db = _make_temp_db("pendo_review_briefing_priority")
    owner_id = "u-briefing-priority"

    try:
        db.insert_item(
            {
                "id": "task-urgent",
                "owner_id": owner_id,
                "type": "task",
                "title": "Fix incident",
                "category": "工作",
                "status": "open",
                "priority": 1,
                "plan_date": "2026-04-23",
                "deadline_at": "2026-04-23T18:00:00",
                "created_at": "2026-04-23T09:00:00",
                "updated_at": "2026-04-23T09:00:00",
            }
        )
        db.insert_item(
            {
                "id": "task-later",
                "owner_id": owner_id,
                "type": "task",
                "title": "Tidy backlog",
                "category": "工作",
                "status": "open",
                "priority": 4,
                "plan_date": "2026-04-23",
                "deadline_at": "2026-04-23T18:00:00",
                "created_at": "2026-04-23T09:05:00",
                "updated_at": "2026-04-23T09:05:00",
            }
        )

        _events, tasks, _overdue = db.get_briefing_items(
            owner_id,
            "2026-04-23T00:00:00",
            "2026-04-24T00:00:00",
        )

        assert [task.id for task in tasks] == ["task-urgent", "task-later"]
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_daily_briefing_excludes_cancelled_tasks():
    temp_dir, db = _make_temp_db("pendo_review_briefing_cancelled")
    owner_id = "u-briefing-cancelled"

    try:
        db.insert_item(
            {
                "id": "task-cancelled-today",
                "owner_id": owner_id,
                "type": "task",
                "title": "Cancelled today",
                "category": "工作",
                "status": "cancelled",
                "priority": 1,
                "plan_date": "2026-04-23",
                "deadline_at": "2026-04-22T18:00:00",
                "cancelled_at": "2026-04-22T09:00:00",
                "created_at": "2026-04-22T08:00:00",
                "updated_at": "2026-04-22T09:00:00",
            }
        )
        db.insert_item(
            {
                "id": "task-open-today",
                "owner_id": owner_id,
                "type": "task",
                "title": "Open today",
                "category": "工作",
                "status": "open",
                "priority": 3,
                "plan_date": "2026-04-23",
                "created_at": "2026-04-23T09:00:00",
                "updated_at": "2026-04-23T09:00:00",
            }
        )

        _events, tasks, overdue = db.get_briefing_items(
            owner_id,
            "2026-04-23T00:00:00",
            "2026-04-24T00:00:00",
        )

        assert [task.id for task in tasks] == ["task-open-today"]
        assert overdue == []
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_daily_migration_updates_plan_date_and_invalidates_task_cache():
    from plugins.pendo.commands import scheduled as scheduled_module

    temp_dir, db = _make_temp_db("pendo_review_task_migration_cache")
    owner_id = "u-task-migration-cache"

    try:
        db.insert_item(
            {
                "id": "task-yesterday",
                "owner_id": owner_id,
                "type": "task",
                "title": "Move me",
                "category": "工作",
                "status": "open",
                "priority": 3,
                "plan_date": "2026-04-28",
                "created_at": "2026-04-28T08:00:00",
                "updated_at": "2026-04-28T08:00:00",
            }
        )
        assert db.get_item("task-yesterday", owner_id).plan_date == "2026-04-28"
        assert db.get_items(owner_id, filters={"type": "task"}, limit=10)[0].plan_date == "2026-04-28"

        migrated = asyncio.run(
            scheduled_module._batch_migrate_tasks_to_date(
                db,
                [SimpleNamespace(id="task-yesterday")],
                "2026-04-29",
                owner_id,
            )
        )

        assert migrated == 1
        assert db.get_item("task-yesterday", owner_id).plan_date == "2026-04-29"
        assert db.get_items(owner_id, filters={"type": "task"}, limit=10)[0].plan_date == "2026-04-29"
        raw = db.get_connection().execute(
            "SELECT category FROM items WHERE id = ?",
            ("task-yesterday",),
        ).fetchone()
        assert raw["category"] == "工作"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_task_parser_uses_user_timezone_for_default_plan_date(monkeypatch):
    from plugins.pendo.handlers import task as task_module
    from plugins.pendo.utils import validators as validators_module

    class _ServerDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = cls(2030, 1, 1, 10, 0, 0)
            return base if tz is None else base.replace(tzinfo=tz)

    monkeypatch.setattr(validators_module, "datetime", _ServerDatetime)
    monkeypatch.setattr(
        task_module,
        "now_in_timezone",
        lambda user_id, db: datetime(2030, 1, 1, 21, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles")),
    )

    handler = TaskHandler(db=SimpleNamespace())

    parsed = handler._parse_task_text("Write weekly recap", "u-la")

    assert parsed["category"] == "未分类"
    assert parsed["plan_date"] == "2030-01-02"


def test_task_today_shortcut_uses_user_timezone(monkeypatch):
    from plugins.pendo.handlers import task as task_module

    class _ServerDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = cls(2030, 1, 2, 13, 0, 0)
            return base if tz is None else base.replace(tzinfo=tz)

    class _ItemsRepo:
        def __init__(self):
            self.captured_filters = None

        def get_items(self, owner_id, filters, limit):
            self.captured_filters = filters
            return [
                SimpleNamespace(
                    id="la",
                    title="LA today",
                    status="open",
                    priority=3,
                    plan_date="2030-01-01",
                    deadline_at=None,
                    category="工作",
                    created_at="2030-01-01T08:00:00",
                ),
                SimpleNamespace(
                    id="server",
                    title="Server tomorrow",
                    status="open",
                    priority=3,
                    plan_date="2030-01-02",
                    deadline_at=None,
                    category="工作",
                    created_at="2030-01-02T08:00:00",
                ),
            ]

    items_repo = _ItemsRepo()
    handler = TaskHandler(db=SimpleNamespace(items=items_repo))

    monkeypatch.setattr(task_module, "datetime", _ServerDatetime)
    monkeypatch.setattr(
        task_module,
        "now_in_timezone",
        lambda user_id, db: datetime(2030, 1, 1, 23, 30, 0, tzinfo=ZoneInfo("America/Los_Angeles")),
    )

    result = asyncio.run(handler.list_tasks("u-la", "today", SimpleNamespace()))

    assert items_repo.captured_filters is not None
    assert items_repo.captured_filters["status"] == "open"
    assert "LA today" in result["message"]
    assert "Server tomorrow" not in result["message"]


def test_web_task_api_source_uses_user_timezone_for_task_timestamps():
    src = (ROOT / "plugins" / "pendo" / "web" / "api" / "items.py").read_text(encoding="utf-8")

    expected = 'now_in_timezone(owner_id, db).replace(tzinfo=None).isoformat()'

    assert src.count(expected) >= 2


def test_web_redesign_pages_escape_user_controlled_list_fields():
    tasks_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "tasks.js").read_text(encoding="utf-8")
    ledger_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "ledger.js").read_text(encoding="utf-8")
    dashboard_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "dashboard.js").read_text(encoding="utf-8")

    assert "escapeHtml(task.title || '(无标题)')" in tasks_src
    assert "escapeHtml(task.content)" in tasks_src
    assert "escapeHtml(textCategory)" in tasks_src
    assert "escapeHtml(item.title || '(无摘要)')" in ledger_src
    assert "escapeHtml(item.ledger_category)" in ledger_src
    assert "escapeHtml(accountText)" in ledger_src
    assert "const safeValue = escapeHtml(String(value));" in ledger_src
    assert "${safeValue}</span>" in ledger_src
    assert "escapeHtml(heading)" in dashboard_src
    assert "escapeHtml(task.title || '(无标题)')" in dashboard_src
    assert "escapeHtml(item.title || '(无摘要)')" in dashboard_src


def test_rrule_generation_is_bounded_before_materializing_instances():
    src = (ROOT / "plugins" / "pendo" / "handlers" / "event.py").read_text(encoding="utf-8")

    assert "from itertools import islice" in src
    assert "list(islice(rrule_obj, PendoConfig.EVENT_MAX_RRULE_COUNT))" in src
    assert "list(rrule_obj)[: PendoConfig.EVENT_MAX_RRULE_COUNT]" not in src


def test_web_list_and_search_api_source_bounds_pagination_inputs():
    items_src = (ROOT / "plugins" / "pendo" / "web" / "api" / "items.py").read_text(encoding="utf-8")
    search_src = (ROOT / "plugins" / "pendo" / "web" / "api" / "search.py").read_text(encoding="utf-8")

    assert "page: int = Query(1, ge=1)" in items_src
    assert "page_size: int = Query(20, ge=1, le=100)" in items_src
    assert "page: int = Query(1, ge=1)" in search_src
    assert "page_size: int | None = Query(None, ge=1, le=100)" in search_src
    assert "limit: int = Query(50, ge=1, le=100)" in search_src


def test_pendo_active_session_source_bypasses_explicit_commands():
    src = (ROOT / "plugins" / "pendo" / "main.py").read_text(encoding="utf-8")

    assert "def _is_explicit_pendo_command" in src
    assert "await safe_end_session(context)" in src
    assert "return await _handle_command_routing(user_id, args, context, group_id, log)" in src


def test_undo_delete_restores_logged_task_and_note_batches():
    temp_dir, db = _make_temp_db("pendo_review_undo_batch")
    owner_id = "u-undo-batch"

    try:
        for item_id, title in (("task-a", "A"), ("task-b", "B")):
            db.insert_item({
                "id": item_id,
                "owner_id": owner_id,
                "type": "task",
                "title": title,
                "category": "工作",
                "status": "open",
                "priority": 3,
                "created_at": "2026-04-30T09:00:00",
                "updated_at": "2026-04-30T09:00:00",
            })

        deleted = asyncio.run(
            TaskHandler(db)._delete_category_tasks(owner_id, "工作", SimpleNamespace())
        )
        assert deleted["status"] == "success"
        assert db.get_item("task-a", owner_id) is None
        assert db.get_item("task-b", owner_id) is None

        restored = db.undo_delete(owner_id)
        assert restored["status"] == "success"
        assert restored["affected"] == 2
        assert db.get_item("task-a", owner_id).title == "A"
        assert db.get_item("task-b", owner_id).title == "B"

        for item_id, title in (("note-a", "NA"), ("note-b", "NB")):
            db.insert_item({
                "id": item_id,
                "owner_id": owner_id,
                "type": "note",
                "title": title,
                "content": "body",
                "category": "知识",
                "created_at": "2026-04-30T10:00:00",
                "updated_at": "2026-04-30T10:00:00",
            })

        deleted_notes = asyncio.run(
            NoteHandler(db)._delete_category_notes(owner_id, "知识", SimpleNamespace())
        )
        assert deleted_notes["status"] == "success"
        assert db.get_item("note-a", owner_id) is None
        assert db.get_item("note-b", owner_id) is None

        restored_notes = db.undo_delete(owner_id)
        assert restored_notes["status"] == "success"
        assert restored_notes["affected"] == 2
        assert db.get_item("note-a", owner_id).title == "NA"
        assert db.get_item("note-b", owner_id).title == "NB"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_undo_delete_restores_event_collection_and_children_from_log():
    temp_dir, db = _make_temp_db("pendo_review_undo_collection")
    owner_id = "u-undo-collection"

    try:
        db.create_event_collection({
            "id": "coll-undo",
            "owner_id": owner_id,
            "kind": "multi_node",
            "title": "项目发布",
            "category": "项目",
            "start_time": "2030-05-01T10:00:00",
            "end_time": "2030-05-02T18:00:00",
            "created_at": "2026-04-30T09:00:00",
            "updated_at": "2026-04-30T09:00:00",
        })
        for item_id, index, title, start_time in (
            ("coll-undo_m01", 1, "提审", "2030-05-01T10:00:00"),
            ("coll-undo_m02", 2, "上线", "2030-05-02T18:00:00"),
        ):
            db.insert_item({
                "id": item_id,
                "owner_id": owner_id,
                "type": "event",
                "title": title,
                "category": "项目",
                "start_time": start_time,
                "event_role": "multi_node_child",
                "event_collection_id": "coll-undo",
                "event_collection_kind": "multi_node",
                "event_index": index,
                "created_at": "2026-04-30T09:00:00",
                "updated_at": "2026-04-30T09:00:00",
            })

        assert db.delete_event_collection("coll-undo", owner_id, cascade=True) is True
        db.log_operation(owner_id, "delete_event_collection", item_type="event", item_id="coll-undo")
        assert db.get_event_collection("coll-undo", owner_id) is None
        assert db.get_item("coll-undo_m01", owner_id) is None

        restored = db.undo_delete(owner_id)
        assert restored["status"] == "success"
        assert restored["collection_id"] == "coll-undo"
        assert db.get_event_collection("coll-undo", owner_id)["title"] == "项目发布"
        assert db.get_item("coll-undo_m01", owner_id).title == "提审"
        assert db.get_item("coll-undo_m02", owner_id).title == "上线"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_rebuild_fts_index_repairs_missing_and_stale_rows():
    temp_dir, db = _make_temp_db("pendo_review_fts_rebuild")
    owner_id = "u-fts-rebuild"

    try:
        db.insert_item({
            "id": "note-live",
            "owner_id": owner_id,
            "type": "note",
            "title": "全文索引修复",
            "content": "可搜索正文",
            "category": "研究",
            "created_at": "2026-04-30T09:00:00",
            "updated_at": "2026-04-30T09:00:00",
        })
        db.insert_item({
            "id": "note-deleted",
            "owner_id": owner_id,
            "type": "note",
            "title": "应删除索引",
            "content": "旧正文",
            "category": "研究",
            "created_at": "2026-04-30T09:00:00",
            "updated_at": "2026-04-30T09:00:00",
        })
        db.delete_item("note-deleted", soft=True, owner_id=owner_id)
        conn = db.get_connection()
        conn.execute("DELETE FROM items_fts WHERE id = ?", ("note-live",))
        conn.execute(
            "INSERT INTO items_fts (id, title, content, tags, category) VALUES (?, ?, ?, ?, ?)",
            ("note-deleted", "应删除索引", "旧正文", "", "研究"),
        )
        conn.commit()

        result = db.rebuild_fts_index(owner_id)

        assert result["indexed"] == 1
        assert conn.execute("SELECT 1 FROM items_fts WHERE id = ?", ("note-live",)).fetchone() is not None
        assert conn.execute("SELECT 1 FROM items_fts WHERE id = ?", ("note-deleted",)).fetchone() is None
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
