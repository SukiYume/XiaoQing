import os
from datetime import datetime, timedelta

import pytest

from plugins.qingpet.services.admin_service import AdminService
from plugins.qingpet.services.database import Database, MinigameOutcome
from plugins.qingpet.services.economy_service import EconomyService
from plugins.qingpet.services.item_service import ItemService
from plugins.qingpet.services.pet_service import PetService
from plugins.qingpet.services.social_service import SocialService
from plugins.qingpet.services.user_service import UserService
from plugins.qingpet.utils.constants import PetStatus


def test_database_initialization(qingpet_db):
    assert qingpet_db is not None
    assert os.path.exists(qingpet_db.db_path)


def test_user_creation(qingpet_db):
    user_service = UserService(qingpet_db)
    user = user_service.get_or_create_user("test_user", 123456)
    assert user.user_id == "test_user"
    assert user.group_id == 123456
    assert user.coins == 100


def test_group_economy_stats_use_constant_query_count(qingpet_db):
    user_service = UserService(qingpet_db)
    pet_service = PetService(qingpet_db)
    for index in range(4):
        user_id = f"stats-user-{index}"
        user_service.get_or_create_user(user_id, 123456)
        pet_service.adopt_pet(user_id, 123456, f"统计宠{index}")

    statements: list[str] = []
    connection = qingpet_db._get_connection()
    connection.set_trace_callback(statements.append)
    try:
        stats = EconomyService(qingpet_db).get_group_stats(123456)
    finally:
        connection.set_trace_callback(None)

    selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(("SELECT", "WITH"))
    ]
    assert len(selects) == 3
    assert stats["total_pets"] == 4
    assert stats["total_coins"] == 400
    assert stats["ledger_status"] == "baseline_created"
    assert stats["ledger_consistent"] is True


def test_group_economy_stats_detect_unledgered_balance_drift(qingpet_db):
    user_service = UserService(qingpet_db)
    pet_service = PetService(qingpet_db)
    user_service.get_or_create_user("ledger-user", 123456)
    pet_service.adopt_pet("ledger-user", 123456, "账本宠")
    service = EconomyService(qingpet_db)

    baseline = service.get_group_stats(123456)
    settlement = qingpet_db.settle_minigame_atomic(
        "ledger-user",
        123456,
        "reconciliation_test",
        reference_id="reconciliation-test:credit",
        daily_coin_limit=500,
        cooldown_seconds=0,
        outcome_factory=lambda _pet, _opponent: MinigameOutcome(requested_coins=25),
    )
    consistent = service.get_group_stats(123456)
    user = qingpet_db.get_user("ledger-user", 123456)
    user.coins -= 10
    assert qingpet_db.update_user(user) is True
    after_user_update = service.get_group_stats(123456)
    bought_item, _ = qingpet_db.purchase_item_atomic("ledger-user", 123456, "apple", 1, 5)
    after_item_purchase = service.get_group_stats(123456)
    bought_dress, _ = qingpet_db.purchase_dress_atomic(
        "ledger-user", 123456, "red_hat", "coins", 50
    )
    after_dress_purchase = service.get_group_stats(123456)
    connection = qingpet_db._get_connection()
    connection.execute(
        "UPDATE users SET coins = coins + 7 WHERE user_id = ? AND group_id = ?",
        ("ledger-user", 123456),
    )
    connection.commit()
    mismatched = service.get_group_stats(123456)

    assert baseline["ledger_status"] == "baseline_created"
    assert settlement.success is True
    assert settlement.coin_grant == 25
    assert consistent["total_coins"] == 125
    assert consistent["ledger_status"] == "consistent"
    assert consistent["ledger_difference"] == 0
    assert after_user_update["total_coins"] == 115
    assert after_user_update["ledger_consistent"] is True
    assert bought_item is True
    assert after_item_purchase["total_coins"] == 110
    assert after_item_purchase["ledger_consistent"] is True
    assert bought_dress is True
    assert after_dress_purchase["total_coins"] == 60
    assert after_dress_purchase["ledger_consistent"] is True
    assert mismatched["total_coins"] == 67
    assert mismatched["ledger_expected_coins"] == 60
    assert mismatched["ledger_difference"] == 7
    assert mismatched["ledger_consistent"] is False
    assert "⚠️ 金币账本" in service.format_stats(123456)


def test_pet_adopt(qingpet_db):
    user_service = UserService(qingpet_db)
    user_service.get_or_create_user("test_user", 123456)

    pet_service = PetService(qingpet_db)
    success, message = pet_service.adopt_pet("test_user", 123456, "小白")
    assert success
    assert "小白" in message


def test_pet_feed(qingpet_db):
    user_service = UserService(qingpet_db)
    user = user_service.get_or_create_user("test_user", 123456)

    pet_service = PetService(qingpet_db)
    pet_service.adopt_pet("test_user", 123456, "小白")

    pet = qingpet_db.get_pet("test_user", 123456)
    success, _message, coins = pet_service.feed_pet(pet, user, "apple")
    assert success
    assert coins > 0


def test_pet_clean(qingpet_db):
    user_service = UserService(qingpet_db)
    user = user_service.get_or_create_user("test_user", 123456)

    pet_service = PetService(qingpet_db)
    pet_service.adopt_pet("test_user", 123456, "小白")

    pet = qingpet_db.get_pet("test_user", 123456)
    success, _message, _coins = pet_service.clean_pet(pet, user)
    assert success


def test_pet_play(qingpet_db):
    user_service = UserService(qingpet_db)
    user = user_service.get_or_create_user("test_user", 123456)

    pet_service = PetService(qingpet_db)
    pet_service.adopt_pet("test_user", 123456, "小白")

    pet = qingpet_db.get_pet("test_user", 123456)
    success, _message, _coins = pet_service.play_with_pet(pet, user)
    assert success
    assert pet.intimacy > 0


def test_pet_sleep_and_wake(qingpet_db):
    pet_service = PetService(qingpet_db)
    pet_service.adopt_pet("test_user", 123456, "小白")

    pet = qingpet_db.get_pet("test_user", 123456)

    success, message = pet_service.sleep_pet(pet)
    assert success
    assert "睡觉" in message

    success, message = pet_service.wake_pet(pet)
    assert success
    assert "睡醒" in message


def test_pet_train(qingpet_db):
    user_service = UserService(qingpet_db)
    user = user_service.get_or_create_user("test_user", 123456)

    pet_service = PetService(qingpet_db)
    pet_service.adopt_pet("test_user", 123456, "小白")

    pet = qingpet_db.get_pet("test_user", 123456)
    pet.energy = 50

    success, _message, _coins = pet_service.train_pet(pet, user)
    assert success


def test_pet_explore(qingpet_db):
    user_service = UserService(qingpet_db)
    user = user_service.get_or_create_user("test_user", 123456)

    pet_service = PetService(qingpet_db)
    pet_service.adopt_pet("test_user", 123456, "小白")

    pet = qingpet_db.get_pet("test_user", 123456)
    pet.energy = 50

    success, _message, _coins = pet_service.explore(pet, user)
    assert success


def test_item_service(qingpet_db):
    item_service = ItemService(qingpet_db)
    user_service = UserService(qingpet_db)
    _user = user_service.get_or_create_user("test_user", 123456)

    success, _message = item_service.buy_item("test_user", 123456, "apple", 5)
    assert success

    inventory = item_service.get_inventory("test_user", 123456)
    assert inventory.get_item_count("apple") == 5


def test_social_visit(qingpet_db):
    user_service = UserService(qingpet_db)
    user_service.get_or_create_user("visitor", 123456)
    user_service.get_or_create_user("owner", 123456)

    pet_service = PetService(qingpet_db)
    pet_service.adopt_pet("owner", 123456, "小白")

    social_service = SocialService(qingpet_db)
    success, _message = social_service.visit_pet("visitor", "owner", 123456)
    assert success


def test_social_ranking(qingpet_db):
    pet_service = PetService(qingpet_db)
    user_service = UserService(qingpet_db)

    for i in range(5):
        user_id = f"user{i}"
        user_service.get_or_create_user(user_id, 123456)
        pet_service.adopt_pet(user_id, 123456, f"宠物{i}")

    social_service = SocialService(qingpet_db)
    ranking = social_service.get_ranking(123456, "care_score", 10)
    assert len(ranking) == 5


def test_admin_enable_disable(qingpet_db):
    admin_service = AdminService(qingpet_db)

    success = admin_service.disable_plugin(123456)
    assert success

    config = admin_service.get_config(123456)
    assert not config.enabled

    success = admin_service.enable_plugin(123456)
    assert success

    config = admin_service.get_config(123456)
    assert config.enabled


def test_admin_config(qingpet_db):
    admin_service = AdminService(qingpet_db)

    success = admin_service.set_config(123456, "economy_multiplier", "2.0")
    assert success

    config = admin_service.get_config(123456)
    assert config.economy_multiplier == 2.0


def test_pet_decay(qingpet_db):
    from datetime import datetime, timedelta

    pet_service = PetService(qingpet_db)
    pet_service.adopt_pet("test_user", 123456, "小白")

    pet = qingpet_db.get_pet("test_user", 123456)
    # Set last_update to 10 minutes ago so decay will apply (>1 min threshold)
    pet.last_update = datetime.now() - timedelta(minutes=10)
    qingpet_db.update_pet(pet)

    pet = qingpet_db.get_pet("test_user", 123456)
    initial_hunger = pet.hunger

    # apply_decay returns Optional[str] (alert message or None), not bool
    pet_service.apply_decay(pet, 1.0)

    pet = qingpet_db.get_pet("test_user", 123456)
    assert pet.hunger < initial_hunger


def test_user_daily_reset(qingpet_db):
    user_service = UserService(qingpet_db)
    user = user_service.get_or_create_user("test_user", 123456)

    user.today_coins_earned = 100
    user.today_feed_count = 5
    qingpet_db.update_user(user)

    result = qingpet_db.run_daily_reset_atomic("test-user-daily-reset:123456", 123456)
    assert result is not None
    assert result.users_reset >= 1

    user = qingpet_db.get_user("test_user", 123456)
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


def test_admin_reset_clears_pet_cooldowns_and_social_cooldowns(qingpet_db):
    user_service = UserService(qingpet_db)
    user_service.get_or_create_user("test_user", 123456)

    pet_service = PetService(qingpet_db)
    pet_service.adopt_pet("test_user", 123456, "小白")

    pet = qingpet_db.get_pet("test_user", 123456)
    pet.last_feed = datetime.now() - timedelta(minutes=1)
    pet.last_clean = datetime.now() - timedelta(minutes=1)
    pet.last_play = datetime.now() - timedelta(minutes=1)
    pet.last_train = datetime.now() - timedelta(minutes=1)
    pet.last_explore = datetime.now() - timedelta(minutes=1)
    pet.status = PetStatus.SICK
    pet.status_expire_time = datetime.now() + timedelta(hours=1)
    qingpet_db.update_pet(pet)

    user = qingpet_db.get_user("test_user", 123456)
    user.last_visit_time = datetime.now() - timedelta(minutes=1)
    user.last_gift_time = datetime.now() - timedelta(minutes=1)
    qingpet_db.update_user(user)

    admin_service = AdminService(qingpet_db)
    assert admin_service.reset_user_pet("test_user", 123456) is True

    reset_pet = qingpet_db.get_pet("test_user", 123456)
    reset_user = qingpet_db.get_user("test_user", 123456)

    assert reset_pet.status == PetStatus.NORMAL
    assert reset_pet.status_expire_time is None
    assert reset_pet.last_feed is None
    assert reset_pet.last_clean is None
    assert reset_pet.last_play is None
    assert reset_pet.last_train is None
    assert reset_pet.last_explore is None
    assert reset_user.last_visit_time is None
    assert reset_user.last_gift_time is None


def test_resolve_pet_for_self_command_rejects_disabled_group_in_private(qingpet_db):
    from plugins.qingpet.commands.basic_commands import resolve_pet_for_self_command

    user_service = UserService(qingpet_db)
    user_service.get_or_create_user("test_user", 123456)
    pet_service = PetService(qingpet_db)
    pet_service.adopt_pet("test_user", 123456, "小白")

    admin_service = AdminService(qingpet_db)
    assert admin_service.disable_plugin(123456) is True

    pet, resolved_group_id, _, err = resolve_pet_for_self_command(
        qingpet_db,
        "test_user",
        0,
        "123456",
        "状态",
    )

    assert pet is None
    assert resolved_group_id == 123456
    assert "尚未启用" in err


@pytest.mark.asyncio
async def test_trade_cancel_returns_item_to_listing_group_inventory(qingpet_db):
    from plugins.qingpet.commands.new_commands import _trade_cancel, _trade_sell

    UserService(qingpet_db).get_or_create_user("seller", 123456)
    assert qingpet_db.purchase_item_atomic("seller", 123456, "apple", 2, 0)[0] is True

    ok, _ = _trade_sell("seller", 123456, "apple 2 20", qingpet_db)
    assert ok is True

    ok, _ = _trade_cancel("seller", 999999, "1", qingpet_db)
    assert ok is False

    ok, _ = _trade_cancel("seller", 123456, "1", qingpet_db)
    assert ok is True

    listing_group_inventory = qingpet_db.get_or_create_inventory("seller", 123456)
    other_group_inventory = qingpet_db.get_or_create_inventory("seller", 999999)
    assert listing_group_inventory.get_item_count("apple") == 2
    assert other_group_inventory.get_item_count("apple") == 0


def test_purchase_trade_listing_updates_users_and_inventory(qingpet_db):
    user_service = UserService(qingpet_db)
    seller = user_service.get_or_create_user("seller", 123456)
    buyer = user_service.get_or_create_user("buyer", 123456)
    seller.coins = 100
    buyer.coins = 200
    qingpet_db.update_user(seller)
    qingpet_db.update_user(buyer)
    pet_service = PetService(qingpet_db)
    assert pet_service.adopt_pet("seller", 123456, "卖家宠")[0] is True
    assert pet_service.adopt_pet("buyer", 123456, "买家宠")[0] is True
    economy_service = EconomyService(qingpet_db)
    assert economy_service.get_group_stats(123456)["ledger_status"] == "baseline_created"

    assert qingpet_db.purchase_item_atomic("seller", 123456, "apple", 3, 0)[0] is True
    assert qingpet_db.create_trade_listing_atomic("seller", 123456, "apple", 3, 90, 72, 10) is True

    success, result = qingpet_db.purchase_trade_listing(1, "buyer", 123456, 0.05)

    assert success is True
    assert result["tax"] == 4
    assert qingpet_db.get_user("buyer", 123456).coins == 110
    assert qingpet_db.get_user("seller", 123456).coins == 186
    assert qingpet_db.get_or_create_inventory("buyer", 123456).get_item_count("apple") == 3
    assert qingpet_db.get_listing_by_id(1) is None
    stats = economy_service.get_group_stats(123456)
    assert stats["total_coins"] == 296
    assert stats["ledger_consistent"] is True


def test_pet_show_votes_allow_multiple_votes_per_user(qingpet_db):
    user_service = UserService(qingpet_db)
    pet_service = PetService(qingpet_db)
    user_service.get_or_create_user("voter", 123456)
    user_service.get_or_create_user("pet_a", 123456)
    user_service.get_or_create_user("pet_b", 123456)
    pet_service.adopt_pet("pet_a", 123456, "展示宠A")
    pet_service.adopt_pet("pet_b", 123456, "展示宠B")
    show_id = qingpet_db.create_pet_show(123456, "春季展示会", 24)
    assert show_id is not None

    assert qingpet_db.vote_pet_show_atomic(show_id, "voter", "pet_a", 5) is True
    assert qingpet_db.vote_pet_show_atomic(show_id, "voter", "pet_b", 5) is True

    votes = qingpet_db.get_pet_show_votes(show_id)
    assert votes["pet_a"] == 1
    assert votes["pet_b"] == 1


def test_admin_ban_user_logs_operator_id(qingpet_db):
    user_service = UserService(qingpet_db)
    user_service.get_or_create_user("target", 123456)

    admin_service = AdminService(qingpet_db)
    assert admin_service.ban_user("target", 123456, 3, operator_user_id="operator") is True

    logs = admin_service.get_logs(123456, limit=5)
    assert logs
    assert logs[0].user_id == "operator"
