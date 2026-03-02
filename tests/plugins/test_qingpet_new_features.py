import pytest
import tempfile
import os
import asyncio
import inspect
from plugins.qingpet.services.database import Database
from plugins.qingpet.services.pet_service import PetService
from plugins.qingpet.services.user_service import UserService
from plugins.qingpet.commands.new_commands import _dress_shop, _dress_buy, _dress_equip
from plugins.qingpet.utils.formatters import format_help_text, format_pet_card
from plugins.qingpet.utils.constants import DEFAULT_DRESS_ITEMS, DressSlot
from plugins.qingpet import main as qingpet_main

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
    default_text = format_help_text()
    assert "基础" in default_text
    assert "进阶" in default_text
    
    social_text = format_help_text("social")
    assert "社交互动" in social_text
    assert "互访" in social_text
    
    shop_text = format_help_text("shop")
    assert "道具与装扮" in shop_text

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
    
    temp_db.add_dress_item(user_id, group_id, "halo")
    _dress_equip(user_id, group_id, "halo", temp_db)
    
    pet = temp_db.get_pet(user_id, group_id)
    card = format_pet_card(pet, user)
    
    assert "🎩 帽子: 天使光环" in card
    assert "✨ 心情加成" in card
    assert "🪙 金币" in card
    assert "❤️ 友情点" in card


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


def test_qingpet_handle_uses_to_thread_for_command_path(monkeypatch):
    calls = {"count": 0}

    async def _fake_to_thread(func, *args, **kwargs):
        calls["count"] += 1
        return [{"type": "text", "data": {"text": "ok"}}]

    monkeypatch.setattr(qingpet_main, "_db_instance", _FakeDB())
    monkeypatch.setattr(qingpet_main, "_router", None)
    monkeypatch.setattr(qingpet_main.asyncio, "to_thread", _fake_to_thread)

    event = {"user_id": 10001, "group_id": 20001}
    result = asyncio.run(qingpet_main.handle("qingpet", "help social", event, None))

    assert calls["count"] >= 1
    assert isinstance(result, list)
    assert _segments_text(result) == "ok"


class _FakeGroupConfigForJobs:
    enabled = True
    activity_enabled = False


class _FakeJobDB:
    def get_all_pets(self):
        return []

    def get_group_config(self, group_id):
        return _FakeGroupConfigForJobs()

    def cleanup_old_timestamps(self):
        return None


class _FakePetService:
    def apply_decay(self, pet, decay_multiplier):
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
