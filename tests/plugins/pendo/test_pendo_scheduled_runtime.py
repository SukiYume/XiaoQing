"""定时任务锁、周期消息和投递状态。"""

from __future__ import annotations

from datetime import UTC

from tests.helpers.pendo_test_support import (
    ROOT,
    SimpleNamespace,
    _ReminderMessageService,
    _with_scheduled_delivery_contract,
    asyncio,
    datetime,
    json,
    timezone,
)


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

        db       = _Db()
        delivery = scheduled_module._ReminderDelivery(
            user_id            = "1001",
            message            = "提醒消息",
            item_id            = "evt123",
            remind_time        = "2030-01-01T09:00:00",
            claim_token        = "claim-1",
            claim_kind         = "initial",
            claim_repeat_count = 0,
            delivery_key       = "delivery-1",
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
        from datetime import datetime
        from typing import Any, cast

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(2030, 1, 1, 0, 0, tzinfo=UTC)
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
                    get_user_settings       = get_user_settings,
                    get_user_settings_batch = get_user_settings_batch,
                    update_user_settings    = lambda user_id, settings: True,
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
        from datetime import datetime
        from typing import Any, cast

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(2030, 1, 1, 0, 0, tzinfo=UTC)
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
                    get_user_settings_batch = get_user_settings_batch,
                    update_user_settings    = lambda user_id, settings: True,
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
            batch_calls           = []
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
                owner_id              = "u1",
                title                 = "主会场报告",
                start_time            = "2030-01-02T10:30:00",
                remind_times          = [],
                event_role            = "multi_node_child",
                event_collection_id   = "milebrief",
                event_collection_kind = "multi_node",
                event_index           = 2,
                event_node_key        = "m02",
                created_at            = "2030-01-01T00:00:00",
                updated_at            = "2030-01-01T00:00:00",
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
        from datetime import datetime

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module
        from plugins.pendo.models.item import TaskItem

        fixed_now = datetime(2030, 1, 2, 8, 0, tzinfo=UTC)
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
            staticmethod(lambda _user_id, _db: UTC),
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
        from datetime import datetime
        from typing import Any, cast

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(2030, 1, 1, 13, 30, tzinfo=UTC)
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
                    get_user_settings_batch = get_user_settings_batch,
                    update_user_settings    = lambda user_id, settings: True,
                    has_diary_for_date      = has_diary_for_date,
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
            "pendo_reminders": ("scheduled", "targeted", {"minute": "*"}),
            "pendo_daily_briefing": (
                "scheduled_daily_briefing",
                "targeted",
                {"minute": "*"},
            ),
            "pendo_diary_reminder": (
                "scheduled_diary_reminder",
                "targeted",
                {"minute": "*"},
            ),
            "pendo_migrate_todos": (
                "scheduled_migrate_todos",
                "targeted",
                {"hour": 0, "minute": 5},
            ),
            "pendo_prune_operation_logs": (
                "scheduled_prune_operation_logs",
                "silent",
                {"hour": 0, "minute": 15},
            ),
            "pendo_weekly_finance_summary": (
                "scheduled_weekly_finance_summary",
                "targeted",
                {"day_of_week": "sun", "hour": 21, "minute": 0},
            ),
            "pendo_month_end_finance_summary": (
                "scheduled_month_end_finance_summary",
                "targeted",
                {"day": "last", "hour": 21, "minute": 0},
            ),
            "pendo_cleanup_demo_data": (
                "scheduled_cleanup_demo_data",
                "silent",
                {"hour": "*/6", "minute": 15},
            ),
        }
        schedule = config.get("schedule", [])
        assert len(schedule) == len(expected)
        assert {
            entry["id"]: (entry["handler"], entry["delivery"], entry["cron"]) for entry in schedule
        } == expected

        from plugins.pendo import main as pendo_main

        assert all(hasattr(pendo_main, handler) for handler, _delivery, _cron in expected.values())

    def test_cleanup_expired_demo_data_runs_periodic_purge(self, monkeypatch):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.commands import scheduled as scheduled_module

        calls = []

        def fake_purge(_db):
            calls.append(_db)
            return 3

        monkeypatch.setattr(scheduled_module, "purge_expired_demo_users", fake_purge)

        db     = SimpleNamespace()
        result = asyncio.run(scheduled_module.cleanup_expired_demo_data(SimpleNamespace(), db))

        assert result == []
        assert calls == [db]

    def test_demo_cleanup_scheduled_handler_delegates_with_runtime_database(self, monkeypatch):
        from plugins.pendo import main as pendo_main

        context = SimpleNamespace(logger=pendo_main.logger)
        database = SimpleNamespace()
        calls    = []

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

        assert result is None
        assert calls[0][0:3] == ("run", context, "cleanup_demo_data")
        assert calls[1] == ("cleanup", context, database)
