"""Pendo 时间写入产生侧的 fail-closed 契约。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from plugins.pendo.services import db as db_module
from plugins.pendo.services.db import Database


@pytest.mark.parametrize(
    "value",
    (
        "2030-01-01T09:00:00",
        "2030-01-01T09:00:00+08:00",
        "2030-01-01T01:00:00Z",
        "2030-01-01T01:00:00.123456+00:00",
    ),
)
def test_sql_serializers_reject_noncanonical_timestamp_forms(
    db: Database,
    value: str,
) -> None:
    with pytest.raises(ValueError, match="canonical|UTC"):
        db._prepare_data({"start_time": value})
    with pytest.raises(ValueError, match="canonical|UTC"):
        db._prepare_event_collection_data({"start_time": value})

    assert db._prepare_data({"start_time": "2030-01-01T01:00:00+00:00"}) == {
        "start_time": "2030-01-01T01:00:00+00:00"
    }


def test_item_producer_guard_catches_a_skipped_normalizer_and_rolls_back(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.insert_item(
        {
            "id": "guarded-event",
            "owner_id": "guard-owner",
            "type": "event",
            "title": "原始时间",
            "start_time": "2030-01-01T09:00:00",
        }
    )
    original_start = db.get_item("guarded-event", "guard-owner").start_time
    monkeypatch.setattr(
        db_module,
        "normalize_item_datetimes_for_storage",
        lambda payload, _timezone: dict(payload),
    )

    with pytest.raises(ValueError, match="start_time must use UTC"):
        db.update_item(
            "guarded-event",
            {"start_time": "2030-01-02T09:00:00"},
            owner_id="guard-owner",
        )

    db.cache_clear()
    assert db.get_item("guarded-event", "guard-owner").start_time == original_start


def test_collection_producer_guards_reject_headers_and_children_before_commit(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        db_module,
        "normalize_event_collection_datetimes_for_storage",
        lambda payload, _timezone: dict(payload),
    )

    with pytest.raises(ValueError, match="start_time must use UTC"):
        db.create_event_collection(
            {
                "id": "guarded-header",
                "owner_id": "guard-owner",
                "kind": "multi_node",
                "title": "集合头",
                "start_time": "2030-01-01T09:00:00",
            }
        )
    assert db.get_event_collection("guarded-header", "guard-owner") is None

    monkeypatch.setattr(
        db_module,
        "normalize_event_collection_datetimes_for_storage",
        lambda payload, _timezone: {
            **dict(payload),
            "created_at": "2030-01-01T00:00:00+00:00",
            "updated_at": "2030-01-01T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        db_module,
        "normalize_item_datetimes_for_storage",
        lambda payload, _timezone: dict(payload),
    )
    child: dict[str, Any] = {
        "title": "节点",
        "start_time": "2030-01-01T09:00:00",
    }
    with pytest.raises(ValueError, match="start_time must use UTC"):
        db.create_event_collection_with_children(
            {
                "id": "guarded-family",
                "owner_id": "guard-owner",
                "kind": "multi_node",
                "title": "整组",
            },
            [("guarded-family_m01", child)],
        )

    assert db.get_event_collection("guarded-family", "guard-owner") is None
    assert db.get_item("guarded-family_m01", "guard-owner") is None


def test_direct_event_reminder_update_rejects_a_skipped_normalizer(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.create_event_collection_with_children(
        {
            "id": "guarded-update-family",
            "owner_id": "guard-owner",
            "kind": "multi_node",
            "title": "整组",
            "timezone": "Asia/Shanghai",
        },
        [
            (
                "guarded-update-family_m01",
                {
                    "title": "节点",
                    "start_time": "2030-01-01T09:00:00",
                    "remind_times": ["2030-01-01T08:00:00"],
                },
            )
        ],
    )
    original = db.get_item("guarded-update-family_m01", "guard-owner").remind_times
    monkeypatch.setattr(
        db_module,
        "normalize_item_datetimes_for_storage",
        lambda payload, _timezone: dict(payload),
    )

    with pytest.raises(ValueError, match=r"remind_times\[0\] must use UTC"):
        db.update_event_collection_reminders(
            "guarded-update-family",
            "guard-owner",
            {"guarded-update-family_m01": (["2030-01-02T08:00:00"], [])},
            None,
        )

    db.cache_clear()
    assert db.get_item("guarded-update-family_m01", "guard-owner").remind_times == original


def test_reminder_log_public_producers_reject_noncanonical_keys(db: Database) -> None:
    operations = (
        lambda: db.log_reminder("guard-item", "2030-01-01T09:00:00"),
        lambda: db.claim_reminder("guard-item", "2030-01-01T09:00:00"),
        lambda: db.claim_reminder_repeat("guard-item", "2030-01-01T09:00:00", 1),
        lambda: db.complete_reminder_repeat("guard-item", "2030-01-01T09:00:00", "token", 1),
        lambda: db.release_reminder_repeat("guard-item", "2030-01-01T09:00:00", "token", 1),
        lambda: db.complete_reminder_claim("guard-item", "2030-01-01T09:00:00", "token"),
        lambda: db.release_reminder_claim("guard-item", "2030-01-01T09:00:00", "token"),
        lambda: db.confirm_reminder(
            "guard-item",
            remind_time="2030-01-01T09:00:00",
        ),
    )

    for operation in operations:
        with pytest.raises(ValueError, match="remind_time must use UTC"):
            operation()

    assert db.get_reminder_logs("guard-item") == []


def test_undo_normalizes_legacy_wall_time_snapshot_before_writing(db: Database) -> None:
    owner_id = "guard-owner"
    item_id = "guarded-undo-event"
    db.insert_item(
        {
            "id": item_id,
            "owner_id": owner_id,
            "type": "event",
            "title": "当前值",
            "timezone": "America/Los_Angeles",
            "start_time": "2030-01-02T09:00:00",
        }
    )
    db.log_operation(
        owner_id,
        "edit_event",
        item_type="event",
        item_id=item_id,
        details={"old_values": {"start_time": "2030-01-01T09:00:00"}},
    )

    assert db.undo_edit(owner_id)["status"] == "success"
    db.cache_clear()
    assert db.get_item(item_id, owner_id).start_time == "2030-01-01T17:00:00+00:00"


def test_unsent_confirmation_normalizes_a_legacy_reminder_key(db: Database) -> None:
    owner_id = "guard-owner"
    item_id = "guarded-confirm-event"
    db.insert_item(
        {
            "id": item_id,
            "owner_id": owner_id,
            "type": "event",
            "title": "旧提醒",
            "timezone": "America/Los_Angeles",
            "start_time": "2020-01-02T09:00:00",
            "remind_times": ["2020-01-01T09:00:00"],
        }
    )
    connection = db.get_connection()
    with connection:
        connection.execute("DELETE FROM reminder_logs WHERE item_id = ?", (item_id,))
        connection.execute(
            "UPDATE items SET remind_times = ? WHERE id = ?",
            ('["2020-01-01T09:00:00"]', item_id),
        )

    assert db.confirm_reminder(item_id, owner_id=owner_id)["status"] == "success"
    assert db.get_reminder_logs(item_id)[0]["remind_time"] == "2020-01-01T17:00:00+00:00"


def test_reminder_and_outbox_lifecycle_timestamps_use_canonical_precision(db: Database) -> None:
    owner_id = "guard-owner"
    item_id = "guarded-lifecycle-event"
    db.insert_item(
        {
            "id": item_id,
            "owner_id": owner_id,
            "type": "event",
            "title": "租约精度",
            "start_time": "2030-01-01T10:00:00",
            "remind_times": ["2030-01-01T09:00:00"],
        }
    )
    remind_time = db.get_item(item_id, owner_id).remind_times[0]
    now = datetime(2030, 1, 1, 0, 0, 0, 123456, tzinfo=timezone.utc)

    assert db.claim_reminder(item_id, remind_time, now=now, lease_seconds=30)
    reminder_row = (
        db.get_connection()
        .execute(
            "SELECT claim_expires_at FROM reminder_logs WHERE item_id = ? AND remind_time = ?",
            (item_id, remind_time),
        )
        .fetchone()
    )
    assert reminder_row["claim_expires_at"] == "2030-01-01T00:00:30+00:00"

    assert db.claim_scheduled_delivery("daily", owner_id, "2030-01-01", now=now)
    outbox = db.get_scheduled_delivery("daily", owner_id, "2030-01-01")
    assert outbox is not None
    assert outbox["created_at"] == "2030-01-01T00:00:00+00:00"
    assert outbox["claim_expires_at"] == "2030-01-01T00:02:00+00:00"
