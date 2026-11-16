from __future__ import annotations

import sqlite3
import threading

import pytest

from plugins.qingpet import main as qingpet_main
from plugins.qingpet.models import GroupConfigReadError
from plugins.qingpet.services.database import Database
from tests.helpers.assertions import text_segments_text

GROUP_ID = 97531


def _configure(database: Database) -> Database:
    assert database.update_group_config_field(GROUP_ID, "enabled", False)
    assert database.update_group_config_field(GROUP_ID, "trade_enabled", True)
    return database


@pytest.fixture
def config_db(qingpet_db: Database) -> Database:
    return _configure(qingpet_db)


def _row(database: Database) -> tuple:
    row = (
        database._get_connection()
        .execute(
            """SELECT group_id, enabled, economy_multiplier, decay_multiplier,
                  trade_enabled, activity_enabled, sensitive_words
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
    database = config_db
    database._get_connection().execute(
        f"UPDATE group_configs SET {column} = ? WHERE group_id = ?", (value, GROUP_ID)
    )
    database._get_connection().commit()
    before = _row(database)

    with pytest.raises(GroupConfigReadError):
        database.get_group_config(GROUP_ID)

    assert _row(database) == before


def test_missing_required_column_is_rejected(config_db):
    database    = config_db
    partial_row = (
        database._get_connection()
        .execute("SELECT group_id, enabled FROM group_configs WHERE group_id = ?", (GROUP_ID,))
        .fetchone()
    )
    assert partial_row is not None

    with pytest.raises(ValueError, match="missing required columns"):
        Database._parse_group_config_row(partial_row, GROUP_ID)


def test_connection_failure_raises_stable_config_error_without_default(config_db, monkeypatch):
    database                      = config_db
    before                        = _row(database)
    failures: list[BaseException] = []

    def fail_connect(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    def read_from_new_thread() -> None:
        # 连接按线程缓存；新线程要重新建连接，建连接这一步在这里失败。
        try:
            database.get_group_config(GROUP_ID)
        except BaseException as exc:
            failures.append(exc)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", fail_connect)
        worker = threading.Thread(target=read_from_new_thread)
        worker.start()
        worker.join()

    assert len(failures) == 1
    error = failures[0]
    assert isinstance(error, GroupConfigReadError)
    assert error.group_id == GROUP_ID
    verification = sqlite3.connect(database.db_path)
    try:
        persisted = verification.execute(
            """SELECT group_id, enabled, economy_multiplier, decay_multiplier,
                      trade_enabled, activity_enabled, sensitive_words
               FROM group_configs WHERE group_id = ?""",
            (GROUP_ID,),
        ).fetchone()
    finally:
        verification.close()
    assert persisted == before


def test_only_a_truly_missing_row_creates_product_default(config_db):
    database      = config_db
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


@pytest.mark.asyncio
async def test_main_route_fails_closed_and_admin_gets_repair_guidance(qingpet_runtime):
    context, database = qingpet_runtime
    _configure(database)
    database._get_connection().execute(
        "UPDATE group_configs SET sensitive_words = ? WHERE group_id = ?",
        ("not-json", GROUP_ID),
    )
    database._get_connection().commit()
    before = _row(database)
    event  = {"user_id": "ordinary-user", "group_id": GROUP_ID}

    ordinary   = text_segments_text(await qingpet_main.handle("pet", "状态", event, context))
    management = text_segments_text(await qingpet_main.handle("pet", "管理 配置", event, context))

    assert "安全停用" in ordinary
    assert "group_configs" in management
    assert _row(database) == before
    assert _row(database)[1] == 0
