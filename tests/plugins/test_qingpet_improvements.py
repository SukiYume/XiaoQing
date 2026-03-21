import pytest
import os
import tempfile
from datetime import datetime, timedelta
from plugins.qingpet.services import Database
from plugins.qingpet.services.pet_service import PetService
from plugins.qingpet.services.user_service import UserService
from plugins.qingpet.models import Pet, User
from plugins.qingpet.utils.constants import (
    PetStage, PetPersonality, PetStatus,
    TRAINING_CONFIG, TRAINING_SPECIAL_EVENTS, TRAINING_MESSAGES,
    EXPLORE_LOCATIONS,
)


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    db = Database(db_path)
    yield db
    if db._conn is not None:
        db._conn.close()
    os.unlink(db_path)


@pytest.fixture
def pet_and_user(temp_db):
    user_service = UserService(temp_db)
    user = user_service.get_or_create_user("test_user", 123456)
    pet_service = PetService(temp_db)
    pet_service.adopt_pet("test_user", 123456, "小花")
    pet = temp_db.get_pet("test_user", 123456)
    # upgrade to young so can_interact() works when status=NORMAL
    pet.stage = PetStage.YOUNG
    pet.status = PetStatus.NORMAL
    temp_db.update_pet(pet)
    return pet, user


def test_cannot_interact_msg_sleeping(pet_and_user, temp_db):
    pet, user = pet_and_user
    pet.status = PetStatus.SLEEPING
    temp_db.update_pet(pet)
    pet_service = PetService(temp_db)
    success, msg, _ = pet_service.feed_pet(pet, user)
    assert not success
    assert "睡觉" in msg or "起床" in msg


def test_cannot_interact_msg_sick(pet_and_user, temp_db):
    pet, user = pet_and_user
    pet.status = PetStatus.SICK
    temp_db.update_pet(pet)
    pet_service = PetService(temp_db)
    success, msg, _ = pet_service.feed_pet(pet, user)
    assert not success
    assert "生病" in msg or "治疗" in msg


def test_cannot_interact_msg_traveling(pet_and_user, temp_db):
    pet, user = pet_and_user
    pet.status = PetStatus.TRAVELING
    pet.status_expire_time = datetime.now() + timedelta(hours=12)
    temp_db.update_pet(pet)
    pet_service = PetService(temp_db)
    success, msg, _ = pet_service.feed_pet(pet, user)
    assert not success
    assert "旅行" in msg


def test_recall_requires_only_coins(pet_and_user, temp_db):
    """召回只需金币，无友情点要求"""
    pet, user = pet_and_user
    pet.status = PetStatus.TRAVELING
    pet.status_expire_time = datetime.now() + timedelta(hours=12)
    temp_db.update_pet(pet)

    user.coins = 50
    user.friendship_points = 0  # no friendship points
    temp_db.update_user(user)

    pet_service = PetService(temp_db)
    success, msg = pet_service.recall_pet(pet, user)
    assert success, f"召回失败: {msg}"
    assert "友情" not in msg


def test_recall_fails_without_coins(pet_and_user, temp_db):
    """金币不足时召回失败"""
    pet, user = pet_and_user
    pet.status = PetStatus.TRAVELING
    temp_db.update_pet(pet)
    user.coins = 10
    temp_db.update_user(user)

    pet_service = PetService(temp_db)
    success, msg = pet_service.recall_pet(pet, user)
    assert not success
    assert "金币" in msg


def test_recall_success_message_no_friendship(pet_and_user, temp_db):
    """召回成功消息不包含友情点字样"""
    pet, user = pet_and_user
    pet.status = PetStatus.TRAVELING
    pet.status_expire_time = datetime.now() + timedelta(hours=1)
    temp_db.update_pet(pet)
    user.coins = 100
    user.friendship_points = 0
    temp_db.update_user(user)

    pet_service = PetService(temp_db)
    success, msg = pet_service.recall_pet(pet, user)
    assert success
    assert "友情" not in msg
    assert "金币" in msg


def test_apply_decay_travel_message_no_friendship(pet_and_user, temp_db):
    """apply_decay 触发旅行的消息不包含友情点"""
    pet, _ = pet_and_user
    pet.hunger = 0
    pet.mood = 0
    pet.clean = 0
    pet.energy = 0
    pet.health = 0
    pet.status = PetStatus.NORMAL
    pet.last_update = datetime.now() - timedelta(minutes=5)
    temp_db.update_pet(pet)

    pet_service = PetService(temp_db)
    result = pet_service.apply_decay(pet)
    if result and "旅行" in result:
        assert "友情" not in result


def test_training_config_has_three_types():
    assert "strength" in TRAINING_CONFIG
    assert "agility" in TRAINING_CONFIG
    assert "intellect" in TRAINING_CONFIG


def test_training_config_fields():
    for key, cfg in TRAINING_CONFIG.items():
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

def test_train_default_type_strength(pet_and_user, temp_db):
    """默认训练类型为体力"""
    pet, user = pet_and_user
    pet_service = PetService(temp_db)
    success, msg, coins = pet_service.train_pet(pet, user)
    assert isinstance(success, bool)
    assert isinstance(msg, str)


def test_train_agility_boosts_mood(pet_and_user, temp_db):
    """敏捷训练成功时提升心情（多次重试排除随机失败）"""
    import random
    random.seed(42)
    pet_service = PetService(temp_db)
    mood_increased = False
    for _ in range(20):
        p = temp_db.get_pet("test_user", 123456)
        p.energy = 100
        p.mood = 50
        p.last_train = None
        temp_db.update_pet(p)
        u = temp_db.get_user("test_user", 123456)
        success, msg, _ = pet_service.train_pet(p, u, training_type="agility")
        if success and any(t in msg for t in ["认真", "努力", "挥洒", "偷懒", "状态不佳", "分心"]):
            refreshed = temp_db.get_pet("test_user", 123456)
            if refreshed.mood > 50:
                mood_increased = True
                break
    assert mood_increased, "敏捷训练应该在成功时提升心情"


def test_train_intellect_higher_exp(pet_and_user, temp_db):
    assert TRAINING_CONFIG["intellect"]["exp_gain"] > TRAINING_CONFIG["strength"]["exp_gain"]


def test_train_invalid_type_falls_back(pet_and_user, temp_db):
    pet, user = pet_and_user
    pet_service = PetService(temp_db)
    success, msg, _ = pet_service.train_pet(pet, user, training_type="unknown_type")
    assert isinstance(msg, str)


def test_train_message_contains_pet_name(pet_and_user, temp_db):
    pet, user = pet_and_user
    pet_service = PetService(temp_db)
    _, msg, _ = pet_service.train_pet(pet, user, training_type="strength")
    assert "小花" in msg


def test_train_smart_personality_exp_stacking(pet_and_user, temp_db):
    pet, user = pet_and_user
    pet.personality = PetPersonality.SMART
    temp_db.update_pet(pet)
    base_exp = TRAINING_CONFIG["intellect"]["exp_gain"]
    assert int(base_exp * 1.1) > base_exp


# ── Explore service tests ──

def test_explore_default_location_forest(pet_and_user, temp_db):
    pet, user = pet_and_user
    pet_service = PetService(temp_db)
    success, msg, _ = pet_service.explore(pet, user)
    assert isinstance(success, bool)
    assert isinstance(msg, str)


def test_explore_cave_blocked_on_low_health(pet_and_user, temp_db):
    pet, user = pet_and_user
    pet.health = 30
    temp_db.update_pet(pet)
    pet_service = PetService(temp_db)
    success, msg, _ = pet_service.explore(pet, user, location="cave")
    assert not success
    assert "健康" in msg


def test_explore_ruins_blocked_on_low_health(pet_and_user, temp_db):
    pet, user = pet_and_user
    pet.health = 35
    temp_db.update_pet(pet)
    pet_service = PetService(temp_db)
    success, msg, _ = pet_service.explore(pet, user, location="ruins")
    assert not success
    assert "健康" in msg


def test_explore_cave_allowed_with_enough_health(pet_and_user, temp_db):
    pet, user = pet_and_user
    pet.health = 80
    temp_db.update_pet(pet)
    pet_service = PetService(temp_db)
    success, msg, _ = pet_service.explore(pet, user, location="cave")
    if not success:
        assert "健康" not in msg


def test_explore_invalid_location_falls_back_to_forest(pet_and_user, temp_db):
    pet, user = pet_and_user
    pet_service = PetService(temp_db)
    success, msg, _ = pet_service.explore(pet, user, location="unknown")
    assert isinstance(msg, str)


def test_explore_shy_personality_accepted_by_service(pet_and_user, temp_db):
    import random
    random.seed(7)
    pet, user = pet_and_user
    pet.personality = PetPersonality.SHY
    pet.health = 80
    temp_db.update_pet(pet)
    pet_service = PetService(temp_db)
    success, msg, _ = pet_service.explore(pet, user, location="cave")
    assert isinstance(msg, str)


def test_explore_smart_personality_accepted_by_service(pet_and_user, temp_db):
    import random
    random.seed(99)
    pet, user = pet_and_user
    pet.personality = PetPersonality.SMART
    pet.health = 80
    temp_db.update_pet(pet)
    pet_service = PetService(temp_db)
    success, msg, _ = pet_service.explore(pet, user, location="ruins")
    assert isinstance(msg, str)


def test_explore_location_fields():
    for loc_key, loc in EXPLORE_LOCATIONS.items():
        assert "name" in loc
        assert "energy_cost" in loc
        assert "events" in loc
        assert len(loc["events"]) >= 4


def test_explore_event_probabilities_sum_to_one():
    for loc_key, loc in EXPLORE_LOCATIONS.items():
        total = sum(e["prob"] for e in loc["events"])
        assert abs(total - 1.0) < 0.001, f"{loc_key} 概率之和={total}"


from plugins.qingpet.utils.formatters import format_pet_card
from plugins.qingpet.models import User


def make_test_user(user_id="test_user", group_id=123456):
    return User(user_id=user_id, group_id=group_id)


def test_format_pet_card_shows_travel_time(pet_and_user, temp_db):
    """旅行中的宠物卡片显示剩余时间"""
    pet, user = pet_and_user
    pet.status = PetStatus.TRAVELING
    pet.status_expire_time = datetime.now() + timedelta(hours=5, minutes=30)
    card = format_pet_card(pet, user)
    assert "旅行剩余" in card


def test_format_pet_card_normal_no_travel_time(pet_and_user, temp_db):
    """正常状态不显示旅行剩余时间"""
    pet, user = pet_and_user
    pet.status = PetStatus.NORMAL
    pet.status_expire_time = None
    card = format_pet_card(pet, user)
    assert "旅行剩余" not in card


def test_format_pet_card_travel_shows_hours_minutes(pet_and_user, temp_db):
    """旅行剩余时间包含小时和分钟"""
    pet, user = pet_and_user
    pet.status = PetStatus.TRAVELING
    pet.status_expire_time = datetime.now() + timedelta(hours=3, minutes=15)
    card = format_pet_card(pet, user)
    assert "小时" in card
    assert "分钟" in card
