"""Regression coverage for QingPet daily group-task identity and transactions."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from plugins.qingpet.services.database import Database
from plugins.qingpet.services.pet_service import PetService
from plugins.qingpet.services.user_service import UserService
from plugins.qingpet.utils.constants import GROUP_TASK_TEMPLATES
from plugins.qingpet.utils.time import utc_now


def _today() -> str:
    return utc_now().strftime("%Y-%m-%d")


def _rows(db: Database, group_id: int) -> list[sqlite3.Row]:
    return (
        db._get_connection()
        .execute(
            """SELECT * FROM group_tasks
           WHERE group_id = ? AND created_date = ? ORDER BY task_type""",
            (group_id, _today()),
        )
        .fetchall()
    )


def test_legacy_group_task_duplicates_are_merged_before_unique_key(tmp_path):
    path = tmp_path / "legacy-group-tasks.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE group_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            task_type TEXT NOT NULL,
            target_value INTEGER NOT NULL,
            current_value INTEGER DEFAULT 0,
            reward_coins INTEGER DEFAULT 0,
            description TEXT DEFAULT '',
            created_date TEXT,
            is_completed BOOLEAN DEFAULT 0
        )"""
    )
    conn.executemany(
        """INSERT INTO group_tasks
           (group_id, task_type, target_value, current_value, reward_coins,
            description, created_date, is_completed)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (70001, "group_feed", 50, 4, 20, "旧描述", _today(), 0),
            (70001, "group_feed", 50, 7, 20, "新描述", _today(), 0),
            (70001, "group_feed", 40, 5, 15, "较小配置", _today(), 0),
            (70001, "legacy", 1, 1, 1, "无日期", None, 1),
            (70001, "legacy", 1, 0, 1, "无日期副本", None, 0),
        ],
    )
    conn.commit()
    conn.close()

    db = Database(str(path))
    try:
        feed_rows = (
            db._get_connection()
            .execute(
                """SELECT * FROM group_tasks
               WHERE group_id = 70001 AND task_type = 'group_feed' AND created_date = ?""",
                (_today(),),
            )
            .fetchall()
        )
        assert len(feed_rows) == 1
        assert feed_rows[0]["id"] == 1
        assert feed_rows[0]["target_value"] == 50
        assert feed_rows[0]["current_value"] == 7
        assert feed_rows[0]["reward_coins"] == 20
        assert (
            db._get_connection()
            .execute(
                "SELECT COUNT(*) FROM group_tasks WHERE group_id = 70001 AND task_type = 'legacy'"
            )
            .fetchone()[0]
            == 1
        )

        created_date = next(
            row
            for row in db._get_connection().execute("PRAGMA table_info(group_tasks)")
            if row["name"] == "created_date"
        )
        assert created_date["notnull"] == 1
        unique_indexes = [
            row
            for row in db._get_connection().execute("PRAGMA index_list(group_tasks)")
            if row["unique"] == 1
        ]
        assert any(
            [
                column["name"]
                for column in db._get_connection().execute(f'PRAGMA index_info("{index["name"]}")')
            ]
            == ["group_id", "task_type", "created_date"]
            for index in unique_indexes
        )

        tasks = db.get_or_create_group_tasks(70001)
        assert len(tasks) == len(GROUP_TASK_TEMPLATES)
        assert (
            next(task for task in tasks if task["task_type"] == "group_feed")["current_value"] == 7
        )
    finally:
        db.cleanup()


def test_group_task_initialization_and_increment_roll_back_together(tmp_path):
    db = Database(str(tmp_path / "group-task-rollback.db"))
    group_id = 70002
    try:
        user = UserService(db).get_or_create_user("rollback-keeper", group_id)
        assert PetService(db).adopt_pet("rollback-keeper", group_id, "回滚测试")[0] is True
        pet = db.get_pet("rollback-keeper", group_id)
        assert pet is not None

        conn = db._get_connection()
        conn.execute(
            """CREATE TRIGGER fail_group_task_increment
               BEFORE UPDATE ON group_tasks
               BEGIN SELECT RAISE(ABORT, 'injected group task failure'); END"""
        )
        conn.commit()

        assert (
            db.atomic_update_pet_and_user(
                pet,
                user,
                group_task_type="group_feed",
            )
            is False
        )
        assert _rows(db, group_id) == []

        conn.execute("DROP TRIGGER fail_group_task_increment")
        conn.commit()
        assert (
            db.atomic_update_pet_and_user(
                pet,
                user,
                group_task_type="group_feed",
            )
            is True
        )
        rows = _rows(db, group_id)
        assert len(rows) == len(GROUP_TASK_TEMPLATES)
        assert next(row for row in rows if row["task_type"] == "group_feed")["current_value"] == 1
    finally:
        db.cleanup()


def test_two_database_instances_create_once_and_increment_one_row_per_action(tmp_path):
    path = str(tmp_path / "group-task-concurrent.db")
    first = Database(path)
    second = Database(path)
    group_id = 70003
    barrier = threading.Barrier(2)

    keepers = ((first, "keeper-first"), (second, "keeper-second"))
    for db, keeper in keepers:
        UserService(db).get_or_create_user(keeper, group_id)
        assert PetService(db).adopt_pet(keeper, group_id, keeper)[0] is True

    def increment(entry: tuple[Database, str]) -> list[bool]:
        db, keeper = entry
        user = db.get_user(keeper, group_id)
        pet = db.get_pet(keeper, group_id)
        assert user is not None
        assert pet is not None
        barrier.wait(timeout=5)
        return [
            db.atomic_update_pet_and_user(
                pet,
                user,
                group_task_type="group_feed",
            )
            for _ in range(10)
        ]

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(increment, keepers))

        assert all(result for batch in results for result in batch)
        rows = _rows(first, group_id)
        assert len(rows) == len(GROUP_TASK_TEMPLATES)
        feed = next(row for row in rows if row["task_type"] == "group_feed")
        assert feed["current_value"] == 20
        assert (
            first._get_connection()
            .execute(
                """SELECT COUNT(*) FROM group_tasks
                   WHERE group_id = ? AND task_type = 'group_feed' AND created_date = ?""",
                (group_id, _today()),
            )
            .fetchone()[0]
            == 1
        )
    finally:
        second.cleanup()
        first.cleanup()


def test_atomic_pet_action_creates_templates_and_advances_only_selected_task(tmp_path):
    db = Database(str(tmp_path / "group-task-pet-action.db"))
    group_id = 70004
    try:
        user = UserService(db).get_or_create_user("keeper", group_id)
        pet_service = PetService(db)
        assert pet_service.adopt_pet("keeper", group_id, "团团")[0] is True
        pet = db.get_pet("keeper", group_id)
        assert pet is not None

        assert (
            db.atomic_update_pet_and_user(
                pet,
                user,
                group_task_type="group_clean",
            )
            is True
        )
        rows = _rows(db, group_id)
        assert len(rows) == len(GROUP_TASK_TEMPLATES)
        progress = {row["task_type"]: row["current_value"] for row in rows}
        assert progress == {
            "group_clean": 1,
            "group_explore": 0,
            "group_feed": 0,
        }
    finally:
        db.cleanup()


def test_completed_group_task_reward_can_only_be_claimed_once(tmp_path):
    path = str(tmp_path / "group-task-claim.db")
    first = Database(path)
    second = Database(path)
    group_id = 70005
    try:
        user = UserService(first).get_or_create_user("claimer", group_id)
        tasks = first.get_or_create_group_tasks(group_id)
        feed = next(task for task in tasks if task["task_type"] == "group_feed")
        first._get_connection().execute(
            """UPDATE group_tasks SET current_value = target_value, is_completed = 1
               WHERE group_id = ? AND task_type = 'group_feed' AND created_date = ?""",
            (group_id, _today()),
        )
        first._get_connection().commit()

        reward = first.claim_group_task_reward("claimer", group_id, "group_feed")
        duplicate = second.claim_group_task_reward("claimer", group_id, "group_feed")

        assert reward == feed["reward_coins"]
        assert duplicate is None
        persisted = first.get_user("claimer", group_id)
        assert persisted is not None
        assert persisted.coins == user.coins + reward
        assert (
            first._get_connection()
            .execute(
                """SELECT COUNT(*) FROM asset_ledger
                   WHERE reason = 'group_task' AND user_id = ? AND group_id = ?""",
                ("claimer", group_id),
            )
            .fetchone()[0]
            == 1
        )
    finally:
        second.cleanup()
        first.cleanup()
