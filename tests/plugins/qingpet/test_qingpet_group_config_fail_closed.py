from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile

import pytest

from plugins.qingpet import main as qingpet_main
from plugins.qingpet.models import GroupConfigReadError
from plugins.qingpet.services.database import Database
from tests.helpers.assertions import text_segments_text

GROUP_ID = 97531


@pytest.fixture
def config_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as file:
        db_path = file.name
    database             = Database(db_path)
    config               = database.get_group_config(GROUP_ID)
    config.enabled       = False
    config.trade_enabled = True
    assert database.update_group_config(config)
    try:
        yield database, db_path
    finally:
        database.cleanup()
        os.unlink(db_path)
        backup_path = db_path + ".pre-migration.bak"
        if os.path.exists(backup_path):
            os.unlink(backup_path)


def _row(database: Database) -> tuple:
    row = (
        database._get_connection()
        .execute(
            """SELECT group_id, enabled, economy_multiplier, decay_multiplier,
                  trade_enabled, natural_trigger_enabled, activity_enabled, sensitive_words
           FROM group_configs WHERE group_id = ?""",
            (GROUP_ID,),
        )
        .fetchone()
    )
    assert row is not None
    return tuple(row)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("sensitive_words", "{"),
        ("sensitive_words", '{"not":"a-list"}'),
        ("economy_multiplier", 10.1),
        ("decay_multiplier", 0),
        ("enabled", 2),
    ],
)
def test_existing_corrupt_config_never_falls_back_or_overwrites_row(config_db, column: str, value):
    database, _db_path = config_db
    database._get_connection().execute(
        f"UPDATE group_configs SET {column} = ? WHERE group_id = ?", (value, GROUP_ID)
    )
    database._get_connection().commit()
    before = _row(database)

    with pytest.raises(GroupConfigReadError):
        database.get_group_config(GROUP_ID)

    assert _row(database) == before


def test_missing_required_column_is_rejected(config_db):
    database, _db_path = config_db
    partial_row = (
        database._get_connection()
        .execute("SELECT group_id, enabled FROM group_configs WHERE group_id = ?", (GROUP_ID,))
        .fetchone()
    )
    assert partial_row is not None

    with pytest.raises(ValueError, match="missing required columns"):
        Database._parse_group_config_row(partial_row, GROUP_ID)


def test_connection_failure_raises_stable_config_error_without_default(config_db, monkeypatch):
    database, db_path = config_db
    before = _row(database)

    def fail_connection():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(database, "_get_connection", fail_connection)
    with pytest.raises(GroupConfigReadError) as error:
        database.get_group_config(GROUP_ID)

    assert error.value.group_id == GROUP_ID
    verification = sqlite3.connect(db_path)
    try:
        persisted = verification.execute(
            """SELECT group_id, enabled, economy_multiplier, decay_multiplier,
                      trade_enabled, natural_trigger_enabled, activity_enabled, sensitive_words
               FROM group_configs WHERE group_id = ?""",
            (GROUP_ID,),
        ).fetchone()
    finally:
        verification.close()
    assert persisted == before


def test_only_a_truly_missing_row_creates_product_default(config_db):
    database, _db_path = config_db
    missing_group = GROUP_ID + 1
    assert (
        database._get_connection()
        .execute("SELECT 1 FROM group_configs WHERE group_id = ?", (missing_group,))
        .fetchone()
        is None
    )

    config = database.get_group_config(missing_group)

    assert config.group_id == missing_group
    assert config.enabled is True
    row = (
        database._get_connection()
        .execute("SELECT enabled FROM group_configs WHERE group_id = ?", (missing_group,))
        .fetchone()
    )
    assert row is not None and row["enabled"] == 1


def test_main_route_fails_closed_and_admin_gets_repair_guidance(config_db):
    database, _db_path = config_db
    database._get_connection().execute(
        "UPDATE group_configs SET sensitive_words = ? WHERE group_id = ?",
        ("not-json", GROUP_ID),
    )
    database._get_connection().commit()
    before                    = _row(database)
    original_db               = qingpet_main._db_instance
    original_router           = qingpet_main._router
    qingpet_main._db_instance = database
    qingpet_main._router      = None
    event                     = {"user_id": "ordinary-user", "group_id": GROUP_ID}
    try:
        ordinary   = text_segments_text(asyncio.run(qingpet_main.handle("pet", "状态", event, None)))
        management = text_segments_text(
            asyncio.run(qingpet_main.handle("pet", "管理 配置", event, None))
        )
    finally:
        qingpet_main._db_instance = original_db
        qingpet_main._router      = original_router

    assert "安全停用" in ordinary
    assert "group_configs" in management
    assert _row(database) == before
    assert _row(database)[1] == 0
