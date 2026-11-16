"""Pendo 确认、延后和撤销命令的边界。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from plugins.pendo.commands import operations
from plugins.pendo.config import PendoConfig
from plugins.pendo.models.item import EventItem, LedgerItem
from plugins.pendo.services.reminder import ReminderService


def _seed_event(db, owner_id: str, offsets_minutes: list[int]) -> tuple[str, list[str]]:
    """写入一条日程，提醒点按相对当前时刻的分钟数给出，返回条目 ID 和落库后的提醒时间。"""

    now = datetime.now(UTC).replace(microsecond=0)
    item_id = db.insert_item(
        {
            "owner_id"    : owner_id,
            "type"        : "event",
            "title"       : "跨时区提醒",
            "start_time"  : (now + timedelta(hours=5)).isoformat(),
            "remind_times": [(now + timedelta(minutes=m)).isoformat() for m in offsets_minutes],
        }
    )
    return item_id, list(db.get_item(item_id, owner_id).remind_times)


def _log_states(db, item_id: str) -> dict[str, tuple[str, str | None]]:
    return {
        row["remind_time"]: (row["state"], row["user_action"])
        for row in db.get_reminder_logs(item_id)
    }


def test_snooze_without_sent_log_keeps_future_reminders_and_confirms_nothing(db) -> None:
    """没有已发送日志时不能用空提醒时间批量确认其他日志；用户时区只影响显示。"""

    db.update_user_settings("owner-a", {"timezone": "America/Los_Angeles"})
    item_id, (past, future) = _seed_event(db, "owner-a", [-30, 60])
    before = datetime.now(UTC)

    result = asyncio.run(operations.handle_snooze("owner-a", f"{item_id} 10m", ReminderService(db)))

    assert result["status"] == "success"
    snoozed, kept = db.get_item(item_id, "owner-a").remind_times
    assert past not in (snoozed, kept)
    assert kept == future
    assert (
        timedelta(minutes=10) - timedelta(seconds=5)
        <= datetime.fromisoformat(snoozed) - before
        <= timedelta(minutes=10) + timedelta(seconds=5)
    )
    shown = datetime.fromisoformat(snoozed).astimezone(ZoneInfo("America/Los_Angeles"))
    assert result["message"] == f"已将提醒延后到: {shown:%m月%d日 %H:%M}"
    assert {state for state, _ in _log_states(db, item_id).values()} == {"pending"}


def test_confirm_without_sent_log_confirms_nothing() -> None:
    """快捷确认只能处理具体已发送日志，不能退化成整条目批量确认。"""

    db = MagicMock()
    db.get_item.return_value = EventItem(id="event-a", title="未发送提醒")
    db.get_last_unconfirmed_remind_time.return_value = None
    reminder_service = MagicMock(db=db)

    result = asyncio.run(operations.handle_confirm("owner-a", "event-a", reminder_service, db))

    assert result == {"status": "error", "message": "未找到待确认的已发送提醒"}
    reminder_service.confirm_reminder.assert_not_called()


def test_snooze_confirms_only_the_located_log(db) -> None:
    item_id, (sent, upcoming) = _seed_event(db, "owner-a", [-30, 60])
    db.log_reminder(item_id, sent, sent=True)

    result = asyncio.run(operations.handle_snooze("owner-a", f"{item_id} 10m", ReminderService(db)))

    assert result["status"] == "success"
    states = _log_states(db, item_id)
    assert states[sent] == ("confirmed", "delayed")
    assert states[upcoming] == ("pending", None)
    assert sent not in db.get_item(item_id, "owner-a").remind_times


def test_snooze_update_failure_does_not_confirm_old_log() -> None:
    db                       = MagicMock()
    db.get_item.return_value = EventItem(
        id           = "event-a",
        remind_times = ["2030-01-01T09:00:00+08:00"],
    )
    db.get_last_unconfirmed_remind_time.return_value = "2030-01-01T09:00:00+08:00"
    db.update_item.return_value                      = False
    reminder_service = MagicMock(db=db)

    result = asyncio.run(operations.handle_snooze("owner-a", "event-a 10m", reminder_service))

    assert result["status"] == "error"
    assert "条目已变化" in result["message"]
    reminder_service.confirm_reminder.assert_not_called()


def test_operations_reject_ambiguous_arguments_without_database_access() -> None:
    db = MagicMock()
    reminder_service = MagicMock(db=db)

    confirm = asyncio.run(
        operations.handle_confirm("owner-a", "event-a extra", reminder_service, db)
    )
    snooze = asyncio.run(operations.handle_snooze("owner-a", "event-a 10m extra", reminder_service))
    undo   = asyncio.run(operations.handle_undo("owner-a", "five", db))

    assert confirm["status"] == snooze["status"] == undo["status"] == "error"
    db.get_item.assert_not_called()
    db.get_latest_undoable_operation.assert_not_called()


@pytest.mark.parametrize("minutes", ["²", "٣", "１２"])
def test_undo_rejects_unicode_digits_without_database_access(minutes: str) -> None:
    db = MagicMock()

    result = asyncio.run(operations.handle_undo("owner-a", minutes, db))

    assert result == {
        "status" : "error",
        "message": f"分钟数必须是 1～{PendoConfig.UNDO_WINDOW_MINUTES} 的整数",
    }
    db.get_latest_undoable_operation.assert_not_called()


@pytest.mark.parametrize("minutes", ["0", "6", "999999"])
def test_undo_rejects_ranges_beyond_snapshot_window(minutes: str) -> None:
    db = MagicMock()

    result = asyncio.run(operations.handle_undo("owner-a", minutes, db))

    assert result["status"] == "error"
    assert f"1～{PendoConfig.UNDO_WINDOW_MINUTES}" in result["message"]
    db.get_latest_undoable_operation.assert_not_called()


def test_undo_delete_reports_ledger_type() -> None:
    db                                            = MagicMock()
    db.get_latest_undoable_operation.return_value = {"type": "delete"}
    db.undo_delete.return_value                   = {
        "status": "success",
        "item"  : LedgerItem(id="ledger-a", title="午餐"),
    }

    result = asyncio.run(operations.handle_undo("owner-a", "3", db))

    assert result == {
        "status" : "success",
        "message": "✅ 已恢复账目: 午餐 (ledger-a)",
    }
    db.undo_delete.assert_called_once_with("owner-a", 3)


def test_undo_restores_queue_without_replaying_sent_history(db):
    now = datetime.now(UTC).replace(microsecond=0)
    times = [(now + timedelta(minutes=m)).isoformat() for m in [1, 5]]
    iid = db.insert_item(
        {"owner_id": "u", "type": "task", "title": "restore", "remind_times": times}
    )
    db.get_connection().execute(
        "UPDATE reminder_logs SET sent_at=?, confirmed_at=? WHERE item_id=? AND remind_time=?",
        (now.isoformat(), now.isoformat(), iid, times[0]),
    )
    db.get_connection().commit()
    db.delete_item(iid, owner_id="u")
    assert db.undo_delete("u")["status"] == "success"
    rows = (
        db.get_connection()
        .execute("SELECT * FROM reminder_logs WHERE item_id=? ORDER BY remind_time", (iid,))
        .fetchall()
    )
    assert len(rows) == 2
    assert rows[0]["sent_at"] is not None
    assert rows[1]["state"] == "pending"
