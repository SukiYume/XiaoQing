"""已到达、还没开始处理的消息登记：按发送者隔离、开始处理即注销、过期自动失效。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.capabilities import InboundBacklogService
from core.inbound_backlog import InboundBacklog
from core.onebot import OneBotWsClient
from core.server import _InboundEventDispatcher


def _message(message_id: int, text: str, *, user_id: int = 7, group_id: int | None = None) -> dict:
    event: dict[str, Any] = {
        "post_type"  : "message",
        "user_id"    : user_id,
        "message_id" : message_id,
        "message"    : [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
        "time"       : 1_800_000_000,
        "_auth_token": "must-not-leak",
    }
    if group_id is not None:
        event["group_id"] = group_id
    return event


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.unit
def test_waiting_lists_only_this_senders_unstarted_messages_in_this_chat() -> None:
    backlog = InboundBacklog()
    first, second = _message(1, "我今天"), _message(2, "考试挂了")
    backlog.arrived(first)
    backlog.arrived(second)
    backlog.arrived(_message(3, "别人的话", user_id=8))
    backlog.arrived(_message(4, "群里说的", group_id=55))

    backlog.started(first)
    waiting = backlog.waiting(7, None)
    assert [item["message_id"] for item in waiting] == [2]
    assert waiting[0]["raw_message"] == "考试挂了"
    # 只给判断“是不是接着说”用得上的字段，传输层的东西不出去。
    assert set(waiting[0]) == {"message_id", "message", "raw_message", "time"}
    assert [item["message_id"] for item in backlog.waiting(7, 55)] == [4]
    assert backlog.waiting(9, None) == ()
    assert backlog.waiting(None, None) == ()

    backlog.started(second)
    assert backlog.waiting(7, None) == ()


@pytest.mark.unit
def test_returned_events_are_copies() -> None:
    backlog = InboundBacklog()
    backlog.arrived(_message(1, "原话"))
    backlog.waiting(7, None)[0]["raw_message"] = "被改了"
    assert backlog.waiting(7, None)[0]["raw_message"] == "原话"


@pytest.mark.unit
def test_registrations_expire_and_duplicates_of_started_messages_are_ignored() -> None:
    clock = _Clock()
    backlog = InboundBacklog(clock=clock)
    backlog.arrived(_message(1, "通道丢掉了，永远不会开始处理"))
    clock.now += 59
    assert len(backlog.waiting(7, None)) == 1
    clock.now += 2
    assert backlog.waiting(7, None) == ()

    # 另一个通道又送来一份已经在处理的消息：不能把它登记成“还在等”。
    running = _message(2, "正在处理")
    backlog.arrived(running)
    backlog.started(running)
    backlog.arrived(dict(running))
    assert backlog.waiting(7, None) == ()


@pytest.mark.unit
def test_only_message_events_with_a_sender_and_an_id_are_registered() -> None:
    backlog = InboundBacklog()
    backlog.arrived({"post_type": "notice", "user_id": 7, "message_id": 1})
    backlog.arrived({"post_type": "message", "message_id": 2})
    backlog.arrived({"post_type": "message", "user_id": 7})
    backlog.started({"post_type": "meta_event"})
    assert backlog.waiting(7, None) == ()


@pytest.mark.unit
def test_one_sender_cannot_grow_the_registry_without_bound() -> None:
    backlog = InboundBacklog()
    for index in range(200):
        backlog.arrived(_message(index, "刷屏"))
    assert len(backlog.waiting(7, None)) == 20


@pytest.mark.unit
def test_capability_exposes_exactly_the_bound_lookup() -> None:
    backlog = InboundBacklog()
    backlog.arrived(_message(1, "在吗", group_id=55))
    service = InboundBacklogService(lambda: backlog.waiting(7, 55))
    assert [item["message_id"] for item in service.waiting()] == [1]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_upstream_ws_registers_events_when_they_are_queued() -> None:
    backlog = InboundBacklog()
    client  = OneBotWsClient("ws://127.0.0.1:1", "token")
    client.set_on_event_queued(backlog.arrived)
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(event: dict[str, Any]) -> None:
        backlog.started(event)
        entered.set()
        await release.wait()

    try:
        await client._dispatch_event(handler, _message(1, "小青"))
        await asyncio.wait_for(entered.wait(), 5)
        await client._dispatch_event(handler, _message(2, "我今天考试挂了"))
        # 第一条还在处理，第二条排在同一个人的队里。
        assert [item["message_id"] for item in backlog.waiting(7, None)] == [2]
    finally:
        release.set()
        await client.stop()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_a_failing_queue_observer_does_not_drop_the_event() -> None:
    client  = OneBotWsClient("ws://127.0.0.1:1", "token")
    handled = asyncio.Event()

    def broken(_event: dict[str, Any]) -> None:
        raise RuntimeError("observer bug")

    async def handler(_event: dict[str, Any]) -> None:
        handled.set()

    client.set_on_event_queued(broken)
    try:
        await client._dispatch_event(handler, _message(1, "照常处理"))
        await asyncio.wait_for(handled.wait(), 5)
    finally:
        await client.stop()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_inbound_dispatcher_registers_events_when_they_are_admitted() -> None:
    backlog = InboundBacklog()
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(event: dict[str, Any]) -> list[dict[str, Any]]:
        backlog.started(event)
        entered.set()
        await release.wait()
        return []

    dispatcher = _InboundEventDispatcher(
        handler, max_workers=1, queue_size=8, on_admit=backlog.arrived
    )
    await dispatcher.start()
    first = asyncio.create_task(dispatcher.dispatch(_message(1, "小青", group_id=55)))
    await asyncio.wait_for(entered.wait(), 5)
    second = asyncio.create_task(dispatcher.dispatch(_message(2, "帮我看个题", group_id=55)))
    await asyncio.sleep(0)
    assert [item["message_id"] for item in backlog.waiting(7, 55)] == [2]
    release.set()
    await asyncio.gather(first, second)
    assert backlog.waiting(7, 55) == ()
    await dispatcher.stop()
