import asyncio
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from plugins.pendo.config import PendoConfig
from plugins.pendo.models.item import TaskStatus
from plugins.pendo.services import db as db_module
from plugins.pendo.services.db import Database
from plugins.pendo.services.reminder import ReminderService


def test_initial_reminder_outside_check_window_is_not_backfilled():
    current = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)

    class _Db:
        def claim_reminder(self, *_args, **_kwargs):
            raise AssertionError("expired reminder must not be claimed")

    service = ReminderService(_Db())
    service._parse_user_time = lambda *_args: (
        current - timedelta(seconds=PendoConfig.REMINDER_CHECK_WINDOW_SECONDS + 1)
    )
    item = SimpleNamespace(id="old-event", owner_id="1001")

    delivery = service._claim_initial_delivery(
        item,
        "2017-02-27T12:00:00+00:00",
        current,
        log=None,
        settings={},
    )

    assert delivery is None


def test_failed_initial_reminder_can_retry_after_normal_check_window():
    current = datetime(2030, 1, 1, 12, 5, tzinfo=timezone.utc)

    class _Db:
        def claim_reminder(self, *_args, **_kwargs):
            return "retry-claim"

    service = ReminderService(_Db())
    service._parse_user_time = lambda *_args: current - timedelta(minutes=5)
    service._should_suppress = lambda *_args, **_kwargs: False
    service._build_reminder_message = lambda *_args, **_kwargs: "retry"
    item = SimpleNamespace(id="recent-failure", owner_id="1001", context=None)

    delivery = service._claim_initial_delivery(
        item,
        "2030-01-01T12:00:00+00:00",
        current,
        log={"failure_count": 1, "confirmed_at": None},
        settings={},
    )

    assert delivery is not None
    assert delivery["claim_token"] == "retry-claim"


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


def test_database_update_does_not_mutate_caller_mapping(db):
    item_id = db.insert_item(
        {
            "type": "note",
            "owner_id": "u-input",
            "title": "before",
            "content": "before",
        }
    )
    single_update = {"title": "after"}

    assert db.update_item(item_id, single_update, owner_id="u-input")

    assert single_update == {"title": "after"}


def test_owner_mismatch_cannot_delete_item_auxiliary_rows(db):
    item_id = db.insert_item(
        {
            "id": "owner-isolation-event",
            "type": "event",
            "owner_id": "u-owner",
            "title": "所有者隔离测试",
            "start_time": "2030-01-01T10:00:00+00:00",
            "remind_times": ["2030-01-01T09:00:00+00:00"],
        }
    )
    db.log_reminder(item_id, "2030-01-01T09:00:00+00:00")

    assert not db.delete_item(item_id, owner_id="u-other")
    assert db.batch_soft_delete([item_id], "u-other") == 0

    assert db.get_item(item_id, "u-owner") is not None
    assert len(db.get_reminder_logs(item_id)) == 1
    assert [item.id for item in db.search_items("u-owner", "所有者隔离测试")] == [item_id]


def test_row_decoder_restores_declared_json_containers_and_task_status(db):
    item_id = db.insert_item(
        {
            "id": "container-shape-task",
            "type": "task",
            "owner_id": "u-shape",
            "title": "容器类型",
            "status": "open",
            "context": {},
            "attachments": [],
            "ai_meta": {},
        }
    )
    conn = db.get_connection()
    conn.execute(
        "UPDATE items SET context = '[]', attachments = '{}', ai_meta = '[]' WHERE id = ?",
        (item_id,),
    )
    conn.commit()
    db.cache_clear()

    task = db.get_item(item_id, "u-shape")

    assert task is not None
    assert task.context == {}
    assert task.attachments == []
    assert task.ai_meta == {}
    assert task.status is TaskStatus.OPEN


def test_get_items_by_ids_batches_above_sqlite_parameter_limit(db):
    item_ids = [f"bulk-note-{index:04d}" for index in range(501)]
    now = datetime.now(timezone.utc).isoformat()
    conn = db.get_connection()
    conn.executemany(
        """
        INSERT INTO items (id, type, title, created_at, updated_at, owner_id)
        VALUES (?, 'note', ?, ?, ?, 'u-bulk')
        """,
        [(item_id, item_id, now, now) for item_id in item_ids],
    )
    conn.commit()

    loaded = db.get_items_by_ids("u-bulk", item_ids)

    assert len(loaded) == len(item_ids)
    assert set(loaded) == set(item_ids)


def test_literal_search_and_tag_filters_do_not_expand_like_wildcards(db):
    records = (
        ("literal-percent", "进度 50%", ["percent"]),
        ("plain-percent", "进度 500", ["500"]),
        ("literal-underscore", "under_score", ["under_score"]),
        ("plain-underscore", "underXscore", ["underXscore"]),
    )
    for item_id, title, tags in records:
        db.insert_item(
            {
                "id": item_id,
                "type": "note",
                "owner_id": "u-like-literal",
                "title": title,
                "tags": tags,
            }
        )

    assert [item.id for item in db.search_items("u-like-literal", "%")] == ["literal-percent"]
    assert [
        item.id for item in db.get_items("u-like-literal", filters={"keyword": "under_score"})
    ] == ["literal-underscore"]
    assert [
        item.id for item in db.get_items("u-like-literal", filters={"tags": "under_score"})
    ] == ["literal-underscore"]


def test_batch_soft_delete_logs_and_cleans_only_actual_matches(db):
    for item_id in ("matched-a", "matched-b"):
        db.insert_item(
            {
                "id": item_id,
                "type": "note",
                "owner_id": "u-batch-delete",
                "title": item_id,
            }
        )
    db.insert_item(
        {
            "id": "wrong-type",
            "type": "task",
            "owner_id": "u-batch-delete",
            "title": "不应删除",
        }
    )
    db.log_reminder("matched-a", "2030-01-01T00:00:00+00:00")

    affected = db.batch_soft_delete(
        ["matched-a", "missing", "wrong-type", "matched-b", "matched-a"],
        "u-batch-delete",
        item_type="note",
        operation_action="delete_note",
    )

    assert affected == 2
    assert db.get_item("matched-a", "u-batch-delete") is None
    assert db.get_item("matched-b", "u-batch-delete") is None
    assert db.get_item("wrong-type", "u-batch-delete") is not None
    conn = db.get_connection()
    assert (
        conn.execute("SELECT COUNT(*) FROM reminder_logs WHERE item_id = 'matched-a'").fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM items_fts WHERE id IN ('matched-a', 'matched-b')"
        ).fetchone()[0]
        == 0
    )
    logs = conn.execute(
        "SELECT item_id, created_at FROM operation_logs ORDER BY item_id"
    ).fetchall()
    assert [row["item_id"] for row in logs] == ["matched-a", "matched-b"]
    assert len({row["created_at"] for row in logs}) == 1


def test_user_settings_hydration_accepts_dict_payload(db):
    hydrated = db._hydrate_user_settings_row(
        {
            "user_id": "u-settings-dict",
            "settings_json": {"reminder_enabled": False, "custom_flag": True},
        }
    )

    assert hydrated["settings_json"]["reminder_enabled"] is False
    assert hydrated["settings_json"]["custom_flag"] is True


def test_far_future_event_with_due_reminder_is_not_dropped(db):
    db.insert_item(
        {
            "id": "far-future-reminder",
            "type": "event",
            "owner_id": "u-far-future",
            "title": "远期日程",
            "start_time": "2099-01-01T09:00:00+00:00",
            "remind_times": ["2020-01-01T09:00:00+00:00"],
        }
    )

    assert [item.id for item in db.get_all_events_with_reminders(owner_id="u-far-future")] == [
        "far-future-reminder"
    ]


def test_latest_confirmable_reminder_uses_absolute_time_order():
    remind_times = [
        "2030-01-01T09:00:00+08:00",
        "2030-01-01T00:30:00-08:00",
    ]

    selected = Database._latest_confirmable_time(
        remind_times,
        requested_time=None,
        allow_future=False,
        now=datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc),
        timezone_info=ZoneInfo("UTC"),
    )

    assert selected == "2030-01-01T00:30:00-08:00"


def test_confirm_unsent_reminder_creates_confirmed_state(db):
    item_id = db.insert_item(
        {
            "id": "unsent-confirmation",
            "type": "event",
            "owner_id": "u-unsent-confirmation",
            "title": "静默时段提醒",
            "remind_times": ["2020-01-01T09:00:00+00:00"],
        }
    )

    result = db.confirm_reminder(
        item_id,
        user_action="done",
        owner_id="u-unsent-confirmation",
    )

    assert result["status"] == "success"
    row = (
        db.get_connection()
        .execute(
            "SELECT sent_at, confirmed_at, user_action, state FROM reminder_logs WHERE item_id = ?",
            (item_id,),
        )
        .fetchone()
    )
    assert row["sent_at"] is None
    assert row["confirmed_at"] is not None
    assert row["user_action"] == "done"
    assert row["state"] == "confirmed"


def test_scheduled_delivery_validates_time_lease_and_normalizes_identity(db):
    aware_now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="timezone-aware"):
        db.claim_scheduled_delivery("briefing", "u1", "2030-01-01", now=datetime(2030, 1, 1))
    with pytest.raises(ValueError, match="lease must be positive"):
        db.claim_scheduled_delivery("briefing", "u1", "2030-01-01", now=aware_now, lease_seconds=0)

    claim = db.claim_scheduled_delivery("  briefing  ", "  u1  ", "  2030-01-01  ", now=aware_now)

    assert claim is not None
    assert db.complete_scheduled_delivery(
        "briefing", "u1", "2030-01-01", claim["claim_token"], now=aware_now
    )
    assert db.get_scheduled_delivery(" briefing ", "u1", "2030-01-01")["state"] == "sent"


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


def test_repeat_release_respects_next_attempt_time(db):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    item_id = db.insert_item(
        {
            "type": "event",
            "owner_id": "1001",
            "title": "repeat retry",
            "start_time": now.isoformat(),
            "remind_times": [now.isoformat()],
        }
    )
    db.log_reminder(item_id, now.isoformat())
    token = db.claim_reminder_repeat(item_id, now.isoformat(), 1, now=now)
    assert token
    assert db.release_reminder_repeat(
        item_id,
        now.isoformat(),
        token,
        1,
        retry_at=now + timedelta(minutes=10),
    )
    assert (
        db.claim_reminder_repeat(
            item_id,
            now.isoformat(),
            1,
            now=now + timedelta(minutes=9),
        )
        is None
    )
    assert db.claim_reminder_repeat(
        item_id,
        now.isoformat(),
        1,
        now=now + timedelta(minutes=10),
    )


def test_scheduled_delivery_outbox_is_cross_instance_atomic_and_recoverable(tmp_path):
    path = str(tmp_path / "shared.db")
    first_db = Database(path)
    second_db = Database(path)
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    start = threading.Barrier(2)
    claims: list[dict[str, str] | None] = []

    def claim(database: Database) -> None:
        start.wait()
        claims.append(
            database.claim_scheduled_delivery(
                "daily_briefing",
                "1001",
                "2030-01-01",
                now=now,
                lease_seconds=30,
            )
        )

    left = threading.Thread(target=claim, args=(first_db,))
    right = threading.Thread(target=claim, args=(second_db,))
    left.start()
    right.start()
    left.join()
    right.join()

    winner = next(value for value in claims if value is not None)
    assert sum(value is not None for value in claims) == 1
    assert winner["delivery_key"] == Database.scheduled_delivery_key(
        "daily_briefing", "1001", "2030-01-01"
    )
    assert first_db.release_scheduled_delivery(
        "daily_briefing", "1001", "2030-01-01", winner["claim_token"], now=now
    )

    recovered = second_db.claim_scheduled_delivery("daily_briefing", "1001", "2030-01-01", now=now)
    assert recovered is not None
    assert recovered["delivery_key"] == winner["delivery_key"]
    assert second_db.complete_scheduled_delivery(
        "daily_briefing", "1001", "2030-01-01", recovered["claim_token"], now=now
    )
    assert (
        first_db.claim_scheduled_delivery(
            "daily_briefing", "1001", "2030-01-01", now=now + timedelta(days=1)
        )
        is None
    )
    row = first_db.get_scheduled_delivery("daily_briefing", "1001", "2030-01-01")
    assert row is not None
    assert row["state"] == "sent"
    assert row["failure_count"] == 1

    first_db.cleanup()
    second_db.cleanup()


def test_scheduled_delivery_reclaims_expired_lease_with_same_key(db):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    first = db.claim_scheduled_delivery(
        "weekly_finance_summary", "1001", "2030-W01", now=now, lease_seconds=30
    )
    assert first is not None
    assert (
        db.claim_scheduled_delivery(
            "weekly_finance_summary",
            "1001",
            "2030-W01",
            now=now + timedelta(seconds=29),
        )
        is None
    )
    recovered = db.claim_scheduled_delivery(
        "weekly_finance_summary",
        "1001",
        "2030-W01",
        now=now + timedelta(seconds=31),
    )
    assert recovered is not None
    assert recovered["delivery_key"] == first["delivery_key"]


def test_unconfirmed_repeat_is_leased_once_across_database_instances(tmp_path):
    path = str(tmp_path / "repeat.db")
    first_db = Database(path)
    second_db = Database(path)
    remind_time = "2030-01-01T12:00:00+00:00"
    item_id = first_db.insert_item(
        {
            "type": "event",
            "owner_id": "1001",
            "title": "leased repeat",
            "start_time": remind_time,
            "remind_times": [remind_time],
        }
    )
    first_db.log_reminder(item_id, remind_time)
    current = datetime(2030, 1, 1, 13, 0, tzinfo=timezone.utc)
    old = (current - timedelta(minutes=10)).isoformat()
    first_db.get_connection().execute(
        "UPDATE reminder_logs SET sent_at = ?, last_sent_at = ?, repeat_count = 1, state = 'sent' "
        "WHERE item_id = ? AND remind_time = ?",
        (old, old, item_id, remind_time),
    )
    first_db.get_connection().commit()

    start = threading.Barrier(2)
    results: list[list[dict[str, object]]] = []

    def check(database: Database) -> None:
        start.wait()
        results.append(ReminderService(database)._check_unconfirmed_repeats(current))

    left = threading.Thread(target=check, args=(first_db,))
    right = threading.Thread(target=check, args=(second_db,))
    left.start()
    right.start()
    left.join()
    right.join()

    messages = [message for batch in results for message in batch]
    assert len(messages) == 1
    message = messages[0]
    assert message["claim_kind"] == "repeat"
    assert message["claim_token"]
    assert message["delivery_key"].startswith("pendo-reminder-")
    assert first_db.complete_reminder_repeat(
        item_id,
        remind_time,
        str(message["claim_token"]),
        1,
    )
    log = first_db.get_reminder_logs(item_id)[0]
    assert log["repeat_count"] == 2

    first_db.cleanup()
    second_db.cleanup()


def test_daily_briefing_outbox_prevents_duplicate_after_marker_failure(tmp_path, monkeypatch):
    from plugins.pendo.commands import scheduled as scheduled_module

    path = str(tmp_path / "briefing.db")
    first_db = Database(path)
    second_db = Database(path)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2030, 1, 1, tzinfo=timezone.utc)
            return value if tz is not None else value.replace(tzinfo=None)

    async def active_users(_db):
        return ["1001"]

    async def settings(user_ids, _db):
        return {
            user_ids[0]: {
                "settings": {"timezone": "Asia/Shanghai", "daily_report_time": "08:00"},
                "custom_settings": {"daily_briefing_enabled": True},
            }
        }

    generated: list[str] = []

    async def generate(user_id, _db):
        generated.append(user_id)
        return "daily content"

    def fail_marker(*_args, **_kwargs):
        raise RuntimeError("injected marker failure")

    sent: list[dict[str, object]] = []

    class Context:
        async def send_action(self, action):
            sent.append(action)
            return True

    monkeypatch.setattr(scheduled_module, "datetime", _FixedDateTime)
    monkeypatch.setattr(scheduled_module, "_get_active_user_ids", active_users)
    monkeypatch.setattr(scheduled_module, "get_user_settings_bundle_map", settings)
    monkeypatch.setattr(scheduled_module, "_generate_briefing_content", generate)
    monkeypatch.setattr(scheduled_module, "save_user_setting", fail_marker)

    async def run_both():
        await asyncio.gather(
            scheduled_module.send_daily_briefings(Context(), first_db),
            scheduled_module.send_daily_briefings(Context(), second_db),
        )
        await scheduled_module.send_daily_briefings(Context(), second_db)

    asyncio.run(run_both())

    assert len(sent) == 1
    assert len(generated) == 1
    assert sent[0]["echo"] == Database.scheduled_delivery_key(
        "daily_briefing", "1001", "2030-01-01"
    )
    row = first_db.get_scheduled_delivery("daily_briefing", "1001", "2030-01-01")
    assert row is not None and row["state"] == "sent"

    first_db.cleanup()
    second_db.cleanup()


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


def test_daily_log_prune_preserves_snapshots_inside_active_undo_window(db):
    cleanup_time = datetime(2030, 1, 2, 0, 15, tzinfo=timezone.utc)
    db.log_operation("u1", "edit_note", details={"old_values": {"content": "fresh"}})
    db.log_operation("u1", "edit_note", details={"old_values": {"content": "expired"}})
    conn = db.get_connection()
    rows = conn.execute("SELECT id FROM operation_logs ORDER BY id").fetchall()
    conn.execute(
        "UPDATE operation_logs SET created_at = ? WHERE id = ?",
        ((cleanup_time - timedelta(minutes=4)).isoformat(), rows[0]["id"]),
    )
    conn.execute(
        "UPDATE operation_logs SET created_at = ? WHERE id = ?",
        ((cleanup_time - timedelta(minutes=6)).isoformat(), rows[1]["id"]),
    )
    conn.commit()

    result = db.prune_operation_logs(now=cleanup_time, retention_days=90)
    details = [
        row["details"]
        for row in conn.execute("SELECT details FROM operation_logs ORDER BY id").fetchall()
    ]

    assert result == {"deleted": 0, "redacted": 1}
    assert "fresh" in details[0]
    assert "expired" not in details[1]


@pytest.mark.parametrize("minutes", [0, 6, True])
def test_database_rejects_undo_windows_outside_the_shared_contract(db, minutes):
    with pytest.raises(
        ValueError,
        match=rf"undo window must be from 1 to {PendoConfig.UNDO_WINDOW_MINUTES} minutes",
    ):
        db.get_latest_undoable_operation("u1", minutes)


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


def test_get_all_items_bypasses_shared_page_cache(db):
    db.insert_item({"type": "note", "owner_id": "u-cache", "title": "cached note"})

    db.cache_clear()
    assert len(db.get_all_items("u-cache", {"type": "note"}, page_size=1)) == 1
    assert not db._cache

    assert db.get_items("u-cache", {"type": "note"}, use_cache=True)
    assert db._cache
    db.cache_clear()
    assert db.get_items("u-cache", {"type": "note"}, use_cache=False)
    assert not db._cache


def test_reminder_queue_materializes_utc_and_uses_partial_indexes(db):
    owner_id = "u-reminder-queue"
    due_time = "2030-01-01T09:00:00"
    future_time = "2030-01-02T09:00:00"
    db.insert_item(
        {
            "id": "due-reminder",
            "type": "event",
            "owner_id": owner_id,
            "title": "due",
            "content": "large field must not drive the queue" * 100,
            "start_time": "2030-01-01T10:00:00",
            "remind_times": [due_time],
        }
    )
    db.insert_item(
        {
            "id": "future-reminder",
            "type": "event",
            "owner_id": owner_id,
            "title": "future",
            "start_time": "2030-01-02T10:00:00",
            "remind_times": [future_time],
        }
    )

    rows = (
        db.get_connection()
        .execute("SELECT item_id, fire_at_utc FROM reminder_logs ORDER BY item_id")
        .fetchall()
    )
    assert [(row["item_id"], row["fire_at_utc"]) for row in rows] == [
        ("due-reminder", "2030-01-01T01:00:00+00:00"),
        ("future-reminder", "2030-01-02T01:00:00+00:00"),
    ]

    due = db.get_due_reminder_items(now=datetime(2030, 1, 1, 1, tzinfo=timezone.utc))
    assert [item.id for item in due] == ["due-reminder"]
    assert due[0].content == ""

    pending_plan = (
        db.get_connection()
        .execute(
            """
        EXPLAIN QUERY PLAN
        SELECT item_id FROM reminder_logs
        WHERE sent_at IS NULL AND confirmed_at IS NULL AND fire_at_utc <= ?
        """,
            ("2030-01-01T01:00:00+00:00",),
        )
        .fetchall()
    )
    assert "idx_reminder_logs_pending_fire" in " ".join(str(row[3]) for row in pending_plan)

    db.log_reminder("due-reminder", db.get_item("due-reminder", owner_id).remind_times[0])
    repeat_plan = (
        db.get_connection()
        .execute(
            """
        EXPLAIN QUERY PLAN
        SELECT item_id FROM reminder_logs
        WHERE sent_at IS NOT NULL AND confirmed_at IS NULL
        ORDER BY sent_at
        """
        )
        .fetchall()
    )
    assert "idx_reminder_logs_unconfirmed_sent" in " ".join(str(row[3]) for row in repeat_plan)


def test_timezone_change_preserves_canonical_pending_reminder_instant(db):
    owner_id = "u-reminder-timezone-change"
    db.insert_item(
        {
            "id": "timezone-reminder",
            "type": "task",
            "owner_id": owner_id,
            "title": "timezone",
            "remind_times": ["2030-01-01T09:00:00"],
        }
    )
    conn = db.get_connection()
    assert (
        conn.execute(
            "SELECT fire_at_utc FROM reminder_logs WHERE item_id = 'timezone-reminder'"
        ).fetchone()[0]
        == "2030-01-01T01:00:00+00:00"
    )

    db.update_user_settings(owner_id, {"timezone": "America/Los_Angeles"})

    assert (
        conn.execute(
            "SELECT fire_at_utc FROM reminder_logs WHERE item_id = 'timezone-reminder'"
        ).fetchone()[0]
        == "2030-01-01T01:00:00+00:00"
    )


def test_prune_reminder_logs_removes_only_expired_confirmed_history(db):
    db.insert_item(
        {
            "id": "retention-reminder",
            "type": "event",
            "owner_id": "u-retention",
            "title": "retention",
            "remind_times": ["2030-01-01T09:00:00", "2030-01-02T09:00:00"],
        }
    )
    conn = db.get_connection()
    with conn:
        conn.execute(
            """
            UPDATE reminder_logs SET confirmed_at = '2029-01-01T00:00:00+00:00',
                                     state = 'confirmed'
            WHERE item_id = 'retention-reminder'
              AND remind_time = '2030-01-01T01:00:00+00:00'
            """
        )

    assert db.prune_reminder_logs(before=datetime(2029, 4, 1, tzinfo=timezone.utc)) == 1
    assert [log["remind_time"] for log in db.get_reminder_logs("retention-reminder")] == [
        "2030-01-02T01:00:00+00:00"
    ]


def test_reminder_service_prunes_history_once_per_utc_day():
    class _Db:
        def __init__(self):
            self.prune_calls = 0

        def prune_reminder_logs(self, *, before):
            assert before.tzinfo is timezone.utc
            self.prune_calls += 1
            return 0

        def get_due_reminder_items(self, *, now):
            return []

        def get_unconfirmed_sent_reminders(self):
            return []

    db = _Db()
    service = ReminderService(db)

    assert service.check_and_send_reminders()["sent"] == 0
    assert service.check_and_send_reminders()["sent"] == 0
    assert db.prune_calls == 1
