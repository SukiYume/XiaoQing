"""命令层共用的参数解析，以及两步确认命令的解析。"""

import pytest

from plugins.qingpet.commands.admin_commands import (
    _parse_confirmed_target,
    handle_manage_delete,
    handle_manage_reset,
)
from plugins.qingpet.commands.parsing import parse_target_qq, split_first_word
from plugins.qingpet.services.database_actions import _coin_grant_within_limit
from plugins.qingpet.services.database_support import _adjust_item_count


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ("", "")),
        ("   ", ("", "")),
        ("投票", ("投票", "")),
        ("  投票   @10086  ", ("投票", "@10086")),
        ("展示会 春季 第一期", ("展示会", "春季 第一期")),
    ],
)
def test_split_first_word(text, expected):
    assert split_first_word(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10086", "10086"),
        ("@10086", "10086"),
        ("  @10086 ", "10086"),
        ("", None),
        ("@", None),
        ("10086 extra", None),
        ("@@10086", None),
        ("[CQ:at,qq=10086]", None),
    ],
)
def test_parse_target_qq_requires_the_whole_text_to_be_a_target(text, expected):
    assert parse_target_qq(text) == expected


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("10086", ("10086", False)),
        ("@10086", ("10086", False)),
        ("@10086 确认", ("10086", True)),
        ("10086   CONFIRM", ("10086", True)),
        ("10086 确认 多余", None),
        ("确认", None),
        ("", None),
    ],
)
def test_parse_confirmed_target(args, expected):
    assert _parse_confirmed_target(args) == expected


@pytest.mark.parametrize(
    ("handler", "verb"),
    [(handle_manage_reset, "重置"), (handle_manage_delete, "删除")],
)
def test_destructive_admin_commands_ask_for_confirmation_before_touching_the_database(
    handler, verb
):
    ok, message = handler("admin", 123456, "@10086", None)  # type: ignore[arg-type]

    assert ok is False
    assert f"/宠物 管理 {verb} @10086 确认" in message

    ok, message = handler("admin", 123456, "不是QQ号", None)  # type: ignore[arg-type]

    assert ok is False
    assert f"用法: /宠物 管理 {verb} @QQ号" in message


def test_adjust_item_count_removes_empty_entries():
    items = {"apple": 2}

    _adjust_item_count(items, "apple", -1)
    assert items == {"apple": 1}
    _adjust_item_count(items, "apple", -1)
    assert items == {}
    _adjust_item_count(items, "pear", 3)
    assert items == {"pear": 3}


@pytest.mark.parametrize(
    ("requested", "earned_today", "expected"),
    [
        (30, 0, 30),
        (30, 490, 10),
        (30, 500, 0),
        (30, 600, 0),
        (-5, 0, 0),
    ],
)
def test_coin_grant_within_limit(requested, earned_today, expected):
    """每日金币上限是 500（DAILY_LIMITS["coins"]）。"""
    assert _coin_grant_within_limit(requested, earned_today) == expected


def test_rank_label_uses_medals_then_numbers():
    from plugins.qingpet.utils.formatters import rank_label

    assert [rank_label(index) for index in range(5)] == ["🥇", "🥈", "🥉", "#4", "#5"]


def test_recent_command_count_filters_by_user_or_counts_the_whole_group(qingpet_db):
    for user_id in ("u1", "u1", "u2"):
        qingpet_db.record_command_timestamp(user_id, 100)
    qingpet_db.record_command_timestamp("u1", 200)

    assert qingpet_db.get_recent_command_count(100, 60, user_id="u1") == 2
    assert qingpet_db.get_recent_command_count(100, 60) == 3
    assert qingpet_db.get_recent_command_count(200, 60) == 1
