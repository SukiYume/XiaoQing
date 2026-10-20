"""群聊里分清在叫谁：@ 对象保留、点名他人不抢答、配额排队与观望不阻塞。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from plugins.xiaoqing_chat.chat_event import MENTION_LABELS_KEY
from plugins.xiaoqing_chat.config.config import PersonalityConfig, XiaoQingChatConfig
from plugins.xiaoqing_chat.generation_limiter import (
    CHAT_SUPERSEDED,
    GenerationLimiter,
    GenerationLimitExceeded,
)
from plugins.xiaoqing_chat.handler_context import HandlerContext
from plugins.xiaoqing_chat.llm.llm_config import LLMCallConfig
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.planning.pfc_engine import PFCRunResult
from plugins.xiaoqing_chat.runtime_state import ChatRuntime
from plugins.xiaoqing_chat.smalltalk_models import GeneratedSmalltalkTurn, PreparedSmalltalkTurn

BOT_ID   = 11111
OTHER_ID = "3632293749"


def _group_event(*segments: dict) -> dict:
    return {
        "self_id": BOT_ID,
        "group_id": 187217751,
        "user_id": 503906146,
        "message_id": 9,
        "message": list(segments),
        "sender": {"card": "PulsEternal"},
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
        clean_text, event, "小青", state or _state_with_history(), "g187217751"
    )


# @ 对象保留在本轮文本里


@pytest.mark.asyncio
async def test_mention_of_another_member_is_kept_with_recent_speaker_name():
    state = _state_with_history(
        StoredMessage(role="user", name="久远", ts=1.0, user_id=int(OTHER_ID), content="在。")
    )
    event = _group_event(_at(OTHER_ID), _text(" 喂"))

    assert await _chat_text(event, "喂", state) == "@久远 喂"
    assert event[MENTION_LABELS_KEY] == {OTHER_ID: "@久远"}


@pytest.mark.asyncio
async def test_segment_name_is_used_without_reading_history():
    state = _state_with_history()
    event = _group_event(_text("你问问"), _at(OTHER_ID, "@久远"), _text(" 咋回事"))

    assert await _chat_text(event, "你问问 咋回事", state) == "你问问@久远 咋回事"
    state.memory_store.get_recent_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_member_and_everyone_mentions_get_readable_labels():
    event = _group_event(_at("all"), _text(" 今晚开黑"), _at("222"), _text(" 来不来"))

    assert await _chat_text(event, "今晚开黑 来不来") == "@全体成员 今晚开黑@用户222 来不来"


@pytest.mark.asyncio
async def test_bot_mention_keeps_vocative_alongside_other_mention():
    event = _group_event(_at(BOT_ID), _text(" "), _at("222", "Dimi"), _text(" 你俩比一下"))

    assert await _chat_text(event, "你俩比一下") == "小青，@Dimi 你俩比一下"


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
    event[MENTION_LABELS_KEY] = {OTHER_ID: "@久远"}

    parts = _compose_effective_user_parts(clean_text="@久远 看这个", event=event, rendered_items=[])

    assert parts == ({"kind": "text", "text": "@久远 看这个"},)


# 点名他人不抢答


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@久远 喂", True),
        ("@久远", True),
        ("@久远，出来走两步", True),
        ("@全体成员 今晚开黑", False),
        ("你问问@久远 咋回事", False),
        ("发到 foo@example.com 就行", False),
    ],
)
def test_leading_mention_is_directed_to_other(text, expected):
    from plugins.xiaoqing_chat.participation import is_group_turn_directed_to_other

    assert is_group_turn_directed_to_other(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("你问问@久远 他咋回事？", ""),
        ("@全体成员 今晚谁来开黑", "group_invitation"),
        ("群里有没有人玩死亡空间", "group_invitation"),
        ("所以现在群里只有咱俩能驱动我", ""),
        (
            "查到了，是被拦。"
            + "核心层读的是 caller 级名单，所以现在群里只有咱俩能驱动我。" * 3
            + "你说哪个？",
            "",
        ),
    ],
)
def test_participation_cue_skips_mentions_statements_and_long_reports(text, expected):
    from plugins.xiaoqing_chat.participation import classify_group_participation_cue

    assert classify_group_participation_cue(text) == expected


@pytest.mark.asyncio
async def test_reply_gate_blocks_turn_that_mentions_someone_else():
    from plugins.xiaoqing_chat.frequency_control import decide_reply

    runtime                                          = MagicMock()
    runtime.cfg.min_reply_interval_seconds           = 0.0
    runtime.cfg.max_replies_per_minute               = 100
    runtime.cfg.goal.enable_goal                     = False
    state                                            = MagicMock()
    state.get_last_reply_ts.return_value             = 0.0
    state.get_continuous_cooldown_until.return_value = 0.0
    state.get_reply_timestamps.return_value          = []
    state.memory_store.get_recent_async = AsyncMock(return_value=[])

    assert await decide_reply(runtime, state, "g1", "@久远 喂", False, False) is False
    decision = state.set_reply_gate_decision.call_args.args[1]
    assert decision.reason == "directed_to_other"


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


def _planner_wait_response(*_args, **_kwargs):
    content = (
        '{"thinking": "他俩在聊", "action": "wait", "reason": "在接对方的话", "wait_seconds": 0}'
    )
    return {"choices": [{"message": {"content": content}}]}, "responses"


@pytest.mark.asyncio
async def test_open_question_answering_another_member_goes_through_planner(monkeypatch):
    from plugins.xiaoqing_chat.planning import pfc_action_planner

    fake_chat = AsyncMock(side_effect=_planner_wait_response)
    monkeypatch.setattr(pfc_action_planner, "chat_completions_raw_with_fallback_paths", fake_chat)
    history = [
        StoredMessage(
            role="user", name="PulsEternal", ts=100.0, user_id=1, content="搞不好是双层白名单"
        ),
        StoredMessage(
            role="user", name="Dimi", ts=135.0, user_id=2, content="藤子 ban 吗？应该没这么复杂"
        ),
    ]

    plan = await pfc_action_planner.plan_next_action(
        **_planner_kwargs(history, "藤子 ban 吗？应该没这么复杂")
    )

    assert plan.action == "wait"
    fake_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_fresh_open_question_still_joins_directly(monkeypatch):
    from plugins.xiaoqing_chat.planning import pfc_action_planner

    fake_chat = AsyncMock(side_effect=_planner_wait_response)
    monkeypatch.setattr(pfc_action_planner, "chat_completions_raw_with_fallback_paths", fake_chat)
    history = [
        StoredMessage(role="user", name="Dimi", ts=10.0, user_id=2, content="下班了"),
        StoredMessage(role="user", name="PulsEternal", ts=500.0, user_id=1, content="今晚吃啥啊"),
    ]

    plan = await pfc_action_planner.plan_next_action(**_planner_kwargs(history, "今晚吃啥啊"))

    assert plan.reason == "fresh_group_participation:open_question"
    fake_chat.assert_not_awaited()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("小青，不是叫你", True),
        ("小青 没叫你哈", True),
        ("小青，@的不是你", True),
        ("小青，不是叫你去，是叫他去", False),
        ("小青，不是说你笨", False),
        ("不是叫你吗？", False),
    ],
)
def test_not_addressed_to_bot_requests_silence(text, expected):
    from plugins.xiaoqing_chat.llm.communication_constraints import requests_silence

    assert requests_silence(text) is expected


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
        await release.wait()


@pytest.mark.asyncio
async def test_newer_turn_waits_for_chat_slot_instead_of_being_rejected():
    limiter = GenerationLimiter()
    entered, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold_slot(limiter, entered, release))
    await entered.wait()

    admitted = asyncio.Event()

    async def newer():
        async with _admit(limiter, "g1", "u2"):
            admitted.set()

    waiter = asyncio.create_task(newer())
    await asyncio.sleep(0.01)
    assert not admitted.is_set()

    release.set()
    await asyncio.wait_for(waiter, 1)
    await holder
    assert admitted.is_set()


@pytest.mark.asyncio
async def test_queued_turn_is_superseded_by_a_newer_one():
    limiter = GenerationLimiter()
    entered, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold_slot(limiter, entered, release))
    await entered.wait()

    async def queued(user_id: str):
        async with _admit(limiter, "g1", user_id):
            return user_id

    older = asyncio.create_task(queued("u2"))
    await asyncio.sleep(0.01)
    newer = asyncio.create_task(queued("u3"))

    with pytest.raises(GenerationLimitExceeded, match=CHAT_SUPERSEDED):
        await asyncio.wait_for(older, 1)
    release.set()
    assert await asyncio.wait_for(newer, 1) == "u3"
    await holder


@pytest.mark.asyncio
async def test_chat_slot_wait_is_bounded_and_other_chats_are_unaffected():
    limiter = GenerationLimiter()
    entered, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold_slot(limiter, entered, release))
    await entered.wait()

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


@pytest.mark.asyncio
async def test_superseded_forced_turn_does_not_send_busy_notice():
    from plugins.xiaoqing_chat import handlers

    @asynccontextmanager
    async def superseded(**_kwargs):
        raise GenerationLimitExceeded(CHAT_SUPERSEDED)
        yield

    prepared = PreparedSmalltalkTurn(
        text              = "小青，在吗",
        mentioned         = True,
        is_private        = False,
        forced            = True,
        force_reason      = "mentioned",
        brain_chat_active = False,
        mood_text         = "",
    )
    state = SimpleNamespace(generation_limiter=SimpleNamespace(admit=superseded))
    hctx = SimpleNamespace(runtime=ChatRuntime(XiaoQingChatConfig(), []), state=state, chat_id="g1")

    with (
        patch.object(handlers, "_prepare_smalltalk_turn", AsyncMock(return_value=prepared)),
        patch.object(handlers, "log_step", Mock()),
    ):
        result = await handlers._maybe_reply_smalltalk(
            "在吗", _group_event(_text("小青，在吗")), MagicMock(), hctx=hctx
        )

    assert result == []


# 规划器观望不阻塞


@pytest.mark.asyncio
async def test_planner_wait_ends_turn_without_sleeping(tmp_path):
    from plugins.xiaoqing_chat import turn_generation

    runtime = ChatRuntime(XiaoQingChatConfig(), [])
    state   = MagicMock()
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.pfc_state_store.get_async = AsyncMock(return_value=None)
    context = SimpleNamespace(data_dir=tmp_path, logger=MagicMock())
    hctx     = HandlerContext("g1", runtime, state, {}, tmp_path, "小青", context)
    prepared = PreparedSmalltalkTurn(
        text              = "那大概几点",
        mentioned         = False,
        is_private        = False,
        forced            = False,
        force_reason      = "",
        brain_chat_active = False,
        mood_text         = "",
    )
    waiting = PFCRunResult(
        reply="", action="wait", reason="给他们留空间", ended=False, wait_seconds=20
    )
    sleep = AsyncMock()
    lock  = asyncio.Lock()

    with (
        patch.multiple(
            turn_generation,
            ensure_user_message_recorded=AsyncMock(return_value="m1"),
            chat_lock=lambda _: lock,
            retrieve_memory_context=AsyncMock(return_value=""),
            run_pfc_once=AsyncMock(return_value=waiting),
            log_step=Mock(),
        ),
        patch.object(turn_generation.asyncio, "sleep", sleep),
    ):
        generated = await turn_generation.generate_smalltalk_turn(
            prepared, {"group_id": 1, "user_id": 2}, context, hctx
        )

    assert isinstance(generated, GeneratedSmalltalkTurn)
    assert generated.reply == ""
    assert generated.pfc_result is waiting
    sleep.assert_not_awaited()
