"""数据库升级、命令作用域、事务回滚与调度边界测试。"""

import logging
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta

import pytest

from core.interfaces import PluginCapabilities, PluginPrincipal
from core.router import command_access
from plugins.qingpet import main as qingpet_main
from plugins.qingpet.commands.admin_commands import (
    handle_manage_activity,
    handle_manage_ban,
    handle_manage_log,
    handle_manage_unban,
)
from plugins.qingpet.commands.advanced_commands import (
    handle_buy,
    handle_explore,
    handle_task,
    handle_treat,
    handle_view_pet,
)
from plugins.qingpet.commands.basic_commands import handle_feed, handle_status
from plugins.qingpet.services import database_clock
from plugins.qingpet.services.database import Database
from plugins.qingpet.services.item_service import ItemService
from plugins.qingpet.services.pet_service import PetService
from plugins.qingpet.services.social_service import SocialService
from plugins.qingpet.services.user_service import UserService
from plugins.qingpet.utils.constants import DAILY_LIMITS, PetStage, PetStatus
from tests.helpers.assertions import text_segments_text
from tests.helpers.qingpet_test_support import fail_updates_to


def test_database_initialization_schema_is_complete_and_idempotent(tmp_path):
    db = Database(str(tmp_path / "qingpet-schema.db"))
    try:
        connection = db._get_connection()

        def schema_snapshot():
            return connection.execute(
                """SELECT type, name, tbl_name, sql
                   FROM sqlite_master
                   WHERE name NOT LIKE 'sqlite_%'
                   ORDER BY type, name"""
            ).fetchall()

        schema_before = [tuple(row) for row in schema_snapshot()]
        db._init_database()
        schema_after = [tuple(row) for row in schema_snapshot()]

        table_names = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        trigger_names = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }

        assert schema_after == schema_before
        assert {
            "users",
            "pets",
            "group_tasks",
            "pet_show_votes",
            "scheduler_runs",
            "asset_ledger",
        } <= table_names
        assert {
            "trg_users_daily_coin_cap",
            "trg_users_nonnegative_insert",
            "trg_users_nonnegative_update",
        } <= trigger_names
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
    finally:
        db.cleanup()


def test_version_six_database_loses_its_retired_bookkeeping(tmp_path) -> None:
    """第 6 版的库升级时：迁移记录表和与主键重复的索引被去掉，升级前留一份备份，数据不动。"""

    db_path = tmp_path / "qingpet-v6.db"
    Database(str(db_path)).cleanup()
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX idx_daily_likes "
            "ON daily_likes(user_id, target_user_id, group_id, like_date)"
        )
        connection.execute(
            "INSERT INTO daily_likes (user_id, target_user_id, group_id, like_date) "
            "VALUES ('a', 'b', 1, '2026-11-01')"
        )
        connection.execute("PRAGMA user_version=6")
        connection.commit()

    db = Database(str(db_path))
    try:
        connection = db._get_connection()
        names      = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            )
        }
        assert not {"schema_migrations", "idx_daily_likes"} & names
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert connection.execute("SELECT COUNT(*) FROM daily_likes").fetchone()[0] == 1
        assert db_path.with_name(db_path.name + ".pre-migration-v6.bak").is_file()
    finally:
        db.cleanup()


def test_daily_coin_cap_trigger_tracks_updated_limit(tmp_path) -> None:
    db_path = tmp_path / "qingpet-cap-trigger.db"
    db      = Database(str(db_path))
    try:
        connection = db._get_connection()
        connection.execute(
            "INSERT INTO users (user_id, group_id, coins, today_coins_earned) VALUES (?, ?, ?, ?)",
            ("cap-user", 123, 1000, 0),
        )
        # 模拟旧版本部署留下的触发器：上限比现在的小。
        connection.execute("DROP TRIGGER trg_users_daily_coin_cap")
        connection.execute(
            """CREATE TRIGGER trg_users_daily_coin_cap
               AFTER UPDATE OF today_coins_earned ON users
               WHEN NEW.today_coins_earned > 200
               BEGIN
                   UPDATE users SET
                       coins = MAX(0, NEW.coins - (NEW.today_coins_earned - 200)),
                       today_coins_earned = 200
                   WHERE user_id = NEW.user_id AND group_id = NEW.group_id;
               END"""
        )
        connection.commit()

        # 重新打开数据库（相当于升级后重启），触发器按现在的上限重建。
        Database(str(db_path)).cleanup()

        connection.execute(
            "UPDATE users SET today_coins_earned = 400 WHERE user_id = ? AND group_id = ?",
            ("cap-user", 123),
        )
        connection.commit()
        row = connection.execute(
            "SELECT coins, today_coins_earned FROM users WHERE user_id = ? AND group_id = ?",
            ("cap-user", 123),
        ).fetchone()
        assert (row["coins"], row["today_coins_earned"]) == (1000, 400)
    finally:
        db.cleanup()


def test_legacy_task_date_column_and_index_are_installed_in_one_upgrade(tmp_path):
    db_path = tmp_path / "qingpet-legacy-tasks.db"
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("CREATE TABLE tasks (user_id TEXT NOT NULL, group_id INTEGER NOT NULL)")

    db = Database(str(db_path))
    try:
        connection = db._get_connection()
        columns    = {str(row["name"]) for row in connection.execute("PRAGMA table_info(tasks)")}
        indexes    = {str(row["name"]) for row in connection.execute("PRAGMA index_list(tasks)")}
        assert "created_date" in columns
        assert "idx_tasks_date" in indexes
    finally:
        db.cleanup()


def test_database_failure_log_omits_exception_text_path_and_query_data(
    monkeypatch,
    caplog,
    qingpet_db,
):
    sensitive             = "password=do-not-log C:\\private\\qingpet.db user=private-user"
    results: list[object] = []

    def fail_connect(*_args, **_kwargs):
        raise RuntimeError(sensitive)

    def read_from_new_thread() -> None:
        # 连接按线程缓存；新线程要重新建连接，建连接这一步在这里失败。
        results.append(qingpet_db.get_user("private-user", 70099))

    with monkeypatch.context() as patch, caplog.at_level(logging.ERROR):
        patch.setattr(sqlite3, "connect", fail_connect)
        worker = threading.Thread(target=read_from_new_thread)
        worker.start()
        worker.join()

    assert results == [None]
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "operation=get_user" in log_text
    assert "error_type=RuntimeError" in log_text
    assert sensitive not in log_text
    assert "private-user" not in log_text
    assert "qingpet.db" not in log_text


class _PrincipalContext:
    def __init__(
        self,
        principal: PluginPrincipal,
        capabilities: PluginCapabilities | None = None,
    ):
        self.principal    = principal
        self.capabilities = capabilities or PluginCapabilities()


@pytest.mark.parametrize(
    ("role", "is_bot_admin"),
    [("owner", False), ("admin", False), ("member", True)],
)
def test_admin_command_uses_core_principal_for_current_group(role, is_bot_admin, qingpet_db):
    context = _PrincipalContext(
        PluginPrincipal(
            kind         = "user",
            user_id      = 123456789,
            group_id     = 10001,
            is_bot_admin = is_bot_admin,
            group_role   = role,
        ),
        PluginCapabilities(is_bot_admin=is_bot_admin),
    )
    ok, msg = qingpet_main._handle_admin_command(
        "123456789", 10001, "开启", qingpet_db, context=context
    )

    assert ok is True
    assert "已在群 10001 中启用" in msg


@pytest.mark.parametrize(
    ("principal", "allowed"),
    [
        (PluginPrincipal(kind="user", user_id=1, group_id=10001, group_role="owner"), True),
        (PluginPrincipal(kind="user", user_id=1, group_id=10001, group_role="admin"), True),
        (
            PluginPrincipal(
                kind         = "user",
                user_id      = 1,
                group_id     = 10001,
                is_bot_admin = True,
                group_role   = "member",
            ),
            True,
        ),
        (PluginPrincipal(kind="user", user_id=1, group_id=10001, group_role="member"), False),
        (PluginPrincipal(kind="user", user_id=1, is_private=True), False),
    ],
)
def test_admin_commands_are_declared_for_group_managers(principal, allowed):
    """谁能用管理命令写在 plugin.json 里，由 Core 的 command_access 在调用插件前统一判断。"""

    admin = qingpet_main._catalog_root(None).resolve_child("admin")
    assert admin is not None and admin.permission == "group_admin"
    assert (command_access(admin, principal) is None) is allowed


@pytest.mark.asyncio
async def test_private_backpack_uses_real_group_scope_and_private_rate_bucket(qingpet_runtime):
    context, qingpet_db = qingpet_runtime
    user_service = UserService(qingpet_db)
    pet_service  = PetService(qingpet_db)
    item_service = ItemService(qingpet_db)

    user_service.get_or_create_user("private_owner", 81001)
    pet_service.adopt_pet("private_owner", 81001, "私聊宠")
    ok, _ = item_service.buy_item("private_owner", 81001, "apple", 2)
    assert ok is True

    event = {
        "user_id"     : "private_owner",
        "message_type": "private",
        "message"     : [{"type": "text", "data": {"text": "/宠物 背包"}}],
    }

    msg              = text_segments_text(await qingpet_main.handle("pet", "背包", event, context))
    conn             = qingpet_db._get_connection()
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

    assert "苹果" in msg
    assert group_zero_count == 0
    assert real_group_count == 1
    assert negative_bucket_count == 0


@pytest.mark.asyncio
async def test_private_view_auto_scopes_to_single_pet_group(qingpet_runtime):
    context, qingpet_db = qingpet_runtime
    pet_service  = PetService(qingpet_db)
    user_service = UserService(qingpet_db)

    user_service.get_or_create_user("viewer_private", 81002)
    user_service.get_or_create_user("10086", 81002)
    pet_service.adopt_pet("viewer_private", 81002, "观察者")
    pet_service.adopt_pet("10086", 81002, "被看见")

    event = {
        "user_id"     : "viewer_private",
        "message_type": "private",
        "message"     : [{"type": "text", "data": {"text": "/宠物 查看 10086"}}],
    }

    msg = text_segments_text(await qingpet_main.handle("pet", "查看 10086", event, context))

    assert "被看见" in msg


def test_private_status_lists_all_group_pets(qingpet_db):
    pet_service  = PetService(qingpet_db)
    user_service = UserService(qingpet_db)

    user_service.get_or_create_user("u_private", 20001)
    user_service.get_or_create_user("u_private", 20002)
    pet_service.adopt_pet("u_private", 20001, "小白")
    pet_service.adopt_pet("u_private", 20002, "小黑")

    ok, msg = handle_status("u_private", 0, "", qingpet_db)

    assert ok is True
    assert "小白" in msg
    assert "小黑" in msg
    assert "20001" in msg
    assert "20002" in msg


def test_decay_applies_after_elapsed_hours(qingpet_db):
    user_service = UserService(qingpet_db)
    user_service.get_or_create_user("u_decay", 30001)

    pet_service = PetService(qingpet_db)
    pet_service.adopt_pet("u_decay", 30001, "慢慢")

    pet = qingpet_db.get_pet("u_decay", 30001)
    assert pet is not None
    pet.last_update = datetime.now() - timedelta(hours=3)
    qingpet_db.update_pet(pet)

    import plugins.qingpet.services.pet_service as pet_service_module

    original_random                  = pet_service_module.random.random
    pet_service_module.random.random = lambda: 1.0

    try:
        pet = qingpet_db.get_pet("u_decay", 30001)
        assert pet is not None
        pet_service.apply_decay(pet, 1.0)
        updated = qingpet_db.get_pet("u_decay", 30001)
        assert updated is not None
    finally:
        pet_service_module.random.random = original_random

    assert updated.hunger < 100
    assert updated.clean < 100
    assert updated.mood < 100


def test_private_feed_requires_group_when_user_has_multiple_pets(qingpet_db):
    pet_service  = PetService(qingpet_db)
    user_service = UserService(qingpet_db)

    user_service.get_or_create_user("u_multi", 40001)
    user_service.get_or_create_user("u_multi", 40002)
    pet_service.adopt_pet("u_multi", 40001, "甲")
    pet_service.adopt_pet("u_multi", 40002, "乙")

    ok, msg = handle_feed("u_multi", 0, "", qingpet_db, spam_decay_factor=1.0)

    assert ok is False
    assert "请先指定群号" in msg
    assert "40001" in msg
    assert "40002" in msg


def test_private_feed_with_group_uses_target_group_pet(qingpet_db):
    pet_service  = PetService(qingpet_db)
    user_service = UserService(qingpet_db)

    user_service.get_or_create_user("u_multi2", 50001)
    user_service.get_or_create_user("u_multi2", 50002)
    pet_service.adopt_pet("u_multi2", 50001, "阿白")
    pet_service.adopt_pet("u_multi2", 50002, "阿黑")

    ok, msg = handle_feed("u_multi2", 0, "50002 apple", qingpet_db, spam_decay_factor=1.0)

    assert ok is True
    assert "阿黑" in msg


@pytest.mark.parametrize(
    ("target_id", "argument", "pet_name"),
    [
        pytest.param("10086", "10086", "可可", id="plain-qq"),
        pytest.param("10010", "[CQ:at,qq=10010]", "团团", id="cq-at"),
    ],
)
def test_view_pet_accepts_plain_qq_and_cq_at_targets(target_id, argument, pet_name, qingpet_db):
    pet_service  = PetService(qingpet_db)
    user_service = UserService(qingpet_db)

    user_service.get_or_create_user("viewer", 60001)
    user_service.get_or_create_user(target_id, 60001)
    pet_service.adopt_pet(target_id, 60001, pet_name)

    ok, msg = handle_view_pet("viewer", 60001, argument, qingpet_db)

    assert ok is True
    assert pet_name in msg


@pytest.mark.parametrize(
    ("command", "trailing_text", "pet_name", "expected"),
    [
        pytest.param("查看", "", "球球", "球球", id="view"),
        pytest.param("互访", "", "被访宠", "访问了被访宠", id="visit"),
        pytest.param(
            "留言", " 你好呀", "留言宠", "已给留言宠留言", id="message-with-trailing-text"
        ),
    ],
)
@pytest.mark.asyncio
async def test_qingpet_handle_uses_event_at_as_target_when_args_omit_it(
    command, trailing_text, pet_name, expected, qingpet_runtime
):
    context, qingpet_db = qingpet_runtime
    pet_service  = PetService(qingpet_db)
    user_service = UserService(qingpet_db)

    user_service.get_or_create_user("actor", 60003)
    user_service.get_or_create_user("10011", 60003)
    pet_service.adopt_pet("10011", 60003, pet_name)

    segments = [
        {"type": "text", "data": {"text": f"/pet {command} "}},
        {"type": "at", "data": {"qq": "10011"}},
    ]
    if trailing_text:
        segments.append({"type": "text", "data": {"text": trailing_text}})
    event = {"user_id": "actor", "group_id": 60003, "message": segments}

    msg = text_segments_text(await qingpet_main.handle("pet", command, event, context))

    assert expected in msg


def test_feed_shows_remaining_free_apple_count_in_message(qingpet_db):
    user_service = UserService(qingpet_db)
    pet_service  = PetService(qingpet_db)

    user = user_service.get_or_create_user("free_feed_user", 70001)
    pet_service.adopt_pet("free_feed_user", 70001, "苹果酱")
    pet = qingpet_db.get_pet("free_feed_user", 70001)
    assert pet is not None

    ok, msg, _ = pet_service.feed_pet(pet, user, "apple")

    assert ok is True
    assert "免费苹果" in msg
    assert "剩余" in msg


def test_free_feed_counter_persists_after_feed(qingpet_db):
    user_service = UserService(qingpet_db)
    pet_service  = PetService(qingpet_db)

    user = user_service.get_or_create_user("free_counter_user", 70010)
    pet_service.adopt_pet("free_counter_user", 70010, "果果")
    pet = qingpet_db.get_pet("free_counter_user", 70010)
    assert pet is not None

    ok, _, _ = pet_service.feed_pet(pet, user, "apple")
    persisted_user = qingpet_db.get_user("free_counter_user", 70010)

    assert ok is True
    assert persisted_user is not None
    assert persisted_user.today_free_feed_count == 1


def test_feed_updates_daily_task_before_task_panel_initialized(qingpet_db):
    user_service = UserService(qingpet_db)
    pet_service  = PetService(qingpet_db)

    user = user_service.get_or_create_user("task_feed_user", 70011)
    pet_service.adopt_pet("task_feed_user", 70011, "任务果")
    pet = qingpet_db.get_pet("task_feed_user", 70011)
    assert pet is not None

    ok, _, _ = pet_service.feed_pet(pet, user, "apple")
    tasks = qingpet_db.get_or_create_daily_tasks("task_feed_user", 70011)

    assert ok is True
    feed_task = next(t for t in tasks if t["task_type"] == "feed")
    assert feed_task["current_value"] == 1


def test_feed_blocks_when_daily_limit_reached(qingpet_db):
    user_service = UserService(qingpet_db)
    pet_service  = PetService(qingpet_db)

    user = user_service.get_or_create_user("limit_user", 70012)
    # 今天的次数记在 action_quotas 里，判定只看这张表。
    conn = qingpet_db._get_connection()
    conn.execute(
        """INSERT INTO action_quotas
           (user_id, group_id, action, period_date, action_count, available_at)
           VALUES ('limit_user', 70012, 'feed', ?, ?, 0)""",
        (database_clock.business_date(), DAILY_LIMITS["feed"]),
    )
    conn.commit()
    pet_service.adopt_pet("limit_user", 70012, "满满")
    pet = qingpet_db.get_pet("limit_user", 70012)
    assert pet is not None

    ok, msg, _ = pet_service.feed_pet(pet, user, "apple")

    assert ok is False
    assert "上限" in msg


def test_train_failed_attempt_still_counts_daily_usage(qingpet_db):
    user_service = UserService(qingpet_db)
    pet_service  = PetService(qingpet_db)

    user = user_service.get_or_create_user("train_limit_user", 70013)
    pet_service.adopt_pet("train_limit_user", 70013, "练练")
    pet = qingpet_db.get_pet("train_limit_user", 70013)
    assert pet is not None
    pet.energy = 100
    pet.stage  = PetStage.YOUNG
    qingpet_db.update_pet(pet)

    import plugins.qingpet.services.pet_service as pet_service_module

    original_random                  = pet_service_module.random.random
    pet_service_module.random.random = lambda: 1.0

    try:
        ok, _, _ = pet_service.train_pet(pet, user, training_type="strength")
        persisted_user = qingpet_db.get_user("train_limit_user", 70013)
    finally:
        pet_service_module.random.random = original_random

    assert ok is True
    assert persisted_user is not None
    assert persisted_user.today_train_count == 1
    assert persisted_user.total_train_count == 1


def test_explore_uses_numeric_event_reward_values(qingpet_db):
    user_service = UserService(qingpet_db)
    pet_service  = PetService(qingpet_db)

    user = user_service.get_or_create_user("explore_user", 70002)
    pet_service.adopt_pet("explore_user", 70002, "探探")
    pet = qingpet_db.get_pet("explore_user", 70002)
    assert pet is not None
    pet.energy = 100
    qingpet_db.update_pet(pet)

    import plugins.qingpet.services.pet_service as pet_service_module

    original_choices = pet_service_module.random.choices
    pet_service_module.random.choices = lambda _events, weights=None, k=1: [
        {"msg": "固定事件", "coins": 20, "exp": 5}
    ]

    try:
        ok, msg, coins = pet_service.explore(pet, user, spam_decay_factor=1.0)
    finally:
        pet_service_module.random.choices = original_choices

    assert ok is True
    assert "固定事件" in msg
    assert coins == 20


def test_explore_message_contains_pet_name_prefix(qingpet_db):
    pet_service  = PetService(qingpet_db)
    user_service = UserService(qingpet_db)

    user_service.get_or_create_user("u_msg", 70003)
    pet_service.adopt_pet("u_msg", 70003, "阿星")

    ok, msg = handle_explore("u_msg", 70003, "", qingpet_db, spam_decay_factor=1.0)

    assert ok is True
    assert msg.startswith("🐾 阿星\n")


def test_minigame_respects_configured_cooldown(qingpet_db):
    user_service   = UserService(qingpet_db)
    pet_service    = PetService(qingpet_db)
    social_service = SocialService(qingpet_db)

    user_service.get_or_create_user("dice_user", 70014)
    pet_service.adopt_pet("dice_user", 70014, "骰骰")

    ok1, _ = social_service.play_dice("dice_user", 70014)
    ok2, msg2 = social_service.play_dice("dice_user", 70014)

    assert ok1 is True
    assert ok2 is False
    assert "冷却" in msg2


def test_pet_show_settlement_grants_temporary_champion_title(qingpet_db):
    user_service   = UserService(qingpet_db)
    pet_service    = PetService(qingpet_db)
    social_service = SocialService(qingpet_db)

    user_service.get_or_create_user("show_winner", 70015)
    user_service.get_or_create_user("show_other", 70015)
    user_service.get_or_create_user("voter-1", 70015)
    user_service.get_or_create_user("voter-2", 70015)
    user_service.get_or_create_user("voter-3", 70015)
    pet_service.adopt_pet("show_winner", 70015, "冠军宠")
    pet_service.adopt_pet("show_other", 70015, "陪跑宠")
    show_id = qingpet_db.create_pet_show(70015, "春季展示会", 24)
    assert show_id is not None
    assert qingpet_db.vote_pet_show_atomic(show_id, "voter-1", "show_winner", 5) is True
    assert qingpet_db.vote_pet_show_atomic(show_id, "voter-2", "show_winner", 5) is True
    assert qingpet_db.vote_pet_show_atomic(show_id, "voter-3", "show_other", 5) is True

    result = social_service.settle_pet_show(70015)
    titles = user_service.get_user_titles("show_winner", 70015)

    assert "冠军宠" in result
    assert "展示会冠军" in titles


def test_failed_pet_action_keeps_caller_models_unchanged(qingpet_db):
    user_service = UserService(qingpet_db)
    pet_service  = PetService(qingpet_db)
    user         = user_service.get_or_create_user("rollback-action", 70020)
    pet_service.adopt_pet(user.user_id, user.group_id, "稳稳")
    pet = qingpet_db.get_pet(user.user_id, user.group_id)
    assert pet is not None
    before = (
        pet.clean,
        pet.health,
        pet.last_clean,
        pet.last_update,
        pet.version,
        user.coins,
        user.today_clean_count,
        user.version,
    )
    fail_updates_to(qingpet_db, "pets")

    ok, _message, _coins = pet_service.clean_pet(pet, user)

    assert ok is False
    assert (
        pet.clean,
        pet.health,
        pet.last_clean,
        pet.last_update,
        pet.version,
        user.coins,
        user.today_clean_count,
        user.version,
    ) == before


def test_feed_resolves_chinese_food_name_and_rejects_non_food(qingpet_db):
    user_service = UserService(qingpet_db)
    pet_service  = PetService(qingpet_db)
    user         = user_service.get_or_create_user("food-type", 70021)
    pet_service.adopt_pet(user.user_id, user.group_id, "饭饭")
    pet = qingpet_db.get_pet(user.user_id, user.group_id)
    assert pet is not None

    invalid, invalid_message, _ = pet_service.feed_pet(pet, user, "加速卡")
    valid, _valid_message, _ = pet_service.feed_pet(pet, user, "苹果")

    assert invalid is False
    assert "不是食物" in invalid_message
    assert valid is True


def test_treatment_commits_quota_item_and_pet_state_together(qingpet_db):
    user_service = UserService(qingpet_db)
    pet_service  = PetService(qingpet_db)
    user         = user_service.get_or_create_user("atomic-treatment", 70022)
    pet_service.adopt_pet(user.user_id, user.group_id, "药药")
    pet = qingpet_db.get_pet(user.user_id, user.group_id)
    assert pet is not None
    pet.stage  = PetStage.YOUNG
    pet.status = PetStatus.SICK
    pet.health = 10
    assert qingpet_db.update_pet(pet)
    assert qingpet_db.purchase_item_atomic(user.user_id, user.group_id, "rare_medicine", 1, 0)[0]

    ok, _message = handle_treat(user.user_id, user.group_id, "稀有药品", qingpet_db)
    persisted_pet       = qingpet_db.get_pet(user.user_id, user.group_id)
    persisted_inventory = qingpet_db.get_or_create_inventory(user.user_id, user.group_id)
    quota_available, quota_remaining = qingpet_db.check_action_quota(
        user.user_id,
        user.group_id,
        "treat",
        20,
    )

    assert ok is True
    assert persisted_pet is not None
    assert persisted_pet.health == 60
    assert persisted_pet.status == PetStatus.NORMAL
    assert persisted_inventory.get_item_count("rare_medicine") == 0
    assert quota_available is False
    assert quota_remaining > 0


def test_failed_treatment_rolls_back_quota_item_and_pet(qingpet_db):
    user_service = UserService(qingpet_db)
    pet_service  = PetService(qingpet_db)
    user         = user_service.get_or_create_user("failed-treatment", 70023)
    pet_service.adopt_pet(user.user_id, user.group_id, "安安")
    pet = qingpet_db.get_pet(user.user_id, user.group_id)
    assert pet is not None
    pet.stage  = PetStage.YOUNG
    pet.status = PetStatus.SICK
    pet.health = 10
    assert qingpet_db.update_pet(pet)
    assert qingpet_db.purchase_item_atomic(user.user_id, user.group_id, "rare_medicine", 1, 0)[0]
    before = (pet.health, pet.clean, pet.status, pet.version)

    fail_updates_to(qingpet_db, "inventories")
    ok, _message = handle_treat(user.user_id, user.group_id, "稀有药品", qingpet_db)
    persisted_pet       = qingpet_db.get_pet(user.user_id, user.group_id)
    persisted_inventory = qingpet_db.get_or_create_inventory(user.user_id, user.group_id)
    quota_available, quota_remaining = qingpet_db.check_action_quota(
        user.user_id,
        user.group_id,
        "treat",
        20,
    )

    assert ok is False
    assert (pet.health, pet.clean, pet.status, pet.version) == before
    assert persisted_pet is not None
    assert (persisted_pet.health, persisted_pet.clean, persisted_pet.status) == before[:3]
    assert persisted_inventory.get_item_count("rare_medicine") == 1
    assert quota_available is True
    assert quota_remaining == 0


def test_failed_trusteeship_coupon_keeps_user_and_inventory_unchanged(qingpet_db):
    user_service = UserService(qingpet_db)
    pet_service  = PetService(qingpet_db)
    user         = user_service.get_or_create_user("failed-trustee", 70024)
    pet_service.adopt_pet(user.user_id, user.group_id, "托托")
    pet = qingpet_db.get_pet(user.user_id, user.group_id)
    assert pet is not None
    assert qingpet_db.purchase_item_atomic(user.user_id, user.group_id, "trusteeship_coupon", 1, 0)[
        0
    ]
    fail_updates_to(qingpet_db, "pets")

    ok, _message = pet_service.use_trusteeship_coupon(pet, user)
    persisted_inventory = qingpet_db.get_or_create_inventory(user.user_id, user.group_id)

    assert ok is False
    assert user.trustee_until is None
    assert persisted_inventory.get_item_count("trusteeship_coupon") == 1


@pytest.mark.parametrize("arguments", ["apple two", "apple 1 extra"])
def test_buy_rejects_malformed_quantity_instead_of_defaulting_to_one(arguments, qingpet_db):
    ok, _message = handle_buy("invalid-buy", 70025, arguments, qingpet_db)
    inventory = qingpet_db.get_or_create_inventory("invalid-buy", 70025)

    assert ok is False
    assert inventory.get_item_count("apple") == 0


def test_private_task_claim_uses_the_group_prefix(qingpet_db):
    user_id  = "private-task"
    group_id = 70026
    PetService(qingpet_db).adopt_pet(user_id, group_id, "任务宝")
    tasks = qingpet_db.get_or_create_daily_tasks(user_id, group_id)
    conn  = qingpet_db._get_connection()
    conn.execute(
        "UPDATE tasks SET current_value = target_value WHERE user_id = ? AND group_id = ?",
        (user_id, group_id),
    )
    conn.commit()
    coins_before = qingpet_db.get_user(user_id, group_id).coins

    ok, _message = handle_task(user_id, 0, f"{group_id} 领取", qingpet_db)

    assert ok is True
    claimed = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE user_id = ? AND group_id = ? AND claimed = 1",
        (user_id, group_id),
    ).fetchone()[0]
    assert claimed == len(tasks) == 4
    assert qingpet_db.get_user(user_id, group_id).coins > coins_before


def test_task_claim_with_nothing_to_claim_says_so_and_pays_nothing(qingpet_db):
    user_id  = "idle-task"
    group_id = 70029
    PetService(qingpet_db).adopt_pet(user_id, group_id, "任务宝")
    qingpet_db.get_or_create_daily_tasks(user_id, group_id)
    coins_before = qingpet_db.get_user(user_id, group_id).coins

    ok, message = handle_task(user_id, group_id, "领取", qingpet_db)

    # 任务都没完成：照常回复“没有可领的”，不发奖励；奖励领过一次后再领也是这样。
    assert ok is True and "暂无可领取" in message and "任务宝" in message
    assert qingpet_db.get_user(user_id, group_id).coins == coins_before
    conn = qingpet_db._get_connection()
    conn.execute(
        "UPDATE tasks SET current_value = target_value WHERE user_id = ? AND group_id = ?",
        (user_id, group_id),
    )
    conn.commit()
    assert "成功领取" in handle_task(user_id, group_id, "领取", qingpet_db)[1]
    coins_after_claim = qingpet_db.get_user(user_id, group_id).coins
    assert coins_after_claim > coins_before
    assert "暂无可领取" in handle_task(user_id, group_id, "领取", qingpet_db)[1]
    assert qingpet_db.get_user(user_id, group_id).coins == coins_after_claim


@pytest.mark.parametrize(
    ("handler", "arguments"),
    [
        (handle_manage_ban, "@10086 0"),
        (handle_manage_ban, "@10086 7 trailing"),
        (handle_manage_unban, "@10086 trailing"),
        (handle_manage_log, "0"),
        (handle_manage_log, "many"),
    ],
)
def test_admin_commands_reject_malformed_arguments(handler, arguments, qingpet_db):
    ok, _message = handler("admin", 70027, arguments, qingpet_db)

    assert ok is False


@pytest.mark.parametrize("arguments", ["创建 unknown 10 20", "创建 feed 0 20"])
def test_activity_creation_rejects_unreachable_or_empty_progress(arguments, qingpet_db):
    ok, _message = handle_manage_activity("admin", 70028, arguments, qingpet_db)
    activities = qingpet_db.get_active_activities(70028)

    assert ok is False
    assert activities == []


def test_activity_storage_rejects_invalid_parameters_and_hides_expired_rows(qingpet_db):
    assert qingpet_db.create_activity(70029, "feed", "无效目标", 0, 20) is None
    assert qingpet_db.create_activity(70029, " ", "空类型", 10, 20) is None
    activity_id = qingpet_db.create_activity(70029, "feed", "限时活动", 10, 20)
    assert activity_id is not None
    qingpet_db._get_connection().execute(
        "UPDATE activities SET end_time = ? WHERE id = ?",
        (datetime(2000, 1, 1).isoformat(), activity_id),
    )
    qingpet_db._get_connection().commit()
    activities = qingpet_db.get_active_activities(70029)

    assert activities == []
