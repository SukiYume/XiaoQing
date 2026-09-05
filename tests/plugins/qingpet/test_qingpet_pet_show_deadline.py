from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from plugins.qingpet import main as qingpet_main
from plugins.qingpet.services import database_clock
from plugins.qingpet.services.database import Database
from plugins.qingpet.services.pet_service import PetService
from plugins.qingpet.services.user_service import UserService
from tests.helpers.paths import REPOSITORY_ROOT

GROUP_ID = 86420
BASE_TIME = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
TARGET_ID = "target"


@pytest.fixture
def show_context(monkeypatch):
    current_time = [BASE_TIME]
    monkeypatch.setattr(database_clock, "now", lambda: current_time[0])
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as file:
        db_path = file.name
    database = Database(db_path)
    users    = UserService(database)
    pets     = PetService(database)
    for user_id in [TARGET_ID, "voter-before", "voter-exact", "voter-after", "voter-late"]:
        users.get_or_create_user(user_id, GROUP_ID)
    pets.adopt_pet(TARGET_ID, GROUP_ID, "星星")
    show_id = database.create_pet_show(GROUP_ID, "限时展示会", 1)
    assert show_id is not None
    try:
        yield database, db_path, int(show_id), current_time
    finally:
        database.cleanup()
        os.unlink(db_path)
        backup_path = db_path + ".pre-migration.bak"
        if os.path.exists(backup_path):
            os.unlink(backup_path)


def _raw_show(database: Database, show_id: int) -> dict:
    row = (
        database._get_connection()
        .execute("SELECT * FROM pet_shows WHERE id = ?", (show_id,))
        .fetchone()
    )
    assert row is not None
    return dict(row)


def test_vote_is_allowed_only_strictly_before_end_time(show_context):
    database, _db_path, show_id, current_time = show_context
    end_time = BASE_TIME + timedelta(hours=1)

    current_time[0] = end_time - timedelta(microseconds=1)
    assert database.get_active_pet_show(GROUP_ID) is not None
    assert database.vote_pet_show_atomic(show_id, "voter-before", TARGET_ID, 3)

    current_time[0] = end_time
    assert database.get_active_pet_show(GROUP_ID) is None
    assert not database.vote_pet_show_atomic(show_id, "voter-exact", TARGET_ID, 3)

    current_time[0] = end_time + timedelta(seconds=1)
    assert not database.vote_pet_show_atomic(show_id, "voter-after", TARGET_ID, 3)
    assert database.get_pet_show_votes(show_id) == {TARGET_ID: 1}


def test_vote_rejects_forged_show_and_pet_from_another_group(show_context):
    database, _db_path, show_id, current_time = show_context
    users = UserService(database)
    pets  = PetService(database)
    users.get_or_create_user("other-target", GROUP_ID + 1)
    pets.adopt_pet("other-target", GROUP_ID + 1, "异群宠")
    current_time[0] = BASE_TIME + timedelta(minutes=1)

    assert not database.vote_pet_show_atomic(999999, "voter-before", TARGET_ID, 3)
    assert not database.vote_pet_show_atomic(show_id, "voter-before", "other-target", 3)
    assert database.get_pet_show_votes(show_id) == {}


def test_expired_show_is_settled_and_does_not_block_new_show(show_context):
    database, _db_path, show_id, current_time = show_context
    current_time[0] = BASE_TIME + timedelta(hours=1)

    assert database.get_active_pet_show(GROUP_ID) is None
    new_show_id = database.create_pet_show(GROUP_ID, "下一场", 1)

    assert new_show_id is not None
    old_show = _raw_show(database, show_id)
    assert old_show["is_active"] == 0
    assert old_show["status"] == "settled"
    active_rows = (
        database._get_connection()
        .execute("SELECT id FROM pet_shows WHERE group_id = ? AND is_active = 1", (GROUP_ID,))
        .fetchall()
    )
    assert [int(row["id"]) for row in active_rows] == [int(new_show_id)]


def test_concurrent_vote_and_settlement_reward_winner_once(show_context):
    database, db_path, show_id, current_time = show_context
    current_time[0] = BASE_TIME + timedelta(minutes=30)
    assert database.vote_pet_show_atomic(show_id, "voter-before", TARGET_ID, 3)
    assert database.vote_pet_show_atomic(show_id, "voter-exact", TARGET_ID, 3)
    current_time[0] = BASE_TIME + timedelta(hours=1)
    contenders = [Database(db_path) for _ in range(3)]
    barrier    = threading.Barrier(3)

    def late_vote():
        barrier.wait()
        return contenders[0].vote_pet_show_atomic(show_id, "voter-late", TARGET_ID, 3)

    def settle(index: int):
        barrier.wait()
        return contenders[index].settle_pet_show_atomic(GROUP_ID, force=False)

    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(late_vote),
                executor.submit(settle, 1),
                executor.submit(settle, 2),
            ]
            late_vote_result, first_settlement, second_settlement = [
                future.result() for future in futures
            ]
    finally:
        for contender in contenders:
            contender.cleanup()

    assert late_vote_result is False
    assert sum(result is not None for result in [first_settlement, second_settlement]) == 1
    assert _raw_show(database, show_id)["status"] == "settled"
    winner = database.get_user(TARGET_ID, GROUP_ID)
    assert winner is not None
    assert winner.coins == 300
    assert winner.today_coins_earned == 200
    assert winner.titles.count("展示会冠军") == 1
    ledger_rows = (
        database._get_connection()
        .execute(
            "SELECT delta FROM asset_ledger WHERE reference_id LIKE ?",
            (f"show:{show_id}:%",),
        )
        .fetchall()
    )
    assert [int(row["delta"]) for row in ledger_rows] == [200]


def test_concurrent_creation_keeps_one_active_show_per_group(show_context):
    database, db_path, _show_id, current_time = show_context
    current_time[0] = BASE_TIME + timedelta(hours=1)
    assert database.settle_pet_show_atomic(GROUP_ID, force=False) is not None
    contenders = [Database(db_path), Database(db_path)]
    barrier    = threading.Barrier(2)

    def create(index: int):
        barrier.wait()
        return contenders[index].create_pet_show(GROUP_ID, f"并发场次{index}", 1)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [
                future.result()
                for future in [executor.submit(create, 0), executor.submit(create, 1)]
            ]
    finally:
        for contender in contenders:
            contender.cleanup()

    assert sum(result is not None for result in results) == 1
    active_count = (
        database._get_connection()
        .execute(
            "SELECT COUNT(*) AS count FROM pet_shows WHERE group_id = ? AND is_active = 1",
            (GROUP_ID,),
        )
        .fetchone()["count"]
    )
    assert int(active_count) == 1


def test_pet_show_deadline_has_independent_scheduled_handler(monkeypatch):
    calls: list[tuple[int, bool]] = []

    class FakeDatabase:
        @staticmethod
        def get_all_group_ids():
            return [1, 2]

    class FakeSocialService:
        @staticmethod
        def settle_pet_show(group_id: int, *, force: bool):
            calls.append((group_id, force))
            return "已结算" if group_id == 2 else ""

    monkeypatch.setattr(qingpet_main, "_db_instance", FakeDatabase())
    monkeypatch.setattr(qingpet_main, "_social_service", FakeSocialService())

    deliveries = asyncio.run(qingpet_main.scheduled_pet_show_settlement(None))
    assert len(deliveries) == 1
    assert deliveries[0].target.group_id == 2
    assert deliveries[0].message == ({"type": "text", "data": {"text": "已结算"}},)
    assert calls == [(1, False), (2, False)]
    root = REPOSITORY_ROOT
    manifest = json.loads((root / "plugins/qingpet/plugin.json").read_text(encoding="utf-8"))
    jobs = {job["id"]: job for job in manifest["schedule"]}
    assert jobs["qingpet_pet_show_settlement"]["handler"] == ("scheduled_pet_show_settlement")
    assert jobs["qingpet_pet_show_settlement"]["delivery"] == "targeted"
