"""Failure-boundary coverage for QingPet transactional use cases."""

from __future__ import annotations

import asyncio
import logging

import pytest

from plugins.qingpet import main as qingpet_main
from plugins.qingpet.services.admin_service import AdminService
from plugins.qingpet.services.database import Database
from plugins.qingpet.services.pet_service import PetService
from plugins.qingpet.services.social_service import SocialService
from plugins.qingpet.services.user_service import UserService
from plugins.qingpet.utils.constants import PetStage


def _setup(path: str, *, group_id: int = 73001):
    database = Database(path)
    user = UserService(database).get_or_create_user("keeper", group_id)
    service = PetService(database)
    assert service.adopt_pet(user.user_id, group_id, "事务兽")[0]
    pet = database.get_pet(user.user_id, group_id)
    assert pet is not None
    pet.stage = PetStage.YOUNG
    pet.clean = 50
    pet.age = 3
    assert database.update_pet(pet)
    return database, user


def _drop_trigger(database: Database, name: str) -> None:
    database._get_connection().execute(f"DROP TRIGGER {name}")
    database._get_connection().commit()


def test_explicit_activity_progress_is_in_action_transaction_and_ignores_natural_toggle(tmp_path):
    database, user = _setup(str(tmp_path / "action-activity.db"), group_id=73001)
    try:
        config = database.get_group_config(user.group_id)
        assert config.activity_enabled is True
        assert config.natural_trigger_enabled is False
        activity_id = database.create_activity(
            user.group_id,
            "clean",
            "清洁活动",
            10,
            20,
        )
        assert activity_id is not None
        before_pet = database.get_pet(user.user_id, user.group_id)
        before_user = database.get_user(user.user_id, user.group_id)
        assert before_pet is not None and before_user is not None
        conn = database._get_connection()
        conn.execute(
            """CREATE TRIGGER fail_activity_progress BEFORE UPDATE ON activities
               BEGIN SELECT RAISE(ABORT, 'injected activity failure'); END"""
        )
        conn.commit()

        failed = PetService(database).clean_pet(before_pet, before_user)

        persisted_pet = database.get_pet(user.user_id, user.group_id)
        persisted_user = database.get_user(user.user_id, user.group_id)
        assert failed[0] is False
        assert persisted_pet is not None and persisted_user is not None
        assert persisted_pet.clean == 50
        assert persisted_user.today_clean_count == 0
        assert conn.execute("SELECT COUNT(*) FROM action_quotas").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT current_value FROM activities WHERE id = ?", (activity_id,)
            ).fetchone()[0]
            == 0
        )

        _drop_trigger(database, "fail_activity_progress")
        fresh_pet = database.get_pet(user.user_id, user.group_id)
        fresh_user = database.get_user(user.user_id, user.group_id)
        assert fresh_pet is not None and fresh_user is not None
        assert PetService(database).clean_pet(fresh_pet, fresh_user)[0] is True
        assert (
            conn.execute(
                "SELECT current_value FROM activities WHERE id = ?", (activity_id,)
            ).fetchone()[0]
            == 1
        )
    finally:
        database.cleanup()


def test_daily_reset_failure_rolls_back_users_pets_and_scheduler_then_retries(tmp_path):
    database, user = _setup(str(tmp_path / "daily-reset.db"), group_id=73002)
    try:
        persisted_user = database.get_user(user.user_id, user.group_id)
        assert persisted_user is not None
        persisted_user.today_feed_count = 4
        assert database.update_user(persisted_user)
        conn = database._get_connection()
        conn.execute(
            """CREATE TRIGGER fail_daily_pet_age BEFORE UPDATE ON pets
               BEGIN SELECT RAISE(ABORT, 'injected daily age failure'); END"""
        )
        conn.commit()
        period_key = "2026-07-13:73002"

        assert database.run_daily_reset_atomic(period_key, user.group_id) is None
        assert database.get_user(user.user_id, user.group_id).today_feed_count == 4
        assert database.get_pet(user.user_id, user.group_id).age == 3
        assert conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0] == 0

        _drop_trigger(database, "fail_daily_pet_age")
        result = database.run_daily_reset_atomic(period_key, user.group_id)
        assert result is not None
        assert (result.users_reset, result.pets_aged) == (1, 1)
        assert database.get_user(user.user_id, user.group_id).today_feed_count == 0
        assert database.get_pet(user.user_id, user.group_id).age == 4
        assert database.run_daily_reset_atomic(period_key, user.group_id) is None
        scheduler = conn.execute("SELECT * FROM scheduler_runs").fetchone()
        assert scheduler["status"] == "completed"
        assert scheduler["attempt_count"] == 1
    finally:
        database.cleanup()


class _Context:
    logger = logging.getLogger("test.qingpet.transaction_closure")


def test_weekly_failure_is_retryable_and_announcement_uses_actual_ledger_grant(
    tmp_path,
    monkeypatch,
):
    database, user = _setup(str(tmp_path / "weekly.db"), group_id=73003)
    try:
        limited_user = database.get_user(user.user_id, user.group_id)
        assert limited_user is not None
        limited_user.today_coins_earned = 498
        assert database.update_user(limited_user)
        before_coins = limited_user.coins
        conn = database._get_connection()
        conn.execute(
            """CREATE TRIGGER fail_weekly_ledger BEFORE INSERT ON asset_ledger
               BEGIN SELECT RAISE(ABORT, 'injected weekly ledger failure'); END"""
        )
        conn.commit()
        monkeypatch.setattr(qingpet_main, "_db_instance", database)
        monkeypatch.setattr(qingpet_main, "_social_service", SocialService(database))
        monkeypatch.setattr(qingpet_main, "business_week", lambda: "2026-W29")

        assert asyncio.run(qingpet_main.scheduled_weekly_activity(_Context())) == []
        assert database.get_user(user.user_id, user.group_id).coins == before_coins
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM asset_ledger WHERE reason = 'weekly_ranking'"
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0] == 0

        _drop_trigger(database, "fail_weekly_ledger")
        messages = asyncio.run(qingpet_main.scheduled_weekly_activity(_Context()))
        assert len(messages) == 1
        rendered = messages[0].message[0]["data"]["text"]
        assert "+2金币" in rendered
        assert "+100金币" not in rendered
        ledger = conn.execute(
            "SELECT delta FROM asset_ledger WHERE reason = 'weekly_ranking'"
        ).fetchone()
        assert ledger is not None and ledger["delta"] == 2
        winner = database.get_user(user.user_id, user.group_id)
        assert winner is not None
        assert winner.coins == before_coins + 2
        assert "本周之星" in winner.titles
        scheduler = conn.execute("SELECT * FROM scheduler_runs").fetchone()
        assert scheduler["status"] == "completed"
        assert asyncio.run(qingpet_main.scheduled_weekly_activity(_Context())) == []
    finally:
        database.cleanup()


def test_pet_show_reward_and_announcement_roll_back_together(tmp_path):
    database, winner_user = _setup(str(tmp_path / "show.db"), group_id=73004)
    try:
        voter = UserService(database).get_or_create_user("voter", winner_user.group_id)
        show_id = database.create_pet_show(winner_user.group_id, "原子展示会", 24)
        assert show_id is not None
        assert database.vote_pet_show_atomic(
            show_id,
            voter.user_id,
            winner_user.user_id,
            3,
        )
        before = database.get_user(winner_user.user_id, winner_user.group_id)
        assert before is not None
        conn = database._get_connection()
        conn.execute(
            """CREATE TRIGGER fail_show_ledger BEFORE INSERT ON asset_ledger
               BEGIN SELECT RAISE(ABORT, 'injected show ledger failure'); END"""
        )
        conn.commit()

        assert SocialService(database).settle_pet_show(winner_user.group_id, force=True) == ""
        failed_user = database.get_user(winner_user.user_id, winner_user.group_id)
        show = conn.execute("SELECT * FROM pet_shows WHERE id = ?", (show_id,)).fetchone()
        assert failed_user is not None
        assert failed_user.coins == before.coins
        assert "展示会冠军" not in failed_user.titles
        assert show["is_active"] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM asset_ledger WHERE reason = 'pet_show'").fetchone()[
                0
            ]
            == 0
        )

        _drop_trigger(database, "fail_show_ledger")
        message = SocialService(database).settle_pet_show(winner_user.group_id, force=True)
        ledger = conn.execute("SELECT delta FROM asset_ledger WHERE reason = 'pet_show'").fetchone()
        assert ledger is not None
        assert f"+{ledger['delta']}金币" in message
        settled_user = database.get_user(winner_user.user_id, winner_user.group_id)
        assert settled_user is not None and "展示会冠军" in settled_user.titles
    finally:
        database.cleanup()


@pytest.mark.parametrize("operation", ["reset", "ban", "delete"])
def test_admin_mutation_and_audit_log_share_one_transaction(tmp_path, operation):
    database, user = _setup(str(tmp_path / f"admin-{operation}.db"), group_id=73005)
    try:
        before_pet = database.get_pet(user.user_id, user.group_id)
        before_user = database.get_user(user.user_id, user.group_id)
        assert before_pet is not None and before_user is not None
        conn = database._get_connection()
        conn.execute(
            """CREATE TRIGGER fail_admin_audit BEFORE INSERT ON operation_logs
               BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"""
        )
        conn.commit()
        admin = AdminService(database)

        def run() -> bool:
            if operation == "reset":
                return admin.reset_user_pet(user.user_id, user.group_id, "operator")
            if operation == "ban":
                return admin.ban_user(user.user_id, user.group_id, 3, "operator")
            return admin.delete_user_pet(user.user_id, user.group_id, "operator")

        assert run() is False
        failed_pet = database.get_pet(user.user_id, user.group_id)
        failed_user = database.get_user(user.user_id, user.group_id)
        assert failed_pet is not None and failed_user is not None
        assert failed_pet.stage == before_pet.stage
        assert failed_pet.clean == before_pet.clean
        assert failed_user.is_banned is False
        assert conn.execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0] == 0

        _drop_trigger(database, "fail_admin_audit")
        assert run() is True
        log = conn.execute("SELECT * FROM operation_logs").fetchone()
        assert log is not None
        assert log["target_user_id"] == user.user_id
        if operation == "reset":
            assert database.get_pet(user.user_id, user.group_id).stage == PetStage.EGG
            assert log["operation_type"] == "RESET"
        elif operation == "ban":
            assert database.get_user(user.user_id, user.group_id).is_banned is True
            assert log["operation_type"] == "BAN"
        else:
            assert database.get_pet(user.user_id, user.group_id) is None
            assert log["operation_type"] == "DELETE"
    finally:
        database.cleanup()


def test_daily_task_claim_and_ledger_share_one_transaction(tmp_path):
    database, user = _setup(str(tmp_path / "task-reward.db"), group_id=73006)
    try:
        database.get_or_create_daily_tasks(user.user_id, user.group_id)
        conn = database._get_connection()
        conn.execute(
            """UPDATE tasks SET current_value = target_value
               WHERE user_id = ? AND group_id = ? AND task_type = 'feed'""",
            (user.user_id, user.group_id),
        )
        conn.commit()
        before = database.get_user(user.user_id, user.group_id)
        assert before is not None
        conn.execute(
            """CREATE TRIGGER fail_task_ledger BEFORE INSERT ON asset_ledger
               BEGIN SELECT RAISE(ABORT, 'injected task ledger failure'); END"""
        )
        conn.commit()

        assert database.claim_task_reward(user.user_id, user.group_id, "feed") is None
        task = conn.execute(
            "SELECT claimed FROM tasks WHERE user_id = ? AND group_id = ? AND task_type = 'feed'",
            (user.user_id, user.group_id),
        ).fetchone()
        assert task["claimed"] == 0
        assert database.get_user(user.user_id, user.group_id).coins == before.coins

        _drop_trigger(database, "fail_task_ledger")
        granted = database.claim_task_reward(user.user_id, user.group_id, "feed")
        ledger = conn.execute(
            "SELECT delta FROM asset_ledger WHERE reason = 'daily_task'"
        ).fetchone()
        assert granted == 30
        assert ledger is not None and ledger["delta"] == granted
        assert database.get_user(user.user_id, user.group_id).coins == before.coins + granted
    finally:
        database.cleanup()
