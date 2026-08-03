"""周期调度和财务摘要。"""

from __future__ import annotations

from tests.helpers.pendo_test_support import (
    ROOT,
    SimpleNamespace,
    _ReminderMessageService,
    _single_user_shanghai_settings,
    _with_scheduled_delivery_contract,
    asyncio,
    datetime,
    json,
    timezone,
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
                owner_id="u1",
                title="未来会议",
                start_time="2030-01-03T09:00:00",
                end_time="2030-01-03T10:00:00",
                remind_times=["2030-01-03T08:00:00"],
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            past_event = EventItem(
                owner_id="u1",
                title="过去会议",
                start_time="2029-12-30T09:00:00",
                end_time="2029-12-30T10:00:00",
                remind_times=["2029-12-30T08:00:00"],
                created_at="2029-12-01T00:00:00",
                updated_at="2029-12-01T00:00:00",
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
                owner_id="u1",
                title="带时区会议",
                start_time="2026-05-01T09:00:00+08:00",
                end_time="2026-05-01T10:00:00+08:00",
                remind_times=["2026-05-01T08:30:00+08:00"],
                created_at="2026-05-01T00:00:00",
                updated_at="2026-05-01T00:00:00",
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

        db = MagicMock()
        db.get_item.return_value = None

        reminder_service = MagicMock()
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
                owner_id="u1",
                title="提醒测试",
                start_time="2020-01-01T10:00:00",
                remind_times=["2020-01-01T08:00:00", "2020-01-01T09:00:00"],
                created_at="2020-01-01T00:00:00",
                updated_at="2020-01-01T00:00:00",
            )
            db.insert_item(event, "evt123")

            conn = db.get_connection()
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
            logs = db.get_reminder_logs("evt123")
            confirmed_logs = [log for log in logs if log["confirmed_at"]]
            pending_logs = [log for log in logs if not log["confirmed_at"]]

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
                owner_id="u1",
                title="提醒测试",
                start_time="2030-01-02T10:00:00",
                remind_times=["2030-01-02T09:00:00", "2030-01-02T10:00:00"],
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            db.insert_item(event, "evtfuture")

            result = db.confirm_reminder(
                "evtfuture",
                "preconfirmed",
                owner_id="u1",
                remind_time="2030-01-02T01:00:00+00:00",
                allow_future=True,
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
                owner_id="u1",
                title="今日提醒",
                start_time="2030-01-02T14:00:00",
                remind_times=[
                    "2030-01-02T09:00:00",
                    "2030-01-02T13:00:00",
                    "2030-01-02T14:00:00",
                    "2030-01-03T09:00:00",
                ],
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            db.insert_item(event, "evtday02")

            handler = EventHandler(db=db, ai_parser=MagicMock(), reminder_service=MagicMock())
            result = asyncio.run(
                handler.handle_reminders("u1", "confirm evtday02 today", MagicMock())
            )

            assert result["status"] == "success"
            assert "已确认 3 个提醒" in result["message"]
            logs = db.get_reminder_logs("evtday02")
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
            id="evt123",
            owner_id="u1",
            title="晨会",
            start_time="2030-01-01T10:00:00",
            end_time="2030-01-01T11:00:00",
            remind_times=["2030-01-01T09:00:00"],
            context={},
            location="会议室A",
            notes="",
            tags=[],
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
            id="evt123",
            owner_id="u1",
            title="晨会",
            start_time="2030-01-01T10:00:00",
            end_time="2030-01-01T11:00:00",
            remind_times=["2030-01-01T09:00:00"],
            context={},
            location="会议室A",
            notes="",
            tags=[],
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

        fixed_now = datetime.fromisoformat("2030-01-01T09:10:00+08:00")
        remind_time = "2030-01-01T08:00:00"

        monkeypatch.setattr(
            reminder_module,
            "parse_and_localize",
            lambda dt_str, user_id=None, db=None: datetime.fromisoformat(dt_str).replace(
                tzinfo=fixed_now.tzinfo
            ),
        )

        item = SimpleNamespace(
            id="evt123",
            owner_id="u1",
            title="晨会",
            start_time="2030-01-01T10:00:00",
            end_time="2030-01-01T11:00:00",
            remind_times=[remind_time],
            context={},
            location="会议室A",
            notes="",
            tags=[],
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


class TestScheduledRegression:
    def test_check_reminders_returns_messages_without_send_action(self, monkeypatch):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        db = SimpleNamespace(logged=[])

        def fake_log_reminder(item_id, remind_time, sent=True):
            db.logged.append((item_id, remind_time, sent))

        db.log_reminder = fake_log_reminder

        monkeypatch.setattr(scheduled_module, "get_database", lambda context: db)
        monkeypatch.setattr(
            scheduled_module, "_reminder_service_singleton", _ReminderMessageService()
        )

        result = asyncio.run(scheduled_module.check_reminders(SimpleNamespace()))

        assert len(result) == 1
        assert result[0]["params"]["user_id"] == 1001
        assert db.logged == []

    def test_check_reminders_does_not_confirm_onebot_rejection(self, monkeypatch):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        class _RejectedContext:
            async def send_action(self, action):
                return False

        db = SimpleNamespace(logged=[])
        db.log_reminder = lambda item_id, remind_time, sent=True: db.logged.append(
            (item_id, remind_time, sent)
        )

        monkeypatch.setattr(scheduled_module, "get_database", lambda context: db)
        monkeypatch.setattr(
            scheduled_module, "_reminder_service_singleton", _ReminderMessageService()
        )

        result = asyncio.run(scheduled_module.check_reminders(_RejectedContext()))

        assert result == []
        assert db.logged == []

    def test_rejected_claimed_reminder_is_throttled_before_retry(self):
        from plugins.pendo.commands import scheduled as scheduled_module
        from plugins.pendo.config import PendoConfig

        class _Db:
            retry_at = None

            def release_reminder_claim(self, item_id, remind_time, claim_token, *, retry_at):
                assert (item_id, remind_time, claim_token) == (
                    "evt123",
                    "2030-01-01T09:00:00",
                    "claim-1",
                )
                self.retry_at = retry_at
                return True

        db = _Db()
        delivery = scheduled_module._ReminderDelivery(
            user_id="1001",
            message="提醒消息",
            item_id="evt123",
            remind_time="2030-01-01T09:00:00",
            claim_token="claim-1",
            claim_kind="initial",
            claim_repeat_count=0,
            delivery_key="delivery-1",
        )
        before = datetime.now(timezone.utc)

        asyncio.run(scheduled_module._settle_reminder_delivery(db, delivery, delivered=False))

        assert db.retry_at is not None
        delay = (db.retry_at - before).total_seconds()
        assert (
            PendoConfig.REMINDER_REPEAT_INTERVAL_SECONDS
            <= delay
            <= (PendoConfig.REMINDER_REPEAT_INTERVAL_SECONDS + 2)
        )

    def test_daily_briefing_respects_user_timezone_and_configured_time(self, monkeypatch):
        import sys
        from datetime import datetime, timezone
        from typing import Any, cast

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)
                if tz is None:
                    return base.replace(tzinfo=None)
                return base.astimezone(tz)

        settings_map = {
            "1001": {
                "timezone": "Asia/Shanghai",
                "daily_report_time": "08:00",
                "quiet_hours_start": "23:00",
                "quiet_hours_end": "07:00",
                "settings_json": {"daily_briefing_enabled": True},
            },
            "1002": {
                "timezone": "America/New_York",
                "daily_report_time": "08:00",
                "quiet_hours_start": "23:00",
                "quiet_hours_end": "07:00",
                "settings_json": {"daily_briefing_enabled": True},
            },
        }
        batch_calls = []

        def get_user_settings_batch(user_ids):
            batch_calls.append(list(user_ids))
            return {user_id: dict(settings_map[user_id]) for user_id in user_ids}

        def get_user_settings(_user_id):
            raise AssertionError("send_daily_briefings should use batch settings lookup")

        db = cast(
            Any,
            _with_scheduled_delivery_contract(
                SimpleNamespace(
                    get_user_settings=get_user_settings,
                    get_user_settings_batch=get_user_settings_batch,
                    update_user_settings=lambda user_id, settings: True,
                )
            ),
        )
        actions = []

        async def send_action(action):
            actions.append(action)
            return True

        async def fake_get_active_user_ids(_db):
            return ["1001", "1002"]

        async def fake_generate_briefing_content(user_id, _db):
            return f"briefing:{user_id}"

        monkeypatch.setattr(scheduled_module, "datetime", _FixedDateTime)
        monkeypatch.setattr(scheduled_module, "_get_active_user_ids", fake_get_active_user_ids)
        monkeypatch.setattr(
            scheduled_module, "_generate_briefing_content", fake_generate_briefing_content
        )

        context = SimpleNamespace(send_action=send_action)
        result = asyncio.run(scheduled_module.send_daily_briefings(context, db))

        assert result == []
        assert [action["params"]["user_id"] for action in actions] == [1001]
        assert batch_calls == [["1001", "1002"]]

    def test_daily_briefing_returns_messages_without_send_action(self, monkeypatch):
        import sys
        from datetime import datetime, timezone
        from typing import Any, cast

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)
                if tz is None:
                    return base.replace(tzinfo=None)
                return base.astimezone(tz)

        def get_user_settings_batch(user_ids):
            return {
                user_id: {
                    "timezone": "Asia/Shanghai",
                    "daily_report_time": "08:00",
                    "settings_json": {"daily_briefing_enabled": True},
                }
                for user_id in user_ids
            }

        db = cast(
            Any,
            _with_scheduled_delivery_contract(
                SimpleNamespace(
                    get_user_settings=lambda user_id: (_ for _ in ()).throw(
                        AssertionError("send_daily_briefings should use batch settings lookup")
                    ),
                    get_user_settings_batch=get_user_settings_batch,
                    update_user_settings=lambda user_id, settings: True,
                )
            ),
        )

        async def fake_get_active_user_ids(_db):
            return ["1001"]

        async def fake_generate_briefing_content(user_id, _db):
            return f"briefing:{user_id}"

        monkeypatch.setattr(scheduled_module, "datetime", _FixedDateTime)
        monkeypatch.setattr(scheduled_module, "_get_active_user_ids", fake_get_active_user_ids)
        monkeypatch.setattr(
            scheduled_module, "_generate_briefing_content", fake_generate_briefing_content
        )

        result = asyncio.run(scheduled_module.send_daily_briefings(SimpleNamespace(), db))

        assert len(result) == 1
        assert result[0]["params"]["user_id"] == 1001

    def test_generate_daily_briefing_includes_today_multi_node_leaf_events(
        self, tmp_path, monkeypatch
    ):
        import sys
        from datetime import datetime

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        fixed_now = datetime.fromisoformat("2030-01-02T08:00:00+08:00")
        monkeypatch.setattr(
            scheduled_module, "now_in_timezone", lambda user_id=None, db=None: fixed_now
        )

        db = Database(str(tmp_path / "pendo_briefing_milestone.db"))

        try:
            batch_calls = []
            original_batch_lookup = db.get_event_collections_by_ids

            def track_batch_lookup(owner_id, collection_ids):
                batch_calls.append((owner_id, list(collection_ids)))
                return original_batch_lookup(owner_id, collection_ids)

            def reject_single_lookup(*_args, **_kwargs):
                raise AssertionError("briefing should batch event collection lookups")

            monkeypatch.setattr(db, "get_event_collections_by_ids", track_batch_lookup)
            monkeypatch.setattr(db, "get_event_collection", reject_single_lookup)
            db.create_event_collection(
                {
                    "id": "milebrief",
                    "owner_id": "u1",
                    "kind": "multi_node",
                    "title": "学术会议",
                    "category": "未分类",
                    "start_time": "2030-01-01T09:00:00",
                    "end_time": "2030-01-03T18:00:00",
                }
            )
            event = EventItem(
                owner_id="u1",
                title="主会场报告",
                start_time="2030-01-02T10:30:00",
                remind_times=[],
                event_role="multi_node_child",
                event_collection_id="milebrief",
                event_collection_kind="multi_node",
                event_index=2,
                event_node_key="m02",
                created_at="2030-01-01T00:00:00",
                updated_at="2030-01-01T00:00:00",
            )
            db.insert_item(event, "milebrief_m02")

            briefing = asyncio.run(scheduled_module._generate_briefing_content("u1", db))

            assert "🗓️ **今日日程**" in briefing
            assert "10:30 学术会议 · 主会场报告" in briefing
            assert "报到" not in briefing
            assert "闭幕" not in briefing
            assert "今日暂无日程安排" not in briefing
            assert batch_calls == [("u1", ["milebrief"])]
        finally:
            db.cleanup()

    def test_generate_daily_briefing_ignores_malformed_overdue_deadline(self, monkeypatch):
        import sys
        from datetime import datetime, timezone

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module
        from plugins.pendo.models.item import TaskItem

        fixed_now = datetime(2030, 1, 2, 8, 0, tzinfo=timezone.utc)
        overdue = TaskItem(owner_id="1001", title="旧数据", deadline_at="not-an-iso-time")
        db = SimpleNamespace(
            get_briefing_items=lambda *_args: ([], [], [overdue]),
        )
        monkeypatch.setattr(
            scheduled_module,
            "now_in_timezone",
            lambda _user_id, _db: fixed_now,
        )
        monkeypatch.setattr(
            scheduled_module.TimezoneHelper,
            "get_user_timezone",
            staticmethod(lambda _user_id, _db: timezone.utc),
        )

        briefing = asyncio.run(scheduled_module._generate_briefing_content("1001", db))

        assert "⚠️ 逾期待办 (1项):" in briefing
        assert "(截止:" not in briefing

    def test_migrate_todos_returns_messages_without_send_action(self, monkeypatch):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        migration_calls = []

        def migrate(user_id, source_date, target_date):
            migration_calls.append((user_id, source_date, target_date))
            return 1

        db = SimpleNamespace(migrate_undone_tasks_to_date=migrate)

        async def fake_get_active_user_ids(_db):
            return ["1001"]

        monkeypatch.setattr(scheduled_module, "_get_active_user_ids", fake_get_active_user_ids)
        monkeypatch.setattr(
            scheduled_module,
            "now_in_timezone",
            lambda _user_id, _db: datetime(2030, 1, 2, 0, 5),
        )

        result = asyncio.run(scheduled_module.migrate_undone_todos(SimpleNamespace(), db))

        assert len(result) == 1
        assert result[0]["params"]["user_id"] == 1001
        assert migration_calls == [("1001", "2030-01-01", "2030-01-02")]

    def test_diary_reminder_respects_user_timezone_and_existing_diary(self, monkeypatch):
        import sys
        from datetime import datetime, timezone
        from typing import Any, cast

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(2030, 1, 1, 13, 30, tzinfo=timezone.utc)
                if tz is None:
                    return base.replace(tzinfo=None)
                return base.astimezone(tz)

        settings_map = {
            "2001": {
                "timezone": "Asia/Shanghai",
                "diary_remind_time": "21:30",
                "quiet_hours_start": "23:00",
                "quiet_hours_end": "07:00",
                "settings_json": {},
            },
            "2002": {
                "timezone": "Asia/Shanghai",
                "diary_remind_time": "21:30",
                "quiet_hours_start": "23:00",
                "quiet_hours_end": "07:00",
                "settings_json": {},
            },
            "2003": {
                "timezone": "America/New_York",
                "diary_remind_time": "21:30",
                "quiet_hours_start": "23:00",
                "quiet_hours_end": "07:00",
                "settings_json": {},
            },
        }

        def get_user_settings_batch(user_ids):
            return {user_id: dict(settings_map[user_id]) for user_id in user_ids}

        def has_diary_for_date(user_id, _diary_date):
            return user_id == "2002"

        db = cast(
            Any,
            _with_scheduled_delivery_contract(
                SimpleNamespace(
                    get_user_settings=lambda user_id: (_ for _ in ()).throw(
                        AssertionError("check_diary_reminders should use batch settings lookup")
                    ),
                    get_user_settings_batch=get_user_settings_batch,
                    update_user_settings=lambda user_id, settings: True,
                    has_diary_for_date=has_diary_for_date,
                )
            ),
        )
        actions = []

        async def send_action(action):
            actions.append(action)
            return True

        async def fake_get_active_user_ids(_db):
            return ["2001", "2002", "2003"]

        monkeypatch.setattr(scheduled_module, "datetime", _FixedDateTime)
        monkeypatch.setattr(scheduled_module, "_get_active_user_ids", fake_get_active_user_ids)

        context = SimpleNamespace(send_action=send_action)
        result = asyncio.run(scheduled_module.check_diary_reminders(context, db))

        assert result == []
        assert [action["params"]["user_id"] for action in actions] == [2001]

    def test_plugin_manifest_schedule_matches_runtime_contract(self):
        from core.models import PluginManifest

        plugin_json_path = ROOT / "plugins" / "pendo" / "plugin.json"
        with open(plugin_json_path, encoding="utf-8") as f:
            config = json.load(f)

        # 先走生产清单模型，避免测试只验证局部字段却漏掉 schema 漂移。
        manifest = PluginManifest.model_validate(config)
        assert manifest.name == "pendo"

        expected = {
            "pendo_reminders": ("scheduled", {"minute": "*"}),
            "pendo_daily_briefing": ("scheduled_daily_briefing", {"minute": "*"}),
            "pendo_diary_reminder": ("scheduled_diary_reminder", {"minute": "*"}),
            "pendo_migrate_todos": ("scheduled_migrate_todos", {"hour": 0, "minute": 5}),
            "pendo_prune_operation_logs": (
                "scheduled_prune_operation_logs",
                {"hour": 0, "minute": 15},
            ),
            "pendo_weekly_finance_summary": (
                "scheduled_weekly_finance_summary",
                {"day_of_week": "sun", "hour": 21, "minute": 0},
            ),
            "pendo_month_end_finance_summary": (
                "scheduled_month_end_finance_summary",
                {"day": "last", "hour": 21, "minute": 0},
            ),
            "pendo_cleanup_demo_data": (
                "scheduled_cleanup_demo_data",
                {"hour": "*/6", "minute": 15},
            ),
        }
        schedule = config.get("schedule", [])
        assert len(schedule) == len(expected)
        assert {entry["id"]: (entry["handler"], entry["cron"]) for entry in schedule} == expected

        from plugins.pendo import main as pendo_main

        assert all(hasattr(pendo_main, handler) for handler, _cron in expected.values())

    def test_cleanup_expired_demo_data_runs_periodic_purge(self, monkeypatch):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        calls = []

        def fake_purge(_db):
            calls.append(_db)
            return 3

        monkeypatch.setattr(scheduled_module, "purge_expired_demo_users", fake_purge)

        db = SimpleNamespace()
        result = asyncio.run(scheduled_module.cleanup_expired_demo_data(SimpleNamespace(), db))

        assert result == []
        assert calls == [db]

    def test_demo_cleanup_scheduled_handler_delegates_with_runtime_database(self, monkeypatch):
        from plugins.pendo import main as pendo_main

        context = SimpleNamespace(logger=pendo_main.logger)
        database = SimpleNamespace()
        calls = []

        async def fake_cleanup(received_context, received_database):
            calls.append(("cleanup", received_context, received_database))
            return [{"type": "text", "data": {"text": "done"}}]

        async def fake_run(received_context, task_name, task_func, log):
            calls.append(("run", received_context, task_name, log))
            return await task_func()

        monkeypatch.setattr(pendo_main, "_get_database", lambda _context: database)
        monkeypatch.setattr(pendo_main, "cleanup_expired_demo_data", fake_cleanup)
        monkeypatch.setattr(pendo_main, "_run_scheduled_task", fake_run)

        result = asyncio.run(pendo_main.scheduled_cleanup_demo_data(context))

        assert result == [{"type": "text", "data": {"text": "done"}}]
        assert calls[0][0:3] == ("run", context, "cleanup_demo_data")
        assert calls[1] == ("cleanup", context, database)


class TestPendoFinanceSummaries:
    def test_weekly_finance_summary_sends_on_sunday_evening(self, monkeypatch):
        import sys
        from datetime import datetime, timezone

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(2030, 1, 6, 13, 0, tzinfo=timezone.utc)
                if tz is None:
                    return base.replace(tzinfo=None)
                return base.astimezone(tz)

        actions = []

        async def send_action(action):
            actions.append(action)
            return True

        async def fake_get_active_user_ids(_db):
            return ["1001"]

        generate_calls = []

        async def fake_generate_summary(*_args, **_kwargs):
            generate_calls.append(_args)
            return "weekly-summary"

        monkeypatch.setattr(scheduled_module, "datetime", _FixedDateTime)
        monkeypatch.setattr(scheduled_module, "_get_active_user_ids", fake_get_active_user_ids)
        monkeypatch.setattr(
            scheduled_module, "get_user_settings_bundle_map", _single_user_shanghai_settings
        )
        monkeypatch.setattr(
            scheduled_module, "_generate_finance_summary_content", fake_generate_summary
        )
        monkeypatch.setattr(scheduled_module, "save_user_setting", lambda *args, **kwargs: None)

        db = _with_scheduled_delivery_contract(SimpleNamespace())
        result = asyncio.run(
            scheduled_module.send_weekly_finance_summaries(
                SimpleNamespace(send_action=send_action), db
            )
        )

        assert result == []
        assert len(actions) == 1
        assert actions[0]["params"]["user_id"] == 1001
        assert "weekly-summary" in actions[0]["params"]["message"][0]["data"]["text"]
        assert generate_calls == [
            (
                db,
                "1001",
                scheduled_module._FinancePeriod(
                    "2030-W01",
                    "2029-12-31",
                    "2030-01-06",
                    "12/31 - 01/06",
                    "📆 本周财务总结",
                ),
            )
        ]

    def test_month_end_finance_summary_sends_on_last_day_evening(self, monkeypatch):
        import sys
        from datetime import datetime, timezone

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(2030, 3, 31, 13, 0, tzinfo=timezone.utc)
                if tz is None:
                    return base.replace(tzinfo=None)
                return base.astimezone(tz)

        actions = []

        async def send_action(action):
            actions.append(action)
            return True

        async def fake_get_active_user_ids(_db):
            return ["1001"]

        generate_calls = []

        async def fake_generate_summary(*_args, **_kwargs):
            generate_calls.append(_args)
            return "month-summary"

        monkeypatch.setattr(scheduled_module, "datetime", _FixedDateTime)
        monkeypatch.setattr(scheduled_module, "_get_active_user_ids", fake_get_active_user_ids)
        monkeypatch.setattr(
            scheduled_module, "get_user_settings_bundle_map", _single_user_shanghai_settings
        )
        monkeypatch.setattr(
            scheduled_module, "_generate_finance_summary_content", fake_generate_summary
        )
        monkeypatch.setattr(scheduled_module, "save_user_setting", lambda *args, **kwargs: None)

        db = _with_scheduled_delivery_contract(SimpleNamespace())
        result = asyncio.run(
            scheduled_module.send_month_end_finance_summaries(
                SimpleNamespace(send_action=send_action), db
            )
        )

        assert result == []
        assert len(actions) == 1
        assert actions[0]["params"]["user_id"] == 1001
        assert "month-summary" in actions[0]["params"]["message"][0]["data"]["text"]
        assert generate_calls == [
            (
                db,
                "1001",
                scheduled_module._FinancePeriod(
                    "2030-03",
                    "2030-03-01",
                    "2030-03-31",
                    "2030/03/01 - 2030/03/31",
                    "🧾 月底财务总结",
                ),
            )
        ]

    def test_finance_summary_uses_amount_cents_and_ledger_date_range(self):
        import shutil
        import sys
        import uuid

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module
        from plugins.pendo.services.db import Database

        temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_finance_summary_{uuid.uuid4().hex}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        db = Database(str(temp_dir / "pendo.db"))
        owner_id = "u-finance-summary"

        try:
            db.insert_item(
                {
                    "id": "sum_expense",
                    "owner_id": owner_id,
                    "type": "ledger",
                    "title": "午饭",
                    "amount_cents": 12345,
                    "transaction_type": "expense",
                    "ledger_category": "餐饮",
                    "ledger_date": "2026-05-02",
                    "account_name": "微信",
                }
            )
            db.insert_item(
                {
                    "id": "sum_income",
                    "owner_id": owner_id,
                    "type": "ledger",
                    "title": "工资",
                    "amount_cents": 500000,
                    "transaction_type": "income",
                    "ledger_category": "工资",
                    "ledger_date": "2026-05-03",
                    "account_name": "招商银行卡",
                }
            )
            db.insert_item(
                {
                    "id": "sum_transfer",
                    "owner_id": owner_id,
                    "type": "ledger",
                    "title": "转入储蓄",
                    "amount_cents": 20000,
                    "transaction_type": "transfer",
                    "ledger_category": "转账",
                    "ledger_date": "2026-05-04",
                    "account_name": "招商银行卡",
                    "counter_account_name": "储蓄卡",
                }
            )
            db.insert_item(
                {
                    "id": "sum_outside",
                    "owner_id": owner_id,
                    "type": "ledger",
                    "title": "范围外支出",
                    "amount_cents": 999999,
                    "transaction_type": "expense",
                    "ledger_category": "测试",
                    "ledger_date": "2026-06-01",
                    "account_name": "微信",
                }
            )
            conn = db.get_connection()
            with conn:
                conn.execute(
                    "UPDATE items SET amount = 0 WHERE id IN (?, ?)",
                    ("sum_expense", "sum_income"),
                )
            db.cache_clear()

            summary = asyncio.run(
                scheduled_module._generate_finance_summary_content(
                    db,
                    owner_id,
                    scheduled_module._FinancePeriod(
                        "2026-05",
                        "2026-05-01",
                        "2026-05-31",
                        "2026/05/01 - 2026/05/31",
                        "测试财务总结",
                    ),
                )
            )

            assert "🧾 共 3 笔流水" in summary
            assert "💰 收入: ¥5000.00" in summary
            assert "💸 支出: ¥123.45" in summary
            assert "📊 结余: +¥4876.55" in summary
            assert "🔁 转账: ¥200.00" in summary
            assert "📂 最大支出分类: 餐饮 ¥123.45" in summary
            assert "📥 主要收入来源: 工资 ¥5000.00" in summary
            assert "🔥 最大单笔支出: 午饭 ¥123.45 (2026-05-02)" in summary
            assert "账户收支:" in summary
            assert "招商银行卡 收入¥5000.00 支出¥0.00 净额+¥5000.00" in summary
            assert "微信 收入¥0.00 支出¥123.45 净额¥-123.45" in summary
            assert "转账流向:" in summary
            assert "招商银行卡 → 储蓄卡 ¥200.00" in summary
            assert "范围外支出" not in summary
        finally:
            db.cleanup()
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_finance_metrics_ignore_non_ledger_models(self):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module
        from plugins.pendo.models.item import Item, ItemType, LedgerItem

        metrics = scheduled_module._summarize_finance_items(
            [
                Item(type=ItemType.LEDGER, title="错误模型"),
                LedgerItem(
                    title="有效支出",
                    amount_cents=1250,
                    transaction_type="expense",
                ),
            ]
        )

        assert metrics.item_count == 1
        assert metrics.total_expense == 12.5
        assert metrics.top_expense is not None
        assert metrics.top_expense.title == "有效支出"

    def test_scheduled_private_send_skips_non_numeric_owner_ids(self):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        messages = []
        result = asyncio.run(
            scheduled_module._send_private_or_collect(
                SimpleNamespace(),
                messages,
                "demo_web_TEST",
                "测试消息",
            )
        )

        assert result is False
        assert messages == []
