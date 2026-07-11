from __future__ import annotations

import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from plugins.qingpet import main as qingpet_main
from plugins.qingpet.services.database import Database
from plugins.qingpet.services.pet_service import PetService
from plugins.qingpet.services.social_service import SocialService
from plugins.qingpet.services.user_service import UserService
from plugins.qingpet.utils.constants import PetStatus

GROUP = 97001


def _seed(db: Database, group_id: int = GROUP) -> None:
    users = UserService(db)
    users.get_or_create_user("visitor", group_id)
    users.get_or_create_user("owner", group_id)
    assert PetService(db).adopt_pet("owner", group_id, "原子宠")[0]


def _settle(db: Database, reference_id: str = "visit:test"):
    return db.visit_pet_atomic(
        "visitor",
        "owner",
        GROUP,
        coin_reward=5,
        daily_visit_limit=10,
        daily_coin_limit=500,
        cooldown_seconds=0,
        reference_id=reference_id,
    )


def _state(db: Database) -> tuple:
    conn = db._get_connection()
    visitor = conn.execute(
        "SELECT coins, today_coins_earned, today_visit_count, total_visit_count, last_visit_time "
        "FROM users WHERE user_id = 'visitor' AND group_id = ?",
        (GROUP,),
    ).fetchone()
    owner = conn.execute(
        "SELECT coins, today_coins_earned FROM users WHERE user_id = 'owner' AND group_id = ?",
        (GROUP,),
    ).fetchone()
    pet = conn.execute(
        "SELECT intimacy FROM pets WHERE user_id = 'owner' AND group_id = ?",
        (GROUP,),
    ).fetchone()
    return (
        tuple(visitor),
        tuple(owner),
        tuple(pet),
        conn.execute("SELECT COUNT(*) FROM action_quotas").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM asset_ledger").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM visit_settlements").fetchone()[0],
    )


class _FailingConnection:
    def __init__(self, connection: sqlite3.Connection, fragment: str, occurrence: int = 1):
        self.connection = connection
        self.fragment = " ".join(fragment.upper().split())
        self.occurrence = occurrence
        self.matches = 0

    def execute(self, sql, parameters=()):
        normalized = " ".join(str(sql).upper().split())
        if self.fragment in normalized:
            self.matches += 1
            if self.matches == self.occurrence:
                raise sqlite3.OperationalError("injected visit settlement failure")
        return self.connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


class _CommitFailingConnection:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.failed = False

    def commit(self):
        if not self.failed:
            self.failed = True
            raise sqlite3.OperationalError("injected commit failure")
        return self.connection.commit()

    def __getattr__(self, name):
        return getattr(self.connection, name)


@pytest.mark.parametrize(
    ("fragment", "occurrence"),
    [
        ("INSERT INTO ACTION_QUOTAS", 1),
        ("UPDATE USERS SET TODAY_VISIT_COUNT", 1),
        ("UPDATE PETS SET INTIMACY", 1),
        ("UPDATE USERS SET COINS", 1),
        ("INSERT INTO ASSET_LEDGER", 1),
        ("UPDATE USERS SET COINS", 2),
        ("INSERT INTO ASSET_LEDGER", 2),
        ("INSERT OR IGNORE INTO TASKS", 1),
        ("UPDATE TASKS SET CURRENT_VALUE", 1),
        ("INSERT INTO VISIT_SETTLEMENTS", 1),
    ],
)
def test_visit_failure_at_each_write_rolls_back_everything(tmp_path, fragment, occurrence):
    db = Database(str(tmp_path / "failure.db"))
    _seed(db)
    before = _state(db)
    raw = db._get_connection()
    db._conn = _FailingConnection(raw, fragment, occurrence)

    result = _settle(db)

    assert result.success is False
    assert _state(db) == before
    db.cleanup()


def test_visit_commit_failure_rolls_back_everything(tmp_path):
    db = Database(str(tmp_path / "commit-failure.db"))
    _seed(db)
    before = _state(db)
    db._conn = _CommitFailingConnection(db._get_connection())

    result = _settle(db)

    assert result.success is False
    assert _state(db) == before
    db.cleanup()


def test_visit_same_reference_returns_committed_grants_without_double_write(tmp_path):
    db = Database(str(tmp_path / "idempotent.db"))
    _seed(db)

    first = _settle(db, "visit:same")
    second = _settle(db, "visit:same")

    assert first.success is True
    assert second == type(first)(
        True,
        pet_name="原子宠",
        visitor_grant=5,
        target_grant=5,
        intimacy_grant=1,
        duplicate=True,
    )
    conn = db._get_connection()
    assert conn.execute("SELECT COUNT(*) FROM visit_settlements").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM asset_ledger").fetchone()[0] == 2
    assert conn.execute("SELECT action_count FROM action_quotas").fetchone()[0] == 1
    assert conn.execute(
        "SELECT current_value FROM tasks WHERE user_id = 'visitor' AND group_id = ? "
        "AND task_type = 'visit'",
        (GROUP,),
    ).fetchone()[0] == 1
    assert db.get_pet("owner", GROUP).intimacy == 1
    db.cleanup()


def test_visit_two_connections_cannot_exceed_daily_quota(tmp_path):
    path = str(tmp_path / "concurrent.db")
    first_db = Database(path)
    _seed(first_db)
    second_db = Database(path)
    barrier = threading.Barrier(2)

    def run(db: Database, reference: str):
        barrier.wait(timeout=2)
        return db.visit_pet_atomic(
            "visitor",
            "owner",
            GROUP,
            coin_reward=5,
            daily_visit_limit=1,
            daily_coin_limit=500,
            cooldown_seconds=0,
            reference_id=reference,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: run(*pair), [(first_db, "visit:a"), (second_db, "visit:b")]))

    assert sum(result.success for result in results) == 1
    assert first_db._get_connection().execute(
        "SELECT action_count FROM action_quotas"
    ).fetchone()[0] == 1
    assert first_db.get_pet("owner", GROUP).intimacy == 1
    first_db.cleanup()
    second_db.cleanup()


def test_visit_clips_each_users_reward_independently_and_reports_actual_grants(tmp_path):
    db = Database(str(tmp_path / "caps.db"))
    _seed(db)
    conn = db._get_connection()
    conn.execute(
        "UPDATE users SET today_coins_earned = 499 WHERE user_id = 'visitor' AND group_id = ?",
        (GROUP,),
    )
    conn.execute(
        "UPDATE users SET today_coins_earned = 500 WHERE user_id = 'owner' AND group_id = ?",
        (GROUP,),
    )
    conn.commit()

    ok, message = SocialService(db).visit_pet(
        "visitor", "owner", GROUP, message_id="near-cap"
    )

    assert ok is True
    assert "访客获得1金币" in message
    assert "宠物主人获得0金币" in message
    rows = conn.execute(
        "SELECT user_id, delta FROM asset_ledger ORDER BY user_id"
    ).fetchall()
    assert [(row["user_id"], row["delta"]) for row in rows] == [("owner", 0), ("visitor", 1)]
    db.cleanup()


def test_visit_reference_is_isolated_by_group(tmp_path):
    db = Database(str(tmp_path / "groups.db"))
    _seed(db, GROUP)
    _seed(db, GROUP + 1)
    service = SocialService(db)

    assert service.visit_pet("visitor", "owner", GROUP, message_id="same-message")[0]
    assert service.visit_pet("visitor", "owner", GROUP + 1, message_id="same-message")[0]

    refs = db._get_connection().execute(
        "SELECT reference_id FROM visit_settlements ORDER BY group_id"
    ).fetchall()
    assert len({row["reference_id"] for row in refs}) == 2
    db.cleanup()


def test_visit_rechecks_target_pet_status_inside_transaction(tmp_path):
    db = Database(str(tmp_path / "status.db"))
    _seed(db)
    pet = db.get_pet("owner", GROUP)
    pet.status = PetStatus.SICK
    assert db.update_pet(pet)
    before = _state(db)

    result = _settle(db)

    assert result.success is False
    assert "无法互动" in result.reason
    assert _state(db) == before
    db.cleanup()


def test_main_forwards_message_id_so_delivery_retry_is_idempotent(tmp_path):
    db = Database(str(tmp_path / "main.db"))
    UserService(db).get_or_create_user("visitor", GROUP)
    UserService(db).get_or_create_user("123", GROUP)
    assert PetService(db).adopt_pet("123", GROUP, "消息宠")[0]
    original_db = qingpet_main._db_instance
    original_router = qingpet_main._router
    qingpet_main._db_instance = db
    qingpet_main._router = None
    event = {"user_id": "visitor", "group_id": GROUP, "message_id": 123456}
    try:
        first = asyncio.run(qingpet_main.handle("pet", "互访 123", event, None))
        second = asyncio.run(qingpet_main.handle("pet", "互访 123", event, None))
    finally:
        qingpet_main._db_instance = original_db
        qingpet_main._router = original_router

    assert first == second
    assert db._get_connection().execute(
        "SELECT COUNT(*) FROM visit_settlements"
    ).fetchone()[0] == 1
    db.cleanup()
