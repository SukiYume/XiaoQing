"""xiaoqing_chat → xiaoqing_agent 的挂钩：只在 agent 接手时改变本轮，否则完全保持原聊天流程。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from core.delivery import DeliverySegments
from core.interfaces import PluginCapabilities
from plugins.xiaoqing_chat import agent_bridge, handlers
from plugins.xiaoqing_chat.chat_runtime import ChatRuntime
from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
from plugins.xiaoqing_chat.handler_context import HandlerContext
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.smalltalk_models import PreparedSmalltalkTurn

mock_context = _fixture_support.mock_context


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
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
        "message_id": 99,
        "sender": {"user_id": 12345, "card": "主人"},
        "message": [
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


def _hctx(tmp_path, agent) -> tuple[HandlerContext, SimpleNamespace]:
    history = [
        StoredMessage(role="user", name="甲", ts=1.0, user_id=1, message_id=97, content="早"),
        StoredMessage(role="assistant", name="小青", ts=2.0, content="早呀"),
        StoredMessage(
            role="user", name="主人", ts=3.0, user_id=12345, message_id=99, content="当前"
        ),
    ]
    state = SimpleNamespace(
        memory_store=SimpleNamespace(
            get_recent_async=AsyncMock(return_value=history),
            append=Mock(),
        ),
        action_history=SimpleNamespace(append=Mock()),
        set_last_reply_ts=Mock(),
    )
    context = SimpleNamespace(
        capabilities=PluginCapabilities(is_bot_admin=True, agent_turn=agent),
        logger   = MagicMock(),
        data_dir = tmp_path,
    )
    runtime = ChatRuntime(XiaoQingChatConfig(), [])
    return HandlerContext("g67890", runtime, state, {}, tmp_path, "小青", context), context


@pytest.fixture
def quiet_side_effects():
    fakes = {
        "ensure_user_message_recorded": AsyncMock(return_value="m7"),
        "schedule_memory_persist": Mock(),
        "schedule_action_history_flush": Mock(),
        "log_step": Mock(),
        "allocate_local_id": Mock(return_value="m8"),
    }
    with patch.multiple(agent_bridge, **fakes):
        yield fakes


@pytest.mark.asyncio
async def test_handled_turn_returns_agent_reply_and_records_both_sides(
    tmp_path, quiet_side_effects
):
    agent = SimpleNamespace(
        handle_turn=AsyncMock(
            return_value={
                "handled": True,
                "segments": [{"type": "text", "data": {"text": "画好了"}}],
                "reply_text": "画好了",
                "task_id": "a1b2c3",
            }
        )
    )
    hctx, context = _hctx(tmp_path, agent)

    result = await _delegate(_prepared(), _event(), context, hctx)

    assert result == [{"type": "text", "data": {"text": "画好了"}}]
    assert isinstance(result, DeliverySegments)
    turn = agent.handle_turn.await_args.args[0]
    assert turn["text"] == "小青，帮我画个 sin 图"
    assert (turn["user_id"], turn["group_id"], turn["message_id"]) == (12345, 67890, 99)
    assert turn["sender_name"] == "主人" and turn["bot_name"] == "小青"
    assert turn["images"] == ["https://img.example/a.png"]
    assert turn["history"] == [
        {"role": "user", "name": "甲", "user_id": 1, "text": "早"},
        {"role": "assistant", "name": "小青", "user_id": None, "text": "早呀"},
    ]
    # 人设只在聊天插件里配置，随每轮交给 agent，agent 的回复和平时聊天是同一个人。
    # agent 拿到人物事实和〔办事〕各节，闲聊示例留在聊天这边。
    card = hctx.runtime.cfg.personality.card
    assert turn["persona"] == card.for_agent("小青")
    assert turn["persona"].startswith("你的名字是小青。")
    assert "记好了，打车 32" in turn["persona"]
    assert "又喝，明早有你受的" not in turn["persona"]

    quiet_side_effects["ensure_user_message_recorded"].assert_awaited_once()
    append = hctx.state.memory_store.append
    # 回复确认发出后才记成小青的发言。
    append.assert_not_called()
    await result.delivery_receipt.record(True)
    append.assert_called_once()
    assert append.call_args.kwargs["role"] == "assistant"
    assert append.call_args.kwargs["source_plugin"] == "xiaoqing_agent"
    record = hctx.state.action_history.append.call_args.args[1]
    assert record.action == "agent_task" and record.detail == {"task_id": "a1b2c3"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delivered", "recorded"),
    [(True, True), (False, False), (None, True)],
)
async def test_reply_receipt_reports_the_delivery_back_to_the_agent(
    tmp_path, quiet_side_effects, delivered, recorded
):
    agent = SimpleNamespace(
        handle_turn=AsyncMock(
            return_value={
                "handled": True,
                "segments": [{"type": "text", "data": {"text": "画好了"}}],
                "reply_text": "画好了",
                "task_id": "a1b2c3",
            }
        ),
        report_delivery=AsyncMock(side_effect=RuntimeError("agent unloaded")),
    )
    hctx, context = _hctx(tmp_path, agent)
    result = await _delegate(_prepared(), _event(), context, hctx)

    # 回报失败（比如 agent 刚被卸载）不影响聊天侧的收尾。
    await result.delivery_receipt.record(delivered)
    agent.report_delivery.assert_awaited_once_with("a1b2c3", delivered)
    assert hctx.state.memory_store.append.called is recorded


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
async def test_any_non_handled_outcome_falls_back_to_chat(tmp_path, quiet_side_effects, outcome):
    if isinstance(outcome, Exception):
        agent = SimpleNamespace(handle_turn=AsyncMock(side_effect=outcome))
    else:
        agent = SimpleNamespace(handle_turn=AsyncMock(return_value=outcome))
    hctx, context = _hctx(tmp_path, agent)

    assert await _delegate(_prepared(), _event(), context, hctx) is None
    quiet_side_effects["ensure_user_message_recorded"].assert_not_awaited()
    hctx.state.memory_store.append.assert_not_called()


@pytest.mark.asyncio
async def test_wait_boundary_delivers_once_and_followup_uses_the_finished_task(
    tmp_path,
    quiet_side_effects,
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
    hctx, context = _hctx(tmp_path / "chat", agent)
    chat = AsyncMock(return_value=[{"type": "text", "data": {"text": "普通聊天回复"}}])
    prepared = _prepared(text="帮我记一下，订阅费98，支付宝")
    reply = await agent_bridge.answer_with_agent_or_chat(prepared, _event(), context, hctx, chat)
    assert reply == [] and sent_texts(agent_context) == []
    assert chat.await_count <= 1
    quiet_side_effects["ensure_user_message_recorded"].assert_awaited_once()
    hctx.state.memory_store.append.assert_not_called()

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
    reply = await _delegate(_prepared(text="刚才那笔改成微信"), _event(), context, hctx)
    assert isinstance(reply, DeliverySegments)
    assert reply[0]["data"]["text"] == "改好了，走的微信"
    assert "记好了，98，走的支付宝" in agent_context.ai.calls[-1][1][1]["content"]
    await reply.delivery_receipt.record(True)
    hctx.state.memory_store.append.assert_called_once()
    await agent_main.shutdown(agent_context)


@pytest.mark.asyncio
async def test_agent_is_not_consulted_without_capability_or_explicit_address(tmp_path):
    agent = SimpleNamespace(handle_turn=AsyncMock())
    hctx, context = _hctx(tmp_path, agent)
    assert await _delegate(_prepared(forced=False), _event(), context, hctx) is None
    agent.handle_turn.assert_not_awaited()

    hctx, context = _hctx(tmp_path, None)
    assert await _delegate(_prepared(), _event(), context, hctx) is None


@pytest.mark.asyncio
async def test_private_turns_reach_the_agent_even_without_a_forced_summon(
    tmp_path, quiet_side_effects
):
    agent = SimpleNamespace(handle_turn=AsyncMock(return_value=None))
    hctx, context = _hctx(tmp_path, agent)
    prepared = _prepared(forced=False, is_private=True, text="帮我算一下 3 的 50 次方")
    assert await _delegate(prepared, _event(), context, hctx) is None
    agent.handle_turn.assert_awaited_once()
    assert agent.handle_turn.await_args.args[0]["is_private"] is True


@pytest.mark.asyncio
async def test_stuck_agent_is_abandoned_after_the_bridge_timeout(tmp_path, quiet_side_effects):
    async def never(_turn):
        await asyncio.Event().wait()

    hctx, context = _hctx(tmp_path, SimpleNamespace(handle_turn=never))
    # 判断阶段和执行阶段各有自己的兜底时限。
    with patch.object(agent_bridge, "_AGENT_DECIDE_TIMEOUT_SECONDS", 0.05):
        assert await _delegate(_prepared(), _event(), context, hctx) is None

    calls: list[dict] = []

    async def decides_then_hangs(turn):
        calls.append(turn)
        if turn.get("phase") == "decide":
            return {"handled": True, "decision": {"action": "task", "goal": "g"}}
        await asyncio.Event().wait()

    hctx, context = _hctx(tmp_path, SimpleNamespace(handle_turn=decides_then_hangs))
    with patch.object(agent_bridge, "_AGENT_TURN_TIMEOUT_SECONDS", 0.05):
        assert await _delegate(_prepared(), _event(), context, hctx) is None
    assert [turn["phase"] for turn in calls] == ["decide", "act"]
    assert calls[1]["decision"] == {"action": "task", "goal": "g"}


def _slot_spy() -> tuple[SimpleNamespace, list[str]]:
    """记录聊天生成名额的占用与释放。"""

    events: list[str] = []

    class _Admit:
        async def __aenter__(self):
            events.append("enter")

        async def __aexit__(self, *_exc):
            events.append("exit")
            return False

    return SimpleNamespace(admit=lambda **_kwargs: _Admit()), events


def _handler_ctx(limiter: SimpleNamespace) -> SimpleNamespace:
    state = SimpleNamespace(generation_limiter=limiter)
    return SimpleNamespace(runtime=ChatRuntime(XiaoQingChatConfig(), []), state=state, chat_id="g1")


def _agent_context() -> MagicMock:
    context = MagicMock()
    context.capabilities = PluginCapabilities(agent_turn=SimpleNamespace())
    return context


@pytest.mark.asyncio
async def test_agent_taking_over_cancels_the_chat_reply_generated_alongside(tmp_path):
    limiter, slots = _slot_spy()
    started = asyncio.Event()
    reply   = [{"type": "text", "data": {"text": "交给 agent 了"}}]
    plan    = object()

    async def generate(*_args):
        started.set()
        await asyncio.Event().wait()

    async def decide(*_args):
        await started.wait()
        return plan

    act = AsyncMock(return_value=reply)
    with (
        patch.object(handlers, "_prepare_smalltalk_turn", AsyncMock(return_value=_prepared())),
        patch.object(agent_bridge, "_decide", decide),
        patch.object(agent_bridge, "_act", act),
        patch.object(handlers, "generate_smalltalk_turn", generate),
        patch.object(handlers, "finalize_smalltalk_turn", AsyncMock()) as finalize,
    ):
        result = await handlers._maybe_reply_smalltalk(
            "画图", _event(), _agent_context(), hctx=_handler_ctx(limiter)
        )

    assert result == reply
    # 生成和判断同时开始；接手后生成被取消、名额立即让出，聊天回复没有发出。
    assert slots == ["enter", "exit"]
    finalize.assert_not_awaited()
    assert act.await_args.args[0] is plan


@pytest.mark.asyncio
async def test_declined_turn_uses_the_chat_reply_already_being_generated(tmp_path):
    limiter, slots = _slot_spy()
    normal           = [{"type": "text", "data": {"text": "普通回复"}}]
    generating       = asyncio.Event()
    order: list[str] = []

    async def generate(*_args):
        order.append("generate")
        generating.set()
        return "generated"

    async def decide(*_args):
        # 判断还没出来时聊天回复已经在生成：闲聊不用先等判断。
        await generating.wait()
        order.append("declined")
        return None

    act = AsyncMock()
    with (
        patch.object(handlers, "_prepare_smalltalk_turn", AsyncMock(return_value=_prepared())),
        patch.object(agent_bridge, "_decide", decide),
        patch.object(agent_bridge, "_act", act),
        patch.object(handlers, "generate_smalltalk_turn", generate),
        patch.object(
            handlers, "finalize_smalltalk_turn", AsyncMock(return_value=normal)
        ) as finalize,
    ):
        result = await handlers._maybe_reply_smalltalk(
            "在吗", _event(), _agent_context(), hctx=_handler_ctx(limiter)
        )

    assert result == normal
    assert order == ["generate", "declined"]
    assert slots == ["enter", "exit"]
    assert finalize.await_args.args[1] == "generated"
    act.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_reply_finished_first_waits_for_the_decision_and_is_dropped(tmp_path):
    limiter, _slots = _slot_spy()
    generated = MagicMock()
    finished  = asyncio.Event()
    reply     = [{"type": "text", "data": {"text": "agent 的结果"}}]

    async def generate(*_args):
        finished.set()
        return generated

    async def decide(*_args):
        await finished.wait()
        await asyncio.sleep(0)
        return object()

    with (
        patch.object(handlers, "_prepare_smalltalk_turn", AsyncMock(return_value=_prepared())),
        patch.object(agent_bridge, "_decide", decide),
        patch.object(agent_bridge, "_act", AsyncMock(return_value=reply)),
        patch.object(handlers, "generate_smalltalk_turn", generate),
        patch.object(handlers, "finalize_smalltalk_turn", AsyncMock()) as finalize,
    ):
        result = await handlers._maybe_reply_smalltalk(
            "画图", _event(), _agent_context(), hctx=_handler_ctx(limiter)
        )

    assert result == reply
    finalize.assert_not_awaited()


@pytest.mark.asyncio
async def test_turns_the_agent_cannot_take_skip_the_decision(tmp_path):
    limiter, slots = _slot_spy()
    normal = [{"type": "text", "data": {"text": "普通回复"}}]
    decide = AsyncMock()
    with (
        patch.object(
            handlers, "_prepare_smalltalk_turn", AsyncMock(return_value=_prepared(forced=False))
        ),
        patch.object(agent_bridge, "_decide", decide),
        patch.object(handlers, "generate_smalltalk_turn", AsyncMock(return_value="generated")),
        patch.object(handlers, "finalize_smalltalk_turn", AsyncMock(return_value=normal)),
    ):
        result = await handlers._maybe_reply_smalltalk(
            "在吗", _event(), _agent_context(), hctx=_handler_ctx(limiter)
        )

    assert result == normal and slots == ["enter", "exit"]
    decide.assert_not_awaited()


@pytest.mark.asyncio
async def test_quoted_message_and_downloaded_images_reach_the_agent(tmp_path, quiet_side_effects):
    from dataclasses import replace

    from plugins.xiaoqing_chat.media.models import RenderedMedia

    agent = SimpleNamespace(handle_turn=AsyncMock(return_value=None))
    hctx, context = _hctx(tmp_path, agent)
    media = SimpleNamespace(
        get_message=AsyncMock(
            return_value={
                "sender": {"nickname": "甲"},
                "message": [
                    {"type": "text", "data": {"text": "这是上个月的数据表"}},
                    {"type": "image", "data": {"url": "https://img.example/quoted.png"}},
                ],
            }
        )
    )
    context.capabilities = replace(context.capabilities, onebot_media=media)
    cached = tmp_path / "cached.png"
    cached.write_bytes(b"png")
    event = _event()
    event["message"].insert(0, {"type": "reply", "data": {"id": "555"}})
    event["_xc_rendered_media_items"] = [
        RenderedMedia("h1", "image", "图", (), "[图片]", cached_path=cached),
        RenderedMedia("h2", "image", "图", (), "[图片]", cached_path=tmp_path / "gone.png"),
    ]

    await _delegate(_prepared(), event, context, hctx)

    media.get_message.assert_awaited_once_with("555")
    turn = agent.handle_turn.await_args.args[0]
    assert turn["quoted_text"] == "甲: 这是上个月的数据表"
    assert turn["images"] == ["https://img.example/a.png", "https://img.example/quoted.png"]
    assert turn["image_files"] == [str(cached)]


@pytest.mark.asyncio
async def test_quote_lookup_failure_does_not_block_the_turn(tmp_path, quiet_side_effects):
    from dataclasses import replace

    agent = SimpleNamespace(handle_turn=AsyncMock(return_value=None))
    hctx, context = _hctx(tmp_path, agent)
    media = SimpleNamespace(get_message=AsyncMock(side_effect=RuntimeError("onebot down")))
    context.capabilities = replace(context.capabilities, onebot_media=media)
    event = _event()
    event["message"].insert(0, {"type": "reply", "data": {"id": "555"}})

    await _delegate(_prepared(), event, context, hctx)

    turn = agent.handle_turn.await_args.args[0]
    assert turn["quoted_text"] == ""
    assert turn["images"] == ["https://img.example/a.png"]


@pytest.mark.asyncio
async def test_command_runner_events_never_reach_the_agent(tmp_path, quiet_side_effects):
    from core.interfaces import COMMAND_RUNNER_EVENT_KEY

    agent = SimpleNamespace(handle_turn=AsyncMock())
    hctx, context = _hctx(tmp_path, agent)
    event = {**_event(), COMMAND_RUNNER_EVENT_KEY: "xiaoqing_agent"}
    assert await _delegate(_prepared(), event, context, hctx) is None
    agent.handle_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_xc_free_chat_refuses_tool_calls_but_subcommands_still_work(mock_context):
    from core.interfaces import COMMAND_RUNNER_EVENT_KEY
    from plugins.xiaoqing_chat import main as chat_main

    event = {"user_id": 12345, "group_id": None, COMMAND_RUNNER_EVENT_KEY: "xiaoqing_agent"}
    with patch.object(chat_main, "handle_smalltalk", AsyncMock()) as smalltalk:
        reply = await chat_main.handle("xc", "帮我画个图", event, mock_context)
    smalltalk.assert_not_awaited()
    assert "不能作为工具调用" in reply[0]["data"]["text"]


@pytest.mark.asyncio
async def test_quoting_the_agent_reply_in_a_group_is_recognized_as_talking_to_the_bot(tmp_path):
    """回执提交时，这条回复拆成几条发出的全部消息 ID 和记录一起写入，引用哪一条都认得出。"""

    import asyncio as _asyncio

    from core.interfaces import ACTION_RESULT_MESSAGE_ID_KEY
    from core.plugin_base import build_action
    from plugins.xiaoqing_chat.attention_gate import is_reply_to_bot
    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    store      = MemoryStore(tmp_path)
    long_reply = "图画好了，" + "细节说明" * 400
    agent      = SimpleNamespace(
        handle_turn=AsyncMock(
            return_value={
                "handled": True,
                "segments": [{"type": "text", "data": {"text": long_reply}}],
                "reply_text": long_reply,
                "task_id": "a1b2c3",
            }
        ),
        report_delivery=AsyncMock(),
    )
    hctx, context = _hctx(tmp_path, agent)
    hctx.state.memory_store = store
    store.append("g67890", role="assistant", name="小青", content="更早的闲聊", ts=1.0)
    with patch.multiple(
        agent_bridge,
        ensure_user_message_recorded=AsyncMock(return_value="m7"),
        schedule_memory_persist       = Mock(),
        schedule_action_history_flush = Mock(),
        log_step                      = Mock(),
    ):
        reply = await _delegate(_prepared(), _event(), context, hctx)
        # 像 Core 一样：长回复拆成两条，每条发出后先登记消息 ID 再记下发送结果。
        receipt = reply.delivery_receipt
        receipt.add_expected_actions(1)
        for message_id in (5555, 5556):
            receipt.add_message_id(message_id)
            await receipt.record(True)
    agent.report_delivery.assert_awaited_once_with("a1b2c3", True)

    history = store.get("g67890")
    assert history[-1].remote_ids() == (5555, 5556)
    assert history[0].remote_ids() == ()

    # 出站观察者不再按“最近一条”回填聊天自己的回复，也不会重复记一条。
    action = build_action(list(reply), 12345, 67890)
    assert action is not None
    action[ACTION_RESULT_MESSAGE_ID_KEY] = 5556
    runtime                              = SimpleNamespace(
        cfg=SimpleNamespace(enable_smalltalk=True, ban_words=[]), compiled_ban_regex=[]
    )
    _fixture_support._complete_test_runtime_config(runtime)
    with (
        patch.object(handlers, "load_runtime", return_value=runtime),
        patch.object(handlers, "bound_state", return_value=SimpleNamespace(memory_store=store)),
        patch.object(handlers, "chat_lock", return_value=_asyncio.Lock()),
        patch.object(handlers, "schedule_memory_persist"),
    ):
        await handlers.observe_outgoing_action(action, context, source_plugin="xiaoqing_chat")
    assert len(store.get("g67890")) == 2

    for quoted in ("5555", "5556"):
        quote = {
            "message": [
                {"type": "reply", "data": {"id": quoted}},
                {"type": "text", "data": {"text": "改成红色"}},
            ]
        }
        assert await is_reply_to_bot(
            event=quote, state=SimpleNamespace(memory_store=store), chat_id="g67890"
        ), quoted


@pytest.mark.asyncio
async def test_concurrent_replies_keep_their_own_message_ids(tmp_path):
    """两条回复几乎同时发出、回执先后顺序打乱，各自的记录仍对应各自的消息。"""

    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    store   = MemoryStore(tmp_path)
    replies = []
    for text_value, task_id in (("回复 A", "aaaaaa"), ("回复 B", "bbbbbb")):
        agent = SimpleNamespace(
            handle_turn=AsyncMock(
                return_value={
                    "handled": True,
                    "segments": [{"type": "text", "data": {"text": text_value}}],
                    "reply_text": text_value,
                    "task_id": task_id,
                }
            ),
            report_delivery=AsyncMock(),
        )
        hctx, context = _hctx(tmp_path, agent)
        hctx.state.memory_store = store
        with patch.multiple(
            agent_bridge,
            ensure_user_message_recorded=AsyncMock(return_value="m7"),
            schedule_memory_persist       = Mock(),
            schedule_action_history_flush = Mock(),
            log_step                      = Mock(),
        ):
            replies.append(await _delegate(_prepared(), _event(), context, hctx))

    first, second = (reply.delivery_receipt for reply in replies)
    first.add_message_id(501)
    second.add_message_id(502)
    with patch.multiple(
        agent_bridge,
        schedule_memory_persist = Mock(),
        log_step                = Mock(),
    ):
        await second.record(True)
        await first.record(True)

    by_text = {message.content: message.remote_ids() for message in store.get("g67890")}
    assert by_text == {"回复 A": (501,), "回复 B": (502,)}
