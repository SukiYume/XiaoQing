"""xiaoqing_chat → xiaoqing_agent 的挂钩：只在 agent 接手时改变本轮，否则完全保持原聊天流程。

聊天插件用的是真实的运行时状态、门控、生成和投递代码；agent 是另一个插件，经
``context.capabilities.agent_turn`` 这条插件间的边界接入，测试里用替身；模型用剧本模型。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from core.delivery import DeliverySegments
from plugins.xiaoqing_agent.prompts import build_turn_message
from plugins.xiaoqing_agent.tasks import AgentTask, ChatRef
from plugins.xiaoqing_chat import agent_bridge, handlers
from plugins.xiaoqing_chat.chat_event import event_chat_id
from plugins.xiaoqing_chat.generation_limiter import GenerationLimitExceeded
from plugins.xiaoqing_chat.handler_context import HandlerContext
from plugins.xiaoqing_chat.media.settings import figures_inbox_dir
from plugins.xiaoqing_chat.runtime_state import get_state
from plugins.xiaoqing_chat.smalltalk_models import PreparedSmalltalkTurn
from plugins.xiaoqing_memory.model import Note
from tests.helpers.xiaoqing_chat_memory_test_support import MemoryHarness
from tests.helpers.xiaoqing_chat_test_support import (
    GROUP_GATE_CLOSED,
    GROUP_GATE_OPEN,
    ScriptedChatModel,
    ack_reply_delivery,
    chat_message_event,
    plan_answer,
    scripted_chat_context,
    settle_chat_background,
)

mock_context = _fixture_support.mock_context

CHAT_ID = "g67890"


def _prepared(
    *,
    forced: bool     = True,
    is_private: bool = False,
    text: str        = "小青，帮我画个 sin 图",
) -> PreparedSmalltalkTurn:
    return PreparedSmalltalkTurn(
        text              = text,
        mentioned         = forced,
        is_private        = is_private,
        forced            = forced,
        force_reason      = "mentioned" if forced else "",
        brain_chat_active = False,
        mood_text         = "",
    )


def _event() -> dict:
    return {
        "post_type"   : "message",
        "message_type": "group",
        "user_id"     : 12345,
        "group_id"    : 67890,
        "message_id"  : 99,
        "sender"      : {"user_id": 12345, "card": "主人"},
        "message"     : [
            {"type": "text", "data": {"text": "小青，帮我画个 sin 图"}},
            {"type": "image", "data": {"file": "abc.image", "url": "https://img.example/a.png"}},
            {"type": "image", "data": {"file": "local.png"}},
        ],
    }


async def _delegate(prepared, event, context, hctx):
    """走生产入口：agent 接手时返回本轮回复，否则是聊天那一路的结果（这里不生成，为 None）。"""

    async def no_chat(_deciding):
        return None

    return await agent_bridge.answer_with_agent_or_chat(prepared, event, context, hctx, no_chat)


def _seed_history(*, earlier: bool = True) -> None:
    """聊天记录：一条群友的话、她的回复、再加上此刻这条用户消息（已经记进聊天记录）。"""

    store = get_state().memory_store
    if earlier:
        store.append(
            CHAT_ID,
            role       = "user",
            name       = "甲",
            user_id    = 1,
            message_id = 97,
            local_id   = "m97",
            content    = "早",
            ts         = 1.0,
        )
        store.append(CHAT_ID, role="assistant", name="小青", local_id="m98", content="早呀", ts=2.0)
    store.append(
        CHAT_ID,
        role       = "user",
        name       = "主人",
        user_id    = 12345,
        message_id = 99,
        local_id   = "m99",
        content    = "当前",
        ts         = 3.0,
    )


def _hctx(context, agent, *config, seed: bool = True, model=None, **capabilities):
    """聊天插件的真实上下文：模型是剧本模型，agent 是替身（插件之间的边界）。"""

    model = model or ScriptedChatModel()
    scripted_chat_context(context, model, *config)
    context.capabilities = replace(
        context.capabilities, is_bot_admin=True, agent_turn=agent, **capabilities
    )
    hctx = HandlerContext.from_event(_event(), context)
    if seed:
        _seed_history()
    return hctx, model


def _handled(text: str, task_id: str = "a1b2c3", **extra):
    return {
        "handled"   : True,
        "segments"  : [{"type": "text", "data": {"text": text}}],
        "reply_text": text,
        "task_id"   : task_id,
        **extra,
    }


def _bot_replies() -> list:
    """聊天记录里小青说过的话。"""

    return [m for m in get_state().memory_store.get(CHAT_ID) if m.role == "assistant"]


async def _agent_tasks() -> list:
    """行动历史里 agent 接手的记录（agent 接手的一轮才会把用户的请求记进聊天、写下这条）。"""

    records = await get_state().action_history.get_recent_async(CHAT_ID)
    return [record for record in records if record.action == "agent_task"]


async def _chat_slot_is_free(chat_id: str = CHAT_ID, user_id: object = 12345) -> bool:
    """同会话的生成名额空着、也没有还没结算的回复在占用。"""

    try:
        async with asyncio.timeout(1):
            async with get_state().generation_limiter.admit(
                chat_id                    = chat_id,
                user_id                    = str(user_id),
                max_global                 = 0,
                max_per_chat               = 1,
                max_per_user               = 0,
                max_calls_per_user_per_day = 0,
            ):
                return True
    except GenerationLimitExceeded:
        return False


@pytest.mark.asyncio
async def test_handled_turn_returns_agent_reply_and_records_both_sides(mock_context, tmp_path):
    agent = SimpleNamespace(handle_turn=AsyncMock(return_value=_handled("画好了")))
    # 近况来自统一记忆里的自我记录；回复送达后的生活记录抽取要调模型，剧本里答空。
    hctx, model = _hctx(
        mock_context,
        agent,
        {"life": {"enable_self_timeline": True}},
        model=ScriptedChatModel(reasoning="{}"),
        memory=MemoryHarness(tmp_path).service(CHAT_ID),
    )
    now = time.time()
    MemoryHarness(tmp_path).store.put_notes(
        CHAT_ID,
        [
            Note(
                id          = "n1",
                kind        = "self.promise",
                scope       = CHAT_ID,
                subject     = "self",
                text        = "弄完图告诉你",
                origin      = "self",
                created_at  = now,
                observed_at = now,
                expires_at  = now + 6 * 3600,
            )
        ],
    )

    result = await _delegate(_prepared(), _event(), mock_context, hctx)

    assert result == [{"type": "text", "data": {"text": "画好了"}}]
    assert isinstance(result, DeliverySegments)
    turn = agent.handle_turn.await_args.args[0]
    assert turn["text"] == "小青，帮我画个 sin 图"
    assert (turn["user_id"], turn["group_id"], turn["message_id"]) == (12345, 67890, 99)
    assert turn["sender_name"] == "主人" and turn["bot_name"] == "小青"
    assert turn["images"] == ["https://img.example/a.png"]
    assert turn["history"] == [
        {"role": "user", "name": "甲", "user_id": 1, "text": "早", "ts": 1.0},
        {"role": "assistant", "name": "小青", "user_id": None, "text": "早呀", "ts": 2.0},
    ]
    # 人设只在聊天插件里配置，随每轮交给 agent，agent 的回复和平时聊天是同一个人。
    # agent 拿人物事实和〔办事〕各节，闲聊示例留在聊天这边。
    card = hctx.runtime.cfg.personality.card
    assert turn["persona"] == card.for_agent("小青")
    assert turn["persona"].startswith("你的名字是小青。")
    # 近况每轮不同，单独交：人设卡在一个办事会话里保持不变。
    assert "弄完图告诉你" in turn["recent_self"] and "弄完图告诉你" not in turn["persona"]

    # 这句请求记进聊天记录（已有的那条，不重复）、行动历史记下 agent 接手；
    # 回复确认发出后才记成小青的发言。
    (task,) = await _agent_tasks()
    assert task.local_target == "m99"
    assert task.detail == {"task_id": "a1b2c3"}
    assert [m.content for m in get_state().memory_store.get(CHAT_ID)].count("当前") == 1
    assert _bot_replies()[-1].content == "早呀"
    await result.delivery_receipt.record(True)
    reply = _bot_replies()[-1]
    assert (reply.source_plugin, reply.content) == ("xiaoqing_agent", "画好了")
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delivered", "recorded"),
    [(True, True), (False, False), (None, True)],
)
async def test_reply_receipt_reports_the_delivery_back_to_the_agent(
    mock_context, delivered, recorded
):
    agent = SimpleNamespace(
        handle_turn=AsyncMock(return_value=_handled("画好了")),
        report_delivery=AsyncMock(side_effect=RuntimeError("agent unloaded")),
    )
    hctx, model = _hctx(mock_context, agent)
    result = await _delegate(_prepared(), _event(), mock_context, hctx)

    # 回报失败（比如 agent 刚被卸载）不影响聊天侧的收尾。
    await result.delivery_receipt.record(delivered)
    agent.report_delivery.assert_awaited_once_with("a1b2c3", delivered)
    assert any(m.source_plugin == "xiaoqing_agent" for m in _bot_replies()) is recorded
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        None,
        {"handled": False},
        {"handled": True, "segments": "not a list"},
        RuntimeError("plugin service is unavailable"),
        PermissionError("agent capability requires a current Bot admin"),
    ],
)
async def test_any_non_handled_outcome_falls_back_to_chat(mock_context, outcome):
    if isinstance(outcome, Exception):
        agent = SimpleNamespace(handle_turn=AsyncMock(side_effect=outcome))
    else:
        agent = SimpleNamespace(handle_turn=AsyncMock(return_value=outcome))
    hctx, model = _hctx(mock_context, agent)
    before = list(get_state().memory_store.get(CHAT_ID))

    assert await _delegate(_prepared(), _event(), mock_context, hctx) is None
    assert await _agent_tasks() == []
    assert list(get_state().memory_store.get(CHAT_ID)) == before
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_wait_boundary_delivers_once_and_followup_uses_the_finished_task(
    mock_context, tmp_path
):
    from plugins.xiaoqing_agent import main as agent_main
    from tests.helpers.xiaoqing_agent_test_support import (
        FakeAgentContext,
        ai_response,
        sent_texts,
        tool_call,
    )

    release = asyncio.Event()

    async def finish(_route, _messages, _kwargs):
        await release.wait()
        return ai_response("", [tool_call("final_answer", {"text": "记好了，98，走的支付宝"})])

    agent_context = FakeAgentContext(
        tmp_path / "agent",
        user_id      = 12345,
        group_id     = 67890,
        agent_config = {"inline_wait_seconds": 0.02},
    )
    agent_context.ai.script = [
        ai_response(json.dumps({"action": "task", "goal": "记录订阅费98元", "reply": "记了"})),
        finish,
    ]

    async def handle_turn(turn):
        return await agent_main.handle_turn(turn, agent_context)

    agent = SimpleNamespace(handle_turn=handle_turn, report_delivery=AsyncMock())
    hctx, model = _hctx(mock_context, agent)
    chat = AsyncMock(return_value=[{"type": "text", "data": {"text": "普通聊天回复"}}])
    prepared = _prepared(text="帮我记一下，订阅费98，支付宝")
    reply = await agent_bridge.answer_with_agent_or_chat(
        prepared, _event(), mock_context, hctx, chat
    )
    assert reply == [] and sent_texts(agent_context) == []
    assert chat.await_count <= 1
    assert len(await _agent_tasks()) == 1
    assert _bot_replies()[-1].content == "早呀"

    runtime = agent_main.get_runtime(agent_context)
    task    = next(iter(runtime.running.values()))
    release.set()
    assert task.runner is not None
    await task.runner
    assert [text.strip() for text in sent_texts(agent_context)] == ["记好了，98，走的支付宝"]
    records = await runtime.store.recent("g67890", 5)
    assert len(records) == 1 and records[0]["delivery"] == "sent"

    agent_context.agent_config["inline_wait_seconds"] = 1
    agent_context.ai.script                           = [
        ai_response(json.dumps({"action": "task", "goal": "把刚才订阅费的账户改为微信"})),
        ai_response("", [tool_call("final_answer", {"text": "改好了，走的微信"})]),
    ]
    reply = await _delegate(_prepared(text="刚才那笔改成微信"), _event(), mock_context, hctx)
    assert isinstance(reply, DeliverySegments)
    assert reply[0]["data"]["text"] == "改好了，走的微信"
    # 接着说：前一轮的交付（final_answer 的调用）在同一份办事会话里。
    delivered = [
        json.loads(call["function"]["arguments"]).get("text")
        for message in agent_context.ai.calls[-1][1][:-1]
        for call in message.get("tool_calls") or ()
    ]
    assert "记好了，98，走的支付宝" in delivered
    await reply.delivery_receipt.record(True)
    assert [m.content for m in _bot_replies()][-1] == "改好了，走的微信"
    assert sum(m.source_plugin == "xiaoqing_agent" for m in _bot_replies()) == 1
    await agent_main.shutdown(agent_context)
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_agent_is_not_consulted_without_capability_or_explicit_address(mock_context):
    agent = SimpleNamespace(handle_turn=AsyncMock())
    hctx, model = _hctx(mock_context, agent)
    assert await _delegate(_prepared(forced=False), _event(), mock_context, hctx) is None
    agent.handle_turn.assert_not_awaited()

    hctx, model = _hctx(mock_context, None, seed=False, model=model)
    assert await _delegate(_prepared(), _event(), mock_context, hctx) is None
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_private_turns_reach_the_agent_even_without_a_forced_summon(mock_context):
    agent = SimpleNamespace(handle_turn=AsyncMock(return_value=None))
    hctx, model = _hctx(mock_context, agent)
    prepared = _prepared(forced=False, is_private=True, text="帮我算一下 3 的 50 次方")
    assert await _delegate(prepared, _event(), mock_context, hctx) is None
    agent.handle_turn.assert_awaited_once()
    assert agent.handle_turn.await_args.args[0]["is_private"] is True
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["我这周有什么事件", "看看本周的安排", "这星期还有什么事", "记一笔午饭 25"],
    ids=["week-events", "week-plans", "week-tasks", "ledger"],
)
@pytest.mark.parametrize(
    ("idle_seconds", "followup_delay", "interleaved"),
    [(30, 13, False), (2800, 13, False), (2800, 13, True), (2800, 61, False)],
    ids=["active", "idle", "idle-with-other-speaker", "expired"],
)
async def test_name_call_does_not_bypass_gate_after_idle_cleanup(
    mock_context, monkeypatch, text, idle_seconds, followup_delay, interleaved
):
    agent = SimpleNamespace(
        handle_turn=AsyncMock(
            side_effect=[
                {"handled": True, "decision": {"action": "task", "task_id": ""}},
                _handled("任务结果", "task1"),
            ]
        )
    )
    # 群里没人叫她、插话概率为 0：门控必然不放行。
    hctx, model = _hctx(
        mock_context,
        agent,
        GROUP_GATE_CLOSED,
        {
            "goal"       : {"enable_goal": False},
            "reflection" : {"enable_expression_reflection": False},
            "personality": {"state_probability": 0.0},
        },
        seed=False,
    )
    state = get_state()
    clock = [time.time()]
    monkeypatch.setattr(handlers.time, "time", lambda: clock[0])
    state.memory_store.append(
        hctx.chat_id,
        role     = "user",
        name     = "群友",
        content  = "旧话题",
        ts       = clock[0] - idle_seconds,
        local_id = "old",
    )
    state.set_continuous_reply_count(hctx.chat_id, 3)

    if interleaved:
        clock[0] += 5
        other = {
            **_event(),
            "user_id"                   : 54321,
            "message_id"                : 98,
            "message"                   : [{"type": "text", "data": {"text": "今天好冷"}}],
            "_xc_user_recorded_local_id": "other",
        }
        state.memory_store.append(
            hctx.chat_id,
            role       = "user",
            name       = "群友",
            content    = "今天好冷",
            ts         = clock[0],
            local_id   = "other",
            user_id    = 54321,
            message_id = 98,
        )
        assert await handlers.handle_smalltalk("今天好冷", other, mock_context) == []
        agent.handle_turn.assert_not_awaited()
        clock[0] -= 5
    clock[0] += followup_delay
    event = {
        **_event(),
        "message"                   : [{"type": "text", "data": {"text": text}}],
        "_xc_user_recorded_local_id": "current",
    }
    state.memory_store.append(
        hctx.chat_id,
        role       = "user",
        name       = "主人",
        content    = text,
        ts         = clock[0],
        local_id   = "current",
        user_id    = 12345,
        message_id = 99,
    )
    result = await handlers.handle_smalltalk(text, event, mock_context)

    assert state.get_continuous_reply_count(hctx.chat_id) == (0 if idle_seconds > 1800 else 3)
    assert result == []
    agent.handle_turn.assert_not_awaited()
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_stuck_agent_is_abandoned_after_the_bridge_timeout(mock_context):
    async def never(_turn):
        await asyncio.Event().wait()

    hctx, model = _hctx(mock_context, SimpleNamespace(handle_turn=never))
    # 判断阶段和执行阶段各有自己的兜底时限（25 秒 / 50 秒，没有假时钟可推进，所以缩短它们）。
    with patch.object(agent_bridge, "_AGENT_DECIDE_TIMEOUT_SECONDS", 0.05):
        assert await _delegate(_prepared(), _event(), mock_context, hctx) is None

    calls: list[dict] = []

    async def decides_then_hangs(turn):
        calls.append(turn)
        if turn.get("phase") == "decide":
            return {"handled": True, "decision": {"action": "task", "goal": "g"}}
        await asyncio.Event().wait()

    hctx, model = _hctx(
        mock_context, SimpleNamespace(handle_turn=decides_then_hangs), seed=False, model=model
    )
    with patch.object(agent_bridge, "_AGENT_TURN_TIMEOUT_SECONDS", 0.05):
        assert await _delegate(_prepared(), _event(), mock_context, hctx) is None
    assert [turn["phase"] for turn in calls] == ["decide", "act"]
    assert calls[1]["decision"] == {"action": "task", "goal": "g"}
    await settle_chat_background(mock_context, model)


def _private_event(text: str) -> dict:
    return chat_message_event(_event(), text, message_type="private", group_id=None)


@pytest.mark.asyncio
async def test_agent_taking_over_cancels_the_chat_reply_generated_alongside(mock_context):
    started   = asyncio.Event()
    cancelled = []
    busy      = []
    phases    = []
    decisions = []
    reply     = [{"type": "text", "data": {"text": "交给 agent 了"}}]
    event     = _private_event("画图")
    chat_id   = event_chat_id(event)

    async def generate(_call):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def handle_turn(turn):
        phases.append(turn["phase"])
        if turn["phase"] == "decide":
            # 生成和判断同时开始：判断出来时聊天回复正在生成，占着会话的生成名额。
            await started.wait()
            busy.append(not await _chat_slot_is_free(chat_id))
            return {"handled": True, "decision": {"action": "task"}}
        decisions.append(turn["decision"])
        return _handled("交给 agent 了", "t1")

    _, model = _hctx(
        mock_context,
        SimpleNamespace(handle_turn=handle_turn),
        seed=False,
        model=ScriptedChatModel(reply=generate),
    )

    result = await handlers.handle_smalltalk("画图", event, mock_context)

    assert result == reply
    # 接手后生成被取消、名额立即让出，聊天回复没有发出。
    assert busy == [True] and cancelled == [True] and phases == ["decide", "act"]
    assert decisions == [{"action": "task"}]
    assert await _chat_slot_is_free(chat_id)
    await ack_reply_delivery(result)
    assert [
        m.source_plugin for m in get_state().memory_store.get(chat_id) if m.role == "assistant"
    ] == ["xiaoqing_agent"]
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_declined_turn_uses_the_chat_reply_already_being_generated(mock_context):
    generating = asyncio.Event()
    order      = []
    phases     = []
    event      = _private_event("在吗")
    chat_id    = event_chat_id(event)

    def generate(_call):
        order.append("generate")
        generating.set()
        return "普通回复"

    async def handle_turn(turn):
        phases.append(turn["phase"])
        # 判断还没出来时聊天回复已经在生成：闲聊不用先等判断。
        await generating.wait()
        order.append("declined")
        return {"handled": False}

    _, model = _hctx(
        mock_context,
        SimpleNamespace(handle_turn=handle_turn),
        seed=False,
        model=ScriptedChatModel(reply=generate),
    )

    result = await handlers.handle_smalltalk("在吗", event, mock_context)

    assert [seg["data"]["text"] for seg in result] == ["普通回复"]
    assert order == ["generate", "declined"]
    assert phases == ["decide"]
    # 用的就是已经在生成的那份回复，没有重新生成。
    assert len(model.calls_of("reply")) == 1
    await ack_reply_delivery(result)
    assert [m.content for m in get_state().memory_store.get(chat_id) if m.role == "assistant"] == [
        "普通回复"
    ]
    assert await _chat_slot_is_free(chat_id)
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_chat_reply_finished_first_waits_for_the_decision_and_is_dropped(mock_context):
    finished = asyncio.Event()
    event    = _private_event("画图")
    chat_id  = event_chat_id(event)

    def generate(_call):
        finished.set()
        return "聊天回复"

    async def handle_turn(turn):
        if turn["phase"] == "decide":
            # 聊天回复先生成完；判断晚到，这期间聊天回复不能发出去。
            await finished.wait()
            await asyncio.sleep(0.05)
            return {"handled": True, "decision": {"action": "task"}}
        return _handled("agent 的结果", "t1")

    _, model = _hctx(
        mock_context,
        SimpleNamespace(handle_turn=handle_turn),
        seed=False,
        model=ScriptedChatModel(reply=generate),
    )

    result = await handlers.handle_smalltalk("画图", event, mock_context)

    assert [seg["data"]["text"] for seg in result] == ["agent 的结果"]
    # 聊天回复没有进入投递：没有等着结算的回复占着会话。
    assert await _chat_slot_is_free(chat_id)
    await ack_reply_delivery(result)
    assert [
        m.source_plugin for m in get_state().memory_store.get(chat_id) if m.role == "assistant"
    ] == ["xiaoqing_agent"]
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_failed_agent_handoff_generates_one_chat_reply(mock_context):
    phases  = []
    event   = _private_event("画图")
    chat_id = event_chat_id(event)

    async def handle_turn(turn):
        phases.append(turn["phase"])
        if turn["phase"] == "decide":
            return {"handled": True, "decision": {"action": "task"}}
        return {"handled": False}

    _, model = _hctx(
        mock_context,
        SimpleNamespace(handle_turn=handle_turn),
        seed=False,
        model=ScriptedChatModel(reply="聊天回复"),
    )

    result = await handlers.handle_smalltalk("画图", event, mock_context)

    assert phases == ["decide", "act"]
    assert [seg["data"]["text"] for seg in result] == ["聊天回复"]
    await ack_reply_delivery(result)
    assert [m.content for m in get_state().memory_store.get(chat_id) if m.role == "assistant"] == [
        "聊天回复"
    ]
    assert await _chat_slot_is_free(chat_id)
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_turns_the_agent_cannot_take_skip_the_decision(mock_context):
    agent = SimpleNamespace(handle_turn=AsyncMock())
    # 群里没人叫她：只走聊天，由规划器决定参与（插话概率 1，门控放行）。
    model = ScriptedChatModel(reply="普通回复", plan=plan_answer("direct_reply"))
    scripted_chat_context(mock_context, model, GROUP_GATE_OPEN)
    mock_context.capabilities = replace(
        mock_context.capabilities, is_bot_admin=True, agent_turn=agent
    )
    event = chat_message_event(_event(), "今天好冷")

    result = await handlers.handle_smalltalk("今天好冷", event, mock_context)

    assert [seg["data"]["text"] for seg in result] == ["普通回复"]
    agent.handle_turn.assert_not_awaited()
    await ack_reply_delivery(result)
    assert await _chat_slot_is_free()
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_quoted_message_and_downloaded_images_reach_the_agent(mock_context, tmp_path):
    from plugins.xiaoqing_chat.media.models import RenderedMedia

    agent = SimpleNamespace(handle_turn=AsyncMock(return_value=None))
    media = SimpleNamespace(
        get_message=AsyncMock(
            return_value={
                "sender" : {"nickname": "甲"},
                "message": [
                    {"type": "text", "data": {"text": "这是上个月的数据表"}},
                    {"type": "image", "data": {"url": "https://img.example/quoted.png"}},
                ],
            }
        )
    )
    hctx, model = _hctx(mock_context, agent, onebot_media=media)
    cached = tmp_path / "cached.png"
    cached.write_bytes(b"png")
    event = _event()
    event["message"].insert(0, {"type": "reply", "data": {"id": "555"}})
    event["_xc_rendered_media_items"] = [
        RenderedMedia("h1", "image", "图", (), "[图片]", cached_path=cached),
        RenderedMedia("h2", "image", "图", (), "[图片]", cached_path=tmp_path / "gone.png"),
    ]

    await _delegate(_prepared(), event, mock_context, hctx)

    media.get_message.assert_awaited_once_with("555")
    turn = agent.handle_turn.await_args.args[0]
    assert turn["quoted_text"] == "甲: 这是上个月的数据表"
    assert turn["images"] == ["https://img.example/a.png", "https://img.example/quoted.png"]
    assert turn["image_files"] == [str(cached)]
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "intervening_messages", "quote", "in_history"),
    [
        # 最近的聊天带着识图描述，跨过办事会话的边界也一样。
        ("小青，研究一下这个消息", 3, False, True),
        # 更早的、没有明确引用的：不靠措辞猜指代，交给 agent 用 chat_history 自己查。
        ("小青，核对刚才那张截图", 14, False, False),
        # QQ 回复引用是明确的指向：那条连同图片一起交出去。
        ("小青，核对一下", 14, True, True),
    ],
)
async def test_history_carries_media_text_to_agent_even_across_session_boundary(
    mock_context, text, intervening_messages, quote, in_history
):
    agent = SimpleNamespace(handle_turn=AsyncMock(return_value=None))
    hctx, model = _hctx(mock_context, agent, seed=False)
    store = get_state().memory_store
    inbox = figures_inbox_dir(mock_context)
    inbox.mkdir(parents=True, exist_ok=True)
    cached = inbox / "test-image.png"
    cached.write_bytes(b"fixture")
    detail = "活动通知。" + "附加说明。" * 100 + "报名编号是 BLUE-672。"
    store.append(
        CHAT_ID,
        role       = "user",
        name       = "甲",
        user_id    = 1,
        message_id = 12,
        ts         = 1.0,
        parts      = ({"kind": "image", "description": detail, "file_path": str(cached)},),
    )
    for i in range(intervening_messages):
        store.append(CHAT_ID, role="user", name="乙", content=f"消息 {i}", ts=2.0 + i)
    event = {**_event(), "message": [{"type": "text", "data": {"text": text}}]}
    if quote:
        event["message"].insert(0, {"type": "reply", "data": {"id": "12"}})
    await _delegate(_prepared(text=text), event, mock_context, hctx)

    turn = agent.handle_turn.await_args.args[0]
    assert turn["image_files"] == ([str(cached)] if quote else [])
    assert turn["image_description_available"] is quote
    task = AgentTask("demo", ChatRef(12345, 67890), text, "chat", admin=True)
    message = build_turn_message(
        task     = task,
        now      = "现在",
        timezone = "Asia/Shanghai",
        out_dir  = "out",
        history  = turn["history"],
        # 引用的那条在上一轮办事之前也照样交出；其余按会话边界只给新消息。
        since     = 3.0 if quote else 0.0,
        recent    = [],
        images    = [],
        contracts = [],
    )
    assert ("甲（发起人引用的这条）" in message["content"]) is quote
    assert ("报名编号是 BLUE-672" in message["content"]) is in_history
    assert "[[xc_media_" not in message["content"]
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_quote_lookup_failure_does_not_block_the_turn(mock_context):
    agent = SimpleNamespace(handle_turn=AsyncMock(return_value=None))
    media = SimpleNamespace(get_message=AsyncMock(side_effect=RuntimeError("onebot down")))
    hctx, model = _hctx(mock_context, agent, onebot_media=media)
    event = _event()
    event["message"].insert(0, {"type": "reply", "data": {"id": "555"}})

    await _delegate(_prepared(), event, mock_context, hctx)

    turn = agent.handle_turn.await_args.args[0]
    assert turn["quoted_text"] == ""
    assert turn["images"] == ["https://img.example/a.png"]
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_command_runner_events_never_reach_the_agent(mock_context):
    from core.interfaces import COMMAND_RUNNER_EVENT_KEY

    agent = SimpleNamespace(handle_turn=AsyncMock())
    hctx, model = _hctx(mock_context, agent)
    event = {**_event(), COMMAND_RUNNER_EVENT_KEY: "xiaoqing_agent"}
    assert await _delegate(_prepared(), event, mock_context, hctx) is None
    agent.handle_turn.assert_not_awaited()
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_xc_free_chat_refuses_command_runner_tool_calls(mock_context):
    from core.interfaces import COMMAND_RUNNER_EVENT_KEY
    from plugins.xiaoqing_chat import main as chat_main

    model = ScriptedChatModel(reply="不该有这句")
    scripted_chat_context(mock_context, model)
    event = {"user_id": 12345, "group_id": None, COMMAND_RUNNER_EVENT_KEY: "xiaoqing_agent"}

    reply = await chat_main.handle("xc", "帮我画个图", event, mock_context)

    assert model.calls == []
    assert "不能作为工具调用" in reply[0]["data"]["text"]
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_quoting_the_agent_reply_in_a_group_is_recognized_as_talking_to_the_bot(mock_context):
    """回执提交时，这条回复拆成几条发出的全部消息 ID 和记录一起写入，引用哪一条都认得出。"""

    from core.interfaces import ACTION_RESULT_MESSAGE_ID_KEY
    from core.plugin_base import build_action
    from plugins.xiaoqing_chat.attention_gate import is_reply_to_bot

    long_reply = "图画好了，" + "细节说明" * 400
    agent      = SimpleNamespace(
        handle_turn=AsyncMock(return_value=_handled(long_reply)),
        report_delivery=AsyncMock(),
    )
    hctx, model = _hctx(mock_context, agent, seed=False)
    store = get_state().memory_store
    store.append(CHAT_ID, role="assistant", name="小青", content="更早的闲聊", ts=1.0)
    _seed_history(earlier=False)
    reply = await _delegate(_prepared(), _event(), mock_context, hctx)
    # 像 Core 一样：长回复拆成两条，每条发出后先登记消息 ID 再记下发送结果。
    receipt = reply.delivery_receipt
    receipt.add_expected_actions(1)
    for message_id in (5555, 5556):
        receipt.add_message_id(message_id)
        await receipt.record(True)
    agent.report_delivery.assert_awaited_once_with("a1b2c3", True)

    history = store.get(CHAT_ID)
    assert history[-1].remote_ids() == (5555, 5556)
    assert history[0].remote_ids() == ()

    # 出站观察者不按“最近一条”回填聊天自己的回复，也不会重复记一条。
    action = build_action(list(reply), 12345, 67890)
    assert action is not None
    action[ACTION_RESULT_MESSAGE_ID_KEY] = 5556
    await handlers.observe_outgoing_action(action, mock_context, source_plugin="xiaoqing_chat")
    assert len(store.get(CHAT_ID)) == len(history)

    for quoted in ("5555", "5556"):
        quote = {
            "message": [
                {"type": "reply", "data": {"id": quoted}},
                {"type": "text", "data": {"text": "改成红色"}},
            ]
        }
        assert await is_reply_to_bot(event=quote, state=get_state(), chat_id=CHAT_ID), quoted
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_concurrent_replies_keep_their_own_message_ids(mock_context):
    """两条回复几乎同时发出、回执先后顺序打乱，各自的记录仍对应各自的消息。"""

    answers = iter([("回复 A", "aaaaaa"), ("回复 B", "bbbbbb")])

    async def handle_turn(turn):
        if turn["phase"] == "decide":
            return {"handled": True, "decision": {"action": "task"}}
        text, task_id = next(answers)
        return _handled(text, task_id)

    agent = SimpleNamespace(handle_turn=handle_turn, report_delivery=AsyncMock())
    hctx, model = _hctx(mock_context, agent, seed=False)
    store = get_state().memory_store
    _seed_history(earlier=False)
    replies = [await _delegate(_prepared(), _event(), mock_context, hctx) for _ in range(2)]

    first, second = (reply.delivery_receipt for reply in replies)
    first.add_message_id(501)
    second.add_message_id(502)
    await second.record(True)
    await first.record(True)

    by_text = {
        message.content: message.remote_ids()
        for message in store.get(CHAT_ID)
        if message.role == "assistant"
    }
    assert by_text == {"回复 A": (501,), "回复 B": (502,)}
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_a_request_split_over_several_messages_reaches_the_agent_whole(mock_context):
    agent = SimpleNamespace(
        handle_turn=AsyncMock(
            side_effect=[
                {"handled": True, "decision": {"action": "task"}},
                _handled("记好了", "t1", status="done"),
            ]
        ),
        report_delivery=AsyncMock(),
    )
    hctx, model = _hctx(mock_context, agent)
    event = {**_event(), "_xc_burst_texts": ["帮我记一笔"]}

    result = await _delegate(_prepared(text="18 块 麻辣烫 微信"), event, mock_context, hctx)

    assert isinstance(result, DeliverySegments)
    assert agent.handle_turn.await_args_list[0].args[0]["text"] == "帮我记一笔\n18 块 麻辣烫 微信"
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_a_question_the_next_message_already_answers_is_not_sent(mock_context):
    from plugins.xiaoqing_chat import follow_ups

    agent = SimpleNamespace(
        handle_turn=AsyncMock(
            side_effect=[
                {"handled": True, "decision": {"action": "task"}},
                _handled("记什么呀？", "t1", status="needs_user"),
            ]
        ),
        report_delivery=AsyncMock(),
    )
    waiting = ({"message_id": 100, "message": [{"type": "text", "data": {"text": "18 块"}}]},)
    hctx, model = _hctx(
        mock_context, agent, inbound_backlog=SimpleNamespace(waiting=lambda: waiting)
    )
    follow_ups._carried.clear()
    try:
        result = await _delegate(_prepared(text="小青，帮我记一笔"), _event(), mock_context, hctx)
        # 追问没发；这句请求照常记进聊天记录，下一条算接着跟她说。
        assert result == []
        assert len(await _agent_tasks()) == 1
        assert _bot_replies()[-1].content == "早呀"
        agent.report_delivery.assert_awaited_once_with("t1", None)
        assert follow_ups.take_carried_attention({**_event(), "message_id": 100}) is True
    finally:
        follow_ups._carried.clear()
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_a_finished_result_is_sent_even_when_more_messages_are_waiting(mock_context):
    agent = SimpleNamespace(
        handle_turn=AsyncMock(
            side_effect=[
                {"handled": True, "decision": {"action": "task"}},
                _handled("记好了", "t1", status="done"),
            ]
        ),
        report_delivery=AsyncMock(),
    )
    waiting = ({"message_id": 100, "message": [{"type": "text", "data": {"text": "谢谢"}}]},)
    hctx, model = _hctx(
        mock_context, agent, inbound_backlog=SimpleNamespace(waiting=lambda: waiting)
    )

    result = await _delegate(_prepared(text="小青，记一笔 25 午饭"), _event(), mock_context, hctx)

    assert isinstance(result, DeliverySegments)
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_chat_history_service_filters_the_scoped_chat(mock_context):
    """chat.history 只读 Core 写入的会话；关键词、发言人、只看带图和 before 都在那个会话里筛。"""

    from plugins.xiaoqing_chat.chat_history import read_history
    from plugins.xiaoqing_chat.store_binding import bound_state

    store = bound_state(mock_context).memory_store
    inbox = figures_inbox_dir(mock_context)
    inbox.mkdir(parents=True, exist_ok=True)
    shot = inbox / "shot.png"
    shot.write_bytes(b"fixture")
    outside = mock_context.data_dir / "elsewhere.png"
    outside.write_bytes(b"fixture")
    store.append(
        CHAT_ID,
        role       = "user",
        name       = "Mika",
        user_id    = 1,
        message_id = 11,
        ts         = 1.0,
        parts      = ({"kind": "image", "description": "漏洞公告截图", "file_path": str(shot)},),
    )
    store.append(
        CHAT_ID,
        role       = "user",
        name       = "乙",
        user_id    = 2,
        message_id = 12,
        ts         = 2.0,
        parts      = ({"kind": "image", "description": "别处的图", "file_path": str(outside)},),
    )
    for index in range(3):
        store.append(
            CHAT_ID,
            role       = "user",
            name       = "乙",
            user_id    = 2,
            message_id = 20 + index,
            content    = f"闲聊 {index}",
        )
    store.append("g11111", role="user", name="外人", user_id=9, content="漏洞 另一个群")

    result = await read_history({"chat": CHAT_ID, "query": "漏洞"}, mock_context)
    assert result["chat"] == CHAT_ID and result["total"] == 5 and result["matched"] == 1
    [found] = result["messages"]
    assert found["id"] == 11 and found["sender"] == "Mika(1)" and "漏洞公告截图" in found["text"]
    assert found["images"] == [{"path": str(shot), "description": "漏洞公告截图"}]

    # 原图缓存在收图目录外（或已清理）的那条照样算发过图，只是没有可打开的文件。
    images = await read_history({"chat": CHAT_ID, "images_only": True}, mock_context)
    assert [item["id"] for item in images["messages"]] == [11, 12]
    assert images["messages"][1]["images"] == [] and "别处的图" in images["messages"][1]["text"]
    page = await read_history({"chat": CHAT_ID, "sender": "乙", "count": 2}, mock_context)
    assert [item["id"] for item in page["messages"]] == [21, 22] and page["earlier"] is True
    older = await read_history(
        {"chat": CHAT_ID, "sender": "2", "before": page["before"]}, mock_context
    )
    assert [item["id"] for item in older["messages"]] == [12, 20] and older["before"] == ""
    by_id = await read_history({"chat": CHAT_ID, "sender": "2", "before": 21}, mock_context)
    assert [item["id"] for item in by_id["messages"]] == [12, 20]
    for stale in ("99@7.5", "abc", "404"):
        result = await read_history({"chat": CHAT_ID, "before": stale}, mock_context)
        assert result["messages"] == [] and result["error"]
    with pytest.raises(ValueError):
        await read_history({"query": "漏洞"}, mock_context)


@pytest.mark.asyncio
async def test_chat_history_pages_through_records_without_message_ids(mock_context):
    """本地存档里没有 QQ 消息 ID 的记录也能一页页往前翻到头。"""

    from plugins.xiaoqing_chat.chat_history import read_history
    from plugins.xiaoqing_chat.store_binding import bound_state

    store = bound_state(mock_context).memory_store
    for index in range(7):
        store.append(CHAT_ID, role="user", name="乙", content=f"存档 {index}", ts=100.0 + index)
    seen: list[str] = []
    before          = ""
    for _ in range(5):
        page = await read_history({"chat": CHAT_ID, "count": 3, "before": before}, mock_context)
        assert all(item["id"] is None for item in page["messages"])
        seen   = [item["text"] for item in page["messages"]] + seen
        before = page["before"]
        if not before:
            break
    assert seen == [f"存档 {index}" for index in range(7)]
