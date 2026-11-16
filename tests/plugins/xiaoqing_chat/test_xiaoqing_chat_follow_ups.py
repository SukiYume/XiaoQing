"""对方连着发了几条时等他说完再回：识别排着的后续消息、让出这一轮、把“在跟她说话”带到下一条。"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

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
from plugins.xiaoqing_chat.store_binding import bound_state
from tests.helpers.xiaoqing_chat_test_support import (
    GROUP_GATE_OPEN,
    ScriptedChatModel,
    ack_reply_delivery,
    chat_message_event,
    scripted_chat_context,
    settle_chat_background,
)

mock_context       = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event

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


def _chatting_context(mock_context, model, backlog: list[dict[str, Any]]):
    """能跑完整一轮的群聊上下文：插话概率为 1，积压队列是 ``backlog``（测试可以随时往里加）。"""

    scripted_chat_context(mock_context, model, GROUP_GATE_OPEN)
    mock_context.capabilities = replace(
        mock_context.capabilities,
        inbound_backlog=SimpleNamespace(waiting=lambda: tuple(backlog)),
    )
    return mock_context


def _group_message(sample_group_event, text: str, message_id: int, **extra: Any) -> dict[str, Any]:
    return chat_message_event(sample_group_event, text, message_id=message_id, **extra)


@pytest.mark.asyncio
async def test_a_turn_with_a_follow_up_waiting_is_recorded_but_not_answered(
    mock_context, sample_group_event
) -> None:
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    # 同一句话她不会连着说两遍，每次回复的内容各不相同。
    model = ScriptedChatModel(reply=["在呢", "刚在看书，怎么啦"])
    backlog = [_pending(2, _text("帮我看个题"))]
    context = _chatting_context(mock_context, model, backlog)

    # 她是被叫到的：让给下一条之后，下一条也算在跟她说；没回的那句话一起带过去。
    result = await handle_smalltalk("小青", _group_message(sample_group_event, "小青", 1), context)
    assert result == [] and model.calls == []
    history = await bound_state(context).memory_store.get_async("g67890")
    assert [message.content for message in history] == ["小青"]
    next_event = _event(2, user_id=12345, group_id=67890)
    assert take_carried_attention(next_event) is True
    assert take_carried_texts(next_event) == ["小青"]

    # 显式命令照常回复，不让。
    forced = _group_message(sample_group_event, "小青", 3, _xc_command_forced=True)
    result = await handle_smalltalk("小青", forced, context)
    assert result == [{"type": "text", "data": {"text": "在呢"}}]
    await ack_reply_delivery(result)

    # 没人在等：照常生成。
    backlog.clear()
    result = await handle_smalltalk("小青", _group_message(sample_group_event, "小青", 4), context)
    assert result == [{"type": "text", "data": {"text": "刚在看书，怎么啦"}}]
    await ack_reply_delivery(result)
    await settle_chat_background(context, model)


@pytest.mark.asyncio
async def test_plain_interjections_yield_without_carrying_attention(
    mock_context, sample_group_event
) -> None:
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    model   = ScriptedChatModel()
    context = _chatting_context(mock_context, model, [_pending(2, _text("老板又骂人了"))])

    result = await handle_smalltalk(
        "今天好累", _group_message(sample_group_event, "今天好累", 1), context
    )

    # 插话概率放行了这一轮，让出来的是它：话记进了聊天记录，没有生成回复，也不算在叫她。
    assert result == [] and model.calls == []
    history = await bound_state(context).memory_store.get_async("g67890")
    assert [message.content for message in history] == ["今天好累"]
    assert take_carried_attention(_event(2, user_id=12345, group_id=67890)) is False
    await settle_chat_background(context, model)


@pytest.mark.asyncio
async def test_a_generated_reply_is_withheld_when_the_sender_kept_typing(
    mock_context, sample_group_event
) -> None:
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    backlog: list[dict[str, Any]] = []
    typing_during_generation      = True
    replies                       = iter(["怎么了", "刚在看书，咋啦", "来了来了，说吧"])

    def keeps_typing(_call) -> str:
        # 模型生成回复的这段时间里，对方又发来了一条。
        if typing_during_generation:
            backlog.append(_pending(2, _text("考试挂了")))
        return next(replies)

    model = ScriptedChatModel(reply=keeps_typing)
    context = _chatting_context(mock_context, model, backlog)

    result = await handle_smalltalk(
        "小青，我今天", _group_message(sample_group_event, "小青，我今天", 1), context
    )

    assert result == [] and len(model.calls_of("reply")) == 1
    next_event = _event(2, user_id=12345, group_id=67890)
    assert take_carried_attention(next_event) is True
    assert take_carried_texts(next_event) == ["小青，我今天"]

    # 显式命令照常发出，哪怕生成期间对方又发了。
    backlog.clear()
    command = _group_message(sample_group_event, "小青，我今天", 3, _xc_command_forced=True)
    result = await handle_smalltalk("小青，我今天", command, context)
    assert result == [{"type": "text", "data": {"text": "刚在看书，咋啦"}}]
    await ack_reply_delivery(result)

    # 生成期间没有新消息：照常发出。
    typing_during_generation = False
    backlog.clear()
    result = await handle_smalltalk(
        "小青，我今天", _group_message(sample_group_event, "小青，我今天", 4), context
    )
    assert result == [{"type": "text", "data": {"text": "来了来了，说吧"}}]
    await ack_reply_delivery(result)
    await settle_chat_background(context, model)


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
