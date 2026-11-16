"""宠物交互、训练、探索、召回和状态展示测试。"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from plugins.qingpet.services import pet_service as pet_service_module
from plugins.qingpet.services.economy_service import EconomyService
from plugins.qingpet.services.pet_service import PetService
from plugins.qingpet.services.user_service import UserService
from plugins.qingpet.utils.constants import (
    DECAY_RATES,
    EXPLORE_LOCATIONS,
    TRAINING_CONFIG,
    TRAINING_MESSAGES,
    TRAINING_SPECIAL_EVENTS,
    PetPersonality,
    PetStage,
    PetStatus,
)
from plugins.qingpet.utils.formatters import format_pet_card
from plugins.qingpet.utils.time import utc_now


@pytest.fixture
def pet_and_user(qingpet_db):
    user_service = UserService(qingpet_db)
    user         = user_service.get_or_create_user("test_user", 123456)
    pet_service  = PetService(qingpet_db)
    pet_service.adopt_pet("test_user", 123456, "小花")
    pet = qingpet_db.get_pet("test_user", 123456)
    # upgrade to young so can_interact() works when status=NORMAL
    pet.stage  = PetStage.YOUNG
    pet.status = PetStatus.NORMAL
    qingpet_db.update_pet(pet)
    return pet, user


@pytest.mark.parametrize(
    ("status", "expected_words"),
    [
        pytest.param(PetStatus.SLEEPING, ("睡觉", "起床"), id="sleeping"),
        pytest.param(PetStatus.SICK, ("生病", "治疗"), id="sick"),
        pytest.param(PetStatus.TRAVELING, ("旅行",), id="traveling"),
    ],
)
def test_cannot_interact_message_names_the_blocking_status(
    pet_and_user, qingpet_db, status, expected_words
):
    pet, user = pet_and_user
    pet.status = status
    if status == PetStatus.TRAVELING:
        pet.status_expire_time = utc_now() + timedelta(hours=12)
    qingpet_db.update_pet(pet)
    pet_service = PetService(qingpet_db)
    success, msg, _ = pet_service.feed_pet(pet, user)
    assert not success
    assert any(word in msg for word in expected_words)


def test_recall_requires_only_coins(pet_and_user, qingpet_db):
    """召回只需金币，无友情点要求"""
    pet, user = pet_and_user
    pet.status = PetStatus.TRAVELING
    pet.status_expire_time = utc_now() + timedelta(hours=12)
    qingpet_db.update_pet(pet)

    user.coins             = 50
    user.friendship_points = 0  # no friendship points
    qingpet_db.update_user(user)
    economy_service = EconomyService(qingpet_db)
    assert economy_service.get_group_stats(123456)["ledger_status"] == "baseline_created"

    pet_service = PetService(qingpet_db)
    success, msg = pet_service.recall_pet(pet, user)
    assert success, f"召回失败: {msg}"
    assert "友情" not in msg
    assert "金币" in msg
    assert economy_service.get_group_stats(123456)["ledger_consistent"] is True


def test_recall_fails_without_coins(pet_and_user, qingpet_db):
    """金币不足时召回失败"""
    pet, user = pet_and_user
    pet.status = PetStatus.TRAVELING
    qingpet_db.update_pet(pet)
    user.coins = 10
    qingpet_db.update_user(user)

    pet_service = PetService(qingpet_db)
    success, msg = pet_service.recall_pet(pet, user)
    assert not success
    assert "金币" in msg


def test_apply_decay_travel_message_no_friendship(pet_and_user, qingpet_db, monkeypatch):
    """apply_decay 触发旅行的消息不包含友情点"""
    pet, _ = pet_and_user
    pet.hunger = 0
    pet.mood   = 0
    pet.clean  = 0
    pet.energy = 0
    pet.health = 0
    pet.status = PetStatus.NORMAL
    pet.last_update = utc_now() - timedelta(minutes=5)
    monkeypatch.setattr(pet_service_module.random, "random", lambda: 1.0)
    qingpet_db.update_pet(pet)

    pet_service = PetService(qingpet_db)
    result      = pet_service.apply_decay(pet)
    assert result and "旅行" in result
    assert "友情" not in result
    assert qingpet_db.get_pet(pet.user_id, pet.group_id).status == PetStatus.TRAVELING


@pytest.mark.parametrize("minutes", [0.5, 120])
def test_sleep_settles_pending_decay(pet_and_user, qingpet_db, monkeypatch, minutes):
    pet, _ = pet_and_user
    now = utc_now()
    pet.last_update = now - timedelta(minutes=minutes)
    qingpet_db.update_pet(pet)
    monkeypatch.setattr(pet_service_module, "utc_now", lambda: now)
    monkeypatch.setattr(pet_service_module.random, "random", lambda: 1.0)
    initial_hunger = pet.hunger
    success, _message = PetService(qingpet_db).sleep_pet(pet)
    assert success
    stored      = qingpet_db.get_pet(pet.user_id, pet.group_id)
    accumulated = DECAY_RATES["hunger"] * minutes
    assert stored.hunger == max(0, initial_hunger - int(accumulated + 1e-9))
    assert stored.decay_remainders["hunger"] == pytest.approx(accumulated % 1)
    assert stored.last_update == now
    assert stored.status == PetStatus.SLEEPING


@pytest.mark.parametrize("expiry_case", ["valid", "missing", "overlong"])
def test_sleep_expiry_wakes_pet_and_limits_energy_recovery(
    pet_and_user,
    qingpet_db,
    monkeypatch,
    expiry_case,
):
    """休眠只恢复一分钟精力；缺失或超长的 expiry 到期后同样会醒来。"""

    pet, _user = pet_and_user
    pet.hunger = 80
    pet.mood   = 80
    pet.clean  = 80
    pet.energy = 50
    pet.health = 80
    assert qingpet_db.update_pet(pet)

    current_time    = {"value": datetime(2035, 1, 2, 3, 4, 5)}
    pet.last_update = current_time["value"]
    assert qingpet_db.update_pet(pet)
    monkeypatch.setattr(
        pet_service_module,
        "utc_now",
        lambda: current_time["value"],
    )
    pet_service = PetService(qingpet_db)
    success, _message = pet_service.sleep_pet(pet)
    assert success is True
    if expiry_case == "missing":
        pet.status_expire_time = None
        assert qingpet_db.update_pet(pet)
    elif expiry_case == "overlong":
        pet.status_expire_time = current_time["value"] + timedelta(days=1)
        assert qingpet_db.update_pet(pet)

    current_time["value"] += timedelta(minutes=11)
    alert  = pet_service.apply_decay(pet)
    stored = qingpet_db.get_pet(pet.user_id, pet.group_id)

    assert stored is not None
    assert stored.status == PetStatus.NORMAL
    assert stored.status_expire_time is None
    assert stored.last_update == current_time["value"]
    assert (stored.hunger, stored.mood, stored.clean, stored.energy, stored.health) == (
        75,
        72,
        74,
        49,
        79,
    )
    assert alert is not None and "睡醒" in alert


def test_training_config_fields():
    for _key, cfg in TRAINING_CONFIG.items():
        assert "name" in cfg
        assert "exp_gain" in cfg
        assert "energy_cost" in cfg
        assert "success_rate_base" in cfg


def test_training_messages_have_success_and_fail():
    assert "success" in TRAINING_MESSAGES
    assert "fail" in TRAINING_MESSAGES
    assert len(TRAINING_MESSAGES["success"]) >= 2
    assert len(TRAINING_MESSAGES["fail"]) >= 2


def test_training_special_events_structure():
    for event in TRAINING_SPECIAL_EVENTS:
        assert "msg" in event
        assert "prob" in event


# ── Training service tests ──


@pytest.mark.parametrize(
    "extra_args",
    [
        pytest.param({}, id="default"),
        pytest.param({"training_type": "unknown_type"}, id="unknown-type"),
    ],
)
def test_train_defaults_to_strength(pet_and_user, qingpet_db, extra_args):
    pet, user = pet_and_user
    pet_service = PetService(qingpet_db)
    before_energy, before_exp = pet.energy, pet.experience
    with patch(
        "plugins.qingpet.services.pet_service.random.random",
        side_effect=[0.0] + [1.0] * len(TRAINING_SPECIAL_EVENTS),
    ):
        success, _message, _coins = pet_service.train_pet(pet, user, **extra_args)
    assert success
    assert pet.energy == before_energy - TRAINING_CONFIG["strength"]["energy_cost"]
    factor = 1.1 if pet.personality == PetPersonality.SMART else 1
    assert pet.experience == before_exp + int(TRAINING_CONFIG["strength"]["exp_gain"] * factor)
    assert qingpet_db.get_pet("test_user", 123456).energy == pet.energy


def test_train_agility_boosts_mood(pet_and_user, qingpet_db, monkeypatch):
    """敏捷训练成功时提升心情（多次重试排除随机失败）"""
    import random

    monkeypatch.setattr("plugins.qingpet.services.pet_service.random", random.Random(42))
    pet_service    = PetService(qingpet_db)
    mood_increased = False
    for _ in range(20):
        p            = qingpet_db.get_pet("test_user", 123456)
        p.energy     = 100
        p.mood       = 50
        p.last_train = None
        qingpet_db.update_pet(p)
        u = qingpet_db.get_user("test_user", 123456)
        success, msg, _ = pet_service.train_pet(p, u, training_type="agility")
        if success and any(t in msg for t in ["认真", "努力", "挥洒", "偷懒", "状态不佳", "分心"]):
            refreshed = qingpet_db.get_pet("test_user", 123456)
            if refreshed.mood > 50:
                mood_increased = True
                break
    assert mood_increased, "敏捷训练应该在成功时提升心情"


def test_train_message_contains_pet_name(pet_and_user, qingpet_db):
    pet, user = pet_and_user
    pet_service = PetService(qingpet_db)
    _, msg, _ = pet_service.train_pet(pet, user, training_type="strength")
    assert "小花" in msg


def test_train_smart_personality_exp_stacking(pet_and_user, qingpet_db):
    pet, user = pet_and_user
    pet.personality = PetPersonality.SMART
    qingpet_db.update_pet(pet)
    before = pet.experience
    with patch(
        "plugins.qingpet.services.pet_service.random.random",
        side_effect=[0.0] + [1.0] * len(TRAINING_SPECIAL_EVENTS),
    ):
        success, _message, _coins = PetService(qingpet_db).train_pet(
            pet, user, training_type="intellect"
        )
    assert success
    expected = before + int(TRAINING_CONFIG["intellect"]["exp_gain"] * 1.1)
    assert pet.experience == expected
    assert qingpet_db.get_pet("test_user", 123456).experience == expected


# ── Explore service tests ──


def test_explore_default_location_forest(pet_and_user, qingpet_db):
    pet, user = pet_and_user
    pet_service   = PetService(qingpet_db)
    before_energy = pet.energy
    success, msg, _ = pet_service.explore(pet, user)
    assert success
    assert EXPLORE_LOCATIONS["forest"]["name"] in msg
    assert pet.energy == before_energy - EXPLORE_LOCATIONS["forest"]["energy_cost"]


@pytest.mark.parametrize(
    ("location", "health"),
    [("cave", 30), ("ruins", 35)],
)
def test_explore_dangerous_location_blocked_on_low_health(
    pet_and_user, qingpet_db, location, health
):
    pet, user = pet_and_user
    pet.health = health
    qingpet_db.update_pet(pet)
    pet_service = PetService(qingpet_db)
    success, msg, _ = pet_service.explore(pet, user, location=location)
    assert not success
    assert "健康" in msg


def test_explore_cave_allowed_with_enough_health(pet_and_user, qingpet_db):
    pet, user = pet_and_user
    pet.health = 80
    qingpet_db.update_pet(pet)
    pet_service   = PetService(qingpet_db)
    before_energy = pet.energy
    success, msg, _ = pet_service.explore(pet, user, location="cave")
    assert success
    assert EXPLORE_LOCATIONS["cave"]["name"] in msg
    assert pet.energy == before_energy - EXPLORE_LOCATIONS["cave"]["energy_cost"]


def test_explore_invalid_location_falls_back_to_forest(pet_and_user, qingpet_db):
    pet, user = pet_and_user
    pet_service   = PetService(qingpet_db)
    before_energy = pet.energy
    success, msg, _ = pet_service.explore(pet, user, location="unknown")
    assert success
    assert EXPLORE_LOCATIONS["forest"]["name"] in msg
    assert pet.energy == before_energy - EXPLORE_LOCATIONS["forest"]["energy_cost"]


@pytest.mark.parametrize(
    ("personality", "location", "expected_weights", "chosen_index", "outcome"),
    [
        pytest.param(
            PetPersonality.SHY,
            "cave",
            [0.20, 0.20, 0.20, 0.35, 0.15],
            3,
            {
                "msg"   : "遭遇危险！受了点伤",
                "coins" : 5,
                "exp"   : 5,
                "health": 60,
                "item"  : None,
            },
            id="shy-cave-boosts-health-event",
        ),
        pytest.param(
            PetPersonality.SMART,
            "ruins",
            [0.20, 0.10, 0.30, 0.25, 0.25],
            0,
            {
                "msg"   : "捡到了神秘卡片！",
                "coins" : 10,
                "exp"   : 15,
                "health": 80,
                "item"  : "acceleration_card",
            },
            id="smart-ruins-boosts-item-event",
        ),
    ],
)
def test_explore_personality_boosts_matching_event_weights(
    pet_and_user,
    qingpet_db,
    monkeypatch,
    personality,
    location,
    expected_weights,
    chosen_index,
    outcome,
):
    pet, user = pet_and_user
    pet.personality = personality
    pet.health      = 80
    qingpet_db.update_pet(pet)
    before_exp    = pet.experience
    before_energy = pet.energy
    seen_weights  = []

    def pick_fixed_event(events, weights=None, k=1):
        seen_weights.append(list(weights))
        return [events[chosen_index]]

    monkeypatch.setattr(pet_service_module.random, "choices", pick_fixed_event)

    success, msg, coins = PetService(qingpet_db).explore(pet, user, location=location)

    assert success
    assert seen_weights == [pytest.approx(expected_weights)]
    assert msg.startswith(f"[{EXPLORE_LOCATIONS[location]['name']}] {outcome['msg']}")
    assert coins == outcome["coins"]
    assert pet.experience == before_exp + outcome["exp"]
    assert pet.energy == before_energy - EXPLORE_LOCATIONS[location]["energy_cost"]
    assert pet.health == outcome["health"]
    stored_inventory = qingpet_db.get_or_create_inventory("test_user", 123456)
    assert stored_inventory.items == ({outcome["item"]: 1} if outcome["item"] else {})


def test_explore_location_fields():
    for _loc_key, loc in EXPLORE_LOCATIONS.items():
        assert "name" in loc
        assert "energy_cost" in loc
        assert "events" in loc
        assert len(loc["events"]) >= 4


def test_explore_event_probabilities_sum_to_one():
    for loc_key, loc in EXPLORE_LOCATIONS.items():
        total = sum(e["prob"] for e in loc["events"])
        assert abs(total - 1.0) < 0.001, f"{loc_key} 概率之和={total}"


def test_format_pet_card_shows_travel_time(pet_and_user, qingpet_db):
    """旅行中的宠物卡片显示剩余的小时和分钟"""
    pet, user = pet_and_user
    pet.status = PetStatus.TRAVELING
    pet.status_expire_time = utc_now() + timedelta(hours=5, minutes=30)
    card = format_pet_card(pet, user)
    assert "旅行剩余" in card
    assert "小时" in card
    assert "分钟" in card


def test_format_pet_card_normal_no_travel_time(pet_and_user, qingpet_db):
    """正常状态不显示旅行剩余时间"""
    pet, user = pet_and_user
    pet.status             = PetStatus.NORMAL
    pet.status_expire_time = None
    card                   = format_pet_card(pet, user)
    assert "旅行剩余" not in card
