import asyncio
import os
import tempfile
from datetime import datetime, timedelta

from plugins.qingpet import main as qingpet_main
from plugins.qingpet.commands.advanced_commands import handle_explore, handle_view_pet
from plugins.qingpet.commands.basic_commands import handle_feed, handle_status
from plugins.qingpet.services.database import Database
from plugins.qingpet.services.pet_service import PetService
from plugins.qingpet.services.user_service import UserService


def _segments_text(payload) -> str:
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, list):
        return ""
    parts = []
    for seg in payload:
        if isinstance(seg, dict) and seg.get("type") == "text":
            parts.append(str(seg.get("data", {}).get("text", "")))
    return "".join(parts)


def _make_temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    db = Database(db_path)
    return db, db_path


def _cleanup_temp_db(db: Database, db_path: str) -> None:
    db.cleanup()
    if os.path.exists(db_path):
        os.unlink(db_path)


class _SecretsOnlyContext:
    def __init__(self, admin_user_ids):
        self.secrets = {"admin_user_ids": admin_user_ids}


def test_admin_command_recognizes_admin_from_secrets_json():
    temp_db, db_path = _make_temp_db()
    original_db = qingpet_main._db_instance
    qingpet_main._db_instance = temp_db
    try:
        context = _SecretsOnlyContext([123456789])
        ok, msg = asyncio.run(
            qingpet_main._handle_admin_command("开启", "123456789", 10001, context)
        )
    finally:
        qingpet_main._db_instance = original_db
        _cleanup_temp_db(temp_db, db_path)

    assert ok is True
    assert "已在群 10001 中启用" in msg


def test_private_status_lists_all_group_pets():
    temp_db, db_path = _make_temp_db()
    pet_service = PetService(temp_db)
    user_service = UserService(temp_db)

    user_service.get_or_create_user("u_private", 20001)
    user_service.get_or_create_user("u_private", 20002)
    pet_service.adopt_pet("u_private", 20001, "小白")
    pet_service.adopt_pet("u_private", 20002, "小黑")

    try:
        ok, msg = asyncio.run(handle_status("u_private", 0, "", temp_db))
    finally:
        _cleanup_temp_db(temp_db, db_path)

    assert ok is True
    assert "小白" in msg
    assert "小黑" in msg
    assert "20001" in msg
    assert "20002" in msg


def test_decay_applies_after_elapsed_hours():
    temp_db, db_path = _make_temp_db()
    user_service = UserService(temp_db)
    user_service.get_or_create_user("u_decay", 30001)

    pet_service = PetService(temp_db)
    pet_service.adopt_pet("u_decay", 30001, "慢慢")

    pet = temp_db.get_pet("u_decay", 30001)
    assert pet is not None
    pet.last_update = datetime.now() - timedelta(hours=3)
    temp_db.update_pet(pet)

    import plugins.qingpet.services.pet_service as pet_service_module

    original_random = pet_service_module.random.random
    pet_service_module.random.random = lambda: 1.0

    try:
        pet = temp_db.get_pet("u_decay", 30001)
        assert pet is not None
        pet_service.apply_decay(pet, 1.0)
        updated = temp_db.get_pet("u_decay", 30001)
        assert updated is not None
    finally:
        pet_service_module.random.random = original_random
        _cleanup_temp_db(temp_db, db_path)

    assert updated.hunger < 100
    assert updated.clean < 100
    assert updated.mood < 100


def test_private_feed_requires_group_when_user_has_multiple_pets():
    temp_db, db_path = _make_temp_db()
    pet_service = PetService(temp_db)
    user_service = UserService(temp_db)

    user_service.get_or_create_user("u_multi", 40001)
    user_service.get_or_create_user("u_multi", 40002)
    pet_service.adopt_pet("u_multi", 40001, "甲")
    pet_service.adopt_pet("u_multi", 40002, "乙")

    try:
        ok, msg = asyncio.run(handle_feed("u_multi", 0, "", temp_db, spam_decay_factor=1.0))
    finally:
        _cleanup_temp_db(temp_db, db_path)

    assert ok is False
    assert "请先指定群号" in msg
    assert "40001" in msg
    assert "40002" in msg


def test_private_feed_with_group_uses_target_group_pet():
    temp_db, db_path = _make_temp_db()
    pet_service = PetService(temp_db)
    user_service = UserService(temp_db)

    user_service.get_or_create_user("u_multi2", 50001)
    user_service.get_or_create_user("u_multi2", 50002)
    pet_service.adopt_pet("u_multi2", 50001, "阿白")
    pet_service.adopt_pet("u_multi2", 50002, "阿黑")

    try:
        ok, msg = asyncio.run(handle_feed("u_multi2", 0, "50002 apple", temp_db, spam_decay_factor=1.0))
    finally:
        _cleanup_temp_db(temp_db, db_path)

    assert ok is True
    assert "阿黑" in msg


def test_view_pet_with_plain_qq_id_still_works():
    temp_db, db_path = _make_temp_db()
    pet_service = PetService(temp_db)
    user_service = UserService(temp_db)

    user_service.get_or_create_user("viewer", 60001)
    user_service.get_or_create_user("10086", 60001)
    pet_service.adopt_pet("10086", 60001, "可可")

    try:
        ok, msg = asyncio.run(handle_view_pet("viewer", 60001, "10086", temp_db))
    finally:
        _cleanup_temp_db(temp_db, db_path)

    assert ok is True
    assert "可可" in msg


def test_view_pet_accepts_cq_at_format():
    temp_db, db_path = _make_temp_db()
    pet_service = PetService(temp_db)
    user_service = UserService(temp_db)

    user_service.get_or_create_user("viewer2", 60002)
    user_service.get_or_create_user("10010", 60002)
    pet_service.adopt_pet("10010", 60002, "团团")

    try:
        ok, msg = asyncio.run(handle_view_pet("viewer2", 60002, "[CQ:at,qq=10010]", temp_db))
    finally:
        _cleanup_temp_db(temp_db, db_path)

    assert ok is True
    assert "团团" in msg


def test_qingpet_handle_view_uses_event_at_when_args_missing_target():
    temp_db, db_path = _make_temp_db()
    pet_service = PetService(temp_db)
    user_service = UserService(temp_db)

    user_service.get_or_create_user("viewer3", 60003)
    user_service.get_or_create_user("10011", 60003)
    pet_service.adopt_pet("10011", 60003, "球球")

    original_db = qingpet_main._db_instance
    original_router = qingpet_main._router
    qingpet_main._db_instance = temp_db
    qingpet_main._router = None

    event = {
        "user_id": "viewer3",
        "group_id": 60003,
        "message": [
            {"type": "text", "data": {"text": "/pet 查看 "}},
            {"type": "at", "data": {"qq": "10011"}},
        ],
    }

    try:
        msg = _segments_text(asyncio.run(qingpet_main.handle("pet", "查看", event, None)))
    finally:
        qingpet_main._db_instance = original_db
        qingpet_main._router = original_router
        _cleanup_temp_db(temp_db, db_path)

    assert "球球" in msg


def test_feed_shows_remaining_free_apple_count_in_message():
    temp_db, db_path = _make_temp_db()
    user_service = UserService(temp_db)
    pet_service = PetService(temp_db)

    user = user_service.get_or_create_user("free_feed_user", 70001)
    pet_service.adopt_pet("free_feed_user", 70001, "苹果酱")
    pet = temp_db.get_pet("free_feed_user", 70001)
    assert pet is not None

    try:
        ok, msg, _ = pet_service.feed_pet(pet, user, "apple")
    finally:
        _cleanup_temp_db(temp_db, db_path)

    assert ok is True
    assert "免费苹果" in msg
    assert "剩余" in msg


def test_free_feed_counter_persists_after_feed():
    temp_db, db_path = _make_temp_db()
    user_service = UserService(temp_db)
    pet_service = PetService(temp_db)

    user = user_service.get_or_create_user("free_counter_user", 70010)
    pet_service.adopt_pet("free_counter_user", 70010, "果果")
    pet = temp_db.get_pet("free_counter_user", 70010)
    assert pet is not None

    try:
        ok, _, _ = pet_service.feed_pet(pet, user, "apple")
        persisted_user = temp_db.get_user("free_counter_user", 70010)
    finally:
        _cleanup_temp_db(temp_db, db_path)

    assert ok is True
    assert persisted_user is not None
    assert persisted_user.today_free_feed_count == 1


def test_feed_updates_daily_task_before_task_panel_initialized():
    temp_db, db_path = _make_temp_db()
    user_service = UserService(temp_db)
    pet_service = PetService(temp_db)

    user = user_service.get_or_create_user("task_feed_user", 70011)
    pet_service.adopt_pet("task_feed_user", 70011, "任务果")
    pet = temp_db.get_pet("task_feed_user", 70011)
    assert pet is not None

    try:
        ok, _, _ = pet_service.feed_pet(pet, user, "apple")
        tasks = temp_db.get_or_create_daily_tasks("task_feed_user", 70011)
    finally:
        _cleanup_temp_db(temp_db, db_path)

    assert ok is True
    feed_task = next(t for t in tasks if t["task_type"] == "feed")
    assert feed_task["current_value"] == 1


def test_explore_uses_numeric_event_reward_values():
    temp_db, db_path = _make_temp_db()
    user_service = UserService(temp_db)
    pet_service = PetService(temp_db)

    user = user_service.get_or_create_user("explore_user", 70002)
    pet_service.adopt_pet("explore_user", 70002, "探探")
    pet = temp_db.get_pet("explore_user", 70002)
    assert pet is not None
    pet.energy = 100
    temp_db.update_pet(pet)

    import plugins.qingpet.services.pet_service as pet_service_module

    original_choice = pet_service_module.random.choice
    pet_service_module.random.choice = lambda _events: {"msg": "固定事件", "coins": 20, "exp": 5}

    try:
        ok, msg, coins = pet_service.explore(pet, user, 1.0)
    finally:
        pet_service_module.random.choice = original_choice
        _cleanup_temp_db(temp_db, db_path)

    assert ok is True
    assert "固定事件" in msg
    assert coins == 20


def test_explore_message_contains_pet_name_prefix():
    temp_db, db_path = _make_temp_db()
    pet_service = PetService(temp_db)
    user_service = UserService(temp_db)

    user_service.get_or_create_user("u_msg", 70003)
    pet_service.adopt_pet("u_msg", 70003, "阿星")

    try:
        ok, msg = asyncio.run(handle_explore("u_msg", 70003, "", temp_db, spam_decay_factor=1.0))
    finally:
        _cleanup_temp_db(temp_db, db_path)

    assert ok is True
    assert msg.startswith("🐾 阿星\n")


def test_qingpet_handle_visit_uses_event_at_when_args_missing_target():
    temp_db, db_path = _make_temp_db()
    pet_service = PetService(temp_db)
    user_service = UserService(temp_db)

    user_service.get_or_create_user("visitor", 70004)
    user_service.get_or_create_user("20001", 70004)
    pet_service.adopt_pet("20001", 70004, "被访宠")

    original_db = qingpet_main._db_instance
    original_router = qingpet_main._router
    qingpet_main._db_instance = temp_db
    qingpet_main._router = None

    event = {
        "user_id": "visitor",
        "group_id": 70004,
        "message": [
            {"type": "text", "data": {"text": "/pet 互访 "}},
            {"type": "at", "data": {"qq": "20001"}},
        ],
    }

    try:
        msg = _segments_text(asyncio.run(qingpet_main.handle("pet", "互访", event, None)))
    finally:
        qingpet_main._db_instance = original_db
        qingpet_main._router = original_router
        _cleanup_temp_db(temp_db, db_path)

    assert "访问了被访宠" in msg


def test_qingpet_handle_message_uses_event_at_with_trailing_text():
    temp_db, db_path = _make_temp_db()
    pet_service = PetService(temp_db)
    user_service = UserService(temp_db)

    user_service.get_or_create_user("writer", 70005)
    user_service.get_or_create_user("20002", 70005)
    pet_service.adopt_pet("20002", 70005, "留言宠")

    original_db = qingpet_main._db_instance
    original_router = qingpet_main._router
    qingpet_main._db_instance = temp_db
    qingpet_main._router = None

    event = {
        "user_id": "writer",
        "group_id": 70005,
        "message": [
            {"type": "text", "data": {"text": "/pet 留言 "}},
            {"type": "at", "data": {"qq": "20002"}},
            {"type": "text", "data": {"text": " 你好呀"}},
        ],
    }

    try:
        msg = _segments_text(asyncio.run(qingpet_main.handle("pet", "留言", event, None)))
    finally:
        qingpet_main._db_instance = original_db
        qingpet_main._router = original_router
        _cleanup_temp_db(temp_db, db_path)

    assert "已给留言宠留言" in msg
