from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from plugins.pendo.commands import scheduled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delivery_result", "expected_release"),
    [(False, True), (None, False)],
)
async def test_claimed_reminder_distinguishes_rejection_from_unknown_outcome(
    delivery_result,
    expected_release,
    monkeypatch,
) -> None:
    class ReminderService:
        def check_and_send_reminders(self, context=None):
            return {
                "messages": [
                    {
                        "user_id": "1001",
                        "message": "due",
                        "item_id": "evt-1",
                        "remind_time": "2030-01-01T00:00:00+00:00",
                        "claim_token": "lease-1",
                    }
                ]
            }

    db = SimpleNamespace(released=[], completed=[])
    db.release_reminder_claim  = lambda *args, **_kwargs: db.released.append(args) or True
    db.complete_reminder_claim = lambda *args: db.completed.append(args) or True

    async def send_action(_action):
        return delivery_result

    monkeypatch.setattr(scheduled, "get_database", lambda _context: db)
    monkeypatch.setattr(scheduled, "_reminder_service_singleton", ReminderService())
    await scheduled.check_reminders(SimpleNamespace(send_action=send_action))
    assert db.completed == []
    expected = [("evt-1", "2030-01-01T00:00:00+00:00", "lease-1")]
    assert db.released == (expected if expected_release else [])


@pytest.mark.asyncio
async def test_reminder_delivery_always_targets_owner_private_chat(monkeypatch) -> None:
    """提醒来源即使带群上下文，投递目标也固定为条目所有者私聊。"""

    class ReminderService:
        def check_and_send_reminders(self, context=None):
            return {
                "messages": [
                    {
                        "user_id": "1001",
                        "message": "private due",
                        "item_id": "evt-private",
                        "remind_time": "2030-01-01T00:00:00+00:00",
                        "claim_token": "lease-private",
                    }
                ]
            }

    completed: list[tuple]   = []
    sent_actions: list[dict] = []
    db                       = SimpleNamespace(
        complete_reminder_claim=lambda *args: completed.append(args) or True,
    )

    async def send_action(action):
        sent_actions.append(action)
        return True

    context = SimpleNamespace(
        event       = {"message_type": "group", "group_id": 2002, "user_id": 1001},
        send_action = send_action,
    )
    monkeypatch.setattr(scheduled, "get_database", lambda _context: db)
    monkeypatch.setattr(scheduled, "_reminder_service_singleton", ReminderService())

    assert await scheduled.check_reminders(context) == []
    assert len(sent_actions) == 1
    assert sent_actions[0]["action"] == "send_private_msg"
    assert sent_actions[0]["params"] == {
        "user_id": 1001,
        "message": [{"type": "text", "data": {"text": "private due"}}],
    }
    assert "group_id" not in sent_actions[0]["params"]
    assert completed == [("evt-private", "2030-01-01T00:00:00+00:00", "lease-private")]


@pytest.mark.asyncio
async def test_daily_marker_is_written_only_after_confirmed_delivery(monkeypatch) -> None:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime(2030, 1, 1, tzinfo=UTC)
            return instant if tz else instant.replace(tzinfo=None)

    async def active_users(_db):
        return ["1001"]

    async def settings_map(_users, _db):
        return {
            "1001": {
                "settings": {"timezone": "UTC", "daily_report_time": "00:00"},
                "custom_settings": {"daily_briefing_enabled": True},
            }
        }

    async def content(_user, _db):
        return "daily-secret"

    saved: list[tuple]     = []
    completed: list[tuple] = []
    released: list[tuple]  = []
    db                     = SimpleNamespace(
        claim_scheduled_delivery=lambda task, owner, period, **_kwargs: {
            "claim_token": "lease-1",
            "delivery_key": f"{task}:{owner}:{period}",
        },
        complete_scheduled_delivery = lambda *args: completed.append(args) or True,
        release_scheduled_delivery  = lambda *args: released.append(args) or True,
    )
    monkeypatch.setattr(scheduled, "datetime", FixedDateTime)
    monkeypatch.setattr(scheduled, "_get_active_user_ids", active_users)
    monkeypatch.setattr(scheduled, "get_user_settings_bundle_map", settings_map)
    monkeypatch.setattr(scheduled, "_generate_briefing_content", content)
    monkeypatch.setattr(scheduled, "save_user_setting", lambda *args: saved.append(args))

    async def rejected(_action):
        return False

    await scheduled.send_daily_briefings(SimpleNamespace(send_action=rejected), db)
    assert saved == []
    assert len(released) == 1
    assert completed == []

    async def confirmed(_action):
        return True

    await scheduled.send_daily_briefings(SimpleNamespace(send_action=confirmed), db)
    assert saved and saved[0][:3] == (
        "1001",
        "last_daily_briefing_date",
        "2030-01-01",
    )
    assert len(completed) == 1


@pytest.mark.asyncio
async def test_unknown_periodic_delivery_retains_claim_without_completing() -> None:
    completed: list[tuple] = []
    released: list[tuple]  = []
    db                     = SimpleNamespace(
        complete_scheduled_delivery = lambda *args: completed.append(args) or True,
        release_scheduled_delivery  = lambda *args: released.append(args) or True,
    )

    async def unknown(_action):
        return None

    claim  = scheduled._PeriodicClaim("daily_briefing", "1001", "2030-01-01", "lease", "key")
    result = await scheduled._send_claimed_private(
        SimpleNamespace(send_action=unknown),
        [],
        db,
        claim,
        "message",
    )

    assert result is False
    assert completed == []
    assert released == []


@pytest.mark.asyncio
async def test_collecting_action_is_not_delivery_ack() -> None:
    actions: list[dict] = []
    confirmed           = await scheduled._send_private_or_collect(
        SimpleNamespace(), actions, "1001", "message"
    )
    assert confirmed is False
    assert len(actions) == 1


@pytest.mark.asyncio
async def test_confirmed_periodic_send_never_releases_after_completion_error() -> None:
    """消息已被确认后，结算异常不能把租约恢复成可再次投递。"""

    released: list[tuple] = []

    def fail_completion(*_args):
        raise RuntimeError("injected completion failure")

    db = SimpleNamespace(
        complete_scheduled_delivery = fail_completion,
        release_scheduled_delivery  = lambda *args: released.append(args) or True,
    )

    async def confirmed(_action):
        return True

    claim = scheduled._PeriodicClaim("daily_briefing", "1001", "2030-01-01", "lease", "key")
    with pytest.raises(RuntimeError, match="completion failure"):
        await scheduled._send_claimed_private(
            SimpleNamespace(send_action=confirmed),
            [],
            db,
            claim,
            "message",
        )

    assert released == []


@pytest.mark.asyncio
async def test_periodic_send_error_releases_unsent_claim() -> None:
    """发送端抛异常时尚未确认送达，租约应立即释放。"""

    released: list[tuple] = []
    db                    = SimpleNamespace(
        release_scheduled_delivery=lambda *args: released.append(args) or True,
    )

    async def fail_send(_action):
        raise RuntimeError("injected send failure")

    claim = scheduled._PeriodicClaim("daily_briefing", "1001", "2030-01-01", "lease", "key")
    with pytest.raises(RuntimeError, match="send failure"):
        await scheduled._send_claimed_private(
            SimpleNamespace(send_action=fail_send),
            [],
            db,
            claim,
            "message",
        )

    assert released == [("daily_briefing", "1001", "2030-01-01", "lease")]


@pytest.mark.asyncio
async def test_invalid_reminder_owner_releases_claim(monkeypatch) -> None:
    """无法构造 QQ 私聊动作时，已领取的提醒不能滞留到租约超时。"""

    class ReminderService:
        def check_and_send_reminders(self, context=None):
            return {
                "messages": [
                    {
                        "user_id": "demo_web_TEST",
                        "message": "due",
                        "item_id": "evt-1",
                        "remind_time": "2030-01-01T00:00:00+00:00",
                        "claim_token": "lease-1",
                    }
                ]
            }

    released: list[tuple] = []
    db                    = SimpleNamespace(
        release_reminder_claim=lambda *args, **_kwargs: released.append(args) or True,
    )
    monkeypatch.setattr(scheduled, "get_database", lambda _context: db)
    monkeypatch.setattr(scheduled, "_reminder_service_singleton", ReminderService())

    assert await scheduled.check_reminders(SimpleNamespace()) == []
    assert released == [("evt-1", "2030-01-01T00:00:00+00:00", "lease-1")]
