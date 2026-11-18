"""``/qq``：Bot 管理员在私聊里看任意会话的聊天记录、让小青去别的会话说话、查群和成员、撤回、调 OneBot 动作。

谁能用由 plugin.json 声明、Core 检查（见 ``tests/core/test_app_plugin_capabilities.py``）；这里从命令入口
验证办成了什么。OneBot 是边界：发消息的 ``send_action`` 和带响应的 ``onebot_actions`` 都用替身。
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from core.delivery import receipt_from_action
from core.interfaces import PluginCapabilities
from plugins.xiaoqing_chat.message_parts import render_stored_message
from plugins.xiaoqing_chat.store_binding import bound_state
from tests.helpers.xiaoqing_chat_test_support import settle_chat_background, xiaoqing_chat

mock_context = _fixture_support.mock_context

ADMIN = 12345
GROUP = 300400500


def _ok(data: Any = None) -> dict[str, Any]:
    return {"status": "ok", "retcode": 0, "data": data}


class ScriptedOneBot:
    """``onebot_actions`` 的替身：按动作名给出写好的响应，记下每次调用。"""

    def __init__(self, **responses: dict[str, Any] | None) -> None:
        self.responses                               = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, action: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((action, dict(params or {})))
        return self.responses[action]


def _admin_private(context, onebot: ScriptedOneBot | None = None) -> None:
    """Bot 管理员在私聊里发命令时的现场；``onebot`` 是 Core 签发给他的 OneBot 操作能力。"""

    context.current_user_id  = ADMIN
    context.current_group_id = None
    context.capabilities = PluginCapabilities(is_bot_admin=True, onebot_actions=onebot)


async def _qq(context, args: str, *mentions: int) -> list[dict[str, Any]]:
    """管理员在私聊里发 ``/qq <args>``；``mentions`` 是命令里 ``[CQ:at,qq=…]`` 拆出的 @ 段。"""

    message = [{"type": "at", "data": {"qq": str(qq)}} for qq in mentions]
    message.append({"type": "text", "data": {"text": f"/qq {args}"}})
    event = {
        "post_type"   : "message",
        "message_type": "private",
        "self_id"     : 11111,
        "user_id"     : ADMIN,
        "message"     : message,
        "raw_message" : f"/qq {args}",
        "message_id"  : 1,
    }
    return await xiaoqing_chat.handle(command="qq", args=args, event=event, context=context)


def _text(result: list[dict[str, Any]]) -> str:
    return "".join(str(segment["data"]["text"]) for segment in result)


def _failed(result: list[dict[str, Any]]) -> bool:
    """命令有没有回报“没办成”：agent 据此知道这条没有完成。"""

    outcome = getattr(result, "command_outcome", None)
    return outcome is not None and outcome.state == "error"


def _delivering(*message_ids: int, delivered: bool | None = True) -> AsyncMock:
    """发送通道的替身：像 Core 一样把发出的消息 ID 记进回执，再回报送达结果。"""

    async def send(action: dict[str, Any]) -> bool | None:
        receipt = receipt_from_action(action)
        assert receipt is not None
        for message_id in message_ids:
            receipt.add_message_id(message_id)
        return delivered

    return AsyncMock(side_effect=send)


def _seed_group(context, tmp_path) -> str:
    """群里的一段聊天：一句话、一张她看过的图（原图还在本机）、她自己的一句。"""

    picture = tmp_path / "shiba.jpg"
    picture.write_bytes(b"jpeg")
    store = bound_state(context).memory_store
    now   = time.time()
    store.append(
        f"g{GROUP}",
        role       = "user",
        name       = "阿梨",
        user_id    = 7,
        message_id = 5520,
        content    = "早",
        ts         = now - 120,
    )
    store.append(
        f"g{GROUP}",
        role       = "user",
        name       = "阿梨",
        user_id    = 7,
        message_id = 5521,
        ts         = now - 60,
        parts      = [
            {"kind": "text", "text": "你们看这个"},
            {
                "kind"       : "image",
                "media_key"  : "k1",
                "marker"     : "[图片：一只柴犬趴在地板上]",
                "description": "一只柴犬趴在地板上",
                "file_path"  : str(picture),
            },
        ],
    )
    store.append(
        f"g{GROUP}", role="assistant", name="小青", message_id=5522, content="好困", ts=now
    )
    return str(picture)


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_look_shows_another_chat_as_she_saw_it(mock_context, tmp_path):
    _admin_private(mock_context)
    picture = _seed_group(mock_context, tmp_path)

    shown = _text(await _qq(mock_context, f"look g{GROUP}"))

    lines = shown.splitlines()
    assert len(lines) == 5 and f"g{GROUP}" in lines[0]
    # 说话人带 QQ 号，消息带 ID；图片是她当时看到的描述，原图还在时给出文件路径。
    assert "阿梨(7)" in lines[1] and "#5520" in lines[1] and lines[1].endswith("早")
    assert "#5521" in lines[2] and "你们看这个" in lines[2] and "一只柴犬趴在地板上" in lines[2]
    assert lines[3].strip().endswith(picture)
    assert "小青" in lines[4] and "#5522" in lines[4] and lines[4].endswith("好困")

    latest = _text(await _qq(mock_context, f"look g{GROUP} 1"))
    assert "好困" in latest and "你们看这个" not in latest


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_look_reports_a_chat_without_records_and_rejects_bad_arguments(mock_context):
    _admin_private(mock_context)

    empty = await _qq(mock_context, "look u10001")
    assert "没有聊天记录" in _text(empty) and not _failed(empty)
    for args in ("look", f"look {GROUP}", f"look g{GROUP} 0", f"look g{GROUP} 5 extra"):
        assert _failed(await _qq(mock_context, args)), args


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_say_speaks_in_the_group_and_becomes_her_own_line_there(mock_context, tmp_path):
    _admin_private(mock_context)
    _seed_group(mock_context, tmp_path)
    mock_context.send_action = _delivering(5531)

    result = await _qq(mock_context, f"say g{GROUP} 这只柴犬好可爱", 7)

    (sent,) = (call.args[0] for call in mock_context.send_action.await_args_list)
    assert sent["action"] == "send_group_msg" and sent["params"]["group_id"] == GROUP
    assert sent["params"]["message"] == [
        {"type": "at", "data": {"qq": "7"}},
        {"type": "text", "data": {"text": " 这只柴犬好可爱"}},
    ]
    assert not _failed(result) and "5531" in _text(result) and "这只柴犬好可爱" in _text(result)
    # 发出去的话记成她在那个群里的发言，带着消息 ID：群友引用它接话时认得出是在跟她说。
    last = bound_state(mock_context).memory_store.get(f"g{GROUP}")[-1]
    assert last.role == "assistant" and last.message_id == 5531
    assert render_stored_message(last) == "@7 这只柴犬好可爱"

    # 管理员自己发命令时 @ 留在内容里：就地换成 @ 段。
    await _qq(mock_context, f"say g{GROUP} 这是[CQ:at,qq=9]拍的吧")
    assert mock_context.send_action.await_args.args[0]["params"]["message"] == [
        {"type": "text", "data": {"text": "这是"}},
        {"type": "at", "data": {"qq": "9"}},
        {"type": "text", "data": {"text": " 拍的吧"}},
    ]
    await settle_chat_background(mock_context)


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_say_reaches_a_private_chat_without_mentions(mock_context):
    _admin_private(mock_context)
    mock_context.send_action = _delivering(77)

    await _qq(mock_context, "say u10001 我到了", 7)

    (sent,) = (call.args[0] for call in mock_context.send_action.await_args_list)
    assert sent["action"] == "send_private_msg" and sent["params"]["user_id"] == 10001
    assert sent["params"]["message"] == [{"type": "text", "data": {"text": "我到了"}}]
    assert bound_state(mock_context).memory_store.get("u10001")[-1].message_id == 77
    await settle_chat_background(mock_context)


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_say_that_was_not_delivered_is_reported_and_not_remembered(mock_context):
    _admin_private(mock_context)
    mock_context.send_action = _delivering(delivered=False)

    result = await _qq(mock_context, f"say g{GROUP} 在吗")

    assert _failed(result)
    assert bound_state(mock_context).memory_store.get(f"g{GROUP}") == []
    for args in ("say", f"say g{GROUP}", f"say {GROUP} 在吗"):
        assert _failed(await _qq(mock_context, args)), args
    assert mock_context.send_action.await_count == 1


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_groups_and_members_come_from_onebot(mock_context):
    onebot = ScriptedOneBot(
        get_group_list=_ok(
            [
                {"group_id": GROUP, "group_name": "观星台", "member_count": 3},
                {"group_id": 300400501, "group_name": "", "member_count": None},
            ]
        ),
        get_group_member_list=_ok(
            [
                {"user_id": 7, "nickname": "ali", "card": "阿梨", "role": "member"},
                {"user_id": 8, "nickname": "老周", "card": "", "role": "owner"},
                {"user_id": 9, "nickname": "栗子", "card": "栗子", "role": "admin"},
            ]
        ),
    )
    _admin_private(mock_context, onebot)

    groups = _text(await _qq(mock_context, "groups")).splitlines()
    assert f"g{GROUP} 观星台（3 人）" in groups and any("g300400501" in line for line in groups)

    members = _text(await _qq(mock_context, f"members {GROUP}")).splitlines()
    # 群主和管理员排在前面；名片和昵称不同时两个都给。
    assert members[1].startswith("8 老周") and members[2].startswith("9 栗子")
    assert members[3].startswith("7 阿梨") and "ali" in members[3]

    found = _text(await _qq(mock_context, f"members g{GROUP} 阿梨")).splitlines()
    assert len(found) == 2 and found[1].startswith("7 ")
    assert ("get_group_member_list", {"group_id": GROUP}) in onebot.calls
    assert _failed(await _qq(mock_context, "members"))


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_recall_withdraws_a_message_and_reports_refusal(mock_context):
    onebot = ScriptedOneBot(delete_msg=_ok())
    _admin_private(mock_context, onebot)

    done = await _qq(mock_context, "recall 5531")
    assert not _failed(done) and "5531" in _text(done)
    assert onebot.calls == [("delete_msg", {"message_id": 5531})]

    onebot.responses["delete_msg"] = {"status": "failed", "retcode": 1200, "wording": "消息太久"}
    refused                        = await _qq(mock_context, "recall -42")
    assert _failed(refused) and "消息太久" in _text(refused)
    assert _failed(await _qq(mock_context, "recall abc"))
    assert len(onebot.calls) == 2


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_call_runs_other_onebot_actions_and_returns_their_data(mock_context):
    onebot = ScriptedOneBot(
        get_login_info = _ok({"user_id": 11111, "nickname": "小青"}),
        set_group_ban  = _ok(),
        get_status     = None,
    )
    _admin_private(mock_context, onebot)

    info = await _qq(mock_context, "call get_login_info")
    assert not _failed(info) and '"user_id": 11111' in _text(info)

    params = '{"group_id": 300400500, "user_id": 7, "duration": 600}'
    assert not _failed(await _qq(mock_context, f"call set_group_ban {params}"))
    assert onebot.calls[-1] == ("set_group_ban", {"group_id": GROUP, "user_id": 7, "duration": 600})

    # 没有响应（连接不可用或结果未知）如实回报，不当成办成了。
    assert _failed(await _qq(mock_context, "call get_status"))
    calls = len(onebot.calls)
    # 说话要经 say 才会记进聊天记录；参数不是 JSON 对象时不调用。
    for args in (
        'call send_group_msg {"group_id": 300400500, "message": "hi"}',
        "call set_group_ban 123",
        "call set_group_ban {oops",
        "call",
    ):
        assert _failed(await _qq(mock_context, args)), args
    assert len(onebot.calls) == calls


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_onebot_failures_are_reported_as_not_done(mock_context):
    onebot = ScriptedOneBot(
        get_group_list=None, get_group_member_list={"status": "failed", "retcode": 1404}
    )
    _admin_private(mock_context, onebot)

    assert _failed(await _qq(mock_context, "groups"))
    assert _failed(await _qq(mock_context, f"members {GROUP}"))
    # 没有签发这项能力时（不是管理员私聊）同样办不成。
    _admin_private(mock_context)
    assert _failed(await _qq(mock_context, "groups"))


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_bare_qq_lists_every_subcommand(mock_context):
    _admin_private(mock_context)

    listing = _text(await _qq(mock_context, ""))

    for name in ("groups", "members", "look", "say", "recall", "call"):
        assert f"/qq {name}" in listing
