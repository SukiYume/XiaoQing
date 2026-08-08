"""QingPet 非法参数必须在任何业务读写之前失败。"""

from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plugins.qingpet.commands.admin_commands import handle_manage_activity, handle_manage_log
from plugins.qingpet.commands.advanced_commands import (
    handle_activity,
    handle_backpack,
    handle_buy,
    handle_explore,
    handle_group_task,
    handle_minigame,
    handle_shop,
    handle_task,
    handle_title,
    handle_train,
)
from plugins.qingpet.commands.basic_commands import (
    _split_group_prefix,
    handle_clean,
    handle_play,
    handle_sleep,
    handle_status,
    handle_wake,
)
from plugins.qingpet.commands.new_commands import (
    handle_dress,
    handle_recall,
    handle_show,
    handle_trade,
)
from plugins.qingpet.services.admin_service import AdminService
from plugins.qingpet.services.database import Database
from plugins.qingpet.services.pet_service import PetService
from tests.helpers.paths import REPOSITORY_ROOT

CommandHandler = Callable[[str, int, str, Database], tuple[bool, str]]


def test_unicode_digit_group_prefix_is_plain_invalid_input() -> None:
    assert _split_group_prefix("² feed") == (None, "² feed")


def test_status_rejects_unicode_group_id_before_database_access() -> None:
    db = MagicMock()

    success, message = handle_status("owner", 0, "²", db)

    assert success is False
    assert "群号必须是正整数" in message
    db.get_pet.assert_not_called()


@pytest.mark.parametrize(
    ("handler", "args", "expected"),
    [
        (handle_buy, "apple ²", "数量必须是整数"),
        (handle_activity, "claim ²", "用法"),
    ],
)
def test_user_commands_reject_unicode_integer_tokens_before_database_access(
    handler: CommandHandler,
    args: str,
    expected: str,
) -> None:
    db = MagicMock()

    success, message = handler("owner", 42, args, db)

    assert success is False
    assert expected in message
    db.assert_not_called()


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("²", "日志条数"),
        ("创建 feed ² 10", "目标数"),
        ("创建 feed 10 ²", "奖励金币"),
    ],
)
def test_admin_commands_reject_unicode_integer_tokens(
    args: str,
    expected: str,
) -> None:
    db = MagicMock()
    handler = handle_manage_log if args == "²" else handle_manage_activity

    success, message = handler("admin", 42, args, db, is_admin=True)

    assert success is False
    assert expected in message


@pytest.mark.parametrize("action", ["buy", "cancel"])
def test_trade_commands_reject_unicode_listing_ids(action: str) -> None:
    db = MagicMock()
    db.get_group_config.return_value = SimpleNamespace(trade_enabled=True)

    success, message = handle_trade("owner", 42, f"{action} ²", db)

    assert success is False
    assert "订单号" in message
    db.purchase_trade_listing.assert_not_called()
    db.get_listing_by_id.assert_not_called()


def _database_snapshot(db: Database) -> dict[str, tuple[tuple[object, ...], ...]]:
    """抓取全部业务表，确保非法命令连隐蔽的计数和奖励也不会改动。"""

    connection = db._get_connection()
    tables = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {
        table: tuple(tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"'))
        for table in tables
    }


@pytest.mark.parametrize(
    ("handler", "args", "expected"),
    [
        (handle_status, "unknown-group", "不接受额外参数"),
        (handle_clean, "unknown-group", "不接受额外参数"),
        (handle_play, "unknown-group", "不接受额外参数"),
        (handle_sleep, "extra", "不接受额外参数"),
        (handle_wake, "extra", "不接受额外参数"),
        (handle_train, "魔法", "无效训练类型"),
        (handle_explore, "火星", "无效探索地点"),
        (handle_recall, "extra", "不接受额外参数"),
        (handle_backpack, "extra", "不接受额外参数"),
        (handle_shop, "extra", "不接受额外参数"),
        (handle_title, "extra", "不接受额外参数"),
        (handle_dress, "view extra", "不接受额外参数"),
        (handle_dress, "shop extra", "不接受额外参数"),
        (handle_dress, "unknown", "未知装扮命令"),
        (handle_trade, "list extra", "不接受额外参数"),
        (handle_show, "unknown", "未知展示命令"),
        (handle_minigame, "dice extra", "不接受额外参数"),
        (handle_task, "unknown", "未知任务命令"),
        (handle_task, "claim extra", "未知任务命令"),
        (handle_group_task, "unknown", "未知群任务命令"),
        (handle_group_task, "claim extra", "未知群任务命令"),
        (handle_activity, "unknown", "未知活动命令"),
        (handle_activity, "claim extra", "用法"),
    ],
)
def test_invalid_arguments_are_rejected_without_database_side_effects(
    qingpet_db: Database,
    handler: CommandHandler,
    args: str,
    expected: str,
) -> None:
    group_id = 921_001
    user_id = "921002"
    admin = AdminService(qingpet_db)
    assert admin.enable_plugin(group_id)
    assert admin.set_config(group_id, "trade_enabled", "true")
    assert PetService(qingpet_db).adopt_pet(user_id, group_id, "边界宠")[0]
    before = _database_snapshot(qingpet_db)

    success, message = handler(user_id, group_id, args, qingpet_db)

    assert success is False
    assert expected in message
    assert _database_snapshot(qingpet_db) == before


def test_catalog_examples_use_real_static_ids_and_explicit_dynamic_placeholders() -> None:
    """目录不得再伪造不存在的道具，也不能把易碰撞的固定数据库 ID 当示例。"""

    manifest_path = REPOSITORY_ROOT / "plugins" / "qingpet" / "plugin.json"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert "food_basic" not in manifest_text
    assert "hat_basic" not in manifest_text

    root = manifest["commands"][0]
    nodes = {node["name"]: node for node in root["subcommands"]}
    assert nodes["buy"]["examples"] == ["/宠物 buy apple 2"]
    assert nodes["use"]["examples"] == ["/宠物 use acceleration_card"]
    assert nodes["gift"]["examples"] == ["/宠物 gift @10001 apple 1"]

    dress = {node["name"]: node for node in nodes["dress"]["subcommands"]}
    assert dress["buy"]["examples"] == ["/宠物 dress buy red_hat"]
    assert dress["equip"]["examples"] == ["/宠物 dress equip red_hat"]

    trade = {node["name"]: node for node in nodes["trade"]["subcommands"]}
    assert trade["sell"]["examples"] == ["/宠物 trade sell apple 2 20"]
    assert trade["buy"]["examples"] == ["/宠物 trade buy <订单号>"]
    assert trade["cancel"]["examples"] == ["/宠物 trade cancel <自己的订单号>"]

    activity = {node["name"]: node for node in nodes["activity"]["subcommands"]}
    assert activity["claim"]["examples"] == ["/宠物 activity claim <活动ID>"]
