"""对方连着发了几条时等他说完再回：识别排着的后续消息、让出这一轮、把“在跟她说话”带到下一条。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from plugins.xiaoqing_chat import follow_ups
from plugins.xiaoqing_chat.follow_ups import (
    BURST_TEXTS_KEY,
    burst_text,
    carry_attention,
    carry_text,
    sender_has_follow_up,
    take_carried_attention,
    take_carried_texts,
)
from tests.helpers.xiaoqing_chat_test_support import _make_hctx

mock_context = _fixture_support.mock_context

BOT_ID = 11111


def _event(message_id: int = 1, *, group: bool = True, **extra: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "post_type" : "message",
        "self_id"   : BOT_ID,
        "user_id"   : 12345,
        "message_id": message_id,
        "message"   : [{"type": "text", "data": {"text": "小青"}}],
        **extra,
    }
    if group:
        event["group_id"] = 67890
    return event


def _pending(message_id: int, *segments: dict[str, Any], raw: str = "") -> dict[str, Any]:
    return {"message_id": message_id, "message": list(segments), "raw_message": raw}


def _text(value: str) -> dict[str, Any]:
    return {"type": "text", "data": {"text": value}}


def _at(target: int) -> dict[str, Any]:
    return {"type": "at", "data": {"qq": str(target)}}


def _context(*waiting: dict[str, Any], prefixes: tuple[str, ...] = ("/",)) -> SimpleNamespace:
    return SimpleNamespace(
        capabilities=SimpleNamespace(inbound_backlog=SimpleNamespace(waiting=lambda: waiting)),
        get_settings_snapshot=lambda: SimpleNamespace(config={"command_prefixes": list(prefixes)}),
    )


@pytest.fixture(autouse=True)
def _clean_carried():
    """带到下一轮的状态是进程内的：每个用例前后清空，不漏给别的用例。"""

    follow_ups._carried.clear()
    follow_ups._carried_texts.clear()
    yield
    follow_ups._carried.clear()
    follow_ups._carried_texts.clear()


@pytest.mark.parametrize(
    ("pending", "expected"),
    [
        (_pending(2, _text("我今天考试挂了")), True),
        (_pending(2, {"type": "image", "data": {"file": "a.png"}}), True),
        (_pending(2, _at(BOT_ID), _text("你说呢")), True),
        # 命令、单独一个链接（交给链接预览）、@ 了别人的话、只有引用的空消息、正在处理的这一条自己：都不算。
        (_pending(2, _text("/pendo todo list")), False),
        (_pending(2, _text("https://www.bilibili.com/video/BV1xx411c7mD")), False),
        (_pending(2, _text("看这个 https://www.bilibili.com/video/BV1xx411c7mD")), True),
        (_pending(2, _at(99999), _text("你也说说")), False),
        (_pending(2, {"type": "reply", "data": {"id": "5"}}), False),
        (_pending(1, _text("小青")), False),
    ],
)
def test_only_chat_messages_from_the_same_sender_count_as_follow_ups(pending, expected) -> None:
    assert sender_has_follow_up(_event(1), _context(pending)) is expected


def test_command_prefixes_come_from_the_bot_config() -> None:
    pending = _pending(2, _text("#查天气"))
    assert sender_has_follow_up(_event(1), _context(pending)) is True
    assert sender_has_follow_up(_event(1), _context(pending, prefixes=("/", "#"))) is False


def test_without_the_capability_nothing_is_ever_waiting() -> None:
    assert sender_has_follow_up(_event(1), SimpleNamespace(capabilities=SimpleNamespace())) is False
    assert sender_has_follow_up(_event(1), SimpleNamespace()) is False

    def broken() -> tuple[dict[str, Any], ...]:
        raise RuntimeError("backlog unavailable")

    failing = SimpleNamespace(
        capabilities=SimpleNamespace(inbound_backlog=SimpleNamespace(waiting=broken))
    )
    assert sender_has_follow_up(_event(1), failing) is False


def test_carried_attention_is_per_sender_and_used_once(monkeypatch) -> None:
    now = 1000.0
    monkeypatch.setattr(follow_ups.time, "monotonic", lambda: now)
    carry_attention(_event(1))
    assert take_carried_attention(_event(2, user_id=999)) is False
    assert take_carried_attention(_event(2)) is True
    assert take_carried_attention(_event(3)) is False

    carry_attention(_event(1))
    now += follow_ups.CARRY_SECONDS + 1
    assert take_carried_attention(_event(2)) is False


@pytest.mark.asyncio
async def test_a_turn_with_a_follow_up_waiting_is_recorded_but_not_answered(mock_context) -> None:
    from plugins.xiaoqing_chat import handlers

    event = _event(1)
    runtime = SimpleNamespace(cfg=SimpleNamespace(debug=SimpleNamespace(log_steps=False)))
    state = MagicMock()
    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    recorded = AsyncMock(return_value="m1")
    prepared = SimpleNamespace(text="小青", forced=True)
    waiting                   = _context(_pending(2, _text("帮我看个题")))
    mock_context.capabilities = waiting.capabilities

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(handlers, "ensure_user_message_recorded", recorded)
        assert await handlers._yields_to_follow_up(prepared, event, mock_context, hctx) is True
        recorded.assert_awaited_once()
        # 她是被叫到的：让给下一条之后，下一条也算在跟她说；没回的那句话一起带过去。
        assert take_carried_attention(_event(2)) is True
        assert take_carried_texts(_event(2)) == ["小青"]

        # 显式命令照常回复，不让。
        forced = {**event, "_xc_command_forced": True}
        assert await handlers._yields_to_follow_up(prepared, forced, mock_context, hctx) is False

        # 没人在等：照常生成。
        mock_context.capabilities = _context().capabilities
        assert await handlers._yields_to_follow_up(prepared, event, mock_context, hctx) is False


@pytest.mark.asyncio
async def test_plain_interjections_yield_without_carrying_attention(mock_context) -> None:
    from plugins.xiaoqing_chat import handlers

    runtime = SimpleNamespace(cfg=SimpleNamespace(debug=SimpleNamespace(log_steps=False)))
    hctx = _make_hctx(runtime=runtime, state=MagicMock(), context=mock_context)
    mock_context.capabilities = _context(_pending(2, _text("老板又骂人了"))).capabilities
    prepared = SimpleNamespace(text="今天好累", forced=False)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(handlers, "ensure_user_message_recorded", AsyncMock(return_value="m1"))
        assert await handlers._yields_to_follow_up(prepared, _event(1), mock_context, hctx)
    assert take_carried_attention(_event(2)) is False


def test_a_generated_reply_is_withheld_when_the_sender_kept_typing(mock_context) -> None:
    from plugins.xiaoqing_chat.turn_delivery import _SmalltalkFinalization, _yield_to_follow_up

    runtime = SimpleNamespace(cfg=SimpleNamespace(debug=SimpleNamespace(log_steps=False)))
    hctx = _make_hctx(runtime=runtime, state=MagicMock(), context=mock_context)
    generated          = MagicMock()
    generated.local_id = "m1"

    def finalization(event: dict[str, Any], *, forced: bool) -> _SmalltalkFinalization:
        return _SmalltalkFinalization(
            prepared=SimpleNamespace(forced=forced, text="我今天"),
            generated  = generated,
            event      = event,
            context    = mock_context,
            hctx       = hctx,
            started_at = 0.0,
        )

    mock_context.capabilities = _context(_pending(2, _text("考试挂了"))).capabilities
    assert _yield_to_follow_up(finalization(_event(1), forced=True), phase="after_presend_delay")
    generated.cancel_speculative_memory.assert_called_once()
    assert take_carried_attention(_event(2)) is True
    assert take_carried_texts(_event(2)) == ["我今天"]

    command = {**_event(1), "_xc_command_forced": True}
    assert not _yield_to_follow_up(finalization(command, forced=True), phase="after_presend_delay")

    mock_context.capabilities = _context().capabilities
    assert not _yield_to_follow_up(finalization(_event(1), forced=True), phase="before_last_batch")


def test_yielded_fragments_reach_the_next_turn_in_order(monkeypatch) -> None:
    now = 1000.0
    monkeypatch.setattr(follow_ups.time, "monotonic", lambda: now)
    first = _event(1)
    carry_text(first, "帮我记一笔")
    # 第二条也让出去：它带着第一条一起往后传。
    second = {**_event(2), BURST_TEXTS_KEY: take_carried_texts(_event(2))}
    carry_text(second, "18 块")
    third = {**_event(3), BURST_TEXTS_KEY: take_carried_texts(_event(3))}
    assert burst_text(third, "麻辣烫 微信") == "帮我记一笔\n18 块\n麻辣烫 微信"
    # 取过就没有了；没带东西的一轮原样返回。
    assert take_carried_texts(_event(4)) == []
    assert burst_text(_event(4), "晚上吃啥") == "晚上吃啥"

    carry_text(_event(5), "过期的话")
    now += follow_ups.CARRY_SECONDS + 1
    assert take_carried_texts(_event(6)) == []
