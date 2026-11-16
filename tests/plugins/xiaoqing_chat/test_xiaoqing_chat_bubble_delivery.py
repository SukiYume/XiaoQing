"""多气泡回复经真实 Core 发送：聊天记录只记真正发出去（或可能已被看到）的气泡。

Core 在 ``send_action()`` 返回之前就按传输结果结算动作上的回执，所以这里不替换 Core
的发送和结算，只在 OneBot 传输层模拟每个气泡的结果。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tests.helpers.app_test_support as _app_support
import tests.helpers.xiaoqing_chat_test_support as _chat_support
from core.delivery import resolve_action_handoff
from core.interfaces import ACTION_RESULT_MESSAGE_ID_KEY
from plugins.xiaoqing_chat import turn_delivery
from plugins.xiaoqing_chat.handler_context import HandlerContext
from plugins.xiaoqing_chat.media.qq_face_catalog import load_qq_face_catalog
from plugins.xiaoqing_chat.message_parts import build_text_message_parts
from plugins.xiaoqing_chat.runtime_state import get_state
from plugins.xiaoqing_chat.smalltalk_models import GeneratedSmalltalkTurn, PreparedSmalltalkTurn
from plugins.xiaoqing_chat.store_binding import bound_state
from tests.helpers.app_test_support import OneBotActionOutcomeUnknown, XiaoQingApp
from tests.helpers.xiaoqing_chat_test_support import (
    ScriptedChatModel,
    scripted_chat_context,
    settle_chat_background,
)

temp_app_root = _app_support.temp_app_root
mock_context  = _chat_support.mock_context

REPLY   = "在呢\n\n刚下课\n\n你说吧"
CHAT_ID = "u7"


class _OneBotWs:
    """按顺序给出每条消息的传输结果：True 发出并带回消息 ID，False 明确失败，None 结果未知。"""

    credentials_trusted = True

    def __init__(self, outcomes: list[bool | None]) -> None:
        self._outcomes                  = list(outcomes)
        self.texts: list[str]           = []
        self.messages: list[list[dict]] = []

    def connected(self) -> bool:
        return True

    async def send_action(self, action: dict) -> bool:
        outcome = self._outcomes.pop(0)
        message = action["params"]["message"]
        self.messages.append(message)
        self.texts.append("".join(seg["data"]["text"] for seg in message if seg["type"] == "text"))
        if outcome is None:
            raise OneBotActionOutcomeUnknown(action["action"])
        if outcome:
            action[ACTION_RESULT_MESSAGE_ID_KEY] = 100 + len(self.texts)
        return outcome


def _app_with_turn(root: Path, context, ws: _OneBotWs | None):
    """真实 Core 应用，事件分发到一轮三气泡的回复；中间气泡经 Core 的 ``_send_action`` 发出。

    聊天记录、行动历史、媒体登记都用真实的运行时状态（数据在 ``context.data_dir`` 下），
    要接的那句用户消息（本地 ID ``u1``）已经在聊天记录里。
    """

    # Core 应用一建好就挡住插件模块的惰性导入，所以先把聊天运行时加载好。
    hctx                = HandlerContext.from_event(_event(), context)
    app                 = XiaoQingApp(root)
    app.ws_client       = ws
    app.http_sender     = None
    app.inbound_manager = None

    generated = GeneratedSmalltalkTurn(
        local_id    = "u1",
        reply       = REPLY,
        reply_parts = build_text_message_parts(REPLY),
    )
    prepared = PreparedSmalltalkTurn(
        text              = "在吗",
        mentioned         = True,
        is_private        = True,
        forced            = True,
        force_reason      = "test",
        brain_chat_active = False,
        mood_text         = "",
    )
    bound_state(context).memory_store.append(
        CHAT_ID, role="user", name="朋友", user_id=7, local_id="u1", content="在吗", ts=time.time()
    )
    context.send_action = app._send_action

    async def finalize(event):
        return await turn_delivery.finalize_smalltalk_turn(
            prepared, generated, event, context, hctx, started_at=time.monotonic()
        )

    app.dispatcher.handle_event = finalize
    return app, generated


def _event() -> dict:
    return {"post_type": "message", "message_type": "private", "user_id": 7}


@pytest.fixture
def turn_model(mock_context):
    """投递这一段不该请求模型，所以不写任何剧本；打字延迟关掉，气泡之间不等。"""

    model = ScriptedChatModel()
    scripted_chat_context(
        mock_context,
        model,
        {"humanize": {"enable_typing_delay": False, "type_per_char_seconds": 0.0}},
    )
    return model


async def _finish(context, model: ScriptedChatModel) -> None:
    """等回复送达之后的后台任务（落盘、记媒体使用次数）做完，再用插件自己的关闭流程收尾。"""

    tasks = get_state().background_tasks()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await settle_chat_background(context, model)


def _bot_replies() -> list:
    """聊天记录里小青说过的话。"""

    return [
        message for message in get_state().memory_store.get(CHAT_ID) if message.role == "assistant"
    ]


async def _action_records() -> list:
    return await get_state().action_history.get_recent_async(CHAT_ID)


async def _assert_delivery_settled() -> None:
    """这一轮的回复已经结算：同会话的下一轮不用再等它。"""

    async with asyncio.timeout(1):
        async with get_state().generation_limiter.admit(
            chat_id                    = CHAT_ID,
            user_id                    = "7",
            max_global                 = 0,
            max_per_chat               = 0,
            max_per_user               = 0,
            max_calls_per_user_per_day = 0,
        ):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcomes,sent_texts,recorded_text,message_ids",
    [
        # 第一条成功、第二条明确失败：已发出的第一条照样记，第三条不再发。
        ([True, False], ["在呢", "刚下课"], "在呢", (101,)),
        # 第一条成功、第二条结果未知：第二条可能已被看到要记，没发的第三条不记。
        ([True, None], ["在呢", "刚下课"], "在呢\n\n刚下课", (101,)),
        # 最后一条（由 Core 在处理完事件后发出）失败：只记前两条。
        ([True, True, False], ["在呢", "刚下课", "你说吧"], "在呢\n\n刚下课", (101, 102)),
        # 三条都发出：整条回复连同每个消息 ID 都记。
        ([True, True, True], ["在呢", "刚下课", "你说吧"], REPLY, (101, 102, 103)),
    ],
)
async def test_reply_records_only_bubbles_that_went_out(
    temp_app_root, mock_context, turn_model, outcomes, sent_texts, recorded_text, message_ids
) -> None:
    ws = _OneBotWs(outcomes)
    app, _generated = _app_with_turn(temp_app_root, mock_context, ws)

    await app._handle_upstream_event(_event())
    await _finish(mock_context, turn_model)

    assert ws.texts == sent_texts
    (reply,) = _bot_replies()
    assert reply.parts == build_text_message_parts(recorded_text)
    assert reply.remote_ids() == message_ids
    assert len(await _action_records()) == 1
    await _assert_delivery_settled()


@pytest.mark.asyncio
async def test_reply_whose_first_bubble_fails_is_rolled_back(
    temp_app_root, mock_context, turn_model
) -> None:
    ws = _OneBotWs([False])
    app, _generated = _app_with_turn(temp_app_root, mock_context, ws)

    await app._handle_upstream_event(_event())
    await _finish(mock_context, turn_model)

    assert ws.texts == ["在呢"]
    assert _bot_replies() == []
    assert await _action_records() == []
    await _assert_delivery_settled()


@pytest.mark.asyncio
async def test_collected_bubbles_settle_when_the_server_actually_sends_them(
    temp_app_root, mock_context, turn_model
) -> None:
    """反向 WS 下气泡先被 Core 收起来，等服务端真正写出后才有结果，结算也要等到那时。"""

    app, _generated = _app_with_turn(temp_app_root, mock_context, None)

    actions = await app._collect_actions_for_event(_event(), default_source="inbound_ws")
    assert len(actions) == 3
    assert _bot_replies() == []

    # 与服务端逐条写出的顺序一致：第二条写出失败，剩下的不再写，按失败结算。
    for action, delivered in zip(actions, (True, False, False), strict=True):
        await resolve_action_handoff(action, delivered=delivered)
    await _finish(mock_context, turn_model)

    (reply,) = _bot_replies()
    assert reply.parts == build_text_message_parts("在呢")
    assert reply.remote_ids() == ()
    await _assert_delivery_settled()


def _emoji_usage(context) -> int:
    index = context.data_dir / "media" / "library" / "index.json"
    return int(json.loads(index.read_text(encoding="utf-8"))["entries"]["test-hash"]["usage_count"])


async def _face_usage(context, face_id: str) -> int:
    return sum(
        entry.usage_count
        for entry in await load_qq_face_catalog(context)
        if entry.face_id == face_id
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image", "emoji", "qq_face", "unicode_face"])
@pytest.mark.parametrize("media_bubble", [0, 1, 2])
@pytest.mark.parametrize(
    "outcomes,seen",
    [
        ([True, False], [0]),
        ([True, None], [0, 1]),
        ([True, True, False], [0, 1]),
        ([True, True, None], [0, 1, 2]),
        ([True, True, True], [0, 1, 2]),
        ([False], []),
        ([None], [0]),
    ],
)
async def test_media_delivery_scope(
    temp_app_root, mock_context, turn_model, kind, media_bubble, outcomes, seen
):
    ws = _OneBotWs(outcomes)
    app, generated = _app_with_turn(temp_app_root, mock_context, ws)
    media = {
        "kind"       : "qq_face" if kind == "unicode_face" else kind,
        "media_key"  : "media:test",
        "marker"     : "[媒体:测试]",
        "mode"       : "text_with_media",
        "description": "测试媒体",
    }
    if kind in ("qq_face", "unicode_face"):
        media.update(face_id="14" if kind == "qq_face" else "🚀", label="微笑")
    else:
        media.update(media_hash="test-hash", file_path=str(mock_context.data_dir / "test.png"))
    parts = []
    for index, word in enumerate(("看这个", "第二条", "第三条")):
        parts.append({"kind": "text", "text": ("\n\n" if index else "") + word})
        if index == media_bubble:
            parts.append(media)
    generated.reply_parts = tuple(parts)
    # 保留生成标记，验证结算内容决定历史与使用统计。
    generated.media_marker = MagicMock(kind=media["kind"], marker=media["marker"])
    # 表情库里已有这张表情（使用次数只给已有条目加）。
    library = mock_context.data_dir / "media" / "library"
    library.mkdir(parents=True)
    (library / "index.json").write_text(
        json.dumps({"entries": {"test-hash": {"media_hash": "test-hash", "usage_count": 0}}}),
        encoding="utf-8",
    )

    await app._handle_upstream_event(_event())
    await _finish(mock_context, turn_model)

    assert len(ws.messages) == len(outcomes)
    expected_media = media_bubble in seen
    assert _emoji_usage(mock_context) == int(expected_media and kind == "emoji")
    if kind in ("qq_face", "unicode_face"):
        assert await _face_usage(mock_context, media["face_id"]) == int(expected_media)
    await _assert_delivery_settled()
    if not seen:
        assert _bot_replies() == []
        assert await _action_records() == []
        return
    (reply,) = _bot_replies()
    assert reply.remote_ids() == tuple(
        101 + i for i, outcome in enumerate(outcomes) if outcome is True
    )
    assert [part for part in reply.parts if part["kind"] != "text"] == (
        [media] if expected_media else []
    )
    (record,) = await _action_records()
    assert record.detail.get("media_key") == ("media:test" if expected_media else None)
    assert record.detail.get("media_kind") == (media["kind"] if expected_media else None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcomes,expected",
    [
        ((True, None, False), ["one", "two"]),
        ((True, False, True), ["one", "three"]),
        ((False, False, False), []),
    ],
)
async def test_collected_media_receipts_settle_once(
    temp_app_root, mock_context, turn_model, outcomes, expected
):
    app, generated = _app_with_turn(temp_app_root, mock_context, None)
    generated.reply_parts = (
        {"kind": "image", "media_key": "one", "file_path": str(mock_context.data_dir / "one.png")},
        {"kind": "text", "text": "\n\n"},
        {"kind": "qq_face", "media_key": "two", "face_id": "14"},
        {"kind": "text", "text": "\n\n"},
        {
            "kind"      : "emoji",
            "media_key" : "three",
            "media_hash": "third",
            "file_path" : str(mock_context.data_dir / "three.png"),
        },
    )
    actions = await app._collect_actions_for_event(_event(), default_source="inbound_ws")
    assert len(actions) == 3
    assert _bot_replies() == []
    # 乱序回执仍按发送顺序合并，并等待全部在途批次结算。
    for index in (2, 0):
        await resolve_action_handoff(actions[index], delivered=outcomes[index])
        assert _bot_replies() == []
    await resolve_action_handoff(actions[1], delivered=outcomes[1])
    for action in actions:
        await resolve_action_handoff(action, delivered=True)
    await _finish(mock_context, turn_model)

    await _assert_delivery_settled()
    if expected:
        (reply,) = _bot_replies()
        assert [p["media_key"] for p in reply.parts if p["kind"] != "text"] == expected
        assert reply.remote_ids() == ()
    else:
        assert _bot_replies() == []
        assert await _action_records() == []


@pytest.mark.asyncio
async def test_full_delivery_excludes_media_without_a_sendable_segment(
    temp_app_root, mock_context, turn_model
):
    ws = _OneBotWs([True])
    app, generated = _app_with_turn(temp_app_root, mock_context, ws)
    generated.reply_parts = (
        {"kind": "text", "text": "文字"},
        {"kind": "emoji", "media_hash": "missing-file", "marker": "[表情包:缺失]"},
    )
    await app._handle_upstream_event(_event())
    await _finish(mock_context, turn_model)

    (reply,) = _bot_replies()
    assert reply.parts == build_text_message_parts("文字")
    assert reply.remote_ids() == (101,)
