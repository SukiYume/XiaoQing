from __future__ import annotations

import json

import pytest

from plugins.qingpet.commands.advanced_commands import handle_gift
from plugins.qingpet.services.database import Database
from plugins.qingpet.services.social_service import SocialService
from plugins.qingpet.services.user_service import UserService

GROUP_ID    = 24680
SENDER_ID   = "10001"
RECEIVER_ID = "10002"


@pytest.fixture
def gift_context(qingpet_db: Database):
    database = qingpet_db
    users    = UserService(database)
    users.get_or_create_user(SENDER_ID, GROUP_ID)
    users.get_or_create_user(RECEIVER_ID, GROUP_ID)
    assert database.purchase_item_atomic(SENDER_ID, GROUP_ID, "cake", 10, 0)[0]
    return database


def _state(database: Database) -> str:
    connection = database._get_connection()
    users      = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM users WHERE group_id = ? ORDER BY user_id", (GROUP_ID,)
        ).fetchall()
    ]
    inventories = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM inventories WHERE group_id = ? ORDER BY user_id", (GROUP_ID,)
        ).fetchall()
    ]
    return json.dumps(
        {"users": users, "inventories": inventories},
        ensure_ascii = False,
        sort_keys    = True,
        separators   = (",", ":"),
    )


@pytest.mark.parametrize(
    "arguments",
    [
        f"@{RECEIVER_ID} cake 0",
        f"@{RECEIVER_ID} cake -1",
        f"@{RECEIVER_ID} cake {'9' * 100}",
        f"@{RECEIVER_ID} cake 1 trailing",
        f"@{RECEIVER_ID} missing_item 1",
        f"@{RECEIVER_ID} cake 11",
    ],
)
def test_gift_command_rejects_invalid_or_unavailable_amount_without_changes(
    gift_context, arguments: str
):
    before = _state(gift_context)

    success, _message = handle_gift(SENDER_ID, GROUP_ID, arguments, gift_context)

    assert not success
    assert _state(gift_context) == before


@pytest.mark.parametrize(
    ("item_id", "amount"),
    [
        ("cake", 0),
        ("cake", -1),
        ("cake", 100),
        ("cake", True),
        ("missing_item", 1),
        ("cake", 11),
    ],
)
def test_gift_service_rejects_invalid_or_unavailable_amount_without_changes(
    gift_context, item_id: str, amount: int
):
    before = _state(gift_context)

    success, _message = SocialService(gift_context).gift_item(
        SENDER_ID, RECEIVER_ID, GROUP_ID, item_id, amount
    )

    assert not success
    assert _state(gift_context) == before


@pytest.mark.parametrize("amount", [0, -1, 100, True])
def test_gift_database_boundary_independently_rejects_invalid_amount(gift_context, amount):
    before = _state(gift_context)

    success, _message = gift_context.gift_item_atomic(
        SENDER_ID,
        RECEIVER_ID,
        GROUP_ID,
        "cake",
        amount,
        friendship_gain  = 2,
        daily_limit      = 3,
        cooldown_seconds = 600,
    )

    assert not success
    assert _state(gift_context) == before


def test_valid_gift_moves_items_and_rewards_both_users_once(gift_context):
    success, message = SocialService(gift_context).gift_item(
        SENDER_ID, RECEIVER_ID, GROUP_ID, "cake", 2
    )

    assert success
    assert "友情点" in message
    assert gift_context.get_or_create_inventory(SENDER_ID, GROUP_ID).items == {"cake": 8}
    assert gift_context.get_or_create_inventory(RECEIVER_ID, GROUP_ID).items == {"cake": 2}
    sender   = gift_context.get_user(SENDER_ID, GROUP_ID)
    receiver = gift_context.get_user(RECEIVER_ID, GROUP_ID)
    assert sender is not None and receiver is not None
    assert sender.friendship_points == 2
    assert receiver.friendship_points == 2
    ledger = (
        gift_context._get_connection()
        .execute(
            "SELECT user_id, asset_type, delta FROM asset_ledger WHERE reason = 'gift' ORDER BY user_id"
        )
        .fetchall()
    )
    assert sorted((row["user_id"], row["asset_type"], row["delta"]) for row in ledger) == sorted(
        (user_id, "friendship_points", 2) for user_id in (SENDER_ID, RECEIVER_ID)
    )
    assert sender.today_gift_count == 1
    assert sender.total_gift_count == 1


def test_gift_cooldown_and_daily_limit_come_from_the_shared_quota_table(gift_context):
    """送礼的冷却和每日次数与互访、照料一样记在 action_quotas 里。"""

    def gift(cooldown_seconds: int, daily_limit: int = 3) -> tuple[bool, str]:
        return gift_context.gift_item_atomic(
            SENDER_ID,
            RECEIVER_ID,
            GROUP_ID,
            "cake",
            1,
            friendship_gain  = 2,
            daily_limit      = daily_limit,
            cooldown_seconds = cooldown_seconds,
        )

    assert gift(600) == (True, "")
    blocked, message = gift(600)
    assert not blocked and "送礼冷却中" in message

    conn = gift_context._get_connection()
    conn.execute("UPDATE action_quotas SET available_at = 0 WHERE action = 'gift'")
    conn.commit()
    limited, message = gift(0, daily_limit=1)
    assert not limited and message == "今日送礼次数已达上限"
    assert gift_context.get_or_create_inventory(SENDER_ID, GROUP_ID).items == {"cake": 9}
