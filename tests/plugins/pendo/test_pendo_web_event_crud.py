"""Pendo Web 日程校验和提醒。"""

from __future__ import annotations

from tests.helpers.pendo_web_items_test_support import (
    ROOT,
    Database,
    items_api,
    normalize_event_fields,
    pytest,
    shutil,
    uuid,
)


def test_event_validation_rejects_invalid_merged_update_values():
    existing = normalize_event_fields(
        {
            "title": "好事件",
            "category": "会议",
            "start_time": "2026-03-26T10:00:00",
            "end_time": "2026-03-26T11:00:00",
            "remind_times": ["2026-03-26T09:00:00"],
        },
        partial=False,
    )

    merged = dict(existing)
    merged.update({"end_time": "2026-03-26T08:00:00"})

    with pytest.raises(ValueError, match="end_time"):
        normalize_event_fields(merged, partial=False)


def test_item_create_event_accepts_reminder_rules_and_builds_cache():
    temp_dir = (
        ROOT / ".pytest_cache" / "tmp" / f"pendo_event_reminder_rules_create_{uuid.uuid4().hex}"
    )
    temp_dir.mkdir(parents=True, exist_ok=True)
    db           = Database(str(temp_dir / "pendo.db"))
    owner_id     = "u-event-rules-create"
    items_module = items_api

    try:
        body = items_module.ItemCreate(
            type           = "event",
            title          = "规则提醒",
            category       = "会议",
            start_time     = "2030-01-02T09:00:00",
            reminder_rules = [{"offset_seconds": 3600}, {"offset_seconds": 0}],
        )

        result = items_module.create_item(body=body, owner_id=owner_id, db=db)

        assert result["ok"] is True
        event = db.get_item(result["data"]["id"], owner_id=owner_id)
        assert event.reminder_rules == [
            {"offset_seconds": 3600},
            {"offset_seconds": 0},
        ]
        assert event.remind_times == [
            "2030-01-02T00:00:00+00:00",
            "2030-01-02T01:00:00+00:00",
        ]
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_item_update_event_explicit_times_replace_existing_rules():
    temp_dir = (
        ROOT / ".pytest_cache" / "tmp" / f"pendo_event_reminder_rules_update_{uuid.uuid4().hex}"
    )
    temp_dir.mkdir(parents=True, exist_ok=True)
    db           = Database(str(temp_dir / "pendo.db"))
    owner_id     = "u-event-rules-update"
    items_module = items_api

    try:
        db.insert_item(
            normalize_event_fields(
                {
                    "id": "ev-rules",
                    "owner_id": owner_id,
                    "type": "event",
                    "title": "移动时间",
                    "category": "会议",
                    "start_time": "2030-01-02T09:00:00",
                    "reminder_rules": [{"offset_seconds": 3600}, {"offset_seconds": 0}],
                },
                partial=False,
            )
        )

        body = items_module.ItemUpdate(
            start_time   = "2030-01-03T10:00:00",
            remind_times = ["2030-01-02T08:00:00", "2030-01-02T09:00:00"],
        )

        result = items_module.update_item("ev-rules", body=body, owner_id=owner_id, db=db)

        assert result["ok"] is True
        event = db.get_item("ev-rules", owner_id=owner_id)
        assert event.reminder_rules == [
            {"offset_seconds": 93600},
            {"offset_seconds": 90000},
            {"offset_seconds": 0},
        ]
        assert event.remind_times == [
            "2030-01-02T00:00:00+00:00",
            "2030-01-02T01:00:00+00:00",
            "2030-01-03T02:00:00+00:00",
        ]
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_item_update_event_start_time_preserves_duration_when_end_time_omitted():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_event_duration_update_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db           = Database(str(temp_dir / "pendo.db"))
    owner_id     = "u-event-duration-update"
    items_module = items_api

    try:
        db.insert_item(
            normalize_event_fields(
                {
                    "id": "ev-duration",
                    "owner_id": owner_id,
                    "type": "event",
                    "title": "移动含结束时间事件",
                    "category": "会议",
                    "start_time": "2030-01-02T09:00:00",
                    "end_time": "2030-01-02T11:30:00",
                    "reminder_rules": [{"offset_seconds": 3600}, {"offset_seconds": 0}],
                },
                partial=False,
            )
        )

        body = items_module.ItemUpdate(start_time="2030-01-03T10:00:00")

        result = items_module.update_item("ev-duration", body=body, owner_id=owner_id, db=db)

        assert result["ok"] is True
        event = db.get_item("ev-duration", owner_id=owner_id)
        assert event.start_time == "2030-01-03T02:00:00+00:00"
        assert event.end_time == "2030-01-03T04:30:00+00:00"
        assert event.remind_times == [
            "2030-01-03T01:00:00+00:00",
            "2030-01-03T02:00:00+00:00",
        ]
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_event_reminder_log_sync_preserves_sent_history_but_excludes_removed_reminders_from_repeat_queue():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_event_reminder_sync_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db       = Database(str(temp_dir / "pendo.db"))
    owner_id = "u-event-sync"

    try:
        db.insert_item(
            {
                "id": "ev1",
                "owner_id": owner_id,
                "type": "event",
                "title": "提醒同步",
                "category": "会议",
                "start_time": "2026-03-26T10:00:00",
                "end_time": "2026-03-26T11:00:00",
                "remind_times": ["2026-03-26T09:00:00", "2026-03-26T09:30:00"],
            }
        )
        db.log_reminder("ev1", "2026-03-26T01:00:00+00:00", sent=True)
        db.log_reminder("ev1", "2026-03-26T01:30:00+00:00", sent=True)

        db.update_item("ev1", {"remind_times": ["2026-03-26T09:30:00"]}, owner_id=owner_id)

        logs   = db.get_reminder_logs("ev1")
        queued = db.get_unconfirmed_sent_reminders()
        assert [row["remind_time"] for row in logs] == [
            "2026-03-26T01:00:00+00:00",
            "2026-03-26T01:30:00+00:00",
        ]
        assert [row["remind_time"] for row in queued] == ["2026-03-26T01:30:00+00:00"]

        assert db.delete_item("ev1", soft=True, owner_id=owner_id) is True
        assert len(db.get_reminder_logs("ev1")) == 2
        assert db.get_unconfirmed_sent_reminders() == []
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)
