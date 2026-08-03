"""Concurrency and rollback coverage for versioned QingPet state writes."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from plugins.qingpet.services.database import Database, MinigameOutcome
from plugins.qingpet.services.pet_service import PetService
from plugins.qingpet.services.user_service import UserService
from plugins.qingpet.utils.constants import PetStage


def _setup(path: str, *, group_id: int):
    database = Database(path)
    user = UserService(database).get_or_create_user("keeper", group_id)
    service = PetService(database)
    assert service.adopt_pet(user.user_id, group_id, "并发兽")[0]
    pet = database.get_pet(user.user_id, group_id)
    assert pet is not None
    pet.stage = PetStage.YOUNG
    pet.hunger = 50
    pet.clean = 50
    pet.health = 50
    assert database.update_pet(pet)
    database.get_or_create_inventory(user.user_id, group_id)
    return database, user


def test_one_database_uses_distinct_connections_per_worker_thread(tmp_path):
    database = Database(str(tmp_path / "thread-local-connections.db"))
    barrier = threading.Barrier(2)

    def connection_identity() -> int:
        connection = database._get_connection()
        barrier.wait(timeout=5)
        connection.execute("SELECT 1").fetchone()
        return id(connection)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            identities = list(executor.map(lambda _index: connection_identity(), range(2)))

        assert len(set(identities)) == 2
        assert set(identities).issubset(database._all_connections)
    finally:
        database.cleanup()


def test_acceleration_inventory_failure_rolls_back_prior_pet_update(tmp_path):
    database, user = _setup(str(tmp_path / "acceleration-rollback.db"), group_id=72001)
    try:
        assert database.purchase_item_atomic(
            user.user_id, user.group_id, "acceleration_card", 1, 0
        )[0]
        before_pet = database.get_pet(user.user_id, user.group_id)
        before_inventory = database.get_or_create_inventory(user.user_id, user.group_id)
        assert before_pet is not None

        conn = database._get_connection()
        conn.execute(
            """CREATE TRIGGER fail_acceleration_inventory BEFORE UPDATE ON inventories
               BEGIN SELECT RAISE(ABORT, 'injected inventory failure'); END"""
        )
        conn.commit()

        success, _message = PetService(database).use_acceleration_card(before_pet, user)

        after_pet = database.get_pet(user.user_id, user.group_id)
        after_inventory = database.get_or_create_inventory(user.user_id, user.group_id)
        assert success is False
        assert after_pet is not None
        assert (after_pet.experience, after_pet.version) == (
            before_pet.experience,
            before_pet.version,
        )
        assert after_inventory.items == before_inventory.items == {"acceleration_card": 1}
        assert after_inventory.version == before_inventory.version
    finally:
        database.cleanup()


def test_concurrent_acceleration_consumes_one_card_and_grants_experience_once(tmp_path):
    path = str(tmp_path / "acceleration-race.db")
    first, user = _setup(path, group_id=72002)
    assert first.purchase_item_atomic(user.user_id, user.group_id, "acceleration_card", 1, 0)[0]
    before_pet = first.get_pet(user.user_id, user.group_id)
    assert before_pet is not None
    second = Database(path)
    barrier = threading.Barrier(2)

    def accelerate(database: Database):
        local_user = database.get_user(user.user_id, user.group_id)
        local_pet = database.get_pet(user.user_id, user.group_id)
        assert local_user is not None and local_pet is not None
        barrier.wait(timeout=5)
        return PetService(database).use_acceleration_card(local_pet, local_user)[0]

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(accelerate, (first, second)))

        after_pet = first.get_pet(user.user_id, user.group_id)
        after_inventory = first.get_or_create_inventory(user.user_id, user.group_id)
        assert results.count(True) == 1
        assert after_pet is not None
        assert after_pet.experience == before_pet.experience + 50
        assert after_pet.version == before_pet.version + 1
        assert after_inventory.items.get("acceleration_card", 0) == 0
    finally:
        second.cleanup()
        first.cleanup()


def test_stale_concurrent_feed_and_clean_preserve_both_pet_and_user_deltas(tmp_path):
    path = str(tmp_path / "pet-action-merge.db")
    first, user = _setup(path, group_id=72003)
    second = Database(path)
    before_pet = first.get_pet(user.user_id, user.group_id)
    before_user = first.get_user(user.user_id, user.group_id)
    assert before_pet is not None and before_user is not None
    barrier = threading.Barrier(2)

    def feed():
        local_pet = first.get_pet(user.user_id, user.group_id)
        local_user = first.get_user(user.user_id, user.group_id)
        assert local_pet is not None and local_user is not None
        barrier.wait(timeout=5)
        return PetService(first).feed_pet(local_pet, local_user, "apple")[0]

    def clean():
        local_pet = second.get_pet(user.user_id, user.group_id)
        local_user = second.get_user(user.user_id, user.group_id)
        assert local_pet is not None and local_user is not None
        barrier.wait(timeout=5)
        return PetService(second).clean_pet(local_pet, local_user)[0]

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [executor.submit(feed), executor.submit(clean)]
            assert [future.result() for future in results] == [True, True]

        after_pet = first.get_pet(user.user_id, user.group_id)
        after_user = first.get_user(user.user_id, user.group_id)
        assert after_pet is not None and after_user is not None
        assert after_pet.hunger > before_pet.hunger
        assert after_pet.clean > before_pet.clean
        assert after_pet.health > before_pet.health
        assert after_pet.experience > before_pet.experience
        assert after_pet.version == before_pet.version + 2
        assert after_user.today_feed_count == before_user.today_feed_count + 1
        assert after_user.today_clean_count == before_user.today_clean_count + 1
        assert after_user.total_feed_count == before_user.total_feed_count + 1
        assert after_user.total_clean_count == before_user.total_clean_count + 1
        assert after_user.version == before_user.version + 2
    finally:
        second.cleanup()
        first.cleanup()


def test_stale_inventory_writes_and_concurrent_rewards_preserve_all_deltas(tmp_path):
    path = str(tmp_path / "asset-merge.db")
    first, user = _setup(path, group_id=72004)
    second = Database(path)
    inventory_barrier = threading.Barrier(2)

    def buy_item(database: Database, item_id: str):
        inventory_barrier.wait(timeout=5)
        return database.purchase_item_atomic(user.user_id, user.group_id, item_id, 1, 0)[0]

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            inventory_results = list(
                executor.map(
                    lambda args: buy_item(*args),
                    (
                        (first, "cake"),
                        (second, "ball"),
                    ),
                )
            )
        assert inventory_results == [True, True]
        before_rewards = first.get_user(user.user_id, user.group_id)
        assert before_rewards is not None

        reward_barrier = threading.Barrier(2)

        def reward(database: Database, reference_id: str):
            reward_barrier.wait(timeout=5)
            result = database.settle_minigame_atomic(
                user.user_id,
                user.group_id,
                reference_id,
                reference_id=reference_id,
                daily_coin_limit=9999,
                cooldown_seconds=0,
                outcome_factory=lambda _pet, _opponent: MinigameOutcome(requested_coins=20),
            )
            assert result.success is True
            return result.coin_grant

        with ThreadPoolExecutor(max_workers=2) as executor:
            rewards = list(
                executor.map(
                    lambda args: reward(*args),
                    ((first, "concurrent-reward-1"), (second, "concurrent-reward-2")),
                )
            )

        after_inventory = first.get_or_create_inventory(user.user_id, user.group_id)
        after_user = first.get_user(user.user_id, user.group_id)
        assert rewards == [20, 20]
        assert after_inventory.items == {"cake": 1, "ball": 1}
        assert after_inventory.version == 2
        assert after_user is not None
        assert after_user.coins == before_rewards.coins + 40
        assert after_user.today_coins_earned == before_rewards.today_coins_earned + 40
        assert after_user.version == before_rewards.version + 2
    finally:
        second.cleanup()
        first.cleanup()
