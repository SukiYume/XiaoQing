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
from plugins.xiaoqing_chat import turn_delivery
from plugins.xiaoqing_chat.chat_runtime import ChatRuntime
from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
from plugins.xiaoqing_chat.message_parts import build_text_message_parts
from plugins.xiaoqing_chat.smalltalk_models import GeneratedSmalltalkTurn, PreparedSmalltalkTurn
from tests.helpers.app_test_support import OneBotActionOutcomeUnknown, XiaoQingApp

temp_app_root = _app_support.temp_app_root

REPLY = "在呢\n\n刚下课\n\n你说吧"


class _OneBotWs:
    """按顺序给出每条消息的传输结果：True 发出并带回消息 ID，False 明确失败，None 结果未知。"""

    credentials_trusted = True

    def __init__(self, outcomes: list[bool | None]) -> None:
        self._outcomes        = list(outcomes)
        self.texts: list[str] = []

    def connected(self) -> bool:
        return True

    async def send_action(self, action: dict) -> bool:
        outcome = self._outcomes.pop(0)
        self.texts.append(action["params"]["message"][0]["data"]["text"])
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
        yield SimpleNamespace(record=record, rollback=rollback)


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
