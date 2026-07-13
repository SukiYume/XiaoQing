import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from plugins.pendo.services import db as db_module
from plugins.pendo.services.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "pendo.db"))
    try:
        yield database
    finally:
        database.cleanup()


def test_reminder_claim_is_atomic_and_recovers_after_lease(db):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)

    first = db.claim_reminder("event-1", "2030-01-01T00:00:00+00:00", now=now, lease_seconds=30)
    assert first
    assert db.claim_reminder("event-1", "2030-01-01T00:00:00+00:00", now=now) is None

    recovered = db.claim_reminder(
        "event-1",
        "2030-01-01T00:00:00+00:00",
        now=now + timedelta(seconds=31),
    )
    assert recovered and recovered != first
    assert db.complete_reminder_claim("event-1", "2030-01-01T00:00:00+00:00", recovered)
    assert (
        db.claim_reminder("event-1", "2030-01-01T00:00:00+00:00", now=now + timedelta(hours=1))
        is None
    )


def test_reminder_release_respects_next_attempt_time(db):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    token = db.claim_reminder("event-2", "2030-01-01T00:00:00+00:00", now=now)
    assert token
    assert db.release_reminder_claim(
        "event-2", "2030-01-01T00:00:00+00:00", token, retry_at=now + timedelta(minutes=10)
    )
    assert (
        db.claim_reminder("event-2", "2030-01-01T00:00:00+00:00", now=now + timedelta(minutes=9))
        is None
    )
    assert db.claim_reminder(
        "event-2", "2030-01-01T00:00:00+00:00", now=now + timedelta(minutes=10)
    )


def test_database_cleanup_closes_connections_created_by_worker_threads(db):
    worker = threading.Thread(target=lambda: db.get_connection())
    worker.start()
    worker.join()

    db.close_all_connections()

    assert db._all_connections == {}


def test_connection_registry_preserves_slots_when_worker_thread_ids_are_reused(db, monkeypatch):
    baseline_slots = len(db._all_connections)
    monkeypatch.setattr(db_module, "threading", SimpleNamespace(get_ident=lambda: 4242))
    errors: list[BaseException] = []

    def connect() -> None:
        try:
            db.get_connection().execute("SELECT 1").fetchone()
        except BaseException as exc:  # pragma: no cover - reported by assertion
            errors.append(exc)

    for _index in range(3):
        worker = threading.Thread(target=connect)
        worker.start()
        worker.join()

    assert errors == []
    assert len(db._all_connections) == baseline_slots + 3
    worker_thread_ids = [thread_id for thread_id, _conn in db._all_connections.values()]
    assert worker_thread_ids.count(4242) == 3
    worker_connections = [
        connection for thread_id, connection in db._all_connections.values() if thread_id == 4242
    ]

    db.cleanup()

    assert db._all_connections == {}
    for connection in worker_connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_item_update_uses_owner_type_and_version_compare_and_swap(db):
    item_id = db.insert_item({"type": "note", "owner_id": "u1", "title": "before"})

    assert db.update_item(
        item_id,
        {"title": "after"},
        owner_id="u1",
        item_type="note",
        expected_version=0,
    )
    assert not db.update_item(
        item_id,
        {"title": "stale"},
        owner_id="u1",
        item_type="note",
        expected_version=0,
    )
    assert not db.update_item(
        item_id,
        {"title": "wrong type"},
        owner_id="u1",
        item_type="task",
        expected_version=1,
    )
    assert db.get_item(item_id, "u1").title == "after"


def test_operation_log_retention_redacts_snapshots_then_deletes_expired_rows(db):
    old = datetime(2030, 1, 1, tzinfo=timezone.utc)
    db.log_operation("u1", "edit_note", details={"old_values": {"content": "secret"}})
    conn = db.get_connection()
    conn.execute(
        "UPDATE operation_logs SET created_at = ?", ((old - timedelta(minutes=10)).isoformat(),)
    )
    conn.commit()

    result = db.prune_operation_logs(now=old, retention_days=90, undo_snapshot_minutes=5)
    row = conn.execute("SELECT details FROM operation_logs").fetchone()
    assert result == {"deleted": 0, "redacted": 1}
    assert "secret" not in row["details"]

    result = db.prune_operation_logs(now=old + timedelta(days=91), retention_days=90)
    assert result["deleted"] == 1


def test_concurrent_user_setting_updates_preserve_disjoint_json_keys(db):
    start = threading.Barrier(2)
    results: list[bool] = []

    def write(key: str):
        start.wait()
        results.append(db.update_user_settings("u1", {"settings_json": {key: True}}))

    left = threading.Thread(target=write, args=("feature_a",))
    right = threading.Thread(target=write, args=("feature_b",))
    left.start()
    right.start()
    left.join()
    right.join()

    assert results == [True, True]

    settings = db.get_user_settings("u1")
    assert settings["settings_json"].get("feature_a") is True, settings
    assert settings["settings_json"].get("feature_b") is True, settings
    assert settings["version"] == 1


def test_get_all_items_iterates_past_legacy_large_query_limits(db):
    for index in range(1, 1_501):
        db.insert_item({"type": "note", "owner_id": "u1", "title": f"note {index}"})

    items = db.get_all_items("u1", {"type": "note"}, page_size=137)

    assert len(items) == 1_500
