"""群聊里分清在叫谁：@ 对象保留、点名他人不抢答、配额排队与观望不阻塞。"""

from __future__ import annotations

import asyncio
import json
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
        "self_id"   : BOT_ID,
        "group_id"  : 100000001,
        "user_id"   : 503906146,
        "message_id": 9,
        "message"   : list(segments),
        "sender"    : {"card": "PulsEternal"},
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
        clean_text, event, "小青", state or _state_with_history(), "g100000001"
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


@pytest.mark.asyncio
async def test_reply_gate_lets_planner_judge_turn_that_mentions_someone_else():
    """点名别人不再硬拦：话题小青感兴趣也可以接一句，由规划器判断。"""

    from plugins.xiaoqing_chat.frequency_control import decide_reply

    runtime                                          = MagicMock()
    runtime.cfg.reply_probability_base               = 1.0
    runtime.cfg.min_reply_interval_seconds           = 0.0
    runtime.cfg.max_replies_per_minute               = 100
    runtime.cfg.heartflow.enable_heartflow           = False
    runtime.cfg.goal.enable_goal                     = False
    state                                            = MagicMock()
    state.get_last_reply_ts.return_value             = 0.0
    state.get_continuous_cooldown_until.return_value = 0.0
    state.get_reply_timestamps.return_value          = []
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))

    assert await decide_reply(runtime, state, "g1", "@久远 喂", False, False) is True
    decision = state.set_reply_gate_decision.call_args.args[1]
    assert decision.reason == "allowed"


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
        "你问问@久远 他咋回事？",
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
    monkeypatch.setattr(pfc_action_planner, "chat_completions_raw_with_fallback_paths", fake_chat)
    history = [
        StoredMessage(role="user", name="Dimi", ts=10.0, user_id=2, content="下次一定"),
        StoredMessage(role="user", name="PulsEternal", ts=500.0, user_id=1, content=text),
    ]

    plan = await pfc_action_planner.plan_next_action(**_planner_kwargs(history, text))

    assert plan.action == action
    assert plan.reason == "上下文判断"
    fake_chat.assert_awaited_once()
    prompt = fake_chat.await_args.kwargs["messages"][0]["content"]
    assert "本轮目标消息：\n" + text in prompt
    assert "下次一定" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "segments,silenced",
    [
        ([_text("不用回复了")], True),
        ([_text("小青，别回复了")], True),
        ([_at(BOT_ID), _text("别回复了")], True),
        ([_text("阿梨，别回复我了")], False),
        ([_at(OTHER_ID, "小明"), _text("别回复我了")], False),
        ([_text("小明，不是叫你")], False),
        ([_text("Dimi，别回复了")], False),
        ([_text("不用回复了吗？")], False),
    ],
)
async def test_prepare_turn_keeps_stop_requests_for_context_planning(tmp_path, segments, silenced):
    from core.message import extract_text
    from plugins.xiaoqing_chat import handlers
    from plugins.xiaoqing_chat.attention_gate import AttentionDecision

    runtime                                             = ChatRuntime(XiaoQingChatConfig(), [])
    runtime.cfg.goal.enable_goal                        = False
    runtime.cfg.reflection.enable_expression_reflection = False
    history = [StoredMessage(role="user", name="Dimi", user_id=2, ts=1, content="在")]
    state = _state_with_history(*history)
    context = SimpleNamespace(data_dir=tmp_path, logger=Mock())
    hctx  = HandlerContext("g100000001", runtime, state, {}, tmp_path, "小青", context)
    event = _group_event(*segments)
    attention = AsyncMock(return_value=(AttentionDecision(False, False), False))
    with patch.multiple(
        handlers,
        build_effective_user_text=AsyncMock(side_effect=lambda text, *_args, **_kwargs: text),
        is_ignored_text=Mock(return_value=False),
        chat_lock=Mock(return_value=asyncio.Lock()),
        _maybe_reset_idle_conversation = AsyncMock(),
        _resolve_attention             = attention,
        _apply_routine=Mock(return_value=1.0),
        spawn_daily_life=Mock(),
        decide_reply=AsyncMock(return_value=True),
        _refresh_mood_state=Mock(return_value=""),
        log_step=Mock(),
    ):
        result = await handlers._prepare_smalltalk_turn(extract_text(event), event, context, hctx)
    assert result is not None
    assert not result.forced
    assert "回复" in result.text or "叫你" in result.text
    attention.assert_awaited_once()


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
        patch.object(
            turn_generation, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": sleep})
        ),
    ):
        generated = await turn_generation.generate_smalltalk_turn(
            prepared, {"group_id": 1, "user_id": 2}, context, hctx
        )

    assert isinstance(generated, GeneratedSmalltalkTurn)
    assert generated.reply == ""
    assert generated.pfc_result is waiting
    sleep.assert_not_awaited()


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
        ("小青，不用回复了，另外那件事已经解决", "plain"),
        ("大家都散了，这事就到这吧", "plain"),
        ("没什么事了", "plain"),
        ("我们已经等了好几天", "plain"),
        ("大家有没有人还想来玩", "plain"),
        ("小青", "plain"),
        ("这句先放着", "bot_at"),
        ("", "bot_at"),
        ("这件事说完了", "reply"),
        ("聊聊刚才的事", "command"),
    ],
)
async def test_group_entry_reaches_context_planner(tmp_path, text, entry, action):
    import time

    from plugins.xiaoqing_chat import handlers, turn_generation
    from plugins.xiaoqing_chat.message_parts import build_text_message_parts
    from plugins.xiaoqing_chat.planning import pfc_action_planner
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    cfg                                         = XiaoQingChatConfig()
    cfg.goal.enable_goal                        = False
    cfg.reflection.enable_expression_reflection = False
    cfg.reply_probability_base                  = 1.0
    cfg.heartflow.enable_heartflow              = False
    cfg.planner.enable_planner                  = False  # 群聊统一进入 PFC，开关只控制私聊。
    runtime                                     = ChatRuntime(cfg, [])
    state                                       = ChatRuntimeState()
    now                                         = time.time()
    state.memory_store.append(
        "g100000001", role="user", name="甲", user_id=2, ts=now - 60, local_id="m1", content="小青"
    )
    state.memory_store.append(
        "g100000001",
        role       = "assistant",
        name       = "小青",
        ts         = now - 50,
        local_id   = "m2",
        message_id = 88,
        content    = "刚才已经回答了",
    )
    event = _group_event(_text(text))
    if entry in {"bot_at", "other_at"}:
        event["message"].insert(0, _at(BOT_ID if entry == "bot_at" else OTHER_ID))
    if entry == "reply":
        event["message"].insert(0, {"type": "reply", "data": {"id": "88"}})
    if entry == "command":
        event["_xc_command_forced"] = True
    context = SimpleNamespace(data_dir=tmp_path, logger=Mock())
    hctx     = HandlerContext("g100000001", runtime, state, {}, tmp_path, "小青", context)
    response = {
        "choices": [
            {"message": {"content": json.dumps({"action": action, "reason": "上下文判断"})}}
        ]
    }
    model = AsyncMock(return_value=(response, "responses"))
    draft = AsyncMock(return_value=("接话内容", build_text_message_parts("接话内容"), None))
    with (
        patch.object(
            handlers,
            "build_effective_user_text",
            AsyncMock(side_effect=lambda value, *_args, **_kwargs: value),
        ),
        patch.object(handlers, "_maybe_reset_idle_conversation", AsyncMock()),
        patch.object(handlers, "spawn_daily_life"),
        patch.object(handlers, "_apply_routine", return_value=1.0),
        patch.object(handlers, "_refresh_mood_state", return_value=""),
        patch.object(handlers, "log_step"),
        patch.object(pfc_action_planner, "chat_completions_raw_with_fallback_paths", model),
        patch.object(turn_generation, "ensure_user_message_recorded", AsyncMock(return_value="m3")),
        patch.object(turn_generation, "retrieve_memory_context", AsyncMock(return_value="")),
        patch.object(turn_generation, "_generate_reply_result", draft),
    ):
        prepared = await handlers._prepare_smalltalk_turn(text, event, context, hctx)
        assert prepared is not None
        assert prepared.forced is False
        state.memory_store.append(
            hctx.chat_id,
            role     = "user",
            name     = "甲",
            user_id  = 2,
            ts       = now,
            local_id = "m3",
            content  = prepared.text,
        )
        generated = await turn_generation.generate_smalltalk_turn(prepared, event, context, hctx)
        generated.cancel_speculative_memory()
        if generated.speculative_memory_task is not None:
            await asyncio.gather(generated.speculative_memory_task, return_exceptions=True)
    model.assert_awaited_once()
    prompt = model.await_args.kwargs["messages"][0]["content"]
    assert prepared.text in prompt and "刚才已经回答了" in prompt
    assert "实际含义" in prompt
    assert generated.pfc_result.action == action
    assert draft.await_count == (1 if action == "direct_reply" else 0)
    assert bool(generated.reply) is (action == "direct_reply")
