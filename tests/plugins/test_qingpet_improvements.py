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
