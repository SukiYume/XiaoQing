from __future__ import annotations

import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from plugins.qingpet import main as qingpet_main
from plugins.qingpet.services import social_service as social_module
from plugins.qingpet.services.database import Database, MinigameOutcome
from plugins.qingpet.services.pet_service import PetService
from plugins.qingpet.services.social_service import SocialService
from plugins.qingpet.services.user_service import UserService

GROUP = 98001


def _seed(db: Database, *, energy: int = 100) -> None:
    users = UserService(db)
    users.get_or_create_user("player", GROUP)
    users.get_or_create_user("opponent", GROUP)
    assert PetService(db).adopt_pet("player", GROUP, "原子玩家")[0]
    assert PetService(db).adopt_pet("opponent", GROUP, "原子对手")[0]
    conn = db._get_connection()
    conn.execute(
        "UPDATE pets SET energy = ? WHERE user_id = 'player' AND group_id = ?",
        (energy, GROUP),
    )
    conn.commit()


def _state(db: Database) -> tuple:
    conn = db._get_connection()
    user = conn.execute(
        "SELECT coins, today_coins_earned FROM users WHERE user_id = 'player' AND group_id = ?",
        (GROUP,),
    ).fetchone()
    pet = conn.execute(
        "SELECT experience, energy FROM pets WHERE user_id = 'player' AND group_id = ?",
        (GROUP,),
    ).fetchone()
    return (
        tuple(user),
        tuple(pet),
        conn.execute("SELECT COUNT(*) FROM minigame_cooldowns").fetchone()[0],
        conn.execute(
            "SELECT COUNT(*) FROM asset_ledger WHERE reason != 'account_opening'"
        ).fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM minigame_settlements").fetchone()[0],
    )


def _settle(db: Database, reference_id: str = "game:test"):
    return db.settle_minigame_atomic(
        "player",
        GROUP,
        "dice",
        reference_id     = reference_id,
        daily_coin_limit = 500,
        cooldown_seconds = 120,
        outcome_factory  = lambda _pet, _opponent: MinigameOutcome(
            requested_coins = 20,
            experience      = 8,
            payload         = {"player_dice": 6, "pet_dice": 1, "result": "你赢了"},
        ),
    )


class _FailingConnection:
    def __init__(self, connection: sqlite3.Connection, fragment: str):
        self.connection = connection
        self.fragment   = " ".join(fragment.upper().split())

    def execute(self, sql, parameters=()):
        normalized = " ".join(str(sql).upper().split())
        if self.fragment in normalized:
            raise sqlite3.OperationalError("injected minigame settlement failure")
        return self.connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


class _CommitFailingConnection:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.failed     = False

    def commit(self):
        if not self.failed:
            self.failed = True
            raise sqlite3.OperationalError("injected minigame commit failure")
        return self.connection.commit()

    def __getattr__(self, name):
        return getattr(self.connection, name)


@pytest.mark.parametrize(
    "fragment",
    [
        "INSERT INTO MINIGAME_COOLDOWNS",
        "UPDATE USERS SET COINS",
        "INSERT INTO ASSET_LEDGER",
        "UPDATE PETS SET EXPERIENCE",
        "INSERT INTO MINIGAME_SETTLEMENTS",
    ],
)
def test_failure_at_each_minigame_write_rolls_back_everything(tmp_path, fragment):
    db = Database(str(tmp_path / "failure.db"))
    _seed(db)
    before         = _state(db)
    db._local.conn = _FailingConnection(db._get_connection(), fragment)

    result = _settle(db)

    assert result.success is False
    assert _state(db) == before
    db.cleanup()


def test_minigame_commit_failure_rolls_back_everything(tmp_path):
    db = Database(str(tmp_path / "commit.db"))
    _seed(db)
    before         = _state(db)
    db._local.conn = _CommitFailingConnection(db._get_connection())

    result = _settle(db)

    assert result.success is False
    assert _state(db) == before
    db.cleanup()


def test_same_reference_replays_original_outcome_without_new_random_or_assets(tmp_path):
    db = Database(str(tmp_path / "duplicate.db"))
    _seed(db)
    calls = 0

    def make_outcome(_pet, _opponent):
        nonlocal calls
        calls += 1
        return MinigameOutcome(
            requested_coins = 20,
            experience      = 8,
            payload         = {"roll": calls},
        )

    kwargs = {
        "reference_id": "game:same",
        "daily_coin_limit": 500,
        "cooldown_seconds": 120,
        "outcome_factory": make_outcome,
    }
    first  = db.settle_minigame_atomic("player", GROUP, "dice", **kwargs)
    second = db.settle_minigame_atomic("player", GROUP, "dice", **kwargs)

    assert first.success is True
    assert second.success is True
    assert second.duplicate is True
    assert first.payload == second.payload == {"roll": 1}
    assert calls == 1
    assert _state(db) == ((120, 20), (8, 100), 1, 1, 1)
    db.cleanup()


def test_two_connections_concurrently_settle_same_reference_once(tmp_path):
    path     = str(tmp_path / "concurrent.db")
    first_db = Database(path)
    _seed(first_db)
    second_db = Database(path)
    barrier   = threading.Barrier(2)

    def run(db: Database):
        barrier.wait(timeout=2)
        return _settle(db, "game:concurrent")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (first_db, second_db)))

    assert all(result.success for result in results)
    assert sum(result.duplicate for result in results) == 1
    assert _state(first_db) == ((120, 20), (8, 100), 1, 1, 1)
    first_db.cleanup()
    second_db.cleanup()


@pytest.mark.parametrize(
    ("game", "expected_coins", "expected_exp", "expected_energy"),
    [
        ("rps", 15, 5, 0),
        ("dice", 20, 8, 0),
        ("race", 30, 10, 15),
    ],
)
def test_all_three_games_use_atomic_actual_settlement(
    tmp_path,
    monkeypatch,
    game,
    expected_coins,
    expected_exp,
    expected_energy,
):
    db = Database(str(tmp_path / f"{game}.db"))
    _seed(db)
    service = SocialService(db)
    monkeypatch.setattr(social_module.random, "choice", lambda _choices: "scissors")
    rolls = iter((6, 1) if game == "dice" else (100, 1))
    monkeypatch.setattr(social_module.random, "randint", lambda _a, _b: next(rolls))

    if game == "rps":
        ok, message = service.play_rock_paper_scissors(
            "player", GROUP, "石头", message_id="rps-message"
        )
    elif game == "dice":
        ok, message = service.play_dice("player", GROUP, message_id="dice-message")
    else:
        ok, message = service.race_pet("player", "opponent", GROUP, message_id="race-message")

    assert ok is True
    assert f"获得{expected_coins}金币" in message
    assert db.get_user("player", GROUP).coins == 100 + expected_coins
    pet = db.get_pet("player", GROUP)
    assert pet.experience == expected_exp
    assert pet.energy == 100 - expected_energy
    ledger = (
        db._get_connection()
        .execute("SELECT delta, reason FROM asset_ledger WHERE reason LIKE 'minigame_%'")
        .fetchone()
    )
    assert tuple(ledger) == (
        expected_coins,
        f"minigame_{'rock_paper_scissors' if game == 'rps' else game}",
    )
    db.cleanup()


@pytest.mark.parametrize(("earned", "actual"), [(500, 0), (497, 3)])
def test_minigame_clips_reward_to_latest_daily_capacity_and_reports_actual(
    tmp_path,
    monkeypatch,
    earned,
    actual,
):
    db = Database(str(tmp_path / f"cap-{earned}.db"))
    _seed(db)
    conn = db._get_connection()
    conn.execute(
        "UPDATE users SET today_coins_earned = ? WHERE user_id = 'player' AND group_id = ?",
        (earned, GROUP),
    )
    conn.commit()
    monkeypatch.setattr(social_module.random, "choice", lambda _choices: "scissors")

    ok, message = SocialService(db).play_rock_paper_scissors(
        "player", GROUP, "石头", message_id=f"cap-{earned}"
    )

    assert ok is True
    assert f"获得{actual}金币" in message
    assert db.get_user("player", GROUP).coins == 100 + actual
    ledger = conn.execute(
        "SELECT delta FROM asset_ledger WHERE reason = 'minigame_rock_paper_scissors'"
    ).fetchone()
    assert ledger["delta"] == actual
    assert db.get_pet("player", GROUP).experience == 5
    db.cleanup()


def test_race_insufficient_energy_does_not_consume_cooldown(tmp_path):
    db = Database(str(tmp_path / "no-energy.db"))
    _seed(db, energy=14)

    ok, message = SocialService(db).race_pet("player", "opponent", GROUP, message_id="no-energy")

    assert ok is False
    assert "精力不足" in message
    assert _state(db) == ((100, 0), (0, 14), 0, 0, 0)
    db.cleanup()


def test_service_retry_replays_identical_random_presentation(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "presentation.db"))
    _seed(db)
    rolls = iter((6, 1, 1, 6))
    monkeypatch.setattr(social_module.random, "randint", lambda _a, _b: next(rolls))
    service = SocialService(db)

    first = service.play_dice("player", GROUP, message_id="same-delivery")
    second = service.play_dice("player", GROUP, message_id="same-delivery")

    assert first == second
    assert _state(db) == ((120, 20), (8, 100), 1, 1, 1)
    db.cleanup()


def test_main_forwards_message_id_for_idempotent_minigame_replay(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "main.db"))
    _seed(db)
    rolls = iter((6, 1, 1, 6))
    monkeypatch.setattr(social_module.random, "randint", lambda _a, _b: next(rolls))
    original_db               = qingpet_main._db_instance
    original_router           = qingpet_main._router
    qingpet_main._db_instance = db
    qingpet_main._router      = None
    event                     = {"user_id": "player", "group_id": GROUP, "message_id": 7654321}
    try:
        first  = asyncio.run(qingpet_main.handle("pet", "游戏 骰子", event, None))
        second = asyncio.run(qingpet_main.handle("pet", "游戏 骰子", event, None))
    finally:
        qingpet_main._db_instance = original_db
        qingpet_main._router      = original_router

    assert first == second
    assert _state(db) == ((120, 20), (8, 100), 1, 1, 1)
    db.cleanup()
