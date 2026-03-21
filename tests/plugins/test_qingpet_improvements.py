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
