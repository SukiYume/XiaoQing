"""提醒回填、默认值和失效数据恢复。"""

from __future__ import annotations

from tests.helpers.pendo_test_support import (
    ROOT,
    SimpleNamespace,
    asyncio,
)


class TestReminderBackfillRegression:
    def test_event_list_includes_day_delta_for_each_event(self, monkeypatch, tmp_path):
        import sys
        from datetime import datetime
        from typing import Any, cast
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers import event as event_module
        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo.db"))

        try:
            future_event = EventItem(
                owner_id     = "u1",
                title        = "未来会议",
                start_time   = "2030-01-03T09:00:00",
                end_time     = "2030-01-03T10:00:00",
                remind_times = ["2030-01-03T08:00:00"],
                created_at   = "2030-01-01T00:00:00",
                updated_at   = "2030-01-01T00:00:00",
            )
            past_event = EventItem(
                owner_id     = "u1",
                title        = "过去会议",
                start_time   = "2029-12-30T09:00:00",
                end_time     = "2029-12-30T10:00:00",
                remind_times = ["2029-12-30T08:00:00"],
                created_at   = "2029-12-01T00:00:00",
                updated_at   = "2029-12-01T00:00:00",
            )
            db.insert_item(future_event, "evt_future")
            db.insert_item(past_event, "evt_past")

            monkeypatch.setattr(
                event_module,
                "now_in_timezone",
                lambda user_id=None, db=None: datetime.fromisoformat("2030-01-01T08:00:00+08:00"),
            )

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())

            result = asyncio.run(
                handler.list_events(
                    "u1",
                    "2029-12-01..2030-01-31",
                    cast(Any, SimpleNamespace()),
                )
            )

            assert result["status"] == "success"
            assert "**01月03日 周四** - 2天后" in result["message"]
            assert "• 09:00 - 10:00 未来会议" in result["message"]
            assert "**12月30日 周日** - 2天前" in result["message"]
            assert "• 09:00 - 10:00 过去会议" in result["message"]
        finally:
            db.cleanup()

    def test_event_list_handles_timezone_aware_event_start_time(self, tmp_path):
        import sys
        from typing import Any, cast
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_event_aware_start.db"))

        try:
            event = EventItem(
                owner_id     = "u1",
                title        = "带时区会议",
                start_time   = "2026-05-01T09:00:00+08:00",
                end_time     = "2026-05-01T10:00:00+08:00",
                remind_times = ["2026-05-01T08:30:00+08:00"],
                created_at   = "2026-05-01T00:00:00",
                updated_at   = "2026-05-01T00:00:00",
            )
            db.insert_item(event, "evtaware")
            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())

            result = asyncio.run(handler.list_events("u1", "2026", cast(Any, SimpleNamespace())))

            assert result["status"] == "success"
            assert "带时区会议" in result["message"]
        finally:
            db.cleanup()

    def test_confirm_requires_item_ownership(self):
        import sys
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands.operations import handle_confirm

        db                       = MagicMock()
        db.get_item.return_value = None

        reminder_service                               = MagicMock()
        reminder_service.confirm_reminder.return_value = {"status": "success", "message": "ok"}

        result = asyncio.run(handle_confirm("u1", "evt123", reminder_service, db))

        assert result["status"] == "error"
        reminder_service.confirm_reminder.assert_not_called()

    def test_confirm_only_marks_latest_unconfirmed_log(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands.operations import handle_confirm
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database
        from plugins.pendo.services.reminder import ReminderService

        db = Database(str(tmp_path / "pendo.db"))

        try:
            event = EventItem(
                owner_id     = "u1",
                title        = "提醒测试",
                start_time   = "2020-01-01T10:00:00",
                remind_times = ["2020-01-01T08:00:00", "2020-01-01T09:00:00"],
                created_at   = "2020-01-01T00:00:00",
                updated_at   = "2020-01-01T00:00:00",
            )
            db.insert_item(event, "evt123")

            conn   = db.get_connection()
            cursor = conn.cursor()
            with conn:
                cursor.execute(
                    """
                    UPDATE reminder_logs
                    SET sent_at = ?, last_sent_at = ?, repeat_count = 1, state = 'sent'
                    WHERE item_id = ? AND remind_time = ?
                    """,
                    (
                        "2020-01-01T00:00:05+00:00",
                        "2020-01-01T00:00:05+00:00",
                        "evt123",
                        "2020-01-01T00:00:00+00:00",
                    ),
                )
                cursor.execute(
                    """
                    UPDATE reminder_logs
                    SET sent_at = ?, last_sent_at = ?, repeat_count = 1, state = 'sent'
                    WHERE item_id = ? AND remind_time = ?
                    """,
                    (
                        "2020-01-01T01:00:05+00:00",
                        "2020-01-01T01:00:05+00:00",
                        "evt123",
                        "2020-01-01T01:00:00+00:00",
                    ),
                )

            result = asyncio.run(handle_confirm("u1", "evt123", ReminderService(db), db))

            assert result["status"] == "success"
            logs           = db.get_reminder_logs("evt123")
            confirmed_logs = [log for log in logs if log["confirmed_at"]]
            pending_logs   = [log for log in logs if not log["confirmed_at"]]

            assert [log["remind_time"] for log in confirmed_logs] == ["2020-01-01T01:00:00+00:00"]
            assert [log["remind_time"] for log in pending_logs] == ["2020-01-01T00:00:00+00:00"]
        finally:
            db.cleanup()

    def test_confirm_future_remind_time_with_allow_future_creates_preconfirmed_log(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo_confirm_future.db"))

        try:
            event = EventItem(
                owner_id     = "u1",
                title        = "提醒测试",
                start_time   = "2030-01-02T10:00:00",
                remind_times = ["2030-01-02T09:00:00", "2030-01-02T10:00:00"],
                created_at   = "2030-01-01T00:00:00",
                updated_at   = "2030-01-01T00:00:00",
            )
            db.insert_item(event, "evtfuture")

            result = db.confirm_reminder(
                "evtfuture",
                "preconfirmed",
                owner_id     = "u1",
                remind_time  = "2030-01-02T01:00:00+00:00",
                allow_future = True,
            )

            assert result["status"] == "success"
            logs = db.get_reminder_logs("evtfuture")
            assert len(logs) == 2
            target = next(log for log in logs if log["remind_time"] == "2030-01-02T01:00:00+00:00")
            assert target["sent_at"] is None
            assert target["confirmed_at"]
            assert target["user_action"] == "preconfirmed"
            assert target["repeat_count"] == 0
            assert target["last_sent_at"] is None
        finally:
            db.cleanup()

    def test_event_reminders_confirm_today_preconfirms_all_matching_reminders(
        self, tmp_path, monkeypatch
    ):
        import sys
        from datetime import datetime
        from unittest.mock import MagicMock

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers import event as event_module
        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        fixed_now = datetime.fromisoformat("2030-01-02T08:00:00+08:00")
        monkeypatch.setattr(
            event_module, "now_in_timezone", lambda user_id=None, db=None: fixed_now
        )

        db = Database(str(tmp_path / "pendo_reminders_confirm_today.db"))

        try:
            event = EventItem(
                owner_id     = "u1",
                title        = "今日提醒",
                start_time   = "2030-01-02T14:00:00",
                remind_times = [
                    "2030-01-02T09:00:00",
                    "2030-01-02T13:00:00",
                    "2030-01-02T14:00:00",
                    "2030-01-03T09:00:00",
                ],
                created_at = "2030-01-01T00:00:00",
                updated_at = "2030-01-01T00:00:00",
            )
            db.insert_item(event, "evtday02")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())
            result = asyncio.run(
                handler.handle_reminders("u1", "confirm evtday02 today", MagicMock())
            )

            assert result["status"] == "success"
            assert "已确认 3 个提醒" in result["message"]
            logs      = db.get_reminder_logs("evtday02")
            confirmed = {log["remind_time"] for log in logs if log["confirmed_at"]}
            assert confirmed == {
                "2030-01-02T01:00:00+00:00",
                "2030-01-02T05:00:00+00:00",
                "2030-01-02T06:00:00+00:00",
            }
            assert "2030-01-03T01:00:00+00:00" not in confirmed
        finally:
            db.cleanup()

    def test_reminder_disabled_user_does_not_receive_scheduled_reminder(self, monkeypatch):
        import sys
        from datetime import datetime

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services import reminder as reminder_module
        from plugins.pendo.services.reminder import ReminderService

        fixed_now = datetime.fromisoformat("2030-01-01T09:00:00+08:00")

        monkeypatch.setattr(
            reminder_module, "now_in_timezone", lambda user_id=None, db=None: fixed_now
        )
        monkeypatch.setattr(
            reminder_module,
            "parse_and_localize",
            lambda dt_str, user_id=None, db=None: datetime.fromisoformat(dt_str).replace(
                tzinfo=fixed_now.tzinfo
            ),
        )

        item = SimpleNamespace(
            id           = "evt123",
            owner_id     = "u1",
            title        = "晨会",
            start_time   = "2030-01-01T10:00:00",
            end_time     = "2030-01-01T11:00:00",
            remind_times = ["2030-01-01T09:00:00"],
            context      = {},
            location     = "会议室A",
            notes        = "",
            tags         = [],
        )

        class _FakeDb:
            def __init__(self):
                self.logged = []

            def get_due_reminder_items(self, *, now):
                return [item]

            def prune_reminder_logs(self, *, before):
                return 0

            def log_reminder(self, item_id, remind_time, sent=True):
                self.logged.append((item_id, remind_time, sent))

            def get_user_settings(self, user_id):
                return {
                    "quiet_hours_start": "23:00",
                    "quiet_hours_end": "07:00",
                    "settings_json": {"reminder_enabled": False},
                }

            def get_unconfirmed_sent_reminders(self):
                return []

            def get_item(self, item_id):
                return item

        service = ReminderService(db=_FakeDb())

        result = service.check_and_send_reminders()

        assert result["sent"] == 0
        assert result["messages"] == []
        assert service.db.logged == []

    def test_preconfirmed_future_reminder_is_not_sent_by_scheduler(self, monkeypatch):
        import sys
        from datetime import datetime

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services import reminder as reminder_module
        from plugins.pendo.services.reminder import ReminderService

        fixed_now = datetime.fromisoformat("2030-01-01T09:00:00+08:00")

        monkeypatch.setattr(
            reminder_module, "now_in_timezone", lambda user_id=None, db=None: fixed_now
        )
        monkeypatch.setattr(
            reminder_module,
            "parse_and_localize",
            lambda dt_str, user_id=None, db=None: datetime.fromisoformat(dt_str).replace(
                tzinfo=fixed_now.tzinfo
            ),
        )

        item = SimpleNamespace(
            id           = "evt123",
            owner_id     = "u1",
            title        = "晨会",
            start_time   = "2030-01-01T10:00:00",
            end_time     = "2030-01-01T11:00:00",
            remind_times = ["2030-01-01T09:00:00"],
            context      = {},
            location     = "会议室A",
            notes        = "",
            tags         = [],
        )

        class _FakeDb:
            def __init__(self):
                self.logged = []

            def get_due_reminder_items(self, *, now):
                return []

            def prune_reminder_logs(self, *, before):
                return 0

            def get_reminder_logs(self, item_id):
                assert item_id == "evt123"
                return [
                    {
                        "remind_time": "2030-01-01T09:00:00",
                        "sent_at": None,
                        "confirmed_at": "2030-01-01T08:00:00",
                        "user_action": "preconfirmed",
                        "repeat_count": 0,
                        "last_sent_at": None,
                    }
                ]

            def log_reminder(self, item_id, remind_time, sent=True):
                self.logged.append((item_id, remind_time, sent))

            def get_user_settings(self, user_id):
                return {
                    "quiet_hours_start": "23:00",
                    "quiet_hours_end": "07:00",
                    "settings_json": {"reminder_enabled": True},
                }

            def get_unconfirmed_sent_reminders(self):
                return []

            def get_item(self, item_id):
                return item

        service = ReminderService(db=_FakeDb())

        result = service.check_and_send_reminders()

        assert result["sent"] == 0
        assert result["messages"] == []
        assert service.db.logged == []

    def test_stale_unconfirmed_send_auto_confirms_after_one_day(self, monkeypatch):
        import sys
        from datetime import datetime

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services import reminder as reminder_module
        from plugins.pendo.services.reminder import ReminderService

        fixed_now   = datetime.fromisoformat("2030-01-01T09:10:00+08:00")
        remind_time = "2030-01-01T08:00:00"

        monkeypatch.setattr(
            reminder_module,
            "parse_and_localize",
            lambda dt_str, user_id=None, db=None: datetime.fromisoformat(dt_str).replace(
                tzinfo=fixed_now.tzinfo
            ),
        )

        item = SimpleNamespace(
            id           = "evt123",
            owner_id     = "u1",
            title        = "晨会",
            start_time   = "2030-01-01T10:00:00",
            end_time     = "2030-01-01T11:00:00",
            remind_times = [remind_time],
            context      = {},
            location     = "会议室A",
            notes        = "",
            tags         = [],
        )

        class _FakeDb:
            def __init__(self):
                self.confirm_calls = []

            def get_unconfirmed_sent_reminders(self):
                return [
                    {
                        "id": 4,
                        "item_id": "evt123",
                        "remind_time": remind_time,
                        "repeat_count": 1,
                        "last_sent_at": "2029-12-31T09:00:00",
                    }
                ]

            def get_item(self, item_id):
                assert item_id == "evt123"
                return item

            def get_user_settings(self, user_id):
                return {
                    "quiet_hours_start": "23:00",
                    "quiet_hours_end": "07:00",
                    "settings_json": {"reminder_enabled": True},
                }

            def confirm_reminder(
                self, item_id, user_action="confirmed", owner_id=None, remind_time=None
            ):
                self.confirm_calls.append(
                    {
                        "item_id": item_id,
                        "user_action": user_action,
                        "owner_id": owner_id,
                        "remind_time": remind_time,
                    }
                )
                return {"status": "success", "message": "ok"}

        service = ReminderService(db=_FakeDb())

        result = service._check_unconfirmed_repeats(fixed_now)

        assert result == []
        assert service.db.confirm_calls == [
            {
                "item_id": "evt123",
                "user_action": "auto_confirmed",
                "owner_id": None,
                "remind_time": remind_time,
            }
        ]
