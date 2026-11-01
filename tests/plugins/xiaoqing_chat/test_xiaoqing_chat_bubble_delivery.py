"""多气泡回复经真实 Core 发送：聊天记录只记真正发出去（或可能已被看到）的气泡。

Core 在 ``send_action()`` 返回之前就按传输结果结算动作上的回执，所以这里不替换 Core
的发送和结算，只在 OneBot 传输层模拟每个气泡的结果。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.helpers.app_test_support as _app_support
from core.delivery import resolve_action_handoff
from core.interfaces import ACTION_RESULT_MESSAGE_ID_KEY
from plugins.xiaoqing_chat import reply_media, turn_delivery
from plugins.xiaoqing_chat.chat_runtime import ChatRuntime
from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
from plugins.xiaoqing_chat.media_registry import MediaRegistryStore
from plugins.xiaoqing_chat.message_parts import build_text_message_parts
from plugins.xiaoqing_chat.smalltalk_models import GeneratedSmalltalkTurn, PreparedSmalltalkTurn
from tests.helpers.app_test_support import OneBotActionOutcomeUnknown, XiaoQingApp

temp_app_root = _app_support.temp_app_root

REPLY = "在呢\n\n刚下课\n\n你说吧"


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


def _app_with_turn(root: Path, tmp_path: Path, ws: _OneBotWs | None):
    """真实 Core 应用，事件分发到一轮三气泡的回复；中间气泡经 Core 的 ``_send_action`` 发出。"""

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
    hctx = SimpleNamespace(
        runtime  = ChatRuntime(XiaoQingChatConfig(), []),
        state    = MagicMock(),
        chat_id  = "u7",
        bot_name = "小青",
    )
    hctx.state.media_store = MediaRegistryStore()
    hctx.state.memory_store.get_async = AsyncMock(return_value=[])
    hctx.state.heartflow.on_bot_reply_async = AsyncMock()
    context = SimpleNamespace(send_action=app._send_action, data_dir=tmp_path, logger=MagicMock())

    async def finalize(event):
        return await turn_delivery.finalize_smalltalk_turn(
            prepared, generated, event, context, hctx, started_at=0.0
        )

    app.dispatcher.handle_event = finalize
    return app, generated


def _event() -> dict:
    return {"post_type": "message", "message_type": "private", "user_id": 7}


@pytest.fixture
def recorded():
    """替换掉提交之后的记忆写入和后台任务，只看提交了哪些内容、哪些消息 ID。"""

    deliver = turn_delivery._record_delivered_reply

    def drop_task(_context, coro, *, name):
        coro.close()

    with (
        patch.object(turn_delivery, "_drop_stale_generated_turn", return_value=False),
        patch.object(
            turn_delivery, "_apply_smalltalk_presend_delay", AsyncMock(return_value=False)
        ),
        patch.object(turn_delivery, "_smalltalk_interbubble_delay", return_value=0.0),
        patch.object(
            turn_delivery,
            "sync_message_parts_to_registry",
            side_effect=lambda _state, parts, **_: tuple(parts),
        ),
        patch.object(turn_delivery, "spawn_bg_task", side_effect=drop_task),
        patch.object(turn_delivery, "log_step"),
        patch.object(
            turn_delivery, "_record_delivered_reply", AsyncMock(return_value=[])
        ) as record,
        patch.object(turn_delivery, "_rollback_smalltalk_delivery", AsyncMock()) as rollback,
    ):
        yield SimpleNamespace(record=record, rollback=rollback, deliver=deliver)


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
    ],
)
async def test_partially_sent_reply_records_only_bubbles_that_went_out(
    temp_app_root, tmp_path, recorded, outcomes, sent_texts, recorded_text, message_ids
) -> None:
    ws = _OneBotWs(outcomes)
    app, _generated = _app_with_turn(temp_app_root, tmp_path, ws)

    await app._handle_upstream_event(_event())

    assert ws.texts == sent_texts
    recorded.rollback.assert_not_awaited()
    recorded.record.assert_awaited_once()
    _finalization, parts, ids = recorded.record.await_args.args
    assert parts == build_text_message_parts(recorded_text)
    assert ids == message_ids


@pytest.mark.asyncio
async def test_fully_sent_reply_records_whole_reply_with_every_message_id(
    temp_app_root, tmp_path, recorded
) -> None:
    ws = _OneBotWs([True, True, True])
    app, generated = _app_with_turn(temp_app_root, tmp_path, ws)

    await app._handle_upstream_event(_event())

    assert ws.texts == ["在呢", "刚下课", "你说吧"]
    recorded.rollback.assert_not_awaited()
    _finalization, parts, ids = recorded.record.await_args.args
    assert parts == generated.reply_parts
    assert ids == (101, 102, 103)


@pytest.mark.asyncio
async def test_reply_whose_first_bubble_fails_is_rolled_back(
    temp_app_root, tmp_path, recorded
) -> None:
    ws = _OneBotWs([False])
    app, _generated = _app_with_turn(temp_app_root, tmp_path, ws)

    await app._handle_upstream_event(_event())

    assert ws.texts == ["在呢"]
    recorded.record.assert_not_awaited()
    recorded.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_collected_bubbles_settle_when_the_server_actually_sends_them(
    temp_app_root, tmp_path, recorded
) -> None:
    """反向 WS 下气泡先被 Core 收起来，等服务端真正写出后才有结果，结算也要等到那时。"""

    app, _generated = _app_with_turn(temp_app_root, tmp_path, None)

    actions = await app._collect_actions_for_event(_event(), default_source="inbound_ws")
    assert len(actions) == 3
    recorded.record.assert_not_awaited()

    # 与服务端逐条写出的顺序一致：第二条写出失败，剩下的不再写，按失败结算。
    for action, delivered in zip(actions, (True, False, False), strict=True):
        await resolve_action_handoff(action, delivered=delivered)

    recorded.rollback.assert_not_awaited()
    _finalization, parts, ids = recorded.record.await_args.args
    assert parts == build_text_message_parts("在呢")
    assert ids == ()


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
    temp_app_root, tmp_path, recorded, kind, media_bubble, outcomes, seen
):
    ws = _OneBotWs(outcomes)
    app, generated = _app_with_turn(temp_app_root, tmp_path, ws)
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
        media.update(media_hash="test-hash", file_path=str(tmp_path / "test.png"))
    parts = []
    for index, word in enumerate(("看这个", "第二条", "第三条")):
        parts.append({"kind": "text", "text": ("\n\n" if index else "") + word})
        if index == media_bubble:
            parts.append(media)
    generated.reply_parts = tuple(parts)
    # 保留生成标记，验证结算内容决定历史与使用统计。
    generated.media_marker = MagicMock(kind=media["kind"], marker=media["marker"])
    recorded.record.side_effect = recorded.deliver
    tasks                       = []

    def run_media_task(_context, coro, *, name):
        if name.startswith("reply_media_used:"):
            tasks.append(coro)
        else:
            coro.close()

    with (
        patch.object(turn_delivery, "spawn_bg_task", side_effect=run_media_task),
        patch.object(
            turn_delivery,
            "sync_message_parts_to_registry",
            side_effect=reply_media.sync_message_parts_to_registry,
        ),
        patch.object(reply_media, "schedule_media_registry_flush"),
        patch.object(turn_delivery, "schedule_memory_persist"),
        patch.object(turn_delivery, "schedule_action_history_flush"),
        patch.object(turn_delivery, "record_reply_frequency"),
        patch.object(reply_media, "mark_emoji_used_by_hash") as emoji_used,
        patch.object(reply_media, "mark_qq_face_used_by_id") as face_used,
    ):
        await app._handle_upstream_event(_event())
        for task in tasks:
            await task

    assert len(ws.messages) == len(outcomes)
    expected_media = media_bubble in seen
    assert emoji_used.call_count == int(expected_media and kind == "emoji")
    assert face_used.call_count == int(expected_media and kind in ("qq_face", "unicode_face"))
    if emoji_used.called:
        assert emoji_used.call_args.args[1] == "test-hash"
    if face_used.called:
        assert face_used.call_args.args[1] == media["face_id"]
    if not seen:
        recorded.record.assert_not_awaited()
        recorded.rollback.assert_awaited_once()
        assert tasks == []
        return
    recorded.record.assert_awaited_once()
    recorded.rollback.assert_not_awaited()
    finalization, delivered_parts, ids = recorded.record.await_args.args
    assert ids == tuple(101 + i for i, outcome in enumerate(outcomes) if outcome is True)
    assert [part for part in delivered_parts if part["kind"] != "text"] == (
        [media] if expected_media else []
    )
    state = finalization.hctx.state
    state.memory_store.append.assert_called_once()
    assert state.memory_store.append.call_args.kwargs["parts"] == delivered_parts
    assert state.memory_store.append.call_args.kwargs["message_ids"] == ids
    state.action_history.append.assert_called_once()
    detail = state.action_history.append.call_args.args[1].detail
    assert detail.get("media_key") == ("media:test" if expected_media else None)
    assert detail.get("media_kind") == (media["kind"] if expected_media else None)


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
    temp_app_root, tmp_path, recorded, outcomes, expected
):
    app, generated = _app_with_turn(temp_app_root, tmp_path, None)
    generated.reply_parts = (
        {"kind": "image", "media_key": "one", "file_path": str(tmp_path / "one.png")},
        {"kind": "text", "text": "\n\n"},
        {"kind": "qq_face", "media_key": "two", "face_id": "14"},
        {"kind": "text", "text": "\n\n"},
        {
            "kind"      : "emoji",
            "media_key" : "three",
            "media_hash": "third",
            "file_path" : str(tmp_path / "three.png"),
        },
    )
    actions = await app._collect_actions_for_event(_event(), default_source="inbound_ws")
    assert len(actions) == 3
    recorded.record.assert_not_awaited()
    # 乱序回执仍按发送顺序合并，并等待全部在途批次结算。
    for index in (2, 0):
        await resolve_action_handoff(actions[index], delivered=outcomes[index])
        recorded.record.assert_not_awaited()
        recorded.rollback.assert_not_awaited()
    await resolve_action_handoff(actions[1], delivered=outcomes[1])
    for action in actions:
        await resolve_action_handoff(action, delivered=True)
    if expected:
        recorded.record.assert_awaited_once()
        recorded.rollback.assert_not_awaited()
        _, parts, ids = recorded.record.await_args.args
        assert [p["media_key"] for p in parts if p["kind"] != "text"] == expected
        assert ids == ()
    else:
        recorded.record.assert_not_awaited()
        recorded.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_full_delivery_excludes_media_without_a_sendable_segment(
    temp_app_root, tmp_path, recorded
):
    ws = _OneBotWs([True])
    app, generated = _app_with_turn(temp_app_root, tmp_path, ws)
    generated.reply_parts = (
        {"kind": "text", "text": "文字"},
        {"kind": "emoji", "media_hash": "missing-file", "marker": "[表情包:缺失]"},
    )
    await app._handle_upstream_event(_event())
    recorded.record.assert_awaited_once()
    _, parts, ids = recorded.record.await_args.args
    assert parts == build_text_message_parts("文字")
    assert ids == (101,)
