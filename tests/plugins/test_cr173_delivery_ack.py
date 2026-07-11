from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from plugins.pendo.commands import scheduled


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_result", [False, None])
async def test_claimed_reminder_is_released_when_onebot_does_not_ack(
    delivery_result,
    monkeypatch,
) -> None:
    class ReminderService:
        def check_and_send_reminders(self, context=None):
            return {
                "messages": [{
                    "user_id": "1001",
                    "message": "due",
                    "item_id": "evt-1",
                    "remind_time": "2030-01-01T00:00:00+00:00",
                    "claim_token": "lease-1",
                }]
            }

    db = SimpleNamespace(released=[], completed=[])
    db.release_reminder_claim = lambda *args: db.released.append(args) or True
    db.complete_reminder_claim = lambda *args: db.completed.append(args) or True

    async def send_action(_action):
        return delivery_result

    monkeypatch.setattr(scheduled, "get_database", lambda _context: db)
    monkeypatch.setattr(scheduled, "_reminder_service_singleton", ReminderService())
    await scheduled.check_reminders(SimpleNamespace(send_action=send_action))
    assert db.completed == []
    assert db.released == [(
        "evt-1",
        "2030-01-01T00:00:00+00:00",
        "lease-1",
    )]


@pytest.mark.asyncio
async def test_daily_marker_is_written_only_after_confirmed_delivery(monkeypatch) -> None:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime(2030, 1, 1, tzinfo=timezone.utc)
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

    async def content(_user, _db, _parser):
        return "daily-secret"

    saved: list[tuple] = []
    monkeypatch.setattr(scheduled, "datetime", FixedDateTime)
    monkeypatch.setattr(scheduled, "_get_active_user_ids", active_users)
    monkeypatch.setattr(scheduled, "get_user_settings_bundle_map", settings_map)
    monkeypatch.setattr(scheduled, "_generate_briefing_content", content)
    monkeypatch.setattr(scheduled, "AIParser", lambda _context: object())
    monkeypatch.setattr(scheduled, "save_user_setting", lambda *args: saved.append(args))

    async def rejected(_action):
        return False

    await scheduled.send_daily_briefings(SimpleNamespace(send_action=rejected), object())
    assert saved == []

    async def confirmed(_action):
        return True

    await scheduled.send_daily_briefings(SimpleNamespace(send_action=confirmed), object())
    assert saved and saved[0][:3] == (
        "1001",
        "last_daily_briefing_date",
        "2030-01-01",
    )


@pytest.mark.asyncio
async def test_collecting_action_is_not_delivery_ack() -> None:
    actions: list[dict] = []
    confirmed = await scheduled._send_private_or_collect(
        SimpleNamespace(), actions, "1001", "message"
    )
    assert confirmed is False
    assert len(actions) == 1
