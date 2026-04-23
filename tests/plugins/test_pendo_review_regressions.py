"""Regression tests for the pendo review findings."""

import asyncio
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

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
        milestones=[],
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
                "category": "2026-04-23",
                "status": "todo",
                "priority": 1,
                "due_time": "2026-04-23T18:00:00",
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
                "category": "2026-04-23",
                "status": "todo",
                "priority": 4,
                "due_time": "2026-04-23T18:00:00",
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


def test_task_parser_uses_user_timezone_for_default_category(monkeypatch):
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

    assert parsed["category"] == "2030-01-02"


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
            return []

    items_repo = _ItemsRepo()
    handler = TaskHandler(db=SimpleNamespace(items=items_repo))

    monkeypatch.setattr(task_module, "datetime", _ServerDatetime)
    monkeypatch.setattr(
        task_module,
        "now_in_timezone",
        lambda user_id, db: datetime(2030, 1, 1, 23, 30, 0, tzinfo=ZoneInfo("America/Los_Angeles")),
    )

    asyncio.run(handler.list_tasks("u-la", "today", SimpleNamespace()))

    assert items_repo.captured_filters is not None
    assert items_repo.captured_filters["category"] == "2030-01-01"


def test_web_task_api_source_uses_user_timezone_for_task_timestamps():
    src = (ROOT / "plugins" / "pendo" / "web" / "api" / "items.py").read_text(encoding="utf-8")

    expected = 'now_in_timezone(owner_id, db).replace(tzinfo=None).isoformat()'

    assert src.count(expected) >= 2
