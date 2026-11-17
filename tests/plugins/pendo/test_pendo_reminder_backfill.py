"""提醒确认、预确认、自动确认，以及日程列表的日期差。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from plugins.pendo import main as pendo_main
from tests.helpers.pendo_test_support import (
    attach_action_recorder,
    freeze_pendo_reminder_clock,
    pendo_service_context,
)


@pytest.fixture
def reminder_context(db, tmp_path):
    """真实服务集合（共用测试数据库）；发出的私聊动作记录在 ``sent``，一律回执成功。"""

    with pendo_service_context(db, tmp_path) as context:
        context.sent = attach_action_recorder(context)
        yield context


def _run_reminders(context) -> list[str]:
    """跑一轮提醒定时任务，返回这一轮发出的消息文本。"""

    context.sent.clear()
    asyncio.run(pendo_main.scheduled(context))
    return [action["params"]["message"][0]["data"]["text"] for action in context.sent]


class TestReminderConfirmation:
    def test_event_list_includes_day_delta_for_each_event(self, monkeypatch, tmp_path):
        from typing import Any, cast
        from unittest.mock import MagicMock

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

            freeze_pendo_reminder_clock(
                monkeypatch, datetime.fromisoformat("2030-01-01T08:00:00+08:00")
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
            assert "01月03日 周四 - 2天后" in result["message"]
            assert "• 09:00 - 10:00 未来会议" in result["message"]
            assert "12月30日 周日 - 2天前" in result["message"]
            assert "• 09:00 - 10:00 过去会议" in result["message"]
        finally:
            db.cleanup()

    def test_event_list_handles_timezone_aware_event_start_time(self, tmp_path):
        from typing import Any, cast
        from unittest.mock import MagicMock

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
        from unittest.mock import MagicMock

        from plugins.pendo.commands.operations import handle_confirm

        db                       = MagicMock()
        db.get_item.return_value = None

        reminder_service                               = MagicMock()
        reminder_service.confirm_reminder.return_value = {"status": "success", "message": "ok"}

        result = asyncio.run(handle_confirm("u1", "evt123", reminder_service, db))

        assert result["status"] == "error"
        reminder_service.confirm_reminder.assert_not_called()

    def test_confirm_only_marks_latest_unconfirmed_log(self, tmp_path):

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
        from unittest.mock import MagicMock

        from plugins.pendo.handlers.event import EventHandler
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        freeze_pendo_reminder_clock(
            monkeypatch, datetime.fromisoformat("2030-01-02T08:00:00+08:00")
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

    def test_reminder_disabled_user_does_not_receive_scheduled_reminder(
        self, db, reminder_context, monkeypatch
    ):
        # 2030-01-01 09:00（默认时区上海）到点的提醒。
        freeze_pendo_reminder_clock(monkeypatch, datetime(2030, 1, 1, 1, 0, tzinfo=UTC))
        item_id = db.insert_item(
            {
                "owner_id"    : "1001",
                "type"        : "event",
                "title"       : "晨会",
                "start_time"  : "2030-01-01T10:00:00",
                "end_time"    : "2030-01-01T11:00:00",
                "remind_times": ["2030-01-01T09:00:00"],
                "location"    : "会议室A",
            }
        )
        db.update_user_settings("1001", {"settings_json": {"reminder_enabled": False}})

        assert _run_reminders(reminder_context) == []

        log = db.get_reminder_logs(item_id)[0]
        assert log["sent_at"] is None
        assert log["state"] == "pending"

        # 同一时刻重新打开提醒后这条提醒确实到期：上面没发出，原因就是用户关了提醒。
        db.update_user_settings("1001", {"settings_json": {"reminder_enabled": True}})
        assert len(_run_reminders(reminder_context)) == 1

    def test_preconfirmed_future_reminder_is_not_sent_by_scheduler(
        self, db, reminder_context, monkeypatch
    ):
        freeze_pendo_reminder_clock(monkeypatch, datetime(2030, 1, 1, 0, 0, tzinfo=UTC))
        preconfirmed_id = db.insert_item(
            {
                "owner_id"    : "1001",
                "type"        : "event",
                "title"       : "已预确认",
                "start_time"  : "2030-01-01T10:00:00",
                "remind_times": ["2030-01-01T09:00:00"],
            }
        )
        db.insert_item(
            {
                "owner_id"    : "1001",
                "type"        : "event",
                "title"       : "待发送",
                "start_time"  : "2030-01-01T10:00:00",
                "remind_times": ["2030-01-01T09:00:00"],
            }
        )
        result = db.confirm_reminder(
            preconfirmed_id,
            "preconfirmed",
            owner_id     = "1001",
            remind_time  = "2030-01-01T01:00:00+00:00",
            allow_future = True,
        )
        assert result["status"] == "success"

        # 到点（上海 09:00）：只有没预确认的那条发出。
        freeze_pendo_reminder_clock(monkeypatch, datetime(2030, 1, 1, 1, 0, tzinfo=UTC))
        texts = _run_reminders(reminder_context)

        assert len(texts) == 1
        assert "待发送" in texts[0]
        assert "已预确认" not in texts[0]
        log = db.get_reminder_logs(preconfirmed_id)[0]
        assert log["sent_at"] is None
        assert log["user_action"] == "preconfirmed"

    def test_stale_unconfirmed_send_auto_confirms_after_one_day(
        self, db, reminder_context, monkeypatch
    ):
        freeze_pendo_reminder_clock(monkeypatch, datetime(2030, 1, 1, 1, 10, tzinfo=UTC))
        stale_id = db.insert_item(
            {
                "owner_id"    : "1001",
                "type"        : "event",
                "title"       : "过期未确认",
                "start_time"  : "2030-01-01T10:00:00",
                "remind_times": ["2030-01-01T08:00:00"],
            }
        )
        recent_id = db.insert_item(
            {
                "owner_id"    : "1001",
                "type"        : "event",
                "title"       : "刚发过未确认",
                "start_time"  : "2030-01-01T10:00:00",
                "remind_times": ["2030-01-01T08:00:00"],
            }
        )
        connection = db.get_connection()
        with connection:
            for item_id, sent_at in (
                (stale_id, "2029-12-31T01:00:00+00:00"),
                (recent_id, "2030-01-01T01:00:00+00:00"),
            ):
                connection.execute(
                    "UPDATE reminder_logs SET sent_at = ?, last_sent_at = ?, "
                    "repeat_count = 1, state = 'sent' WHERE item_id = ?",
                    (sent_at, sent_at, item_id),
                )

        texts = _run_reminders(reminder_context)

        # 发出一天多以前的那条被自动确认，不再重复发送；十分钟前发出的那条照常重复提醒。
        assert len(texts) == 1
        assert "刚发过未确认" in texts[0]
        stale_log = db.get_reminder_logs(stale_id)[0]
        assert stale_log["confirmed_at"]
        assert stale_log["user_action"] == "auto_confirmed"
        assert db.get_reminder_logs(recent_id)[0]["confirmed_at"] is None
