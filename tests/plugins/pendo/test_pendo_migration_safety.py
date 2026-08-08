import importlib
import json
import sqlite3
import sys
from unittest.mock import Mock

import pytest

from plugins.pendo.scripts import migrate_event_graph, migrate_pendo_redesign, migration_utils
from plugins.pendo.scripts.migration_utils import (
    backup_sqlite_database,
    connect_sqlite_database,
    dump_json_field,
    load_json_field,
    normalize_iso_seconds,
    table_columns,
    table_exists,
)
from plugins.pendo.services.db import Database


def test_migration_modules_share_one_set_of_sqlite_and_json_primitives():
    assert migrate_event_graph.backup_sqlite_database is backup_sqlite_database
    assert migrate_pendo_redesign.backup_sqlite_database is backup_sqlite_database
    assert migrate_event_graph._connect is connect_sqlite_database
    assert migrate_pendo_redesign._connect is connect_sqlite_database


def test_migration_utils_normalize_rows_schema_and_json_fields(tmp_path):
    db_path = tmp_path / "shared-utils.db"
    connection = connect_sqlite_database(db_path)
    try:
        connection.execute('CREATE TABLE "odd table" (id INTEGER, payload TEXT)')
        connection.execute(
            'INSERT INTO "odd table" (id, payload) VALUES (?, ?)',
            (1, dump_json_field({"中文": [1]})),
        )
        connection.commit()

        row = connection.execute('SELECT * FROM "odd table"').fetchone()
        assert row["id"] == 1
        assert table_exists(connection, "odd table") is True
        assert table_columns(connection, "odd table") == {"id", "payload"}
        assert table_columns(connection, "missing") == set()
        assert load_json_field(row["payload"], {}) == {"中文": [1]}
        assert load_json_field("not-json", {"fallback": True}) == {"fallback": True}
        assert load_json_field(123, []) == []
    finally:
        connection.close()


def test_shared_backup_captures_committed_wal_pages(tmp_path):
    db_path = tmp_path / "source.db"
    backup_path = tmp_path / "nested" / "backup.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE values_table (value TEXT)")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("INSERT INTO values_table VALUES ('from-wal')")
        connection.commit()

        backup_sqlite_database(db_path, backup_path)
    finally:
        connection.close()

    backup = sqlite3.connect(backup_path)
    try:
        assert backup.execute("SELECT value FROM values_table").fetchall() == [("from-wal",)]
    finally:
        backup.close()


def test_shared_backup_rejects_missing_source_and_same_path(tmp_path):
    """备份不得静默创建空源库，也不得用目标覆盖正在备份的源库。"""

    missing_path = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError, match="不存在"):
        backup_sqlite_database(missing_path, tmp_path / "missing.bak")
    assert not missing_path.exists()

    source_path = tmp_path / "source.db"
    connection = sqlite3.connect(source_path)
    connection.execute("CREATE TABLE marker (value TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="不能与源数据库相同"):
        backup_sqlite_database(source_path, source_path)
    verification = sqlite3.connect(source_path)
    try:
        assert verification.execute("SELECT name FROM sqlite_master").fetchone()
    finally:
        verification.close()


def test_shared_backup_closes_source_when_target_connection_fails(tmp_path, monkeypatch):
    """目标连接失败时也必须关闭已经打开的源连接，避免 Windows 文件锁残留。"""

    source_path = tmp_path / "source.db"
    source_path.touch()
    source_connection = Mock()
    connect_calls = 0

    def fake_connect(_path):
        nonlocal connect_calls
        connect_calls += 1
        if connect_calls == 1:
            return source_connection
        raise sqlite3.OperationalError("target unavailable")

    monkeypatch.setattr(migration_utils.sqlite3, "connect", fake_connect)

    with pytest.raises(sqlite3.OperationalError, match="target unavailable"):
        backup_sqlite_database(source_path, tmp_path / "backup.db")

    source_connection.close.assert_called_once_with()


def test_normalize_iso_seconds_preserves_offsets_and_rejects_invalid_values():
    """迁移时间规范化应移除微秒、保留偏移，并安全跳过损坏旧值。"""

    assert normalize_iso_seconds("2026-07-16T12:34:56.987654+08:00") == (
        "2026-07-16T12:34:56+08:00"
    )
    assert normalize_iso_seconds("not-a-time") is None
    assert normalize_iso_seconds(None) is None


def test_redesign_migration_failure_keeps_original_database_unchanged(tmp_path, monkeypatch):
    db_path = tmp_path / "pendo.db"
    db = Database(str(db_path))
    db.insert_item({"type": "note", "owner_id": "u1", "title": "original"})
    db.cleanup()
    original = db_path.read_bytes()
    module = importlib.import_module("plugins.pendo.scripts.migrate_pendo_redesign")

    def fail_migration(*_args, **_kwargs):
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(module, "migrate_event_graph", fail_migration)
    with pytest.raises(RuntimeError, match="injected"):
        module.migrate_pendo_redesign(db_path, apply=True)

    assert db_path.read_bytes() == original
    assert not (tmp_path / "pendo.db.pendo-redesign.lock").exists()
    assert '"state": "failed"' in (tmp_path / "pendo.db.pendo-redesign-journal.json").read_text(
        encoding="utf-8"
    )


def test_redesign_setup_failure_removes_lock_and_preserves_original(tmp_path, monkeypatch):
    """备份阶段失败也必须进入统一清理边界，不能留下永久迁移锁。"""

    db_path = tmp_path / "pendo.db"
    db = Database(str(db_path))
    db.insert_item({"type": "note", "owner_id": "u1", "title": "original"})
    db.cleanup()
    original = db_path.read_bytes()

    def fail_backup(_source, target):
        target.write_bytes(b"partial-backup")
        raise OSError("backup unavailable")

    monkeypatch.setattr(migrate_pendo_redesign, "backup_sqlite_database", fail_backup)

    with pytest.raises(OSError, match="backup unavailable"):
        migrate_pendo_redesign.migrate_pendo_redesign(db_path, apply=True)

    assert db_path.read_bytes() == original
    assert not (tmp_path / "pendo.db.pendo-redesign.lock").exists()
    journal = json.loads(
        (tmp_path / "pendo.db.pendo-redesign-journal.json").read_text(encoding="utf-8")
    )
    assert journal["state"] == "failed"
    assert "backup unavailable" in journal["error"]
    assert not list(tmp_path.glob("*.pendo-redesign-working-*"))
    assert not list(tmp_path.glob("*.pendo-redesign-backup-*"))


def test_redesign_lock_write_failure_removes_partial_lock(tmp_path, monkeypatch):
    """锁内容写入失败时，锁创建函数自身也要撤销文件和句柄。"""

    db_path = tmp_path / "pendo.db"
    db_path.touch()

    def fail_dump(*_args, **_kwargs):
        raise OSError("lock write failed")

    monkeypatch.setattr(migrate_pendo_redesign.json, "dump", fail_dump)

    with pytest.raises(OSError, match="lock write failed"):
        migrate_pendo_redesign._acquire_migration_lock(db_path)

    assert not (tmp_path / "pendo.db.pendo-redesign.lock").exists()


def test_field_migration_begin_failure_still_closes_connection(tmp_path, monkeypatch):
    """事务获取失败发生在任何读取前，也必须回滚并关闭迁移连接。"""

    connection = Mock()
    connection.execute.side_effect = sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(migrate_pendo_redesign, "_connect", lambda _path: connection)

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        migrate_pendo_redesign._migrate_item_fields(
            tmp_path / "pendo.db",
            apply=True,
            spec=migrate_pendo_redesign._NOTE_MIGRATION,
        )

    connection.rollback.assert_called_once_with()
    connection.close.assert_called_once_with()


def test_cleanup_legacy_columns_does_not_drop_substring_index(tmp_path):
    """删除 direction 列时，direction_suffix 上的无关索引不能被子串误判。"""

    db_path = tmp_path / "pendo.db"
    db = Database(str(db_path))
    try:
        connection = db.get_connection()
        connection.execute("ALTER TABLE items ADD COLUMN direction TEXT")
        connection.execute("ALTER TABLE items ADD COLUMN direction_suffix TEXT")
        connection.execute("CREATE INDEX idx_direction_suffix ON items(direction_suffix)")
        connection.commit()
    finally:
        db.cleanup()

    report = migrate_pendo_redesign._cleanup_legacy_item_columns(
        db_path,
        ("direction",),
        "ledger",
        apply=True,
    )

    connection = connect_sqlite_database(db_path)
    try:
        columns = table_columns(connection, "items")
        indexes = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
    finally:
        connection.close()
    assert report["dropped"] == ["direction"]
    assert report["dropped_indexes"] == []
    assert "direction" not in columns
    assert "direction_suffix" in columns
    assert "idx_direction_suffix" in indexes


def test_redesign_cli_rejects_conflicting_modes(monkeypatch):
    """完整迁移 CLI 不能同时接受应用和预览模式。"""

    monkeypatch.setattr(
        sys,
        "argv",
        ["migrate_pendo_redesign", "--apply", "--dry-run"],
    )

    with pytest.raises(SystemExit) as captured:
        migrate_pendo_redesign.main()

    assert captured.value.code == 2
