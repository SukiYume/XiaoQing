"""Atomic quota/resource regression tests for QingPet actions."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

from plugins.qingpet.services.database import Database
from plugins.qingpet.services.pet_service import PetService
from plugins.qingpet.services.user_service import UserService


def _setup(path: str, *, user_id: str = "keeper", group_id: int = 71001):
    db      = Database(path)
    user    = UserService(db).get_or_create_user(user_id, group_id)
    service = PetService(db)
    assert service.adopt_pet(user_id, group_id, "原子兽")[0] is True
    pet = db.get_pet(user_id, group_id)
    assert pet is not None
    db.get_or_create_inventory(user_id, group_id)
    return db, service, user, pet


def _persisted_state(db: Database, user_id: str, group_id: int) -> str:
    conn = db._get_connection()

    def rows(sql: str, params=()):
        return [dict(row) for row in conn.execute(sql, params).fetchall()]

    state = {
        "user": rows(
            "SELECT * FROM users WHERE user_id = ? AND group_id = ?",
            (user_id, group_id),
        ),
        "pet": rows(
            "SELECT * FROM pets WHERE user_id = ? AND group_id = ?",
            (user_id, group_id),
        ),
        "inventory": rows(
            "SELECT * FROM inventories WHERE user_id = ? AND group_id = ?",
            (user_id, group_id),
        ),
        "quotas": rows(
            "SELECT * FROM action_quotas WHERE user_id = ? AND group_id = ?",
            (user_id, group_id),
        ),
        "tasks": rows(
            "SELECT * FROM tasks WHERE user_id = ? AND group_id = ?",
            (user_id, group_id),
        ),
        "group_tasks": rows(
            "SELECT * FROM group_tasks WHERE group_id = ?",
            (group_id,),
        ),
    }
    return json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_invalid_feed_item_and_missing_inventory_do_not_consume_quota(tmp_path):
    db, service, user, pet = _setup(str(tmp_path / "feed-preconditions.db"))
    try:
        before        = _persisted_state(db, user.user_id, user.group_id)
        invalid       = service.feed_pet(pet, user, "not-an-item")
        after_invalid = _persisted_state(db, user.user_id, user.group_id)
        assert invalid[0] is False
        assert after_invalid == before

        missing       = service.feed_pet(pet, user, "cake")
        after_missing = _persisted_state(db, user.user_id, user.group_id)
        assert missing[0] is False
        assert after_missing == before
    finally:
        db.cleanup()


def test_energy_and_health_preconditions_do_not_consume_quota(tmp_path):
    db, service, user, pet = _setup(str(tmp_path / "energy-health-preconditions.db"))
    try:
        pet.energy = 0
        pet.health = 39
        assert db.update_pet(pet) is True
        pet    = db.get_pet(user.user_id, user.group_id)
        before = _persisted_state(db, user.user_id, user.group_id)

        training         = service.train_pet(pet, user, "strength")
        exploring_energy = service.explore(pet, user, "forest")
        assert training[0] is False
        assert exploring_energy[0] is False
        assert _persisted_state(db, user.user_id, user.group_id) == before

        pet.energy = 100
        assert db.update_pet(pet) is True
        pet              = db.get_pet(user.user_id, user.group_id)
        before_danger    = _persisted_state(db, user.user_id, user.group_id)
        exploring_health = service.explore(pet, user, "cave")
        assert exploring_health[0] is False
        assert _persisted_state(db, user.user_id, user.group_id) == before_danger
    finally:
        db.cleanup()


def test_final_pet_write_failure_rolls_back_quota_cooldown_counters_and_tasks(tmp_path):
    db, service, user, pet = _setup(str(tmp_path / "action-write-failure.db"))
    try:
        before = _persisted_state(db, user.user_id, user.group_id)
        conn   = db._get_connection()
        conn.execute(
            """CREATE TRIGGER fail_pet_action BEFORE UPDATE ON pets
               BEGIN SELECT RAISE(ABORT, 'injected pet action failure'); END"""
        )
        conn.commit()

        result = service.feed_pet(pet, user, "apple")

        assert result[0] is False
        assert _persisted_state(db, user.user_id, user.group_id) == before
    finally:
        db.cleanup()


def test_concurrent_inventory_feed_consumes_one_item_and_commits_one_action(tmp_path):
    path = str(tmp_path / "action-inventory-race.db")
    first, _service, user, _pet = _setup(path, group_id=71004)
    assert first.purchase_item_atomic(user.user_id, user.group_id, "cake", 1, 0)[0] is True
    second  = Database(path)
    barrier = threading.Barrier(2)

    def feed(db: Database):
        service    = PetService(db)
        local_user = db.get_user(user.user_id, user.group_id)
        local_pet  = db.get_pet(user.user_id, user.group_id)
        barrier.wait(timeout=5)
        return service.feed_pet(local_pet, local_user, "cake")

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(feed, (first, second)))

        assert [result[0] for result in results].count(True) == 1
        persisted_inventory = first.get_or_create_inventory(user.user_id, user.group_id)
        persisted_user      = first.get_user(user.user_id, user.group_id)
        assert persisted_inventory.items.get("cake", 0) == 0
        assert persisted_user.today_feed_count == 1
        quota = (
            first._get_connection()
            .execute(
                """SELECT action_count, available_at FROM action_quotas
               WHERE user_id = ? AND group_id = ? AND action = 'feed'""",
                (user.user_id, user.group_id),
            )
            .fetchone()
        )
        assert quota["action_count"] == 1
        assert quota["available_at"] > 0
    finally:
        second.cleanup()
        first.cleanup()


def test_concurrent_atomic_daily_limit_keeps_quota_and_user_counter_in_sync(tmp_path):
    path = str(tmp_path / "action-daily-limit-race.db")
    first, _service, user, _pet = _setup(path, group_id=71005)
    second  = Database(path)
    barrier = threading.Barrier(2)

    def commit(db: Database):
        local_user = db.get_user(user.user_id, user.group_id)
        local_pet  = db.get_pet(user.user_id, user.group_id)
        barrier.wait(timeout=5)
        return db.commit_pet_action(
            local_pet,
            local_user,
            action           = "feed",
            daily_limit      = 1,
            cooldown_seconds = 0,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(commit, (first, second)))

        assert [result.success for result in results].count(True) == 1
        persisted_user = first.get_user(user.user_id, user.group_id)
        quota_count    = (
            first._get_connection()
            .execute(
                """SELECT action_count FROM action_quotas
               WHERE user_id = ? AND group_id = ? AND action = 'feed'""",
                (user.user_id, user.group_id),
            )
            .fetchone()[0]
        )
        assert persisted_user.today_feed_count == quota_count == 1
        assert persisted_user.total_feed_count == 1
    finally:
        second.cleanup()
        first.cleanup()
