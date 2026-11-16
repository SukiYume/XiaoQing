"""群聊里分清在叫谁：@ 对象保留、点名他人不抢答、配额排队与观望不阻塞。"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from core.message import extract_text
from plugins.xiaoqing_chat.chat_event import (
    MENTION_LABELS_KEY,
    is_bare_name_call,
    is_called_by_name,
)
from plugins.xiaoqing_chat.config.config import PersonalityConfig
from plugins.xiaoqing_chat.generation_limiter import (
    CHAT_SUPERSEDED,
    GenerationLimiter,
    GenerationLimitExceeded,
)
from plugins.xiaoqing_chat.llm.llm_config import LLMCallConfig
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from tests.helpers.reply_checker_test_support import checker_verdict
from tests.helpers.xiaoqing_chat_test_support import (
    GROUP_GATE_OPEN,
    ScriptedChatModel,
    ack_reply_delivery,
    chat_message_event,
    plan_answer,
    scripted_chat_context,
    settle_chat_background,
)

mock_context       = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event

BOT_ID   = 11111
OTHER_ID = "20003"


def _group_event(*segments: dict) -> dict:
    return {
        "self_id"   : BOT_ID,
        "group_id"  : 30001,
        "user_id"   : 10002,
        "message_id": 9,
        "message"   : list(segments),
        "sender"    : {"card": "阿禾"},
    }


def _at(qq, name: str | None = None) -> dict:
    data = {"qq": str(qq)}
    if name is not None:
        data["name"] = name
    return {"type": "at", "data": data}


def _text(text: str) -> dict:
    return {"type": "text", "data": {"text": text}}


def _state_with_history(*messages: StoredMessage):
    state = MagicMock()
    state.memory_store.get_recent_async = AsyncMock(return_value=list(messages))
    return state


async def _chat_text(event: dict, clean_text: str, state=None) -> str:
    from plugins.xiaoqing_chat.handlers import _chat_text_for_turn

    return await _chat_text_for_turn(
        clean_text, event, "小青", state or _state_with_history(), "g123456789"
    )


# @ 对象保留在本轮文本里


@pytest.mark.asyncio
async def test_mention_of_another_member_is_kept_with_recent_speaker_name():
    state = _state_with_history(
        StoredMessage(role="user", name="小橘", ts=1.0, user_id=int(OTHER_ID), content="在。")
    )
    event = _group_event(_at(OTHER_ID), _text(" 喂"))

    assert await _chat_text(event, "喂", state) == "@小橘 喂"
    assert event[MENTION_LABELS_KEY] == {OTHER_ID: "@小橘"}


@pytest.mark.asyncio
async def test_segment_name_is_used_without_reading_history():
    state = _state_with_history()
    event = _group_event(_text("你问问"), _at(OTHER_ID, "@小橘"), _text(" 咋回事"))

    assert await _chat_text(event, "你问问 咋回事", state) == "你问问@小橘 咋回事"
    state.memory_store.get_recent_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_member_and_everyone_mentions_get_readable_labels():
    event = _group_event(_at("all"), _text(" 今晚开黑"), _at("222"), _text(" 来不来"))

    assert await _chat_text(event, "今晚开黑 来不来") == "@全体成员 今晚开黑@用户222 来不来"


@pytest.mark.asyncio
async def test_bot_mention_keeps_vocative_alongside_other_mention():
    event = _group_event(_at(BOT_ID), _text(" "), _at("222", "Momo"), _text(" 你俩比一下"))

    assert await _chat_text(event, "你俩比一下") == "小青，@Momo 你俩比一下"


@pytest.mark.asyncio
async def test_message_without_other_mentions_is_unchanged():
    state = _state_with_history()
    event = _group_event(_at(BOT_ID), _text(" 你好"))

    assert await _chat_text(event, "你好", state) == "小青，你好"
    state.memory_store.get_recent_async.assert_not_awaited()


def test_media_message_parts_keep_mention_label():
    from plugins.xiaoqing_chat.media.event_media import _compose_effective_user_parts

    event = _group_event(
        _at(OTHER_ID), _text(" 看这个"), {"type": "image", "data": {"file": "x.png"}}
    )
    event[MENTION_LABELS_KEY] = {OTHER_ID: "@小橘"}

    parts = _compose_effective_user_parts(clean_text="@小橘 看这个", event=event, rendered_items=[])

    assert parts == ({"kind": "text", "text": "@小橘 看这个"},)


def test_effective_text_and_parts_interleave_media_in_message_order():
    from plugins.xiaoqing_chat.media.event_media import (
        _compose_effective_user_parts,
        _compose_effective_user_text,
    )
    from plugins.xiaoqing_chat.media.models import RenderedMedia

    event = _group_event(
        _text("先看这个"), {"type": "image", "data": {"file": "x.png"}}, _text("好笑吧")
    )
    rendered = [RenderedMedia("h1", "image", "一只猫在打哈欠", (), "[图片：一只猫在打哈欠]")]
    kwargs   = {"clean_text": "先看这个好笑吧", "event": event, "rendered_items": rendered}

    text  = _compose_effective_user_text(**kwargs)
    parts = _compose_effective_user_parts(**kwargs)

    assert text == "先看这个\n[图片：一只猫在打哈欠]\n好笑吧"
    assert [part["kind"] for part in parts] == ["text", "image", "text"]
    assert parts[1]["marker"] == "[图片：一只猫在打哈欠]"


# 点名他人不抢答


def _planner_kwargs(history: list[StoredMessage], current_text: str) -> dict:
    return {
        "secrets": {},
        "bot_name": "小青",
        "is_private": False,
        "personality": PersonalityConfig(),
        "history": history,
        "goal_list": [],
        "knowledge_list": [],
        "action_history_summary": "",
        "last_action_context": "",
        "timeout_context": "",
        "last_successful_reply_action": None,
        "llm": LLMCallConfig(temperature=0.7, top_p=0.9, max_tokens=256, timeout_seconds=1.0),
        "current_text": current_text,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["wait", "direct_reply"])
@pytest.mark.parametrize(
    "text",
    [
        "大家都散了，这事就到这吧",
        "大家刚才已经聊完了",
        "大家别再提这件事了",
        "大家有没有人还想来玩",
        "你们都说完了",
        "各位辛苦了，今天到这里",
        "今晚吃啥啊",
        "@全体成员 今晚谁来开黑",
        "你问问@小橘 他咋回事？",
        "我宣布，周一应该从一周里删掉",
        "论文终于投出去了!!",
        "发个狗头",
        "下次一定",
        "核心层读的是 caller 级名单。" * 8 + "你说哪个？",
    ],
)
async def test_fresh_group_turn_respects_planner_decision(monkeypatch, text, action):
    from plugins.xiaoqing_chat.planning import pfc_action_planner

    content   = json.dumps({"action": action, "reason": "上下文判断", "wait_seconds": 0})
    fake_chat = AsyncMock(
        return_value=({"choices": [{"message": {"content": content}}]}, "responses")
    )
    monkeypatch.setattr(pfc_action_planner, "chat_completions_raw_with_profile", fake_chat)
    history = [
        StoredMessage(role="user", name="Momo", ts=10.0, user_id=2, content="下次一定"),
        StoredMessage(role="user", name="阿禾", ts=500.0, user_id=1, content=text),
    ]

    plan = await pfc_action_planner.plan_next_action(**_planner_kwargs(history, text))

    assert plan.action == action
    assert plan.reason == "上下文判断"
    fake_chat.assert_awaited_once()
    prompt = fake_chat.await_args.kwargs["messages"][0]["content"]
    assert "本轮目标消息：\n" + text in prompt
    assert "下次一定" in prompt


def _group_chat_context(mock_context, model, *config: dict) -> object:
    """群里没叫她时插话概率为 1（门控必放行）的上下文。群聊统一进入规划器，开关只控制私聊。"""

    return scripted_chat_context(
        mock_context, model, GROUP_GATE_OPEN, {"planner": {"enable_planner": False}}, *config
    )


def _segments_event(sample_group_event, *segments: dict, **overrides) -> dict:
    """群里的一条消息，消息段由调用方给出；事件时间取现在。"""

    return chat_message_event(
        sample_group_event,
        extract_text({"message": list(segments)}),
        message=list(segments),
        **overrides,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "segments,fragment",
    [
        pytest.param([_text("不用回复了")], "不用回复了", id="bare-stop"),
        pytest.param([_text("阿梨，别回复我了")], "别回复我了", id="named-other-member"),
        pytest.param(
            [_at(OTHER_ID, "小明"), _text("别回复我了")], "别回复我了", id="at-other-member"
        ),
        pytest.param([_text("小明，不是叫你")], "不是叫你", id="not-talking-to-her"),
        pytest.param([_text("Momo，别回复了")], "别回复了", id="named-foreign-name"),
        pytest.param([_text("不用回复了吗？")], "不用回复了吗", id="stop-as-a-question"),
    ],
)
async def test_stop_requests_not_addressed_to_her_go_to_context_planning(
    mock_context, sample_group_event, segments, fragment
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    model = ScriptedChatModel(
        plan=plan_answer("direct_reply"), reply="好的", checker=checker_verdict()
    )
    _group_chat_context(mock_context, model)
    event = _segments_event(sample_group_event, *segments)

    result = await handle_smalltalk(extract_text(event), event, mock_context)

    # 这句话没有被当成静默指令吞掉，也没有被当成冲她来的：交给规划器按上下文判断。
    (plan_call,) = model.calls_of("plan")
    assert fragment in plan_call.text
    assert result == [{"type": "text", "data": {"text": "好的"}}]
    await ack_reply_delivery(result)
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "segments",
    [
        pytest.param([_text("小青，别回复了")], id="named-her"),
        pytest.param([_at(BOT_ID), _text("别回复了")], id="at-her"),
    ],
)
async def test_stop_request_addressed_to_her_still_gets_a_direct_reply(
    mock_context, sample_group_event, segments
):
    """冲着她说的“别回复了”不当静默指令吞掉：被叫到就直接回一句。"""

    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    model = ScriptedChatModel(
        plan=plan_answer("direct_reply"), reply="好的", checker=checker_verdict()
    )
    _group_chat_context(mock_context, model)
    event = _segments_event(sample_group_event, *segments)

    result = await handle_smalltalk(extract_text(event), event, mock_context)

    # 被叫到就直接回，不交给规划器判断要不要接。
    assert model.calls_of("plan") == []
    assert result == [{"type": "text", "data": {"text": "好的"}}]
    await ack_reply_delivery(result)
    await settle_chat_background(mock_context, model)


# 生成配额：同会话排队，最新的一轮接手


def _admit(limiter: GenerationLimiter, chat_id: str, user_id: str, *, wait: float = 5.0):
    return limiter.admit(
        chat_id                    = chat_id,
        user_id                    = user_id,
        max_global                 = 4,
        max_per_chat               = 1,
        max_per_user               = 1,
        max_calls_per_user_per_day = 100,
        chat_wait_seconds          = wait,
    )


async def _hold_slot(limiter: GenerationLimiter, entered: asyncio.Event, release: asyncio.Event):
    async with _admit(limiter, "g1", "u1"):
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=10)


@pytest.mark.asyncio
async def test_newer_turn_waits_for_chat_slot_instead_of_being_rejected():
    limiter = GenerationLimiter()
    entered, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold_slot(limiter, entered, release))
    owned  = [holder]
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)

        admitted = asyncio.Event()

        async def newer():
            async with _admit(limiter, "g1", "u2"):
                admitted.set()

        waiter = asyncio.create_task(newer())
        owned.append(waiter)
        async with asyncio.timeout(5):
            while "g1" not in limiter._chat_waiters:
                await asyncio.sleep(0)
        assert not admitted.is_set()

        release.set()
        await asyncio.wait_for(waiter, 1)
        await holder
        assert admitted.is_set()
    finally:
        release.set()
        for task in owned:
            task.cancel()
        await asyncio.gather(*owned, return_exceptions=True)


@pytest.mark.asyncio
async def test_queued_turn_is_superseded_by_a_newer_one():
    limiter = GenerationLimiter()
    entered, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold_slot(limiter, entered, release))
    owned  = [holder]
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)

        async def queued(user_id: str):
            async with _admit(limiter, "g1", user_id):
                return user_id

        older = asyncio.create_task(queued("u2"))
        owned.append(older)
        async with asyncio.timeout(5):
            while "g1" not in limiter._chat_waiters:
                await asyncio.sleep(0)
        newer = asyncio.create_task(queued("u3"))
        owned.append(newer)

        with pytest.raises(GenerationLimitExceeded, match=CHAT_SUPERSEDED):
            await asyncio.wait_for(older, 1)
        release.set()
        assert await asyncio.wait_for(newer, 1) == "u3"
        await holder
    finally:
        release.set()
        for task in owned:
            task.cancel()
        await asyncio.gather(*owned, return_exceptions=True)


@pytest.mark.asyncio
async def test_chat_slot_wait_is_bounded_and_other_chats_are_unaffected():
    limiter = GenerationLimiter()
    entered, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold_slot(limiter, entered, release))
    owned  = [holder]
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)

        with pytest.raises(GenerationLimitExceeded, match="chat_inflight"):
            async with _admit(limiter, "g1", "u2", wait=0.05):
                pass
        with pytest.raises(GenerationLimitExceeded, match="chat_inflight"):
            async with _admit(limiter, "g1", "u2", wait=0):
                pass
        async with _admit(limiter, "g2", "u3", wait=0):
            pass

        release.set()
        await holder
    finally:
        release.set()
        for task in owned:
            task.cancel()
        await asyncio.gather(*owned, return_exceptions=True)


@pytest.mark.asyncio
async def test_superseded_forced_turn_does_not_send_busy_notice(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk
    from plugins.xiaoqing_chat.store_binding import bound_state

    first_generating = asyncio.Event()
    release_first    = asyncio.Event()
    replies          = iter(["来了来了", "刚好看到，怎么啦"])

    async def reply(_call) -> str:
        answer = next(replies)
        if not first_generating.is_set():
            # 第一轮占着会话的生成名额，等到后面两轮都到了才放开。
            first_generating.set()
            await release_first.wait()
        return answer

    model = ScriptedChatModel(reply=reply, checker=checker_verdict())
    scripted_chat_context(mock_context, model)
    limiter = bound_state(mock_context).generation_limiter

    def called(user_id: int, message_id: int, text: str) -> dict:
        return chat_message_event(sample_group_event, text, user_id=user_id, message_id=message_id)

    turns = [
        asyncio.create_task(
            handle_smalltalk("小青，在吗", called(111, 1, "小青，在吗"), mock_context)
        )
    ]
    try:
        await asyncio.wait_for(first_generating.wait(), timeout=5)
        # 第二轮排队等名额；第三轮到了，第二轮就让给它。
        turns.append(
            asyncio.create_task(
                handle_smalltalk("小青，你好", called(222, 2, "小青，你好"), mock_context)
            )
        )
        async with asyncio.timeout(5):
            while "g67890" not in limiter._chat_waiters:
                await asyncio.sleep(0)
        turns.append(
            asyncio.create_task(
                handle_smalltalk("小青，看一下", called(333, 3, "小青，看一下"), mock_context)
            )
        )

        # 被顶替的这一轮是被叫到的，但不说“消息有点多”：由新一轮接手。
        assert await asyncio.wait_for(turns[1], timeout=5) == []

        release_first.set()
        first = await asyncio.wait_for(turns[0], timeout=5)
        assert first == [{"type": "text", "data": {"text": "来了来了"}}]
        await ack_reply_delivery(first)
        third = await asyncio.wait_for(turns[2], timeout=5)
        assert third == [{"type": "text", "data": {"text": "刚好看到，怎么啦"}}]
        await ack_reply_delivery(third)
    finally:
        release_first.set()
        for turn in turns:
            turn.cancel()
        await asyncio.gather(*turns, return_exceptions=True)
    await settle_chat_background(mock_context, model)


# 规划器观望不阻塞


@pytest.mark.asyncio
async def test_planner_wait_ends_turn_without_sleeping(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk
    from plugins.xiaoqing_chat.store_binding import bound_state

    wait = json.dumps(
        {"action": "wait", "reason": "给他们留空间", "wait_seconds": 20}, ensure_ascii=False
    )
    model = ScriptedChatModel(plan=wait)
    _group_chat_context(mock_context, model)
    event = chat_message_event(sample_group_event, "那大概几点")

    # 时间是边界：要是真的去等那 20 秒，睡眠替身会被调用。
    with patch.object(asyncio, "sleep", AsyncMock()) as sleep:
        result = await handle_smalltalk("那大概几点", event, mock_context)

    assert result == []
    assert len(model.calls_of("plan")) == 1 and model.calls_of("reply") == []
    records = await bound_state(mock_context).action_history.get_recent_async("g67890", max_items=5)
    assert [record.action for record in records] == ["wait"]
    sleep.assert_not_awaited()
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["wait", "direct_reply"])
@pytest.mark.parametrize(
    "text,entry",
    [
        ("我买了一袋小青菜，晚点做饭", "plain"),
        ("阿梨，小青今天没上线", "plain"),
        ("阿梨已经下班了，她在吗", "plain"),
        ("不用@她，她能看到", "plain"),
        ("明天的车票买好了", "other_at"),
        ("他让我不要回复陌生短信", "plain"),
        ("我跟客服说不用回复了", "plain"),
        ("大家都散了，这事就到这吧", "plain"),
        ("没什么事了", "plain"),
        ("我们已经等了好几天", "plain"),
        ("大家有没有人还想来玩", "plain"),
        # @ 了别人时，句首的名字是在跟别人说她。
        ("小青昨天说过这个", "other_at"),
    ],
)
async def test_group_entry_reaches_context_planner(
    mock_context, sample_group_event, text, entry, action
):
    result, model, state = await _run_group_turn(
        mock_context, sample_group_event, text, entry, action
    )
    # 规划器被问到了（这一轮不是被叫到的）：它看到了这句话和群里刚说过的话，并按它的决定走。
    (plan_call,) = model.calls_of("plan")
    assert text in plan_call.text and "刚才已经回答了" in plan_call.text
    assert len(model.calls_of("reply")) == (1 if action == "direct_reply" else 0)
    assert bool(result) is (action == "direct_reply")
    records = await state.action_history.get_recent_async("g67890", max_items=5)
    assert records[-1].action == action


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,entry,reason",
    [
        ("小青，不用回复了，另外那件事已经解决", "plain", "addressed"),
        ("小青", "plain", "addressed"),
        ("小青来点推荐", "plain", "addressed"),
        ("行行行小青你赢了", "plain", "addressed"),
        ("在吗小青", "plain", "addressed"),
        ("这句先放着", "bot_at", "addressed"),
        ("", "bot_at", "addressed"),
        ("这件事说完了", "reply", "addressed"),
        ("聊聊刚才的事", "command", "command"),
    ],
)
async def test_group_turns_addressed_to_her_get_a_direct_reply(
    mock_context, sample_group_event, text, entry, reason
):
    # 规划器即使想旁听也问不到：明确跟她说的话不经概率门、冷却和规划器，直接回复。
    result, model, state = await _run_group_turn(
        mock_context, sample_group_event, text, entry, "wait"
    )
    assert model.calls_of("plan") == []
    assert result == [{"type": "text", "data": {"text": "接话内容"}}]
    records = await state.action_history.get_recent_async("g67890", max_items=5)
    assert records[-1].reasoning == f"forced_direct:{reason}"
    assert records[-1].detail["source"] == "forced"


async def _run_group_turn(mock_context, sample_group_event, text: str, entry: str, action: str):
    """群里先有一句旁人的 “小青” 和她的回答，再来这一条 ``text``（``entry`` 是它进来的方式）；
    规划器的回答是 ``action``。返回（这一轮的返回, 剧本模型, 运行时状态）。"""

    from plugins.xiaoqing_chat.handlers import handle_smalltalk
    from plugins.xiaoqing_chat.store_binding import bound_state

    chat_id = "g67890"
    model   = ScriptedChatModel(
        plan    = plan_answer(action, "上下文判断"),
        reply   = "接话内容",
        checker = checker_verdict(),
    )
    _group_chat_context(mock_context, model)
    state = bound_state(mock_context)
    now   = time.time()
    state.memory_store.append(
        chat_id, role="user", name="甲", user_id=2, ts=now - 60, local_id="m1", content="小青"
    )
    state.memory_store.append(
        chat_id,
        role       = "assistant",
        name       = "小青",
        ts         = now - 50,
        local_id   = "m2",
        message_id = 88,
        content    = "刚才已经回答了",
    )
    segments = [_text(text)]
    if entry == "bot_at":
        segments.insert(0, _at(BOT_ID))
    if entry == "other_at":
        segments.insert(0, _at(OTHER_ID))
    if entry == "reply":
        segments.insert(0, {"type": "reply", "data": {"id": "88"}})
    extra = {"_xc_command_forced": True} if entry == "command" else {}
    event = chat_message_event(sample_group_event, text, message=segments, **extra)

    result = await handle_smalltalk(text, event, mock_context)
    if result:
        await ack_reply_delivery(result)
    await settle_chat_background(mock_context, model)
    return result, model, state


@pytest.mark.parametrize(
    "text,called",
    [
        ("小青帮我看看这个", True),
        ("行吧小青你赢了", True),
        ("在吗小青", True),
        ("问问小青，她肯定知道", True),
        ("我家小青蛙又跑了", False),
        ("青青草原", False),
        ("", False),
    ],
)
def test_called_by_name_needs_a_vocative_shape(text, called):
    """句首称呼一律算；句中只有名字后面接着语气词、标点或“你”才算，“小青蛙”这类更长的词不算。"""

    assert is_called_by_name(text, "小青") is called


@pytest.mark.parametrize(
    "text,bare",
    [
        ("小青", True),
        ("@小青", True),
        ("小青～", True),
        ("小青呢？", True),
        ("小青帮我看看", False),
    ],
)
def test_bare_name_call_is_only_the_name_and_a_tone(text, bare):
    assert is_bare_name_call(text, "小青") is bare
    assert not is_bare_name_call(text, "")
