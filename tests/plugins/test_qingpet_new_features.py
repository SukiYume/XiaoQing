import asyncio
import inspect
import os
import tempfile

import pytest

from plugins.qingpet import main as qingpet_main
from plugins.qingpet.commands.new_commands import (
    _dress_buy,
    _dress_equip,
    _dress_shop,
    _dress_unequip,
    _trade_sell,
)
from plugins.qingpet.services.database import Database
from plugins.qingpet.services.pet_service import PetService
from plugins.qingpet.services.user_service import UserService
from plugins.qingpet.utils.formatters import format_pet_card


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    db = Database(db_path)
    yield db
    db.cleanup()
    if os.path.exists(db_path):
        os.unlink(db_path)


def test_help_menu_categories():
    root = qingpet_main._local_catalog_root()
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
    root = qingpet_main._local_catalog_root()
    category_helpers = set(qingpet_main._HELP_CATEGORIES)
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


def test_dress_buy_with_friendship_points(temp_db):
    user_id = "test_fp_user"
    group_id = 1001

    user_service = UserService(temp_db)
    user = user_service.get_or_create_user(user_id, group_id)
    user.friendship_points = 300
    user.coins = 0
    temp_db.update_user(user)

    success, msg = _dress_buy(user_id, group_id, "halo", temp_db)
    assert success
    assert "花费100友情点" in msg

    user = temp_db.get_user(user_id, group_id)
    assert user.friendship_points == 200

    owned = temp_db.get_dress_inventory(user_id, group_id)
    assert "halo" in owned


def test_dress_buy_insufficient_friendship_points(temp_db):
    user_id = "test_poor_fp_user"
    group_id = 1002

    user_service = UserService(temp_db)
    user = user_service.get_or_create_user(user_id, group_id)
    user.friendship_points = 50
    temp_db.update_user(user)

    success, msg = _dress_buy(user_id, group_id, "halo", temp_db)
    assert not success
    assert "友情点不足" in msg


def test_pet_card_with_dress(temp_db):
    user_id = "test_dress_user"
    group_id = 1003

    user_service = UserService(temp_db)
    user = user_service.get_or_create_user(user_id, group_id)
    pet_service = PetService(temp_db)
    pet_service.adopt_pet(user_id, group_id, "FashionPet")

    assert temp_db.purchase_dress_atomic(user_id, group_id, "halo", "coins", 0)[0]
    _dress_equip(user_id, group_id, "halo", temp_db)

    pet = temp_db.get_pet(user_id, group_id)
    card = format_pet_card(pet, user)

    assert "🎩 帽子: 天使光环" in card
    assert "✨ 心情加成" in card
    assert "🪙 金币" in card
    assert "❤️ 友情点" in card


def test_dress_equip_reports_persistence_failure(temp_db, monkeypatch):
    user_id = "failed_dress_user"
    group_id = 1004
    PetService(temp_db).adopt_pet(user_id, group_id, "试衣宝")
    assert temp_db.purchase_dress_atomic(user_id, group_id, "halo", "coins", 0)[0]
    monkeypatch.setattr(temp_db, "update_pet", lambda _pet: False)

    success, message = _dress_equip(user_id, group_id, "halo", temp_db)

    assert success is False
    assert "失败" in message
    assert temp_db.get_pet(user_id, group_id).dress_hat is None


def test_dress_unequip_reports_persistence_failure(temp_db, monkeypatch):
    user_id = "failed_unequip_user"
    group_id = 1005
    PetService(temp_db).adopt_pet(user_id, group_id, "换装宝")
    assert temp_db.purchase_dress_atomic(user_id, group_id, "halo", "coins", 0)[0]
    equipped, _message = _dress_equip(user_id, group_id, "halo", temp_db)
    assert equipped is True
    monkeypatch.setattr(temp_db, "update_pet", lambda _pet: False)

    success, message = _dress_unequip(user_id, group_id, "帽子", temp_db)

    assert success is False
    assert "失败" in message
    assert temp_db.get_pet(user_id, group_id).dress_hat == "halo"


def test_trade_sell_rejects_trailing_arguments_without_consuming_inventory(temp_db):
    user_id = "invalid_trade_user"
    group_id = 1006
    UserService(temp_db).get_or_create_user(user_id, group_id)
    assert temp_db.purchase_item_atomic(user_id, group_id, "apple", 2, 0)[0]

    success, _message = _trade_sell(user_id, group_id, "apple 1 10 trailing", temp_db)

    assert success is False
    inventory = temp_db.get_or_create_inventory(user_id, group_id)
    assert inventory.get_item_count("apple") == 2


def test_qingpet_handle_signature_matches_core_dispatcher():
    params = list(inspect.signature(qingpet_main.handle).parameters.keys())
    assert params[:4] == ["command", "args", "event", "context"]


class _FakeGroupConfig:
    enabled = True


class _FakeDB:
    def get_recent_command_count(self, user_id, group_id, window_seconds):
        return 0

    def get_group_recent_command_count(self, group_id, window_seconds):
        return 0

    def record_command_timestamp(self, user_id, group_id):
        return None

    def get_group_config(self, group_id):
        return _FakeGroupConfig()

    def get_user(self, user_id, group_id):
        return None


def _segments_text(payload) -> str:
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, list):
        return ""
    return "".join(
        str(seg.get("data", {}).get("text", ""))
        for seg in payload
        if isinstance(seg, dict) and seg.get("type") == "text"
    )


def test_qingpet_help_subcategory_routed_from_args(monkeypatch):
    monkeypatch.setattr(qingpet_main, "_db_instance", _FakeDB())
    monkeypatch.setattr(qingpet_main, "_router", None)

    event = {"user_id": 10001, "group_id": 20001}
    result = asyncio.run(qingpet_main.handle("qingpet", "help social", event, None))

    assert isinstance(result, list)
    assert "社交互动" in _segments_text(result)


def test_qingpet_handle_uses_bounded_worker_for_command_path(monkeypatch):
    calls = {"count": 0}

    async def _fake_run_sync(func, *args, **kwargs):
        calls["count"] += 1
        return func(*args, **kwargs)

    monkeypatch.setattr(qingpet_main, "_db_instance", _FakeDB())
    monkeypatch.setattr(qingpet_main, "_router", None)
    monkeypatch.setattr(qingpet_main, "run_sync", _fake_run_sync)

    event = {"user_id": 10001, "group_id": 20001}
    result = asyncio.run(qingpet_main.handle("qingpet", "help social", event, None))

    assert calls["count"] == 1
    assert isinstance(result, list)
    assert "社交互动" in _segments_text(result)


@pytest.mark.asyncio
async def test_qingpet_handle_does_not_create_nested_event_loop(monkeypatch):
    """命令热路径只应切换工作线程，不能为每条消息再创建事件循环。"""

    def reject_nested_run(coroutine):
        coroutine.close()
        raise AssertionError("QingPet command path created a nested event loop")

    monkeypatch.setattr(qingpet_main, "_db_instance", _FakeDB())
    monkeypatch.setattr(qingpet_main, "_router", None)
    monkeypatch.setattr(qingpet_main.asyncio, "run", reject_nested_run)

    event = {"user_id": 10001, "group_id": 20001}
    result = await qingpet_main.handle("qingpet", "help social", event, None)

    assert isinstance(result, list)
    assert "社交互动" in _segments_text(result)


class _FakeJobDB:
    def get_enabled_group_decay_map(self):
        return {}

    def get_all_pets(self):
        return []

    def cleanup_old_timestamps(self):
        return None

    def get_active_trustee_keys(self):
        return set()


class _FakePetService:
    def apply_decay(self, pet, decay_multiplier, *, is_trustee_override=False):
        return None


def test_qingpet_scheduled_decay_uses_to_thread(monkeypatch):
    calls = {"count": 0}

    async def _fake_to_thread(func, *args, **kwargs):
        calls["count"] += 1
        return func(*args, **kwargs)

    monkeypatch.setattr(qingpet_main, "_db_instance", _FakeJobDB())
    monkeypatch.setattr(qingpet_main, "_pet_service", _FakePetService())
    monkeypatch.setattr(qingpet_main.asyncio, "to_thread", _fake_to_thread)

    result = asyncio.run(qingpet_main.scheduled_decay(None))

    assert calls["count"] == 1
    assert result == []


def test_qingpet_scheduled_decay_uses_enabled_group_decay_map(monkeypatch):
    class _FakeDB:
        def __init__(self):
            self.cleaned = False

        def get_enabled_group_decay_map(self):
            return {123: 1.5}

        def get_all_pets(self):
            return [
                type("Pet", (), {"user_id": "one", "group_id": 123})(),
                type("Pet", (), {"user_id": "two", "group_id": 999})(),
            ]

        def cleanup_old_timestamps(self):
            self.cleaned = True

        def get_active_trustee_keys(self):
            return set()

    applied = []

    class _FakeService:
        def apply_decay(self, pet, decay_multiplier, *, is_trustee_override=False):
            applied.append((pet.group_id, decay_multiplier))
            return None

    db = _FakeDB()

    async def _fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(qingpet_main, "_db_instance", db)
    monkeypatch.setattr(qingpet_main, "_pet_service", _FakeService())
    monkeypatch.setattr(qingpet_main.asyncio, "to_thread", _fake_to_thread)

    result = asyncio.run(qingpet_main.scheduled_decay(None))

    assert result == []
    assert applied == [(123, 1.5)]
    assert db.cleaned is True
