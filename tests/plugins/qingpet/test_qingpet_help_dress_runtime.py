"""分类帮助、装扮交易与异步运行边界测试。"""

import asyncio
import inspect
import time
from datetime import timedelta

import pytest

from plugins.qingpet import main as qingpet_main
from plugins.qingpet.commands.new_commands import (
    _dress_buy,
    _dress_equip,
    _dress_shop,
    _dress_unequip,
    _trade_sell,
)
from plugins.qingpet.services import pet_service as pet_service_module
from plugins.qingpet.services.pet_service import PetService
from plugins.qingpet.services.user_service import UserService
from plugins.qingpet.utils.constants import PetStatus
from plugins.qingpet.utils.formatters import format_pet_card
from plugins.qingpet.utils.time import utc_now
from tests.helpers.assertions import text_segments_text
from tests.helpers.qingpet_test_support import fail_updates_to


def test_help_menu_categories():
    root      = qingpet_main._local_catalog_root()
    help_text = qingpet_main._format_help_overview(root)

    assert "/宠物 help basic" in help_text
    assert "/宠物 help advanced" in help_text
    assert "/宠物 help social" in help_text
    assert "/宠物 visit" not in help_text


def test_category_help_only_expands_selected_manifest_commands():
    root = qingpet_main._local_catalog_root()

    help_text = qingpet_main._format_category_help(root, "social")

    assert "社交互动" in help_text
    assert "/宠物 visit <@QQ号>" in help_text
    assert "/宠物 trade sell" in help_text
    assert "/宠物 feed" not in help_text


def test_help_categories_cover_each_business_command_once():
    root              = qingpet_main._local_catalog_root()
    category_helpers  = set(qingpet_main._HELP_CATEGORIES)
    business_commands = {
        child.name for child in root.children if child.name not in {"help", *category_helpers}
    }
    grouped_commands = [
        command_name
        for _title, command_names in qingpet_main._HELP_CATEGORIES.values()
        for command_name in command_names
    ]
    help_node = root.resolve_child("help")

    assert help_node is not None
    assert {child.name for child in help_node.children} == category_helpers
    assert len(grouped_commands) == len(set(grouped_commands))
    assert set(grouped_commands) == business_commands


def test_dress_shop_display():
    success, text = _dress_shop()
    assert success
    assert "💰" in text  # Coins icon
    assert "❤️" in text  # Friendship icon
    # Check for specific items
    assert "天使光环" in text
    assert "爱心背景" in text


def test_dress_buy_with_friendship_points(qingpet_db):
    user_id  = "test_fp_user"
    group_id = 1001

    user_service           = UserService(qingpet_db)
    user                   = user_service.get_or_create_user(user_id, group_id)
    user.friendship_points = 300
    user.coins             = 0
    qingpet_db.update_user(user)

    success, msg = _dress_buy(user_id, group_id, "halo", qingpet_db)
    assert success
    assert "花费100友情点" in msg

    user = qingpet_db.get_user(user_id, group_id)
    assert user.friendship_points == 200

    owned = qingpet_db.get_dress_inventory(user_id, group_id)
    assert "halo" in owned


def test_dress_buy_insufficient_friendship_points(qingpet_db):
    user_id  = "test_poor_fp_user"
    group_id = 1002

    user_service           = UserService(qingpet_db)
    user                   = user_service.get_or_create_user(user_id, group_id)
    user.friendship_points = 50
    qingpet_db.update_user(user)

    success, msg = _dress_buy(user_id, group_id, "halo", qingpet_db)
    assert not success
    assert "友情点不足" in msg


def test_pet_card_with_dress(qingpet_db):
    user_id  = "test_dress_user"
    group_id = 1003

    user_service = UserService(qingpet_db)
    user         = user_service.get_or_create_user(user_id, group_id)
    pet_service  = PetService(qingpet_db)
    pet_service.adopt_pet(user_id, group_id, "FashionPet")

    assert qingpet_db.purchase_dress_atomic(user_id, group_id, "halo", "coins", 0)[0]
    _dress_equip(user_id, group_id, "halo", qingpet_db)

    pet  = qingpet_db.get_pet(user_id, group_id)
    card = format_pet_card(pet, user)

    assert "🎩 帽子: 天使光环" in card
    assert "✨ 心情加成" in card
    assert "🪙 金币" in card
    assert "❤️ 友情点" in card


def test_dress_equip_reports_persistence_failure(qingpet_db):
    user_id  = "failed_dress_user"
    group_id = 1004
    PetService(qingpet_db).adopt_pet(user_id, group_id, "试衣宝")
    assert qingpet_db.purchase_dress_atomic(user_id, group_id, "halo", "coins", 0)[0]
    fail_updates_to(qingpet_db, "pets")

    success, message = _dress_equip(user_id, group_id, "halo", qingpet_db)

    assert success is False
    assert "失败" in message
    assert qingpet_db.get_pet(user_id, group_id).dress_hat is None


def test_dress_unequip_reports_persistence_failure(qingpet_db):
    user_id  = "failed_unequip_user"
    group_id = 1005
    PetService(qingpet_db).adopt_pet(user_id, group_id, "换装宝")
    assert qingpet_db.purchase_dress_atomic(user_id, group_id, "halo", "coins", 0)[0]
    equipped, _message = _dress_equip(user_id, group_id, "halo", qingpet_db)
    assert equipped is True
    fail_updates_to(qingpet_db, "pets")

    success, message = _dress_unequip(user_id, group_id, "帽子", qingpet_db)

    assert success is False
    assert "失败" in message
    assert qingpet_db.get_pet(user_id, group_id).dress_hat == "halo"


def test_trade_sell_rejects_trailing_arguments_without_consuming_inventory(qingpet_db):
    user_id  = "invalid_trade_user"
    group_id = 1006
    UserService(qingpet_db).get_or_create_user(user_id, group_id)
    assert qingpet_db.purchase_item_atomic(user_id, group_id, "apple", 2, 0)[0]

    success, _message = _trade_sell(user_id, group_id, "apple 1 10 trailing", qingpet_db)

    assert success is False
    inventory = qingpet_db.get_or_create_inventory(user_id, group_id)
    assert inventory.get_item_count("apple") == 2


def test_qingpet_handle_signature_matches_core_dispatcher():
    params = list(inspect.signature(qingpet_main.handle).parameters.keys())
    assert params[:4] == ["command", "args", "event", "context"]


@pytest.mark.asyncio
async def test_qingpet_handle_uses_bounded_worker_for_command_path(qingpet_runtime, monkeypatch):
    context, _database = qingpet_runtime
    calls         = {"count": 0}
    real_run_sync = qingpet_main.run_sync

    async def counting_run_sync(func, *args, **kwargs):
        calls["count"] += 1
        return await real_run_sync(func, *args, **kwargs)

    monkeypatch.setattr(qingpet_main, "run_sync", counting_run_sync)

    event  = {"user_id": 10001, "group_id": 20001}
    result = await qingpet_main.handle("qingpet", "help social", event, context)

    assert calls["count"] == 1
    assert isinstance(result, list)
    assert "社交互动" in text_segments_text(result)


@pytest.mark.asyncio
async def test_qingpet_handle_does_not_create_nested_event_loop(qingpet_runtime, monkeypatch):
    """命令热路径只应切换工作线程，不能为每条消息再创建事件循环。"""

    context, _database = qingpet_runtime

    def reject_nested_run(coroutine):
        coroutine.close()
        raise AssertionError("QingPet command path created a nested event loop")

    monkeypatch.setattr(qingpet_main.asyncio, "run", reject_nested_run)

    event  = {"user_id": 10001, "group_id": 20001}
    result = await qingpet_main.handle("qingpet", "help social", event, context)

    assert isinstance(result, list)
    assert "社交互动" in text_segments_text(result)


@pytest.mark.asyncio
async def test_qingpet_scheduled_decay_uses_to_thread(qingpet_runtime, monkeypatch):
    context, _database = qingpet_runtime
    calls          = {"count": 0}
    real_to_thread = asyncio.to_thread

    async def counting_to_thread(func, *args, **kwargs):
        calls["count"] += 1
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(qingpet_main.asyncio, "to_thread", counting_to_thread)

    result = await qingpet_main.scheduled_decay(context)

    assert calls["count"] == 1
    assert result == []


@pytest.mark.asyncio
async def test_qingpet_scheduled_decay_uses_enabled_group_decay_map(qingpet_runtime, monkeypatch):
    """只衰减已启用群里的宠物，并按各群自己的衰减倍率；陈旧的命令时间戳一并清掉。"""

    context, database = qingpet_runtime
    monkeypatch.setattr(pet_service_module.random, "random", lambda: 1.0)
    fast_group, normal_group, disabled_group, neglected_group = 123, 124, 999, 125
    assert database.update_group_config_field(fast_group, "decay_multiplier", 2.0)
    assert database.update_group_config_field(normal_group, "enabled", True)
    assert database.update_group_config_field(disabled_group, "enabled", False)
    assert database.update_group_config_field(neglected_group, "enabled", True)

    last_update = utc_now() - timedelta(minutes=30)
    for group_id in (fast_group, normal_group, disabled_group, neglected_group):
        user_id = f"owner-{group_id}"
        assert PetService(database).adopt_pet(user_id, group_id, f"宠{group_id}")[0]
        pet = database.get_pet(user_id, group_id)
        assert pet is not None
        stat = 5 if group_id == neglected_group else 100
        pet.hunger = pet.mood = pet.clean = pet.energy = pet.health = stat
        pet.last_update = last_update
        assert database.update_pet(pet)

    connection = database._get_connection()
    connection.execute(
        "INSERT INTO command_timestamps (user_id, group_id, timestamp) VALUES (?, ?, ?)",
        ("stale-user", fast_group, 1.0),
    )
    connection.execute(
        "INSERT INTO command_timestamps (user_id, group_id, timestamp) VALUES (?, ?, ?)",
        ("recent-user", fast_group, time.time()),
    )
    connection.commit()

    deliveries = await qingpet_main.scheduled_decay(context)

    def stored(group_id: int):
        pet = database.get_pet(f"owner-{group_id}", group_id)
        assert pet is not None
        return pet

    assert (stored(normal_group).hunger, stored(normal_group).mood) == (85, 76)
    assert (stored(fast_group).hunger, stored(fast_group).mood) == (70, 52)
    assert (stored(disabled_group).hunger, stored(disabled_group).mood) == (100, 100)
    assert stored(disabled_group).last_update == last_update
    assert stored(neglected_group).status == PetStatus.TRAVELING
    assert [delivery.target.group_id for delivery in deliveries] == [neglected_group]
    assert "旅行" in deliveries[0].message[0]["data"]["text"]
    remaining = {
        row["user_id"] for row in connection.execute("SELECT user_id FROM command_timestamps")
    }
    assert remaining == {"recent-user"}
