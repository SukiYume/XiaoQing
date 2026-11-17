"""单轮生成、提交和并发生命周期。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from plugins.xiaoqing_chat.chat_runtime import chat_lock
from plugins.xiaoqing_chat.memory.memory import MAX_CACHED_MESSAGES_PER_CHAT
from plugins.xiaoqing_chat.planning.pfc_state import PFCConversationState, PFCStateStore
from plugins.xiaoqing_chat.store_binding import bound_state
from tests.helpers.assertions import logged_events
from tests.helpers.reply_checker_test_support import checker_verdict
from tests.helpers.xiaoqing_chat_test_support import (
    GROUP_GATE_OPEN,
    ScriptedChatModel,
    ack_reply_delivery,
    chat_message_event,
    goals_answer,
    plan_answer,
    scripted_chat_context,
    settle_chat_background,
)

mock_context       = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event

_GROUP_CHAT = "g67890"
_PASS       = '{"withhold_reply": false}'
_WITHHOLD   = '{"withhold_reply": true}'
# 群里被她接话的那一句，以及她的回复。
_TARGET_TEXT = "那个车是不是换了"
_REPLY_TEXT  = "对，应该是换车底了"


async def _flushed_pfc_state(context, chat_id: str = _GROUP_CHAT) -> PFCConversationState:
    """等规划状态的后台落盘任务跑完，读回磁盘上的那一份。"""

    path = context.data_dir / "pfc_state" / f"{chat_id}.json"
    for _ in range(100):
        if path.exists():
            break
        await asyncio.sleep(0.02)
    assert path.exists()
    store = PFCStateStore()
    store.bind(context.data_dir)
    return store.get(chat_id)


def _group_speech(event: dict, text: str, *, message_id: int, user_id: int = 12345) -> dict:
    spoken = chat_message_event(event, text, message_id=message_id, user_id=user_id)
    spoken["sender"] = {**event["sender"], "user_id": user_id, "nickname": f"群友{user_id}"}
    return spoken


async def _hears(context, event: dict, text: str, *, message_id: int, user_id: int = 777) -> dict:
    """群里另一位群友发来一句话：走真实的收消息入口，记进聊天记录。"""

    from plugins.xiaoqing_chat.handlers import observe_message

    spoken = _group_speech(event, text, message_id=message_id, user_id=user_id)
    await observe_message(text, spoken, context)
    return spoken


def _flood_group(context, count: int) -> None:
    """群里短时间刷了 ``count`` 条：直接写进聊天记录（这里被测的不是收消息的入口，条数又很多）。"""

    store = bound_state(context).memory_store
    for index in range(count):
        store.append(_GROUP_CHAT, role="user", name="路人", user_id=777, content=f"刷屏{index}")


def _later_texts(count: int) -> list[str]:
    return [f"后来第{index}句" for index in range(count)]


async def _target_with_later(
    context,
    event: dict,
    later: Sequence[str | tuple[str, str]] = (),
    *,
    before: Sequence[str] = (),
) -> dict:
    """群里先有人说了几句，再说了她要接的那句，后来又来了 ``later`` 里的消息；返回那句话的事件。

    ``later`` 里的字符串是群友说的话，``(角色, 文字)`` 可以写成 ``("assistant", ...)``，
    表示小青自己后来说的话。
    """

    for index, text in enumerate(before):
        await _hears(context, event, text, message_id=200 + index)
    target = _group_speech(event, _TARGET_TEXT, message_id=1)
    from plugins.xiaoqing_chat.handlers import observe_message

    await observe_message(_TARGET_TEXT, target, context)
    for index, entry in enumerate(later):
        role, text = ("user", entry) if isinstance(entry, str) else entry
        if role == "user":
            await _hears(context, event, text, message_id=10 + index)
        else:
            bound_state(context).memory_store.append(
                _GROUP_CHAT, role=role, name="小青", content=text
            )
    return target


def _delivery_model(**script) -> ScriptedChatModel:
    """规划器直接回复、复核（route reasoning）默认放行的剧本模型。"""

    return ScriptedChatModel(
        **{"plan": plan_answer("direct_reply"), "reply": _REPLY_TEXT, "reasoning": _PASS, **script}
    )


async def _reply_with_later(
    context, event: dict, model: ScriptedChatModel, later=(), *, before=()
) -> list:
    """群里有人说了一句话、后来又有人接着说了几句，之后她回复最早那一句。"""

    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    scripted_chat_context(context, model, GROUP_GATE_OPEN)
    target = await _target_with_later(context, event, later, before=before)
    return await handle_smalltalk(_TARGET_TEXT, target, context)


@pytest.mark.asyncio
async def test_smalltalk_rejection_persists_updated_pfc_state(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    model = ScriptedChatModel(
        plan  = [plan_answer("rethink_goal"), plan_answer("direct_reply")],
        goals = goals_answer("重规划后的目标"),
        reply = "这样啊",
        checker=checker_verdict(suitable=False, reason="需要重新规划", factually_plausible=False),
    )
    scripted_chat_context(
        mock_context,
        model,
        GROUP_GATE_OPEN,
        {"reply_check": {"llm_checker_mode": "always", "max_regen": 0, "max_replan": 0}},
    )
    state = bound_state(mock_context)
    # 之前有过一次规划失败的记录：这一轮规划正常，写回的状态里它已经被清掉。
    (await state.pfc_state_store.get_async(_GROUP_CHAT)).planner_fail_ts = [time.time() - 10]

    result = await handle_smalltalk(
        "继续聊", chat_message_event(sample_group_event, "继续聊"), mock_context
    )

    # 回复被审查拒绝、没有发出；重规划得到的状态仍然写回并落盘，下一轮从新状态出发。
    assert result == []
    persisted = state.pfc_state_store.get(_GROUP_CHAT)
    assert persisted.top_goal == "重规划后的目标"
    assert persisted.planner_fail_ts == []
    assert (await _flushed_pfc_state(mock_context)).top_goal == "重规划后的目标"
    records = await state.action_history.get_recent_async(_GROUP_CHAT, max_items=5)
    assert [(record.action, record.reasoning) for record in records] == [
        ("reply_rejected", "需要重新规划")
    ]
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_smalltalk_forced_reply_generation_runs_outside_chat_lock_but_commits_inside(
    mock_context, sample_private_event
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    chat_id                              = "u12345"
    lock                                 = chat_lock(chat_id)
    locked_during_generation: list[bool] = []

    def reply(_call) -> str:
        locked_during_generation.append(lock.locked())
        return "forced-ok"

    model = ScriptedChatModel(reply=reply)
    scripted_chat_context(mock_context, model)
    event = chat_message_event(sample_private_event, "你好", _xc_command_forced=True)

    result = await handle_smalltalk("你好", event, mock_context)

    assert result == [{"type": "text", "data": {"text": "forced-ok"}}]
    assert locked_during_generation == [False]
    # 回复送达后的提交要拿到会话锁：锁被别人占着时，回执的提交等着。
    state = bound_state(mock_context)
    await lock.acquire()
    ack = asyncio.create_task(ack_reply_delivery(result))
    try:
        await asyncio.sleep(0.05)
        assert not ack.done()
        assert [m.role for m in await state.memory_store.get_async(chat_id)] == ["user"]
    finally:
        lock.release()
    await asyncio.wait_for(ack, 5)
    assert [m.role for m in await state.memory_store.get_async(chat_id)] == ["user", "assistant"]
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("later", "newer_messages"),
    [
        pytest.param(15, 15, id="too-far"),
        # 聊天记录只留最近 200 条：生成期间来了更多，她要接的那句已经不在记录里。
        pytest.param(MAX_CACHED_MESSAGES_PER_CHAT + 1, None, id="scrolled-out"),
    ],
)
async def test_smalltalk_stale_generation_is_dropped_before_commit(
    mock_context, sample_group_event, caplog, later, newer_messages
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    caplog.set_level(logging.INFO, logger="plugins.xiaoqing_chat")
    state = bound_state(mock_context)

    async def reply(_call) -> str:
        # 生成期间群里又来了很多条，她要接的那句已经太远了。
        if later > 15:
            _flood_group(mock_context, later)
        else:
            for index, text in enumerate(_later_texts(later)):
                await _hears(mock_context, sample_group_event, text, message_id=10 + index)
        return "planner-ok"

    model = ScriptedChatModel(plan=plan_answer("direct_reply"), reply=reply)
    scripted_chat_context(mock_context, model, GROUP_GATE_OPEN)
    (await state.pfc_state_store.get_async(_GROUP_CHAT)).planner_fail_ts = [time.time() - 10]

    result = await handle_smalltalk(
        "继续聊", chat_message_event(sample_group_event, "继续聊"), mock_context
    )

    assert result == []
    assert "assistant" not in [m.role for m in await state.memory_store.get_async(_GROUP_CHAT)]
    assert (await state.heartflow.get_async(_GROUP_CHAT)).no_reply_streak == 0
    assert await state.action_history.get_recent_async(_GROUP_CHAT, max_items=5) == []
    # 规划状态没有写回（失败记录还在），也没有落盘。
    assert state.pfc_state_store.get(_GROUP_CHAT).planner_fail_ts
    # 记录里找不到那句时没有“后来几条”可数。
    drops = logged_events(caplog, "smalltalk.stale.drop")
    assert [drop.fields.get("newer_messages") for drop in drops] == [newer_messages]
    await settle_chat_background(mock_context, model)
    assert not (mock_context.data_dir / "pfc_state" / f"{_GROUP_CHAT}.json").exists()


@pytest.mark.asyncio
async def test_smalltalk_no_reply_current_turn_schedules_pfc_state_flush(
    mock_context, sample_group_event, caplog
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    caplog.set_level(logging.INFO, logger="plugins.xiaoqing_chat")
    lock    = chat_lock(_GROUP_CHAT)
    planned = asyncio.Event()

    async def plan(_call) -> str:
        # 规划一结束，会话锁就被别人占着：这一轮的结算要等锁。
        await lock.acquire()
        planned.set()
        return plan_answer("wait", "later")

    model = ScriptedChatModel(plan=plan)
    scripted_chat_context(mock_context, model, GROUP_GATE_OPEN)
    state = bound_state(mock_context)
    (await state.pfc_state_store.get_async(_GROUP_CHAT)).planner_fail_ts = [time.time() - 10]

    turn = asyncio.create_task(
        handle_smalltalk("你好", chat_message_event(sample_group_event, "你好"), mock_context)
    )
    try:
        await asyncio.wait_for(planned.wait(), 5)
        await asyncio.sleep(0.05)
        assert not turn.done()
        assert state.pfc_state_store.get(_GROUP_CHAT).planner_fail_ts
        assert (await state.heartflow.get_async(_GROUP_CHAT)).no_reply_streak == 0
    finally:
        lock.release()
    result = await asyncio.wait_for(turn, 5)

    assert result == []
    assert state.pfc_state_store.get(_GROUP_CHAT).planner_fail_ts == []
    assert (await state.heartflow.get_async(_GROUP_CHAT)).no_reply_streak == 1
    assert (await _flushed_pfc_state(mock_context)).planner_fail_ts == []
    assert logged_events(caplog, "smalltalk.no_reply")
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_smalltalk_schedules_pfc_flush_even_if_post_commit_reply_record_fails(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    model = ScriptedChatModel(
        plan  = [plan_answer("rethink_goal"), plan_answer("direct_reply")],
        goals = goals_answer("重规划后的目标"),
        reply = "planner-ok",
    )
    scripted_chat_context(mock_context, model, GROUP_GATE_OPEN)
    state = bound_state(mock_context)

    result = await handle_smalltalk(
        "你好", chat_message_event(sample_group_event, "你好"), mock_context
    )
    assert result == [{"type": "text", "data": {"text": "planner-ok"}}]
    with patch(
        "plugins.xiaoqing_chat.turn_delivery.record_bot_reply",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        receipt = await ack_reply_delivery(result)

    assert isinstance(receipt.callback_error, RuntimeError)
    assert str(receipt.callback_error) == "boom"
    assert state.pfc_state_store.get(_GROUP_CHAT).top_goal == "重规划后的目标"
    assert (await _flushed_pfc_state(mock_context)).top_goal == "重规划后的目标"
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_smalltalk_schedules_pfc_flush_even_if_post_commit_no_reply_hook_fails(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    model = ScriptedChatModel(plan=plan_answer("wait", "later"))
    scripted_chat_context(mock_context, model, GROUP_GATE_OPEN)
    state = bound_state(mock_context)
    (await state.pfc_state_store.get_async(_GROUP_CHAT)).planner_fail_ts = [time.time() - 10]

    with patch.object(
        state.heartflow,
        "on_no_reply_async",
        new=AsyncMock(side_effect=RuntimeError("no-reply boom")),
    ):
        result = await handle_smalltalk(
            "你好", chat_message_event(sample_group_event, "你好"), mock_context
        )

    # 不回复的收尾出了故障：这一轮什么也不发、也不记行动，但规划状态已经写回并落盘。
    assert result == []
    assert state.pfc_state_store.get(_GROUP_CHAT).planner_fail_ts == []
    assert (await _flushed_pfc_state(mock_context)).planner_fail_ts == []
    assert await state.action_history.get_recent_async(_GROUP_CHAT, max_items=5) == []
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("brain_chat_active", "expected_text"),
    [(False, "ok"), (True, "[brain]\nok")],
    ids=["normal-chat", "brain-chat"],
)
async def test_mode_indicator_only_applies_in_brain_chat(
    mock_context, sample_private_event, brain_chat_active, expected_text
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    model = ScriptedChatModel(reply="ok")
    scripted_chat_context(
        mock_context,
        model,
        {
            "brain_chat": {
                "enable_private_brain_chat": brain_chat_active,
                "show_mode_indicator"      : True,
                "brain_mode_indicator"     : "[brain]",
            }
        },
    )
    event = chat_message_event(sample_private_event, "你好", _xc_command_forced=True)

    result = await handle_smalltalk("你好", event, mock_context)

    assert result == [{"type": "text", "data": {"text": expected_text}}]
    mock_context.send_action.assert_not_awaited()
    await settle_chat_background(mock_context, model)


# ---------------------------------------------------------------- 晚到的回复：近的直接接，远的引用，太远或换话题才丢


def _record_messages(chat_id: str, *entries: tuple[str, str] | tuple[str, str, str]) -> None:
    from plugins.xiaoqing_chat.message_parts import build_text_message_parts
    from plugins.xiaoqing_chat.runtime_state import get_state

    for local_id, role, *text in entries:
        get_state().memory_store.append(
            chat_id,
            role     = role,
            name     = "群友" if role == "user" else "小青",
            user_id  = 1 if role == "user" else None,
            local_id = local_id,
            parts    = build_text_message_parts(text[0] if text else f"{local_id} 的内容"),
        )


def _later(count: int) -> list[tuple[str, str]]:
    return [(f"m{index + 2}", "user") for index in range(count)]


def _fresh_finalization(*, chat_id: str = "g1", event: dict | None = None):
    """只带 _quote_far_target 读取的字段：会话、要接的那句的本地 ID 和事件。"""

    return SimpleNamespace(
        generated=SimpleNamespace(local_id="m1"),
        hctx=SimpleNamespace(chat_id=chat_id),
        event=event if event is not None else {"group_id": 1, "message_id": 555},
    )


def test_split_history_counts_every_visible_message_after_the_target():
    from plugins.xiaoqing_chat.memory.local_ids import (
        MAX_REPLY_RECENT_MESSAGES,
        reply_is_stale,
        split_history_at,
    )

    _record_messages("g1", ("m0", "user"), ("m1", "user"), ("m2", "user"), ("m3", "assistant"))
    before, after = split_history_at("g1", "m1")
    assert [message.local_id for message in before] == ["m0", "m1"]
    assert [message.local_id for message in after] == ["m2", "m3"]
    assert split_history_at("g1", "m9") is None
    assert not reply_is_stale("g1", "m1")
    assert reply_is_stale("g1", "m9")

    _record_messages(
        "g1", *[(f"x{index}", "user") for index in range(MAX_REPLY_RECENT_MESSAGES - 2)]
    )
    assert reply_is_stale("g1", "m1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("later", "dropped"),
    [
        (1, False),  # 别人（比如别的机器人）插了一句：回复照常发
        (4, False),
        (14, False),  # 要接的那句还在最近 15 条里
        (15, True),
    ],
)
async def test_reply_is_kept_until_the_target_leaves_the_last_15_messages(
    mock_context, sample_group_event, caplog, later, dropped
):
    caplog.set_level(logging.INFO, logger="plugins.xiaoqing_chat")
    model  = _delivery_model()
    result = await _reply_with_later(mock_context, sample_group_event, model, _later_texts(later))

    drops = logged_events(caplog, "smalltalk.stale.drop")
    assert bool(drops) is dropped
    if dropped:
        assert result == []
        assert drops[-1].fields["newer_messages"] == later
    else:
        assert [seg["data"]["text"] for seg in result if seg["type"] == "text"] == [_REPLY_TEXT]
        await ack_reply_delivery(result)
    await settle_chat_background(mock_context, model)


@pytest.mark.parametrize(
    ("later", "reason"),
    [
        ([("m2", "user", "不用回复了")], ""),
        ([("m2", "user", "哈哈"), ("m3", "user", "@的不是你")], ""),
        ([("m2", "assistant", "不用回复了")], ""),  # 小青自己的话不算叫停
        ([("m2", "user", "不用回复了吗？")], ""),  # 问句不是叫停
        ([("m2", "user", "阿梨，别回复我了")], ""),
        ([("m2", "user", "@小明 别回复我了")], ""),
        ([("m2", "user", "小明，不是叫你")], ""),
        ([("m2", "user", "小青，别回复了")], ""),
        ([("m2", "user", "嘶")], ""),
    ],
)
def test_stale_reason_leaves_message_meaning_to_context_review(later, reason):
    from plugins.xiaoqing_chat.memory.local_ids import split_history_at, stale_reason

    _record_messages("g1", ("m1", "user"), *later)
    split = split_history_at("g1", "m1")
    assert split is not None
    assert stale_reason(split[1]) == reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,stopped",
    [("不用回复了", True), ("阿梨，别回复我了", False), ("@小明 别回复我了", False)],
)
async def test_review_between_bubbles_decides_whether_delivery_continues(
    mock_context, sample_group_event, text, stopped
):
    """每发出一个气泡群里就来一句话；复核认为该叫停时，结束投递并只记已发出的部分，否则把气泡发完。"""

    sent: list = []

    async def send_action(action):
        sent.append(action)
        await _hears(mock_context, sample_group_event, text, message_id=100 + len(sent))
        return True

    mock_context.send_action = send_action
    model = _delivery_model(reply="一\n\n二\n\n三", reasoning=_WITHHOLD if stopped else _PASS)
    result = await _reply_with_later(mock_context, sample_group_event, model)

    state = bound_state(mock_context)
    assert text in model.calls_of("reasoning")[0].text

    async def said() -> list[str]:
        history = await state.memory_store.get_async(_GROUP_CHAT)
        return [message.content for message in history if message.role == "assistant"]

    if stopped:
        assert result == []
        assert len(sent) == 1
        assert await said() == ["一"]
    else:
        assert [seg["data"]["text"] for seg in result] == ["三"]
        assert len(sent) == 2
        assert await said() == []
        await ack_reply_delivery(result)
        assert await said() == ["一\n\n二\n\n三"]
    await settle_chat_background(mock_context, model)


@pytest.mark.parametrize(
    "text",
    [
        "不用回复了",
        "小青，别回复了",
        "阿梨，别回复我了",
        "@小明 别回复我了",
    ],
)
def test_reply_generator_defers_stop_wording_to_delivery_review(text):
    from plugins.xiaoqing_chat.reply_generator import _is_turn_stale

    _record_messages("g1", ("m1", "user"))
    event = {"_xc_user_recorded_local_id": "m1"}
    assert not _is_turn_stale("g1", event)
    _record_messages("g1", ("m2", "user", text))
    assert not _is_turn_stale("g1", event)


# ---------------------------------------------------------------- 迟到的回执不能覆盖更新的规划状态


def _pfc_state(goal: str, *, ended: bool):
    return PFCConversationState(
        chat_id                      = "g1",
        ended                        = ended,
        last_successful_reply_action = "",
        goal_list                    = [{"goal": goal}],
        knowledge_list               = [],
        planner_fail_ts              = [],
        planner_skip_until           = 0.0,
        updated_at                   = 0.0,
    )


def test_planner_turns_commit_in_order():
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore

    store = PFCStateStore()
    first, second = store.begin_turn("g1"), store.begin_turn("g1")
    assert (first, second) == (1, 2)
    other = store.begin_turn("g2")
    assert other > second
    assert store.claim_commit("g1", first)  # 新轮次尚未提交时，较早的有效快照仍可写回。
    assert store.claim_commit("g1", second)
    assert not store.claim_commit("g1", first)
    assert store.claim_commit("g1", second)  # 同一轮重复提交（重规划）仍然允许
    assert store.claim_commit("g2", other)


@pytest.mark.asyncio
async def test_late_receipt_does_not_roll_back_a_newer_turns_planner_state(tmp_path):
    """旧回复的回执等到超时、新一轮先提交规划状态后，旧回执迟到也不能把状态改回旧值。"""

    from plugins.xiaoqing_chat.planning.goal_state import GoalStore
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore
    from plugins.xiaoqing_chat.turn_generation import commit_planner_snapshot

    store = PFCStateStore()
    goals = GoalStore()
    state = SimpleNamespace(pfc_state_store=store, goal_store=goals)
    runtime = SimpleNamespace(cfg=SimpleNamespace(goal=SimpleNamespace(enable_goal=True)))

    old_turn = store.begin_turn("g1")  # 旧回复取快照，交给 Core 后回执迟迟不来
    new_turn = store.begin_turn("g1")  # 等待超时，新一轮开始
    assert await commit_planner_snapshot(
        state, runtime, "g1", _pfc_state("new-topic", ended=True), turn=new_turn
    )
    assert not await commit_planner_snapshot(
        state, runtime, "g1", _pfc_state("old-topic", ended=False), turn=old_turn
    )

    current = store.get("g1")
    assert current.top_goal == "new-topic"
    assert current.ended is True
    assert (await goals.get_async("g1")).goal == "new-topic"


@pytest.mark.asyncio
async def test_reply_is_dropped_after_session_reset_even_without_new_messages(
    mock_context, sample_group_event, caplog
):
    from plugins.xiaoqing_chat.memory.person_profile import clear_profiles_and_memory

    caplog.set_level(logging.INFO, logger="plugins.xiaoqing_chat")
    state = bound_state(mock_context)

    def reply(_call) -> str:
        # 生成期间会话被重置：聊天记录里没有新消息，要接的那句也还在。
        clear_profiles_and_memory(mock_context.data_dir, _GROUP_CHAT, state.memory_db)
        return _REPLY_TEXT

    model = _delivery_model(reply=reply)
    result = await _reply_with_later(mock_context, sample_group_event, model)

    assert result == []
    assert [m.role for m in await state.memory_store.get_async(_GROUP_CHAT)] == ["user"]
    assert logged_events(caplog, "smalltalk.stale.drop")
    await settle_chat_background(mock_context, model)


@pytest.mark.parametrize(
    ("later", "event", "quoted"),
    [
        (4, {"group_id": 1, "message_id": 555}, False),  # 还在最近 5 条里：直接接
        (5, {"group_id": 1, "message_id": 555}, True),
        (10, {"group_id": 1, "message_id": 555}, True),
        (6, {"group_id": None, "message_id": 555}, False),
        (6, {"group_id": 1}, False),
    ],
)
def test_reply_quotes_the_target_once_it_is_out_of_the_last_five(later, event, quoted):
    from plugins.xiaoqing_chat import turn_delivery

    _record_messages("g1", ("m1", "user"), *_later(later))
    finalization = _fresh_finalization(event=event)
    first   = [{"type": "text", "data": {"text": "对"}}]
    batches = [first, [{"type": "text", "data": {"text": "就是这样"}}]]
    turn_delivery._quote_far_target(finalization, batches)
    if quoted:
        assert batches[0] == [{"type": "reply", "data": {"id": "555"}}, *first]
    else:
        assert batches[0] == first
    assert batches[1] == [{"type": "text", "data": {"text": "就是这样"}}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("later", "checked", "delivered"),
    [
        pytest.param([], False, True, id="nothing-after"),
        pytest.param(
            ["嘶", "喔？"], True, True, id="reactions-nearby"
        ),  # 近处的反应：直接发，不多问
        pytest.param(["对了，明天谁去图书馆"], True, True, id="subject-change-nearby"),
        pytest.param([("assistant", "上一轮的回复")], False, True, id="only-own-words-after"),
        pytest.param(_later_texts(5), True, True, id="five-later"),  # 已经隔了 5 条：确认话题
        pytest.param(_later_texts(15), False, False, id="too-far-left-to-stale-check"),
    ],
)
async def test_topic_is_only_checked_when_far_or_someone_changes_the_subject(
    mock_context, sample_group_event, later, checked, delivered
):
    model  = _delivery_model()
    result = await _reply_with_later(mock_context, sample_group_event, model, later)

    # 太远的交给过期检查，不再问模型。
    assert len(model.calls_of("reasoning")) == (1 if checked else 0)
    assert bool(result) is delivered
    if result:
        await ack_reply_delivery(result)
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [True, False])
async def test_reply_is_dropped_when_the_group_moved_to_another_topic(
    mock_context, sample_group_event, caplog, changed
):
    caplog.set_level(logging.INFO, logger="plugins.xiaoqing_chat")
    model = _delivery_model(reasoning=_WITHHOLD if changed else _PASS)
    later  = _later_texts(6)
    result = await _reply_with_later(
        mock_context, sample_group_event, model, later, before=["901 换车底了"]
    )

    # 复核拿到的是：她接的那句和它之前的话、她的回复、后来的几条消息。
    (review,) = model.calls_of("reasoning")
    assert "901 换车底了" in review.text and _TARGET_TEXT in review.text
    assert _REPLY_TEXT in review.text
    assert all(text in review.text for text in later)
    assert bool(logged_events(caplog, "smalltalk.stale.drop")) is changed
    assert bool(result) is not changed
    if result:
        await ack_reply_delivery(result)
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [True, False])
@pytest.mark.parametrize(
    "text",
    ["@阿梨 对了，明天谁去图书馆", "@阿梨 话说，明天谁去图书馆", "阿梨，对了，明天谁去图书馆"],
)
async def test_single_addressed_topic_opener_reaches_model(
    mock_context, sample_group_event, text, changed
):
    model = _delivery_model(reasoning=_WITHHOLD if changed else _PASS)
    result = await _reply_with_later(mock_context, sample_group_event, model, [text])

    (review,) = model.calls_of("reasoning")
    assert text in review.text
    assert bool(result) is not changed
    if result:
        await ack_reply_delivery(result)
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
@pytest.mark.parametrize("arrival", ["during_check", "waiting_for_lock"])
@pytest.mark.parametrize(
    "new_role,new_text,second_verdict,delivered,checks",
    [
        ("user", "对了，明天谁去图书馆", True, False, 2),
        ("user", "没错，901 就是这辆", False, True, 2),
        ("assistant", "对了，明天谁去图书馆", False, True, 1),
        ("user", "不用回复了", True, False, 2),
    ],
)
async def test_finalization_refreshes_topic_snapshot_before_delivery(
    mock_context,
    sample_group_event,
    arrival,
    new_role,
    new_text,
    second_verdict,
    delivered,
    checks,
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    started      = asyncio.Event()
    finish_check = asyncio.Event()
    lock         = chat_lock(_GROUP_CHAT)

    async def review(_call) -> str:
        if len(model.calls_of("reasoning")) == 1:
            # 第一次复核卡在模型上，新消息在这期间到来。
            started.set()
            await finish_check.wait()
            return _PASS
        return _WITHHOLD if second_verdict else _PASS

    model = _delivery_model(reasoning=review)
    scripted_chat_context(mock_context, model, GROUP_GATE_OPEN)
    target = await _target_with_later(mock_context, sample_group_event, _later_texts(5))
    turn   = asyncio.create_task(handle_smalltalk(_TARGET_TEXT, target, mock_context))
    held   = False
    try:
        await asyncio.wait_for(started.wait(), 5)
        if arrival == "waiting_for_lock":
            # 复核结束后这一轮要进会话锁，锁被别人占着，新消息在它等锁的时候到来。
            await lock.acquire()
            held = True
            finish_check.set()
            await asyncio.sleep(0.05)
        if new_role == "user" and arrival == "during_check":
            await _hears(mock_context, sample_group_event, new_text, message_id=99)
        else:
            # 收消息的入口要拿会话锁，占着锁时只能直接写进聊天记录。
            bound_state(mock_context).memory_store.append(
                _GROUP_CHAT, role=new_role, name="路人", user_id=777, content=new_text
            )
        finish_check.set()
        if held:
            lock.release()
            held = False
        result = await asyncio.wait_for(turn, 5)
        if result:
            await ack_reply_delivery(result, delivered=False)
    finally:
        finish_check.set()
        if held:
            lock.release()
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)

    assert bool(result) is delivered
    reviews = model.calls_of("reasoning")
    assert len(reviews) == checks
    assert new_text not in reviews[0].text
    if checks == 2:
        assert new_text in reviews[1].text
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_finalization_stops_after_two_outdated_topic_snapshots(
    mock_context, sample_group_event, caplog
):
    caplog.set_level(logging.INFO, logger="plugins.xiaoqing_chat")
    reviews = 0

    async def review(_call) -> str:
        nonlocal reviews
        reviews += 1
        await asyncio.sleep(0)
        await _hears(mock_context, sample_group_event, "还有新消息", message_id=100 + reviews)
        return _PASS

    model = _delivery_model(reasoning=review)
    result = await _reply_with_later(mock_context, sample_group_event, model, _later_texts(5))

    # 每次复核期间又来了新消息：复核两次仍跟不上，这一轮就不发了。
    assert result == []
    assert reviews == 2
    assert logged_events(caplog, "smalltalk.stale.drop")
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_reply_is_dropped_when_its_target_scrolled_out_during_the_typing_wait(
    mock_context, sample_group_event, caplog
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    caplog.set_level(logging.INFO, logger="plugins.xiaoqing_chat")
    real_sleep = asyncio.sleep
    waited     = []

    async def flood_during_the_wait(delay, result=None):
        # 发送前模拟打字的等待期间，群里刷了几百条，她要接的那句被挤出了聊天记录。
        if delay > 0 and not waited:
            waited.append(delay)
            _flood_group(mock_context, MAX_CACHED_MESSAGES_PER_CHAT + 1)
        await real_sleep(0)
        return result

    model = _delivery_model()
    scripted_chat_context(
        mock_context,
        model,
        GROUP_GATE_OPEN,
        {"humanize": {"enable_typing_delay": True, "read_base_seconds": 3.0, "jitter_ratio": 0.0}},
    )
    target = await _target_with_later(mock_context, sample_group_event, _later_texts(5))

    with patch("asyncio.sleep", flood_during_the_wait):
        result = await handle_smalltalk(_TARGET_TEXT, target, mock_context)

    assert waited
    assert result == []
    # 目标已经不在记录里，没有“后来几条”可数，也不再请模型复核话题。
    assert model.calls_of("reasoning") == []
    (drop,) = logged_events(caplog, "smalltalk.stale.drop")
    assert "newer_messages" not in drop.fields
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_delivered_reply_commits_planner_state_despite_interleaved_messages(
    mock_context, sample_group_event
):
    """回复已经发出：后面又有人插话，“刚回复过”的规划状态也要记上，下一轮才会按跟进来规划。"""

    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    model = ScriptedChatModel(
        plan  = [plan_answer("rethink_goal"), plan_answer("direct_reply")],
        goals = goals_answer("接着聊换车底"),
        reply = _REPLY_TEXT,
    )
    scripted_chat_context(mock_context, model, GROUP_GATE_OPEN)
    target = await _target_with_later(mock_context, sample_group_event)
    result = await handle_smalltalk(_TARGET_TEXT, target, mock_context)
    assert [seg["data"]["text"] for seg in result] == [_REPLY_TEXT]

    # 回复交给发送层之后、回执到来之前，群里又有人说话。
    for index, text in enumerate(("插一句", "再插一句")):
        await _hears(mock_context, sample_group_event, text, message_id=10 + index)
    await ack_reply_delivery(result)

    persisted = await _flushed_pfc_state(mock_context)
    assert persisted.top_goal == "接着聊换车底"
    assert persisted.last_successful_reply_action == "direct_reply"
    history = await bound_state(mock_context).memory_store.get_async(_GROUP_CHAT)
    assert [message.role for message in history] == ["user", "user", "user", "assistant"]
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [True, False])
async def test_reply_bubbles_release_the_chat_once_settled(
    mock_context, sample_group_event, outcome
):
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    model = _delivery_model(reply=["第一句回复", "第二句回复"])
    scripted_chat_context(mock_context, model, GROUP_GATE_OPEN)
    first = await handle_smalltalk(
        _TARGET_TEXT, _group_speech(sample_group_event, _TARGET_TEXT, message_id=1), mock_context
    )
    assert first

    # 上一条回复交给发送层、回执还没到：同一个会话的下一轮不开始规划和生成。
    second_event = _group_speech(sample_group_event, "再问一句", message_id=2)
    second_turn = asyncio.create_task(handle_smalltalk("再问一句", second_event, mock_context))
    try:
        await asyncio.sleep(0.1)
        assert not second_turn.done()
        assert len(model.calls_of("reply")) == 1

        # 回执结算（送达或没送达都一样）之后，下一轮才开始。
        await ack_reply_delivery(first, delivered=outcome)
        second = await asyncio.wait_for(second_turn, 5)
    finally:
        second_turn.cancel()
        await asyncio.gather(second_turn, return_exceptions=True)
    assert len(model.calls_of("reply")) == 2
    if second:
        await ack_reply_delivery(second)
    await settle_chat_background(mock_context, model)


def _stored(local_id: str, text: str, role: str = "user"):
    from plugins.xiaoqing_chat.memory.memory import StoredMessage

    return StoredMessage(
        role=role, name="群友", ts=1_000.0, user_id=1, local_id=local_id, content=text
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('{"withhold_reply": true}', True),
        ('```json\n{"withhold_reply": false}\n```', False),
        ('{"withhold_reply": "true"}', True),  # 只认 JSON 布尔
        ("看不懂", True),
    ],
)
async def test_reply_should_be_withheld_reads_the_planner_verdict(monkeypatch, content, expected):
    from plugins.xiaoqing_chat.llm import topic_shift
    from plugins.xiaoqing_chat.llm.llm_config import LLMCallConfig

    captured: dict = {}

    async def fake_chat(**kwargs):
        captured.update(kwargs)
        return {"choices": [{"message": {"content": content}}]}, "profile"

    monkeypatch.setattr(topic_shift, "chat_completions_raw_with_profile", fake_chat)
    changed = await topic_shift.reply_should_be_withheld(
        secrets  = {},
        bot_name = "小青",
        before   = [_stored("m1", "有可能901/902这对车从京局车变成广局车了？")],
        after    = [_stored("m2", "对了，明天谁去图书馆")],
        reply    = "我猜是换车底了",
        llm=LLMCallConfig(timeout_seconds=30.0, total_timeout_seconds=60.0, max_retry=2),
        chat_id=_GROUP_CHAT,
    )

    assert changed is expected
    assert captured["route"] == "reasoning"
    assert captured["timeout_seconds"] <= 6.0
    assert captured["total_timeout_seconds"] <= 6.0
    assert captured["max_retry"] == 0
    prompt = captured["messages"][0]["content"]
    assert "901/902" in prompt and "明天谁去图书馆" in prompt and "我猜是换车底了" in prompt


@pytest.mark.asyncio
async def test_context_review_withholds_reply_when_model_is_unavailable(monkeypatch, caplog):
    from plugins.xiaoqing_chat.llm import topic_shift
    from plugins.xiaoqing_chat.llm.llm_config import LLMCallConfig

    caplog.set_level(logging.INFO, logger="plugins.xiaoqing_chat")

    async def failing_chat(**_kwargs):
        raise TimeoutError

    monkeypatch.setattr(topic_shift, "chat_completions_raw_with_profile", failing_chat)
    kwargs = {
        "bot_name": "小青",
        "before"  : [_stored("m1", "901 换车底了")],
        "after"   : [_stored("m2", "对了，明天考试")],
        "reply"   : "真的假的",
        "llm"     : LLMCallConfig(),
        "chat_id" : _GROUP_CHAT,
    }
    assert await topic_shift.reply_should_be_withheld(secrets={}, **kwargs)
    (error,) = logged_events(caplog, "smalltalk.context_check.error")
    assert error.fields["error"] == "TimeoutError"
    assert await topic_shift.reply_should_be_withheld(secrets={"_ai": None}, **kwargs)
