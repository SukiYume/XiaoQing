from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from plugins.qingpet.services.database import Database
from plugins.qingpet.services.pet_service import PetService
from plugins.qingpet.services.social_service import SocialService
from plugins.qingpet.services.user_service import UserService

GROUP = 98002


def _seed(db: Database) -> None:
    users = UserService(db)
    users.get_or_create_user("sender", GROUP)
    users.get_or_create_user("target", GROUP)
    assert PetService(db).adopt_pet("target", GROUP, "留言宠")[0]


def _state(db: Database) -> tuple[int, int, int]:
    conn = db._get_connection()
    user = conn.execute(
        "SELECT today_message_count, total_message_count FROM users "
        "WHERE user_id = 'sender' AND group_id = ?",
        (GROUP,),
    ).fetchone()
    return (
        int(user["today_message_count"]),
        int(user["total_message_count"]),
        int(conn.execute("SELECT COUNT(*) FROM message_board").fetchone()[0]),
    )


class _FailingConnection:
    def __init__(self, connection: sqlite3.Connection, fragment: str):
        self.connection = connection
        self.fragment = " ".join(fragment.upper().split())

    def execute(self, sql, parameters=()):
        normalized = " ".join(str(sql).upper().split())
        if self.fragment in normalized:
            raise sqlite3.OperationalError("injected message settlement failure")
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
            raise sqlite3.OperationalError("injected message commit failure")
        return self.connection.commit()

    def __getattr__(self, name):
        return getattr(self.connection, name)


def test_leave_message_atomic_commits_message_and_both_counters(tmp_path):
    db = Database(str(tmp_path / "normal.db"))
    _seed(db)

    result = db.leave_message_atomic("sender", "target", GROUP, "你好", 10)

    assert result.success is True
    assert result.pet_name == "留言宠"
    assert _state(db) == (1, 1, 1)
    db.cleanup()


def test_social_service_uses_atomic_result_and_enforces_limit(tmp_path):
    db = Database(str(tmp_path / "service.db"))
    _seed(db)
    conn = db._get_connection()
    conn.execute(
        "UPDATE users SET today_message_count = 9 WHERE user_id = 'sender' AND group_id = ?",
        (GROUP,),
    )
    conn.commit()
    service = SocialService(db)

    first = service.leave_message("sender", "target", GROUP, "最后一条")
    second = service.leave_message("sender", "target", GROUP, "越界")

    assert first == (True, "已给留言宠留言：最后一条")
    assert second[0] is False
    assert "已达上限" in second[1]
    assert _state(db) == (10, 1, 1)
    db.cleanup()


@pytest.mark.parametrize(
    "fragment",
    [
        "INSERT INTO MESSAGE_BOARD",
        "UPDATE USERS SET TODAY_MESSAGE_COUNT",
    ],
)
def test_each_leave_message_write_failure_rolls_back_all_state(tmp_path, fragment):
    db = Database(str(tmp_path / "write-failure.db"))
    _seed(db)
    before = _state(db)
    db._local.conn = _FailingConnection(db._get_connection(), fragment)

    result = db.leave_message_atomic("sender", "target", GROUP, "不会留下", 10)

    assert result.success is False
    assert _state(db) == before
    db.cleanup()


def test_leave_message_commit_failure_rolls_back_all_state(tmp_path):
    db = Database(str(tmp_path / "commit-failure.db"))
    _seed(db)
    before = _state(db)
    db._local.conn = _CommitFailingConnection(db._get_connection())

    result = db.leave_message_atomic("sender", "target", GROUP, "不会提交", 10)

    assert result.success is False
    assert _state(db) == before
    db.cleanup()


def test_two_connections_cannot_both_claim_last_message_slot(tmp_path):
    path = str(tmp_path / "concurrent.db")
    first_db = Database(path)
    _seed(first_db)
    conn = first_db._get_connection()
    conn.execute(
        "UPDATE users SET today_message_count = 9 WHERE user_id = 'sender' AND group_id = ?",
        (GROUP,),
    )
    conn.commit()
    second_db = Database(path)
    barrier = threading.Barrier(2)

    def run(db: Database, message: str):
        barrier.wait(timeout=2)
        return db.leave_message_atomic("sender", "target", GROUP, message, 10)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda args: run(*args),
                ((first_db, "并发甲"), (second_db, "并发乙")),
            )
        )

    assert sum(result.success for result in results) == 1
    assert any("已达上限" in result.reason for result in results if not result.success)
    assert _state(first_db) == (10, 1, 1)
    first_db.cleanup()
    second_db.cleanup()


@pytest.mark.parametrize(
    ("sender", "target", "expected_reason"),
    [
        ("missing", "target", "留言用户不存在"),
        ("sender", "missing", "目标用户不存在"),
        ("sender", "sender", "不能给自己留言"),
    ],
)
def test_leave_message_rejects_invalid_participants_without_writes(
    tmp_path,
    sender,
    target,
    expected_reason,
):
    db = Database(str(tmp_path / f"invalid-{sender}-{target}.db"))
    _seed(db)
    before = _state(db)

    result = db.leave_message_atomic(sender, target, GROUP, "无效", 10)

    assert result.success is False
    assert result.reason == expected_reason
    assert _state(db) == before
    db.cleanup()


def test_leave_message_requires_target_pet_inside_transaction(tmp_path):
    db = Database(str(tmp_path / "missing-pet.db"))
    _seed(db)
    conn = db._get_connection()
    conn.execute(
        "DELETE FROM pets WHERE user_id = 'target' AND group_id = ?",
        (GROUP,),
    )
    conn.commit()
    before = _state(db)

    result = db.leave_message_atomic("sender", "target", GROUP, "无宠物", 10)

    assert result.success is False
    assert result.reason == "该用户没有宠物"
    assert _state(db) == before
    db.cleanup()
