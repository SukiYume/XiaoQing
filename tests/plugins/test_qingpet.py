import os
import tempfile
from datetime import datetime, timedelta

import pytest

from plugins.qingpet.services import Database
from plugins.qingpet.services.admin_service import AdminService
from plugins.qingpet.services.item_service import ItemService
from plugins.qingpet.services.pet_service import PetService
from plugins.qingpet.services.social_service import SocialService
from plugins.qingpet.services.user_service import UserService
from plugins.qingpet.utils.constants import PetStatus


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    db = Database(db_path)
    yield db
    if db._conn is not None:
        db._conn.close()
    os.unlink(db_path)

def test_database_initialization(temp_db):
    assert temp_db is not None
    assert os.path.exists(temp_db.db_path)

def test_user_creation(temp_db):
    user_service = UserService(temp_db)
    user = user_service.get_or_create_user("test_user", 123456)
    assert user.user_id == "test_user"
    assert user.group_id == 123456
    assert user.coins == 100

def test_pet_adopt(temp_db):
    user_service = UserService(temp_db)
    user_service.get_or_create_user("test_user", 123456)

    pet_service = PetService(temp_db)
    success, message = pet_service.adopt_pet("test_user", 123456, "小白")
    assert success
    assert "小白" in message

def test_pet_feed(temp_db):
    user_service = UserService(temp_db)
    user = user_service.get_or_create_user("test_user", 123456)

    pet_service = PetService(temp_db)
    pet_service.adopt_pet("test_user", 123456, "小白")

    pet = temp_db.get_pet("test_user", 123456)
    success, message, coins = pet_service.feed_pet(pet, user, "apple")
    assert success
    assert coins > 0

def test_pet_clean(temp_db):
    user_service = UserService(temp_db)
    user = user_service.get_or_create_user("test_user", 123456)

    pet_service = PetService(temp_db)
    pet_service.adopt_pet("test_user", 123456, "小白")

    pet = temp_db.get_pet("test_user", 123456)
    success, message, coins = pet_service.clean_pet(pet, user)
    assert success

def test_pet_play(temp_db):
    user_service = UserService(temp_db)
    user = user_service.get_or_create_user("test_user", 123456)

    pet_service = PetService(temp_db)
    pet_service.adopt_pet("test_user", 123456, "小白")

    pet = temp_db.get_pet("test_user", 123456)
    success, message, coins = pet_service.play_with_pet(pet, user)
    assert success
    assert pet.intimacy > 0

def test_pet_sleep_and_wake(temp_db):
    pet_service = PetService(temp_db)
    pet_service.adopt_pet("test_user", 123456, "小白")

    pet = temp_db.get_pet("test_user", 123456)

    success, message = pet_service.sleep_pet(pet)
    assert success
    assert "睡觉" in message

    success, message = pet_service.wake_pet(pet)
    assert success
    assert "睡醒" in message

def test_pet_train(temp_db):
    user_service = UserService(temp_db)
    user = user_service.get_or_create_user("test_user", 123456)

    pet_service = PetService(temp_db)
    pet_service.adopt_pet("test_user", 123456, "小白")

    pet = temp_db.get_pet("test_user", 123456)
    pet.energy = 50

    success, message, coins = pet_service.train_pet(pet, user)
    assert success

def test_pet_explore(temp_db):
    user_service = UserService(temp_db)
    user = user_service.get_or_create_user("test_user", 123456)

    pet_service = PetService(temp_db)
    pet_service.adopt_pet("test_user", 123456, "小白")

    pet = temp_db.get_pet("test_user", 123456)
    pet.energy = 50

    success, message, coins = pet_service.explore(pet, user)
    assert success

def test_item_service(temp_db):
    item_service = ItemService(temp_db)
    user_service = UserService(temp_db)
    _user = user_service.get_or_create_user("test_user", 123456)

    success, message = item_service.buy_item("test_user", 123456, "apple", 5)
    assert success

    inventory = item_service.get_inventory("test_user", 123456)
    assert inventory.get_item_count("apple") == 5

def test_social_visit(temp_db):
    user_service = UserService(temp_db)
    user_service.get_or_create_user("visitor", 123456)
    user_service.get_or_create_user("owner", 123456)

    pet_service = PetService(temp_db)
    pet_service.adopt_pet("owner", 123456, "小白")

    social_service = SocialService(temp_db)
    success, message = social_service.visit_pet("visitor", "owner", 123456)
    assert success

def test_social_ranking(temp_db):
    pet_service = PetService(temp_db)
    user_service = UserService(temp_db)

    for i in range(5):
        user_id = f"user{i}"
        user_service.get_or_create_user(user_id, 123456)
        pet_service.adopt_pet(user_id, 123456, f"宠物{i}")

    social_service = SocialService(temp_db)
    ranking = social_service.get_ranking(123456, "care_score", 10)
    assert len(ranking) == 5

def test_admin_enable_disable(temp_db):
    admin_service = AdminService(temp_db)

    success = admin_service.disable_plugin(123456)
    assert success

    config = admin_service.get_config(123456)
    assert not config.enabled

    success = admin_service.enable_plugin(123456)
    assert success

    config = admin_service.get_config(123456)
    assert config.enabled

def test_admin_config(temp_db):
    admin_service = AdminService(temp_db)

    success = admin_service.set_config(123456, "economy_multiplier", "2.0")
    assert success

    config = admin_service.get_config(123456)
    assert config.economy_multiplier == 2.0

def test_pet_decay(temp_db):
    from datetime import datetime, timedelta
    pet_service = PetService(temp_db)
    pet_service.adopt_pet("test_user", 123456, "小白")

    pet = temp_db.get_pet("test_user", 123456)
    # Set last_update to 10 minutes ago so decay will apply (>1 min threshold)
    pet.last_update = datetime.now() - timedelta(minutes=10)
    temp_db.update_pet(pet)

    pet = temp_db.get_pet("test_user", 123456)
    initial_hunger = pet.hunger

    # apply_decay returns Optional[str] (alert message or None), not bool
    pet_service.apply_decay(pet, 1.0)

    pet = temp_db.get_pet("test_user", 123456)
    assert pet.hunger < initial_hunger

def test_user_daily_reset(temp_db):
    user_service = UserService(temp_db)
    user = user_service.get_or_create_user("test_user", 123456)

    user.today_coins_earned = 100
    user.today_feed_count = 5
    user_service.update_user(user)

    count = user_service.reset_daily(123456)
    assert count >= 1

    user = temp_db.get_user("test_user", 123456)
    assert user.today_coins_earned == 0
    assert user.today_feed_count == 0


def test_safe_add_column_rejects_invalid_identifiers_without_executing_sql():
    class _DummyCursor:
        def __init__(self):
            self.called = False

        def execute(self, sql):
            self.called = True

    cursor = _DummyCursor()
    Database._safe_add_column(cursor, "users;DROP TABLE users", "hacked", "INTEGER DEFAULT 0")
    assert cursor.called is False


def test_admin_reset_clears_pet_cooldowns_and_social_cooldowns(temp_db):
    user_service = UserService(temp_db)
    user_service.get_or_create_user("test_user", 123456)

    pet_service = PetService(temp_db)
    pet_service.adopt_pet("test_user", 123456, "小白")

    pet = temp_db.get_pet("test_user", 123456)
    pet.last_feed = datetime.now() - timedelta(minutes=1)
    pet.last_clean = datetime.now() - timedelta(minutes=1)
    pet.last_play = datetime.now() - timedelta(minutes=1)
    pet.last_train = datetime.now() - timedelta(minutes=1)
    pet.last_explore = datetime.now() - timedelta(minutes=1)
    pet.status = PetStatus.SICK
    pet.status_expire_time = datetime.now() + timedelta(hours=1)
    temp_db.update_pet(pet)

    user = temp_db.get_user("test_user", 123456)
    user.last_visit_time = datetime.now() - timedelta(minutes=1)
    user.last_gift_time = datetime.now() - timedelta(minutes=1)
    user_service.update_user(user)

    admin_service = AdminService(temp_db)
    assert admin_service.reset_user_pet("test_user", 123456) is True

    reset_pet = temp_db.get_pet("test_user", 123456)
    reset_user = temp_db.get_user("test_user", 123456)

    assert reset_pet.status == PetStatus.NORMAL
    assert reset_pet.status_expire_time is None
    assert reset_pet.last_feed is None
    assert reset_pet.last_clean is None
    assert reset_pet.last_play is None
    assert reset_pet.last_train is None
    assert reset_pet.last_explore is None
    assert reset_user.last_visit_time is None
    assert reset_user.last_gift_time is None


def test_resolve_pet_for_self_command_rejects_disabled_group_in_private(temp_db):
    from plugins.qingpet.commands.basic_commands import resolve_pet_for_self_command

    user_service = UserService(temp_db)
    user_service.get_or_create_user("test_user", 123456)
    pet_service = PetService(temp_db)
    pet_service.adopt_pet("test_user", 123456, "小白")

    admin_service = AdminService(temp_db)
    assert admin_service.disable_plugin(123456) is True

    pet, resolved_group_id, _, err = resolve_pet_for_self_command(
        temp_db,
        "test_user",
        0,
        "123456",
        "状态",
    )

    assert pet is None
    assert resolved_group_id == 123456
    assert "尚未启用" in err


@pytest.mark.asyncio
async def test_trade_cancel_returns_item_to_listing_group_inventory(temp_db):
    from plugins.qingpet.commands.new_commands import _trade_cancel, _trade_sell

    inventory = temp_db.get_or_create_inventory("seller", 123456)
    inventory.add_item("apple", 2)
    temp_db.update_inventory(inventory)

    ok, _ = _trade_sell("seller", 123456, "apple 2 20", temp_db)
    assert ok is True

    ok, _ = _trade_cancel("seller", 999999, "1", temp_db)
    assert ok is False

    ok, _ = _trade_cancel("seller", 123456, "1", temp_db)
    assert ok is True

    listing_group_inventory = temp_db.get_or_create_inventory("seller", 123456)
    other_group_inventory = temp_db.get_or_create_inventory("seller", 999999)
    assert listing_group_inventory.get_item_count("apple") == 2
    assert other_group_inventory.get_item_count("apple") == 0


def test_purchase_trade_listing_updates_users_and_inventory(temp_db):
    user_service = UserService(temp_db)
    seller = user_service.get_or_create_user("seller", 123456)
    buyer = user_service.get_or_create_user("buyer", 123456)
    seller.coins = 100
    buyer.coins = 200
    temp_db.update_user(seller)
    temp_db.update_user(buyer)

    inventory = temp_db.get_or_create_inventory("seller", 123456)
    inventory.add_item("apple", 3)
    temp_db.update_inventory(inventory)
    inventory.remove_item("apple", 3)
    temp_db.update_inventory(inventory)
    assert temp_db.create_trade_listing("seller", 123456, "apple", 3, 90) is True

    success, result = temp_db.purchase_trade_listing(1, "buyer", 123456, 0.05)

    assert success is True
    assert result["tax"] == 4
    assert temp_db.get_user("buyer", 123456).coins == 110
    assert temp_db.get_user("seller", 123456).coins == 186
    assert temp_db.get_or_create_inventory("buyer", 123456).get_item_count("apple") == 3
    assert temp_db.get_listing_by_id(1) is None


def test_pet_show_votes_allow_multiple_votes_per_user(temp_db):
    show_id = temp_db.create_pet_show(123456, "春季展示会", 24)
    assert show_id is not None

    assert temp_db.vote_pet_show(show_id, "voter", "pet_a") is True
    assert temp_db.vote_pet_show(show_id, "voter", "pet_b") is True

    assert temp_db.get_user_vote_count(show_id, "voter") == 2
    votes = temp_db.get_pet_show_votes(show_id)
    assert votes["pet_a"] == 1
    assert votes["pet_b"] == 1


def test_admin_ban_user_logs_operator_id(temp_db):
    user_service = UserService(temp_db)
    user_service.get_or_create_user("target", 123456)

    admin_service = AdminService(temp_db)
    assert admin_service.ban_user("target", 123456, 3, operator_user_id="operator") is True

    logs = admin_service.get_logs(123456, limit=5)
    assert logs
    assert logs[0].user_id == "operator"
