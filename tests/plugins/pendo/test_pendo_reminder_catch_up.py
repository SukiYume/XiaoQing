"""停机补发、错过状态、定时窗口、延后写法和会话草稿的回归测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from plugins.pendo import main as pendo_main
from plugins.pendo.commands.operations import handle_snooze
from plugins.pendo.commands.scheduled import _is_within_schedule_window
from plugins.pendo.config import PendoConfig
from plugins.pendo.handlers.event_support import get_remind_status, get_remind_status_label
from plugins.pendo.handlers.ledger import LedgerHandler
from plugins.pendo.services.exporter import _sanitize_export_filename
from plugins.pendo.services.reminder import ReminderService
from plugins.pendo.utils.time_utils import parse_delay_time, parse_event_time_range
from plugins.pendo.web.analytics.events_overview import _build_reminder_rows

USER     = "10001"
SHANGHAI = ZoneInfo("Asia/Shanghai")
START = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


def _event(db, item_id: str, remind_at: datetime) -> str:
    remind_time = remind_at.isoformat(timespec="seconds")
    return db.insert_item(
        {
            "id"            : item_id,
            "owner_id"      : USER,
            "type"          : "event",
            "title"         : "汇报",
            "timezone"      : "Asia/Shanghai",
            "start_time"    : remind_time,
            "remind_times"  : [remind_time],
            "reminder_rules": [{"offset_seconds": 0}],
        }
    )


def _log(db, item_id: str) -> dict:
    return db.get_reminder_logs(item_id)[0]


def test_reminder_due_while_scheduler_was_down_is_still_delivered(db):
    db.get_due_reminder_items(now=START)
    item_id = _event(db, "down", START + timedelta(minutes=10))

    due = db.get_due_reminder_items(now=START + timedelta(minutes=30))

    assert [item.id for item in due] == [item_id]
    assert _log(db, item_id)["state"] == "pending"


def test_reminder_already_past_when_created_is_not_backfilled(db):
    db.get_due_reminder_items(now=START + timedelta(minutes=29))
    item_id = _event(db, "late", START + timedelta(minutes=10))

    due = db.get_due_reminder_items(now=START + timedelta(minutes=30))

    assert due == []
    assert _log(db, item_id)["user_action"] == "expired"


def test_first_ever_check_keeps_the_short_window(db):
    item_id = _event(db, "first", START + timedelta(minutes=10))

    assert db.get_due_reminder_items(now=START + timedelta(minutes=30)) == []
    assert _log(db, item_id)["user_action"] == "expired"


def test_catch_up_stops_at_the_configured_limit(db):
    db.get_due_reminder_items(now=START)
    item_id = _event(db, "stale", START + timedelta(minutes=10))
    resumed = START + timedelta(seconds=PendoConfig.REMINDER_CATCH_UP_SECONDS + 900)

    assert db.get_due_reminder_items(now=resumed) == []
    assert _log(db, item_id)["user_action"] == "expired"


def test_caught_up_delivery_says_how_late_it_is(db):
    db.get_due_reminder_items(now=START)
    remind_at = START + timedelta(minutes=10)
    item_id = _event(db, "note", remind_at)
    resumed = START + timedelta(minutes=30)
    [item] = db.get_due_reminder_items(now=resumed)
    service = ReminderService(db)

    delivery = service._claim_initial_delivery(
        item,
        item.remind_times[0],
        resumed.astimezone(SHANGHAI),
        _log(db, item_id),
        {"quiet_hours_start": "00:00", "quiet_hours_end": "00:00"},
    )

    assert delivery is not None
    assert "晚了20分钟送达" in delivery["message"]


def test_on_time_delivery_has_no_late_note(db):
    remind_at = START + timedelta(minutes=10)
    item_id = _event(db, "ontime", remind_at)
    [item] = db.get_due_reminder_items(now=remind_at + timedelta(seconds=30))

    delivery = ReminderService(db)._claim_initial_delivery(
        item,
        item.remind_times[0],
        (remind_at + timedelta(seconds=30)).astimezone(SHANGHAI),
        _log(db, item_id),
        {"quiet_hours_start": "00:00", "quiet_hours_end": "00:00"},
    )

    assert delivery is not None
    assert "送达" not in delivery["message"]


def test_missed_reminder_is_not_shown_as_confirmed(db):
    item_id = _event(db, "missed", START + timedelta(minutes=10))
    db.get_due_reminder_items(now=START + timedelta(minutes=30))
    log = db.get_reminder_logs_by_item_ids(USER, [item_id])[item_id][
        (START + timedelta(minutes=10)).isoformat(timespec="seconds")
    ]

    assert get_remind_status(log) == "⌛"
    assert get_remind_status_label(log) == "⌛ 已过期（未发送）"
    assert get_remind_status({"confirmed_at": "x", "user_action": "confirmed"}) == "✅"

    [row] = _build_reminder_rows(db.get_item(item_id, USER), db.get_reminder_logs(item_id))
    assert row["status"] == "confirmed"
    assert row["missed"] is True


@pytest.mark.parametrize(
    "clock,expected",
    [
        ((7, 59), False),
        ((8, 0), True),
        ((8, 1), True),
        ((9, 59), True),
        ((10, 0), False),
    ],
)
def test_schedule_window_covers_a_missed_minute_but_not_the_whole_day(clock, expected):
    current = datetime(2031, 5, 1, *clock, tzinfo=SHANGHAI)

    assert _is_within_schedule_window(current, "08:00") is expected
    assert _is_within_schedule_window(current, "25:00") is False


def test_schedule_window_does_not_wrap_past_midnight():
    assert (
        _is_within_schedule_window(datetime(2031, 5, 2, 0, 30, tzinfo=SHANGHAI), "23:30") is False
    )


@pytest.mark.parametrize(
    "text,delta",
    [
        ("10m", timedelta(minutes=10)),
        ("10min", timedelta(minutes=10)),
        ("30分钟", timedelta(minutes=30)),
        ("2小时", timedelta(hours=2)),
        ("1 h", timedelta(hours=1)),
        ("3天", timedelta(days=3)),
    ],
)
def test_snooze_accepts_documented_duration_spellings(text, delta):
    now = datetime(2031, 5, 1, 9, 0, 0, 123456, tzinfo=SHANGHAI)

    assert parse_delay_time(text, now=now) == (now + delta).isoformat(timespec="seconds")


@pytest.mark.parametrize("text", ["10", "min", "10x", "25:00", "999999d"])
def test_snooze_rejects_malformed_durations(text):
    assert parse_delay_time(text, now=datetime(2031, 5, 1, 9, 0, tzinfo=SHANGHAI)) is None


def test_snooze_receipt_uses_local_readable_time(db):
    remind_at = datetime.now(UTC) + timedelta(hours=3)
    item_id = _event(db, "snooze", remind_at)

    result = asyncio.run(handle_snooze(USER, f"{item_id} 10min", ReminderService(db)))

    assert result["status"] == "success"
    assert "T" not in result["message"] and "+08:00" not in result["message"]
    assert "月" in result["message"]


def test_event_range_accepts_month_day_and_rejects_absurd_spans():
    now = datetime(2031, 3, 1, 9, 0)

    assert parse_event_time_range("05-01", now=now, strict=True) == (
        "2031-05-01T00:00:00",
        "2031-05-01T23:59:59",
    )
    with pytest.raises(ValueError):
        parse_event_time_range("02-30", now=now, strict=True)
    with pytest.raises(ValueError):
        parse_event_time_range("last99999999d", now=now, strict=True)


class _Session:
    plugin_name = "pendo"

    def __init__(self, data: dict) -> None:
        self.data = dict(data)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value) -> None:
        self.data[key] = value


def _ledger_session(step: str) -> _Session:
    return _Session(
        {
            "type": "ledger_add",
            "step": step,
            "data": {
                "amount"          : 10.0,
                "amount_cents"    : 1000,
                "title"           : "午饭",
                "transaction_type": "expense",
                "account_name"    : "现金",
                "ledger_category" : "餐饮",
            },
        }
    )


def _run_ledger_step(db, monkeypatch, session: _Session, text: str) -> tuple[str, list[bool]]:
    ledger            = LedgerHandler(db)
    ended: list[bool] = []

    async def end_session() -> None:
        ended.append(True)

    context = SimpleNamespace(state={}, end_session=end_session, logger=None)
    monkeypatch.setattr(pendo_main, "_get_services", lambda _context: {"ledger_handler": ledger})
    monkeypatch.setattr(
        "plugins.pendo.commands.session.get_cached_services",
        lambda _context: {"ledger_handler": ledger},
    )
    reply = asyncio.run(pendo_main._handle_active_session(USER, text, context, session))
    return reply[0]["data"]["text"], ended


def test_ledger_session_keeps_draft_when_merchant_is_too_long(db, monkeypatch):
    session = _ledger_session("merchant")

    text, ended = _run_ledger_step(db, monkeypatch, session, "商" * 200)

    assert "不能超过 120 个字符" in text
    assert ended == []

    text, ended = _run_ledger_step(db, monkeypatch, session, "食堂")
    assert "记账成功" in text
    assert ended == [True]


def test_ledger_session_keeps_draft_when_saving_is_rejected(db, monkeypatch):
    session                          = _ledger_session("merchant")
    session.data["data"]["currency"] = "人民币"

    text, ended = _run_ledger_step(db, monkeypatch, session, "食堂")

    assert "草稿已保留" in text
    assert ended == []


def test_ledger_session_rejects_overlong_description_at_that_step(db, monkeypatch):
    session = _Session(
        {"type": "ledger_add", "step": "description", "data": {"amount": 1.0, "amount_cents": 100}}
    )

    text, ended = _run_ledger_step(db, monkeypatch, session, "饭" * 201)

    assert "描述不能超过 200 个字符" in text
    assert ended == []
    assert session.get("step") == "description"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("../../evil", "_.._evil.md"),
        ("CON", "_CON.md"),
        ("nul.txt", "_nul.txt.md"),
        ("报告.md", "报告.md"),
        ("x" * 300, "x" * 80 + ".md"),
        ("..", ""),
    ],
)
def test_export_filename_is_bounded_and_avoids_reserved_device_names(raw, expected):
    assert _sanitize_export_filename(raw) == expected
