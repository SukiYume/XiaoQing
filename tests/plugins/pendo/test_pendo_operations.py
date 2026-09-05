"""Pendo 确认、延后和撤销命令的边界回归。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from plugins.pendo.commands import operations
from plugins.pendo.config import PendoConfig
from plugins.pendo.models.item import EventItem, LedgerItem


def test_snooze_without_sent_log_keeps_user_timezone_future_and_confirms_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有已发送日志时不能用空提醒时间批量确认其他日志。"""

    user_now = datetime.fromisoformat("2030-01-01T09:30:00-08:00")
    monkeypatch.setattr(operations, "now_in_timezone", lambda *_args: user_now)
    db                       = MagicMock()
    db.get_item.return_value = EventItem(
        id           = "event-a",
        title        = "跨时区提醒",
        remind_times = ["2030-01-01T09:00:00", "2030-01-01T10:00:00"],
    )
    db.get_last_unconfirmed_remind_time.return_value = None
    db.update_item.return_value                      = True
    reminder_service = MagicMock(db=db)

    result = asyncio.run(operations.handle_snooze("owner-a", "event-a 10m", reminder_service))

    assert result["status"] == "success"
    db.update_item.assert_called_once_with(
        "event-a",
        {
            "remind_times": [
                "2030-01-01T09:40:00-08:00",
                "2030-01-01T10:00:00",
            ]
        },
        "owner-a",
        expected_version=0,
    )
    reminder_service.confirm_reminder.assert_not_called()


def test_confirm_without_sent_log_confirms_nothing() -> None:
    """快捷确认只能处理具体已发送日志，不能退化成整条目批量确认。"""

    db = MagicMock()
    db.get_item.return_value = EventItem(id="event-a", title="未发送提醒")
    db.get_last_unconfirmed_remind_time.return_value = None
    reminder_service = MagicMock(db=db)

    result = asyncio.run(operations.handle_confirm("owner-a", "event-a", reminder_service, db))

    assert result == {"status": "error", "message": "未找到待确认的已发送提醒"}
    reminder_service.confirm_reminder.assert_not_called()


def test_snooze_confirms_only_the_located_log(monkeypatch: pytest.MonkeyPatch) -> None:
    user_now = datetime.fromisoformat("2030-01-01T09:30:00+08:00")
    monkeypatch.setattr(operations, "now_in_timezone", lambda *_args: user_now)
    db                       = MagicMock()
    db.get_item.return_value = EventItem(
        id           = "event-a",
        remind_times = ["2030-01-01T09:00:00+08:00", "2030-01-01T10:00:00+08:00"],
    )
    db.get_last_unconfirmed_remind_time.return_value = "2030-01-01T09:00:00+08:00"
    db.update_item.return_value                      = True
    reminder_service = MagicMock(db=db)
    reminder_service.confirm_reminder.return_value = {"status": "success"}

    result = asyncio.run(operations.handle_snooze("owner-a", "event-a 10m", reminder_service))

    assert result["status"] == "success"
    reminder_service.confirm_reminder.assert_called_once_with(
        "event-a",
        "delayed",
        "owner-a",
        "2030-01-01T09:00:00+08:00",
    )


def test_snooze_update_failure_does_not_confirm_old_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        operations,
        "now_in_timezone",
        lambda *_args: datetime.fromisoformat("2030-01-01T09:30:00+08:00"),
    )
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
        "status": "error",
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
        "item": LedgerItem(id="ledger-a", title="午餐"),
    }

    result = asyncio.run(operations.handle_undo("owner-a", "3", db))

    assert result == {
        "status": "success",
        "message": "✅ 已恢复账目: 午餐 (ledger-a)",
    }
    db.undo_delete.assert_called_once_with("owner-a", 3)
