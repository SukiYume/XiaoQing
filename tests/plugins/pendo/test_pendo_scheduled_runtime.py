"""定时任务锁、周期消息和投递状态。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from plugins.pendo import main as pendo_main
from plugins.pendo.commands import scheduled as scheduled_module
from tests.helpers.pendo_test_support import (
    ROOT,
    ReminderMessageService,
    attach_action_recorder,
    freeze_pendo_reminder_clock,
    pendo_service_context,
)


@pytest.fixture
def pendo_context(db, tmp_path):
    """真实服务集合（共用测试数据库）；没有 ``send_action``，定时任务把动作原样返回。"""

    with pendo_service_context(db, tmp_path) as context:
        yield context


def _seed_note(db, owner_id: str) -> None:
    """定时任务只处理至少有一条数据的用户。"""

    db.insert_item({"owner_id": owner_id, "type": "note", "title": f"笔记{owner_id}"})


def _set_schedule(db, owner_id: str, timezone_name: str) -> None:
    db.update_user_settings(
        owner_id,
        {
            "timezone"         : timezone_name,
            "daily_report_time": "08:00",
            "diary_remind_time": "21:30",
            "quiet_hours_start": "23:00",
            "quiet_hours_end"  : "07:00",
        },
    )


def _seed_demo_owners(db) -> None:
    """一个早已过期、一个远未过期的 Web 演示用户，各有一条笔记。"""

    for owner_id, expires_at in (
        ("demo_web_expired", "2020-01-01T00:00:00"),
        ("demo_web_active", "2999-01-01T00:00:00"),
    ):
        db.update_user_settings(
            owner_id,
            {"settings_json": {"demo_mode": True, "demo_expires_at": expires_at}},
        )
        _seed_note(db, owner_id)


def _assert_only_active_demo_owner_remains(db) -> None:
    assert db.get_items("demo_web_expired", filters={"type": "note"}, limit=10) == []
    assert db.get_user_settings("demo_web_expired")["settings_json"].get("demo_mode") is not True
    assert len(db.get_items("demo_web_active", filters={"type": "note"}, limit=10)) == 1
    assert db.get_user_settings("demo_web_active")["settings_json"]["demo_mode"] is True


class TestScheduledTasks:
    def test_check_reminders_returns_messages_without_send_action(self, monkeypatch):

        from plugins.pendo.commands import scheduled as scheduled_module

        db = SimpleNamespace(logged=[])

        def fake_log_reminder(item_id, remind_time, sent=True):
            db.logged.append((item_id, remind_time, sent))

        db.log_reminder = fake_log_reminder

        result = asyncio.run(
            scheduled_module.check_reminders(SimpleNamespace(), db, ReminderMessageService())
        )

        assert len(result) == 1
        assert result[0]["params"]["user_id"] == 1001
        assert db.logged == []

    def test_check_reminders_does_not_confirm_onebot_rejection(self, monkeypatch):

        from plugins.pendo.commands import scheduled as scheduled_module

        class _RejectedContext:
            async def send_action(self, action):
                return False

        db = SimpleNamespace(logged=[])
        db.log_reminder = lambda item_id, remind_time, sent=True: db.logged.append(
            (item_id, remind_time, sent)
        )

        result = asyncio.run(
            scheduled_module.check_reminders(_RejectedContext(), db, ReminderMessageService())
        )

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
        before = datetime.now(UTC)

        asyncio.run(scheduled_module._settle_reminder_delivery(db, delivery, delivered=False))

        assert db.retry_at is not None
        delay = (db.retry_at - before).total_seconds()
        assert (
            PendoConfig.REMINDER_REPEAT_INTERVAL_SECONDS
            <= delay
            <= (PendoConfig.REMINDER_REPEAT_INTERVAL_SECONDS + 2)
        )

    def test_daily_briefing_respects_user_timezone_and_configured_time(
        self, db, pendo_context, monkeypatch
    ):
        # 2030-01-01 08:00（上海）；同一时刻纽约是 2029-12-31 19:00。
        freeze_pendo_reminder_clock(monkeypatch, datetime(2030, 1, 1, 0, 0, tzinfo=UTC))
        for owner_id, timezone_name, start_time in (
            ("1001", "Asia/Shanghai", "2030-01-01T09:30:00"),
            ("1002", "America/New_York", "2029-12-31T09:30:00"),
        ):
            _set_schedule(db, owner_id, timezone_name)
            db.update_user_settings(owner_id, {"settings_json": {"daily_briefing_enabled": True}})
            db.insert_item(
                {
                    "owner_id"  : owner_id,
                    "type"      : "event",
                    "title"     : f"晨跑{owner_id}",
                    "start_time": start_time,
                }
            )
        sent = attach_action_recorder(pendo_context)

        result = asyncio.run(pendo_main.scheduled_daily_briefing(pendo_context))

        assert result == []
        assert [action["params"]["user_id"] for action in sent] == [1001]
        assert "晨跑1001" in sent[0]["params"]["message"][0]["data"]["text"]
        assert (
            db.get_user_settings("1001")["settings_json"]["last_daily_briefing_date"]
            == "2030-01-01"
        )
        assert "last_daily_briefing_date" not in db.get_user_settings("1002")["settings_json"]

    def test_daily_briefing_returns_messages_without_send_action(
        self, db, pendo_context, monkeypatch
    ):
        freeze_pendo_reminder_clock(monkeypatch, datetime(2030, 1, 1, 0, 0, tzinfo=UTC))
        _set_schedule(db, "1001", "Asia/Shanghai")
        _seed_note(db, "1001")

        result = asyncio.run(pendo_main.scheduled_daily_briefing(pendo_context))

        assert len(result) == 1
        assert result[0]["params"]["user_id"] == 1001

    def test_generate_daily_briefing_includes_today_multi_node_leaf_events(
        self, tmp_path, monkeypatch
    ):
        from plugins.pendo.models.item import EventItem
        from plugins.pendo.services.db import Database

        freeze_pendo_reminder_clock(
            monkeypatch, datetime.fromisoformat("2030-01-02T08:00:00+08:00")
        )

        db = Database(str(tmp_path / "pendo_briefing_milestone.db"))

        try:
            db.create_event_collection(
                {
                    "id"        : "milebrief",
                    "owner_id"  : "u1",
                    "kind"      : "multi_node",
                    "title"     : "学术会议",
                    "category"  : "未分类",
                    "start_time": "2030-01-01T09:00:00",
                    "end_time"  : "2030-01-03T18:00:00",
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

            assert "🗓️ 今日日程" in briefing
            assert "10:30 学术会议 · 主会场报告" in briefing
            assert "报到" not in briefing
            assert "闭幕" not in briefing
            assert "今日暂无日程安排" not in briefing
        finally:
            db.cleanup()

    def test_generate_daily_briefing_ignores_malformed_overdue_deadline(self):
        from plugins.pendo.models.item import TaskItem

        overdue = TaskItem(owner_id="1001", title="旧数据", deadline_at="not-an-iso-time")
        db = SimpleNamespace(
            get_briefing_items=lambda *_args: ([], [], [overdue]),
        )

        briefing = asyncio.run(scheduled_module._generate_briefing_content("1001", db))

        assert "⚠️ 逾期待办 (1项):" in briefing
        assert "(截止:" not in briefing

    def test_migrate_todos_returns_messages_without_send_action(
        self, db, pendo_context, monkeypatch
    ):
        # 2030-01-02 00:05（上海）：昨天的未完成待办顺延到今天。
        freeze_pendo_reminder_clock(monkeypatch, datetime(2030, 1, 1, 16, 5, tzinfo=UTC))
        task_id = db.insert_item(
            {
                "owner_id" : "1001",
                "type"     : "task",
                "title"    : "昨天没做完",
                "status"   : "open",
                "plan_date": "2030-01-01",
            }
        )

        result = asyncio.run(pendo_main.scheduled_migrate_todos(pendo_context))

        assert len(result) == 1
        assert result[0]["params"]["user_id"] == 1001
        assert db.get_item(task_id, "1001").plan_date == "2030-01-02"

    def test_diary_reminder_respects_user_timezone_and_existing_diary(
        self, db, pendo_context, monkeypatch
    ):
        # 2030-01-01 21:30（上海）；同一时刻纽约是 08:30。
        freeze_pendo_reminder_clock(monkeypatch, datetime(2030, 1, 1, 13, 30, tzinfo=UTC))
        for owner_id, timezone_name in (
            ("2001", "Asia/Shanghai"),
            ("2002", "Asia/Shanghai"),
            ("2003", "America/New_York"),
        ):
            _set_schedule(db, owner_id, timezone_name)
            _seed_note(db, owner_id)
        db.insert_item(
            {
                "owner_id"  : "2002",
                "type"      : "diary",
                "title"     : "已写日记",
                "diary_date": "2030-01-01",
            }
        )
        sent = attach_action_recorder(pendo_context)

        result = asyncio.run(pendo_main.scheduled_diary_reminder(pendo_context))

        assert result == []
        assert [action["params"]["user_id"] for action in sent] == [2001]

    def test_plugin_manifest_schedule_matches_runtime_contract(self):
        from core.models import PluginManifest

        plugin_json_path = ROOT / "plugins" / "pendo" / "plugin.json"
        with open(plugin_json_path, encoding="utf-8") as f:
            config = json.load(f)

        # 先走生产清单模型，避免测试只验证局部字段却漏掉 schema 漂移。
        manifest = PluginManifest.model_validate(config)
        assert manifest.name == "pendo"

        expected = {
            "pendo_reminders"     : ("scheduled", "targeted", {"minute": "*"}),
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
                # 每小时检查，按用户本地日期跨天后迁移；数据库标记保证每天只迁移一次。
                {"minute": 5},
            ),
            "pendo_prune_operation_logs": (
                "scheduled_prune_operation_logs",
                "silent",
                {"hour": 0, "minute": 15},
            ),
            "pendo_weekly_finance_summary": (
                "scheduled_weekly_finance_summary",
                "targeted",
                {"minute": "*"},
            ),
            "pendo_month_end_finance_summary": (
                "scheduled_month_end_finance_summary",
                "targeted",
                {"minute": "*"},
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

    def test_cleanup_expired_demo_data_runs_periodic_purge(self, db, pendo_context):
        _seed_demo_owners(db)

        result = asyncio.run(scheduled_module.cleanup_expired_demo_data(pendo_context, db))

        assert result == []
        _assert_only_active_demo_owner_remains(db)

    def test_demo_cleanup_scheduled_handler_purges_the_runtime_database(self, db, pendo_context):
        _seed_demo_owners(db)

        result = asyncio.run(pendo_main.scheduled_cleanup_demo_data(pendo_context))

        assert result is None
        _assert_only_active_demo_owner_remains(db)

    def test_reminder_scheduled_handler_uses_the_shared_services(
        self, db, pendo_context, monkeypatch
    ):
        # 2030-01-01 09:00（上海）到点的提醒；上下文没有 send_action，动作原样返回。
        freeze_pendo_reminder_clock(monkeypatch, datetime(2030, 1, 1, 1, 0, tzinfo=UTC))
        db.insert_item(
            {
                "owner_id"    : "1001",
                "type"        : "event",
                "title"       : "到点提醒",
                "start_time"  : "2030-01-01T10:00:00",
                "remind_times": ["2030-01-01T09:00:00"],
            }
        )

        result = asyncio.run(pendo_main.scheduled(pendo_context))

        assert len(result) == 1
        assert result[0]["params"]["user_id"] == 1001
        assert "到点提醒" in result[0]["params"]["message"][0]["data"]["text"]
