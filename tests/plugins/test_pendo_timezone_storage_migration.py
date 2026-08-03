"""Pendo 存量时区探测、阻塞与备份迁移回归。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from plugins.pendo.scripts.migrate_timezone_storage import (
    TimezoneMigrationBlocked,
    _default_database_path,
    migrate_timezone_storage,
)


def _create_legacy_database(path: Path, *, user_timezone: str = "Asia/Shanghai") -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE user_settings (
            user_id TEXT PRIMARY KEY,
            timezone TEXT
        );
        CREATE TABLE items (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            timezone TEXT,
            created_at TEXT,
            updated_at TEXT,
            deleted_at TEXT,
            start_time TEXT,
            end_time TEXT,
            deadline_at TEXT,
            completed_at TEXT,
            cancelled_at TEXT,
            last_viewed TEXT,
            entry_time TEXT,
            remind_times TEXT
        );
        CREATE TABLE event_collections (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            timezone TEXT,
            created_at TEXT,
            updated_at TEXT,
            deleted_at TEXT,
            start_time TEXT,
            end_time TEXT
        );
        CREATE TABLE reminder_logs (
            id INTEGER PRIMARY KEY,
            item_id TEXT NOT NULL,
            remind_time TEXT,
            sent_at TEXT,
            confirmed_at TEXT,
            last_sent_at TEXT,
            claim_expires_at TEXT,
            next_attempt_at TEXT,
            fire_at_utc TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO user_settings (user_id, timezone) VALUES (?, ?)",
        ("u1", user_timezone),
    )
    connection.commit()
    connection.close()


def _fetch_value(path: Path, sql: str) -> object:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql).fetchone()[0]
    finally:
            connection.close()


def test_timezone_migration_cli_default_matches_runtime_data_directory() -> None:
    assert _default_database_path() == Path(__file__).resolve().parents[2] / "data" / "pendo" / "pendo.db"


def test_timezone_migration_dry_run_reports_four_forms_without_writing(tmp_path: Path) -> None:
    db_path = tmp_path / "pendo.db"
    _create_legacy_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.executemany(
        """
        INSERT INTO items (
            id, type, owner_id, timezone, created_at, updated_at, last_viewed, remind_times
        ) VALUES (?, 'note', 'u1', 'Asia/Shanghai', ?, ?, ?, '[]')
        """,
        [
            ("naive", "2026-05-01T10:00:00", "2026-05-01T10:00:00", None),
            ("utc", "2026-05-01T02:00:00+00:00", "2026-05-01T02:00:00Z", None),
            ("offset", "2026-05-01T10:00:00+08:00", "broken", None),
        ],
    )
    connection.commit()
    connection.close()

    report = migrate_timezone_storage(db_path)

    assert report["mode"] == "dry-run"
    assert report["forms"] == {
        "naive": 2,
        "aware_utc": 2,
        "aware_offset": 1,
        "invalid": 1,
    }
    assert report["invalid_count"] == 1
    assert report["blocked"] is True
    assert (
        _fetch_value(db_path, "SELECT created_at FROM items WHERE id = 'naive'")
        == "2026-05-01T10:00:00"
    )


def test_timezone_migration_backs_up_applies_atomically_and_is_idempotent(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "pendo.db"
    _create_legacy_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        INSERT INTO items (
            id, type, owner_id, timezone, created_at, updated_at, last_viewed, remind_times
        ) VALUES (
            'note-1', 'note', 'u1', 'Asia/Shanghai',
            '2026-05-01T10:00:00', '2026-05-01T10:00:00',
            '2026-05-01T11:00:00+08:00', '[]'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO items (
            id, type, owner_id, timezone, created_at, updated_at,
            start_time, end_time, remind_times
        ) VALUES (
            'event-1', 'event', 'u1', 'Asia/Shanghai',
            '2026-05-02T09:00:00', '2026-05-02T09:00:00',
            '2026-05-02T09:00:00', '2026-05-02T10:00:00',
            '["2026-05-02T08:30:00"]'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO event_collections (
            id, owner_id, timezone, created_at, updated_at, start_time, end_time
        ) VALUES (
            'collection-1', 'u1', 'Asia/Shanghai',
            '2026-05-02T09:00:00', '2026-05-02T09:00:00',
            '2026-05-02T09:00:00', '2026-05-02T10:00:00'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO reminder_logs (
            id, item_id, remind_time, sent_at, fire_at_utc
        ) VALUES (
            1, 'event-1', '2026-05-02T08:30:00',
            '2026-05-02T09:05:00', '2026-05-02T00:30:00'
        )
        """
    )
    connection.commit()
    connection.close()

    report = migrate_timezone_storage(
        db_path,
        apply=True,
        legacy_server_timezone="Asia/Shanghai",
    )

    backup_path = Path(str(report["backup"]))
    assert backup_path.is_file()
    assert report["blocked"] is False
    assert report["integrity_check"] == "ok"
    assert report["foreign_key_violations"] == 0
    assert (
        _fetch_value(backup_path, "SELECT created_at FROM items WHERE id = 'note-1'")
        == "2026-05-01T10:00:00"
    )
    assert (
        _fetch_value(db_path, "SELECT created_at FROM items WHERE id = 'note-1'")
        == "2026-05-01T02:00:00+00:00"
    )
    assert (
        _fetch_value(db_path, "SELECT start_time FROM items WHERE id = 'event-1'")
        == "2026-05-02T01:00:00+00:00"
    )
    assert json.loads(
        str(_fetch_value(db_path, "SELECT remind_times FROM items WHERE id = 'event-1'"))
    ) == ["2026-05-02T00:30:00+00:00"]
    assert (
        _fetch_value(db_path, "SELECT fire_at_utc FROM reminder_logs WHERE id = 1")
        == "2026-05-02T00:30:00+00:00"
    )

    second = migrate_timezone_storage(
        db_path,
        legacy_server_timezone="Asia/Shanghai",
    )
    assert second["blocked"] is False
    assert second["would_change_values"] == 0
    assert second["forms"]["naive"] == 0


def test_timezone_migration_refuses_ambiguous_event_without_resolution(tmp_path: Path) -> None:
    db_path = tmp_path / "pendo.db"
    _create_legacy_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        INSERT INTO items (
            id, type, owner_id, timezone, created_at, updated_at, start_time, remind_times
        ) VALUES (
            'event-ambiguous', 'event', 'u1', 'Asia/Shanghai',
            '2026-05-02T09:00:00', '2026-05-02T09:00:00',
            '2026-05-02T09:00:00', '[]'
        )
        """
    )
    connection.commit()
    connection.close()

    dry_run = migrate_timezone_storage(db_path, legacy_server_timezone="UTC")
    assert dry_run["blocked"] is True
    assert dry_run["unresolved_count"] == 3
    assert {issue["key"] for issue in dry_run["issues"]} == {
        "items:event-ambiguous:created_at",
        "items:event-ambiguous:updated_at",
        "items:event-ambiguous:start_time",
    }

    with pytest.raises(TimezoneMigrationBlocked):
        migrate_timezone_storage(
            db_path,
            apply=True,
            legacy_server_timezone="UTC",
        )
    assert not list(tmp_path.glob("*.timezone-backup-*"))
    assert (
        _fetch_value(db_path, "SELECT start_time FROM items WHERE id = 'event-ambiguous'")
        == "2026-05-02T09:00:00"
    )

    resolution_path = tmp_path / "resolution.json"
    resolution_path.write_text(
        json.dumps({"items:event-ambiguous:event_source": "owner"}),
        encoding="utf-8",
    )
    applied = migrate_timezone_storage(
        db_path,
        apply=True,
        legacy_server_timezone="UTC",
        resolution_file=resolution_path,
    )
    assert applied["blocked"] is False
    assert (
        _fetch_value(db_path, "SELECT start_time FROM items WHERE id = 'event-ambiguous'")
        == "2026-05-02T01:00:00+00:00"
    )


def test_timezone_migration_rejects_dst_gap_instead_of_guessing(tmp_path: Path) -> None:
    db_path = tmp_path / "pendo.db"
    _create_legacy_database(db_path, user_timezone="America/New_York")
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        INSERT INTO items (
            id, type, owner_id, timezone, created_at, updated_at, entry_time, remind_times
        ) VALUES (
            'diary-gap', 'diary', 'u1', 'America/New_York',
            '2026-03-08T02:30:00', '2026-03-08T02:30:00',
            '2026-03-08T02:30:00', '[]'
        )
        """
    )
    connection.commit()
    connection.close()

    report = migrate_timezone_storage(db_path, legacy_server_timezone="UTC")

    assert report["blocked"] is True
    assert report["unresolved_count"] == 3
    assert all("Nonexistent local time" in issue["reason"] for issue in report["issues"])
