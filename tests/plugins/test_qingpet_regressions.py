import asyncio
import os
import tempfile
from datetime import datetime, timedelta

import pytest

from core.interfaces import PluginCapabilities, PluginPrincipal
from plugins.qingpet import main as qingpet_main
from plugins.qingpet.commands.advanced_commands import handle_explore, handle_view_pet
from plugins.qingpet.commands.basic_commands import handle_feed, handle_status
from plugins.qingpet.services.database import Database
from plugins.qingpet.services.item_service import ItemService
from plugins.qingpet.services.pet_service import PetService
from plugins.qingpet.services.social_service import SocialService
from plugins.qingpet.services.user_service import UserService
from plugins.qingpet.utils.constants import DAILY_LIMITS, PetStage


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


class _PrincipalContext:
    def __init__(
        self,
        principal: PluginPrincipal,
        capabilities: PluginCapabilities | None = None,
    ):
        self.principal = principal
        self.capabilities = capabilities or PluginCapabilities()


@pytest.mark.parametrize(
    ("role", "is_bot_admin"),
    [("owner", False), ("admin", False), ("member", True)],
)
def test_admin_command_uses_core_principal_for_current_group(role, is_bot_admin):
    temp_db, db_path = _make_temp_db()
    original_db = qingpet_main._db_instance
    qingpet_main._db_instance = temp_db
    try:
        context = _PrincipalContext(
            PluginPrincipal(
                kind="user",
                user_id=123456789,
                group_id=10001,
                is_bot_admin=is_bot_admin,
                group_role=role,
            ),
            PluginCapabilities(is_bot_admin=is_bot_admin),
        )
        ok, msg = asyncio.run(
            qingpet_main._handle_admin_command("开启", "123456789", 10001, context)
        )
    finally:
        qingpet_main._db_instance = original_db
        _cleanup_temp_db(temp_db, db_path)

    assert ok is True
    assert "已在群 10001 中启用" in msg


@pytest.mark.parametrize(
    "principal",
    [
        PluginPrincipal(kind="user", user_id=999, group_id=10001, group_role="owner"),
        PluginPrincipal(kind="user", user_id=123456789, group_id=10002, group_role="admin"),
        PluginPrincipal(kind="user", user_id=123456789, group_id=10001, group_role="member"),
        PluginPrincipal(
            kind="user",
            user_id=123456789,
            group_id=10001,
            is_bot_admin=True,
            group_role="member",
        ),
        PluginPrincipal(kind="lifecycle"),
    ],
)
def test_admin_command_rejects_mismatched_or_unprivileged_principal(principal):
    temp_db, db_path = _make_temp_db()
    original_db = qingpet_main._db_instance
    qingpet_main._db_instance = temp_db
    try:
        ok, _msg = asyncio.run(
            qingpet_main._handle_admin_command(
                "开启",
                "123456789",
                10001,
                _PrincipalContext(principal),
            )
        )
    finally:
        qingpet_main._db_instance = original_db
        _cleanup_temp_db(temp_db, db_path)

    assert ok is False


def test_private_backpack_uses_real_group_scope_and_private_rate_bucket():
    temp_db, db_path = _make_temp_db()
    user_service = UserService(temp_db)
    pet_service = PetService(temp_db)
    item_service = ItemService(temp_db)

    user_service.get_or_create_user("private_owner", 81001)
    pet_service.adopt_pet("private_owner", 81001, "私聊宠")
    ok, _ = item_service.buy_item("private_owner", 81001, "apple", 2)
    assert ok is True

    original_db = qingpet_main._db_instance
    original_router = qingpet_main._router
    qingpet_main._db_instance = temp_db
    qingpet_main._router = None

    event = {
        "user_id": "private_owner",
        "message_type": "private",
        "message": [{"type": "text", "data": {"text": "/宠物 背包"}}],
    }

    try:
        msg = _segments_text(asyncio.run(qingpet_main.handle("pet", "背包", event, None)))
        conn = temp_db._get_connection()
        group_zero_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM command_timestamps WHERE group_id = 0"
        ).fetchone()["cnt"]
        real_group_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM command_timestamps WHERE user_id = ? AND group_id = ?",
            ("private_owner", 81001),
        ).fetchone()["cnt"]
        negative_bucket_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM command_timestamps WHERE user_id = ? AND group_id < 0",
            ("private_owner",),
        ).fetchone()["cnt"]
    finally:
        qingpet_main._db_instance = original_db
        qingpet_main._router = original_router
        _cleanup_temp_db(temp_db, db_path)

    assert "苹果" in msg
    assert group_zero_count == 0
    assert real_group_count == 1
    assert negative_bucket_count == 0


def test_private_view_auto_scopes_to_single_pet_group():
    temp_db, db_path = _make_temp_db()
    pet_service = PetService(temp_db)
    user_service = UserService(temp_db)

    user_service.get_or_create_user("viewer_private", 81002)
    user_service.get_or_create_user("10086", 81002)
    pet_service.adopt_pet("viewer_private", 81002, "观察者")
    pet_service.adopt_pet("10086", 81002, "被看见")

    original_db = qingpet_main._db_instance
    original_router = qingpet_main._router
    qingpet_main._db_instance = temp_db
    qingpet_main._router = None

    event = {
        "user_id": "viewer_private",
        "message_type": "private",
        "message": [{"type": "text", "data": {"text": "/宠物 查看 10086"}}],
    }

    try:
        msg = _segments_text(asyncio.run(qingpet_main.handle("pet", "查看 10086", event, None)))
    finally:
        qingpet_main._db_instance = original_db
        qingpet_main._router = original_router
        _cleanup_temp_db(temp_db, db_path)

    assert "被看见" in msg


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


def test_feed_blocks_when_daily_limit_reached():
    temp_db, db_path = _make_temp_db()
    user_service = UserService(temp_db)
    pet_service = PetService(temp_db)

    user = user_service.get_or_create_user("limit_user", 70012)
    user.today_feed_count = DAILY_LIMITS["feed"]
    user_service.update_user(user)
    pet_service.adopt_pet("limit_user", 70012, "满满")
    pet = temp_db.get_pet("limit_user", 70012)
    assert pet is not None

    try:
        ok, msg, _ = pet_service.feed_pet(pet, user, "apple")
    finally:
        _cleanup_temp_db(temp_db, db_path)

    assert ok is False
    assert "上限" in msg


def test_train_failed_attempt_still_counts_daily_usage():
    temp_db, db_path = _make_temp_db()
    user_service = UserService(temp_db)
    pet_service = PetService(temp_db)

    user = user_service.get_or_create_user("train_limit_user", 70013)
    pet_service.adopt_pet("train_limit_user", 70013, "练练")
    pet = temp_db.get_pet("train_limit_user", 70013)
    assert pet is not None
    pet.energy = 100
    pet.stage = PetStage.YOUNG
    temp_db.update_pet(pet)

    import plugins.qingpet.services.pet_service as pet_service_module

    original_random = pet_service_module.random.random
    pet_service_module.random.random = lambda: 1.0

    try:
        ok, _, _ = pet_service.train_pet(pet, user, training_type="strength")
        persisted_user = temp_db.get_user("train_limit_user", 70013)
    finally:
        pet_service_module.random.random = original_random
        _cleanup_temp_db(temp_db, db_path)

    assert ok is True
    assert persisted_user is not None
    assert persisted_user.today_train_count == 1
    assert persisted_user.total_train_count == 1


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

    original_choices = pet_service_module.random.choices
    pet_service_module.random.choices = lambda _events, weights=None, k=1: [{"msg": "固定事件", "coins": 20, "exp": 5}]

    try:
        ok, msg, coins = pet_service.explore(pet, user, spam_decay_factor=1.0)
    finally:
        pet_service_module.random.choices = original_choices
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


def test_minigame_respects_configured_cooldown():
    temp_db, db_path = _make_temp_db()
    user_service = UserService(temp_db)
    pet_service = PetService(temp_db)
    social_service = SocialService(temp_db)

    user_service.get_or_create_user("dice_user", 70014)
    pet_service.adopt_pet("dice_user", 70014, "骰骰")

    try:
        ok1, _ = social_service.play_dice("dice_user", 70014)
        ok2, msg2 = social_service.play_dice("dice_user", 70014)
    finally:
        _cleanup_temp_db(temp_db, db_path)

    assert ok1 is True
    assert ok2 is False
    assert "冷却" in msg2


def test_pet_show_settlement_grants_temporary_champion_title():
    temp_db, db_path = _make_temp_db()
    user_service = UserService(temp_db)
    pet_service = PetService(temp_db)
    social_service = SocialService(temp_db)

    user_service.get_or_create_user("show_winner", 70015)
    user_service.get_or_create_user("show_other", 70015)
    pet_service.adopt_pet("show_winner", 70015, "冠军宠")
    pet_service.adopt_pet("show_other", 70015, "陪跑宠")
    show_id = temp_db.create_pet_show(70015, "春季展示会", 24)
    assert show_id is not None
    temp_db.vote_pet_show(show_id, "voter-1", "show_winner")
    temp_db.vote_pet_show(show_id, "voter-2", "show_winner")
    temp_db.vote_pet_show(show_id, "voter-3", "show_other")

    try:
        result = social_service.settle_pet_show(70015)
        titles = user_service.get_user_titles("show_winner", 70015)
    finally:
        _cleanup_temp_db(temp_db, db_path)

    assert "冠军宠" in result
    assert "展示会冠军" in titles


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
