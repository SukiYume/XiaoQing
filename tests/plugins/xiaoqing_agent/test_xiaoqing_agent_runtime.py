"""任务运行时：当轮交付、后台主动发送、停止、并发上限、关闭与重启收尾。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.delivery import CommandOutcome, DeliveryReceipt, DeliverySegments
from core.interfaces import ACTION_BYPASS_SINK_KEY
from core.router import build_command_catalog_node
from plugins.xiaoqing_agent.config import AgentConfig
from plugins.xiaoqing_agent.runtime import AgentRuntime
from plugins.xiaoqing_agent.tasks import Actor, AgentTask, ChatRef, Outcome, TaskStore
from tests.helpers.xiaoqing_agent_test_support import (
    ADMIN_ID,
    GROUP_ID,
    MEMBER_ID,
    FakeAgentContext,
    FakeAI,
    ai_response,
    sent_texts,
    tool_call,
    wait_until,
)

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
    b"\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
CONFIG = AgentConfig(enabled=True)
CHAT = ChatRef(ADMIN_ID, None)
ADMIN = Actor(ADMIN_ID, admin=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["needs_user", "blocked", "unfinished"])
async def test_explicit_final_status_is_preserved_without_rewriting(tmp_path, state):
    context = FakeAgentContext(
        tmp_path,
        ai=FakeAI(
            [
                ai_response(
                    "", [tool_call("final_answer", {"text": "当前完成情况。", "state": state})]
                ),
            ]
        ),
    )
    task = AgentRuntime(tmp_path).start(
        context, CONFIG, CHAT, request_text="查看进展", source="chat"
    )
    await task.runner
    assert task.status == state and len(context.ai.calls) == 1


@pytest.mark.asyncio
async def test_selected_services_still_use_current_permission_and_catalog(tmp_path):
    root = build_command_catalog_node(
        "inventory",
        {
            "name"      : "stock",
            "triggers"  : ["stock"],
            "help"      : "库存",
            "permission": "bot_admin",
        },
        root=True,
    )
    context = FakeAgentContext(tmp_path, catalog=(root,), is_admin=False)
    runtime = AgentRuntime(tmp_path)
    task = AgentTask("selected", CHAT, "查询库存", "chat", plugins=("inventory", "unknown"))
    env, _tools, messages, _session = await runtime._prepare(context, CONFIG, task, [], [])
    assert env.selected_plugins == set()
    assert "本轮所用服务" not in messages[1]["content"]


def _reservation_root():
    return build_command_catalog_node(
        "reservation",
        {
            "name"    : "reserve",
            "triggers": ["reserve"],
            "help"    : "登记预约",
            "usage"   : "/reserve <JSON>",
            "effect"  : "write",
        },
        root=True,
    )


@pytest.mark.asyncio
async def test_a_confirmation_continues_the_same_session_and_runs_the_plugin_command(tmp_path):
    """冲突后回一句“添加”：接在同一份对话后面，模型看到插件给的确认命令，原样执行后回复。"""

    confirm = '/reserve {"title":"甲","force":true}'
    runner  = SimpleNamespace(
        run=AsyncMock(
            side_effect=[
                DeliverySegments(
                    [{"type": "text", "data": {"text": "预约冲突，输入 yes 确认"}}],
                    outcome=CommandOutcome("incomplete", confirmation_command=confirm),
                ),
                DeliverySegments(
                    [{"type": "text", "data": {"text": "已创建预约，编号 aabb1122"}}],
                    outcome=CommandOutcome("completed"),
                ),
            ]
        )
    )

    def first_call(_route, messages, _kwargs):
        assert "本轮所用服务的命令说明" in messages[-1]["content"]
        return ai_response(
            "", [tool_call("xiaoqing_command", {"commands": ['/reserve {"title":"甲"}']})]
        )

    def run_confirmed(_route, messages, _kwargs):
        # 前一轮的冲突回报和确认命令都在上下文里；服务说明不再重复给。
        assert confirm in str(messages)
        assert messages[-1]["role"] == "user" and messages[-1]["content"].endswith("本轮原话：添加")
        assert "本轮所用服务的命令说明" not in messages[-1]["content"]
        return ai_response("", [tool_call("xiaoqing_command", {"commands": [confirm]})])

    context = FakeAgentContext(
        tmp_path,
        catalog        = (_reservation_root(),),
        command_runner = runner,
        ai             = FakeAI(
            [
                first_call,
                ai_response("甲和已有预约冲突了，还要约吗？"),
                run_confirmed,
                ai_response("约好了，编号 aabb1122。"),
            ]
        ),
    )
    runtime = AgentRuntime(tmp_path)
    first   = runtime.start(
        context, CONFIG, CHAT, request_text="创建甲预约", source="chat", plugins=("reservation",)
    )
    await first.runner
    assert first.status == "needs_user" and first.outcome.text == "甲和已有预约冲突了，还要约吗？"
    second = runtime.start(context, CONFIG, CHAT, request_text="添加", source="chat")
    await second.runner
    assert second.status == "done" and second.outcome.text == "约好了，编号 aabb1122。"
    assert second.continues == first.task_id
    assert runner.run.call_args.args == (confirm,) and len(context.ai.calls) == 4
    # 同一会话的系统提示不变，前面的消息原样作为前缀，模型的提示缓存能命中。
    first_request, second_request = context.ai.calls[0][1], context.ai.calls[2][1]
    assert second_request[: len(first_request)] == first_request


@pytest.mark.asyncio
async def test_recent_self_rides_each_turn_and_the_card_keeps_the_session(tmp_path):
    context = FakeAgentContext(tmp_path, ai=FakeAI([_final("好"), _final("改好了")]))
    runtime = AgentRuntime(tmp_path)
    for text, recent in (("建个日程", "你最近说过：在图书馆"), ("改一下", "你最近说过：回宿舍了")):
        task = runtime.start(
            context,
            CONFIG,
            CHAT,
            request_text = text,
            source       = "chat",
            persona      = "人设卡",
            recent_self  = recent,
        )
        await task.runner
    first, second = context.ai.calls[0][1], context.ai.calls[1][1]
    # 同一张人设卡接着同一会话；近况写在每轮请求里，系统提示不变。
    assert second[: len(first)] == first and "人设卡" in first[0]["content"]
    assert "在图书馆" in first[-1]["content"] and "回宿舍了" in second[-1]["content"]
    assert "回宿舍了" not in second[0]["content"]


@pytest.mark.asyncio
async def test_plugins_check_fields_against_everything_the_initiator_said_in_the_session(tmp_path):
    """回答澄清（“描述”）时金额和分类在前一句里：会话里发起人说过的话都交给插件核对来源。"""

    runner = SimpleNamespace(
        run=AsyncMock(return_value=[{"type": "text", "data": {"text": "已记"}}])
    )
    context = FakeAgentContext(
        tmp_path,
        command_runner = runner,
        ai             = FakeAI(
            [
                ai_response("“很久以前”是描述还是日期？"),
                _command("/ledger add 159.7 很久以前 cat:餐饮"),
                ai_response("记好了"),
            ]
        ),
    )
    runtime = AgentRuntime(tmp_path)
    for text in ("记账 159.7 很久以前 餐饮", "描述"):
        task = runtime.start(context, CONFIG, CHAT, request_text=text, source="chat", admin=True)
        await task.runner
    assert runner.run.await_args.kwargs["request_texts"] == ("记账 159.7 很久以前 餐饮", "描述")


@pytest.mark.asyncio
async def test_trimmed_command_help_is_loaded_again_when_the_service_is_selected(tmp_path):
    from dataclasses import replace

    context = FakeAgentContext(
        tmp_path,
        catalog = (_reservation_root(),),
        ai      = FakeAI([ai_response("请补充时间") for _ in range(3)]),
    )
    runtime = AgentRuntime(tmp_path)
    for request in ("预约甲", "时间还没定", "定在周五下午"):
        task = runtime.start(
            context,
            replace(CONFIG, session_max_turns=1),
            CHAT,
            request_text = request,
            source       = "chat",
            plugins      = ("reservation",),
        )
        await task.runner
    prompts = [call[1][-1]["content"] for call in context.ai.calls]
    assert "本轮所用服务的命令说明" in prompts[0]
    assert "本轮所用服务的命令说明" not in prompts[1]
    assert "本轮所用服务的命令说明" in prompts[2]


@pytest.mark.asyncio
async def test_inflight_updates_remain_available_to_plugins_in_the_next_turn(tmp_path):
    runtime = AgentRuntime(tmp_path)

    def add_note(_route, _messages, _kwargs):
        task = next(iter(runtime.running.values()))
        task.notes.append("数量改成2")
        return ai_response("收到")

    runner = SimpleNamespace(run=AsyncMock(return_value=[]))
    context = FakeAgentContext(
        tmp_path,
        command_runner = runner,
        ai             = FakeAI(
            [add_note, ai_response("按2份准备"), _command("/reserve 2"), ai_response("好了")]
        ),
    )
    for request in ("准备一份", "就这样"):
        task = runtime.start(context, CONFIG, CHAT, request_text=request, source="chat")
        await task.runner
    requests = runner.run.await_args.kwargs["request_texts"]
    assert requests[0] == "准备一份" and requests[-1] == "就这样"
    assert any("数量改成2" in text for text in requests)


@pytest.mark.asyncio
async def test_sessions_belong_to_one_initiator_and_expire_or_reset(tmp_path):
    from dataclasses import replace

    context = FakeAgentContext(tmp_path, ai=FakeAI([]))
    runtime = AgentRuntime(tmp_path)
    # 不附最近任务记录，只看会话本身接没接上。
    config = replace(CONFIG, history_tasks=0)
    alice = ChatRef(ADMIN_ID, GROUP_ID)
    bob   = ChatRef(MEMBER_ID, GROUP_ID)

    async def run(chat, text, config=config):
        context.ai.script.append(ai_response(f"回复：{text}"))
        task = runtime.start(context, config, chat, request_text=text, source="chat")
        await task.runner
        return context.ai.calls[-1][1]

    await run(alice, "第一件事")
    assert "第一件事" in str(await run(alice, "接着说"))
    # 同一个群里别人说话是另一份会话。
    assert "第一件事" not in str(await run(bob, "我的事"))
    # 隔太久没说话就开新会话；关掉会话时每句话都从头开始。
    assert "第一件事" not in str(
        await run(alice, "过了很久", replace(config, session_idle_minutes=0))
    )
    await runtime.sessions.clear(alice)
    assert "接着说" not in str(await run(alice, "从头来"))


@pytest.mark.asyncio
async def test_plain_text_reply_is_delivered_without_another_model_call(tmp_path):
    context = FakeAgentContext(tmp_path, ai=FakeAI([ai_response("请补充另外两笔明细。")]))
    runtime = AgentRuntime(tmp_path)
    task = runtime.start(context, CONFIG, CHAT, request_text="记三笔账，只给了一笔", source="chat")
    await task.runner
    assert task.outcome.text == "请补充另外两笔明细。" and len(context.ai.calls) == 1


@pytest.mark.asyncio
async def test_done_claim_over_a_pending_confirmation_is_recorded_as_needs_user(tmp_path):
    """说已完成、但写入还在等发起人决定：记成 needs_user；插件给的确认命令写在回报里。"""

    runner = SimpleNamespace(
        run=AsyncMock(
            return_value=DeliverySegments(
                [{"type": "text", "data": {"text": "时间冲突，回复确认"}}],
                outcome=CommandOutcome("incomplete", confirmation_command="/reserve force 甲"),
            )
        )
    )
    context = FakeAgentContext(
        tmp_path,
        catalog        = (_reservation_root(),),
        command_runner = runner,
        ai             = FakeAI(
            [
                _command("/reserve 甲"),
                ai_response(
                    "",
                    [
                        tool_call(
                            "final_answer", {"text": "甲和别的冲突了，还加吗？", "state": "done"}
                        )
                    ],
                ),
            ]
        ),
    )
    runtime = AgentRuntime(tmp_path)
    task    = runtime.start(
        context, CONFIG, CHAT, request_text="预约甲", source="chat", plugins=("reservation",)
    )
    await task.runner
    assert task.status == "needs_user"
    assert "/reserve force 甲" in context.ai.calls[-1][1][-1]["content"]


def _final(text: str, files: list[str] | None = None) -> dict:
    return ai_response("", [tool_call("final_answer", {"text": text, "files": files or []})])


def _command(command: str) -> dict:
    return ai_response("", [tool_call("xiaoqing_command", {"commands": [command]})])


def _gate(release: asyncio.Event, response: dict):
    async def wait(_route, _messages, _kwargs):
        await release.wait()
        return response

    return wait


def _records(data_dir: Path, chat: ChatRef = CHAT) -> list[dict]:
    path = data_dir / "tasks" / f"{chat.key}.json"
    return json.loads(path.read_text(encoding="utf-8"))["tasks"]


@pytest.mark.asyncio
async def test_fast_task_is_handed_to_the_waiting_turn(tmp_path: Path) -> None:
    context = FakeAgentContext(
        tmp_path, ai=FakeAI([_final("2 的 100 次方是 1267650600228229401496703205376")])
    )
    runtime = AgentRuntime(tmp_path)

    task = runtime.start(context, CONFIG, CHAT, request_text="算 2^100", source="chat", admin=True)
    assert not isinstance(task, str)
    outcome = await runtime.wait_inline(context, task, 5)

    assert outcome is not None and outcome.text.startswith("2 的 100 次方")
    await wait_until(lambda: task.runner.done())
    assert context.actions == []
    assert runtime.running == {}
    record = _records(tmp_path)[-1]
    assert record["status"] == "done" and record["request_text"] == "算 2^100"
    transcript = json.loads((tmp_path / "transcripts" / f"{task.task_id}.json").read_text("utf-8"))
    assert transcript["messages"][0]["role"] == "system"
    assert "算 2^100" in transcript["messages"][1]["content"]


@pytest.mark.asyncio
async def test_slow_task_acknowledges_then_delivers_text_images_and_files(tmp_path: Path) -> None:
    release = asyncio.Event()
    context = FakeAgentContext(tmp_path, group_id=GROUP_ID)
    chat = ChatRef(ADMIN_ID, GROUP_ID)
    out  = tmp_path / "workspaces" / chat.key
    out.mkdir(parents=True)
    (out / "plot.png").write_bytes(PNG)
    (out / "table.csv").write_text("a,b\n", encoding="utf-8")
    context.ai.script = [_gate(release, _final("图和表都好了", ["plot.png", "table.csv"]))]
    runtime           = AgentRuntime(tmp_path)

    task = runtime.start(context, CONFIG, chat, request_text="画图", source="chat", admin=True)
    assert await runtime.wait_inline(context, task, 0.05) is None
    assert task.inline_waiter is None

    release.set()
    await wait_until(lambda: task.runner.done() and len(context.actions) == 2)
    message_action, upload_action = context.actions
    assert message_action["action"] == "send_group_msg"
    assert message_action[ACTION_BYPASS_SINK_KEY] is True
    segments = message_action["params"]["message"]
    assert segments[0] == {"type": "at", "data": {"qq": str(ADMIN_ID)}}
    assert segments[2] == {"type": "text", "data": {"text": "图和表都好了"}}
    assert segments[3]["type"] == "image" and segments[3]["data"]["file"].endswith("plot.png")
    assert upload_action["action"] == "upload_group_file"
    assert upload_action["params"] == {
        "group_id": GROUP_ID,
        "file"    : str((out / "table.csv").resolve()),
        "name"    : "table.csv",
    }


@pytest.mark.asyncio
async def test_inline_reply_uploads_files_after_the_reply_text(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("plugins.xiaoqing_agent.runtime._UPLOAD_AFTER_REPLY_SECONDS", 0.01)
    work = tmp_path / "workspaces" / CHAT.key
    work.mkdir(parents=True)
    (work / "table.csv").write_text("a,b", encoding="utf-8")
    (work / "plot.png").write_bytes(PNG)
    context = FakeAgentContext(tmp_path, ai=FakeAI([_final("表在这", ["table.csv", "plot.png"])]))
    runtime = AgentRuntime(tmp_path)
    task = runtime.start(context, CONFIG, CHAT, request_text="g", source="chat", admin=True)
    outcome = await runtime.wait_inline(context, task, 5)
    reply   = await runtime.inline_reply(context, task, outcome)

    assert [segment["type"] for segment in reply] == ["text", "image"]
    assert context.actions == []
    await wait_until(lambda: len(context.actions) == 1)
    assert context.actions[0]["action"] == "upload_private_file"
    assert context.actions[0]["params"]["name"] == "table.csv"


@pytest.mark.asyncio
async def test_stop_cancels_without_delivering_and_records_it(tmp_path: Path) -> None:
    release = asyncio.Event()
    context = FakeAgentContext(tmp_path, ai=FakeAI([_gate(release, _final("不该发出"))]))
    runtime = AgentRuntime(tmp_path)
    task = runtime.start(context, CONFIG, CHAT, request_text="长任务", source="chat", admin=True)
    await wait_until(lambda: len(context.ai.calls) == 1)

    stopped = await runtime.stop(CHAT, "", ADMIN)
    assert stopped == [task]
    assert task.status == "cancelled"
    assert context.actions == []
    assert _records(tmp_path)[-1]["status"] == "cancelled"
    assert await runtime.stop(CHAT, "", ADMIN) == []


@pytest.mark.asyncio
async def test_stop_while_a_turn_is_waiting_answers_that_turn(tmp_path: Path) -> None:
    release = asyncio.Event()
    context = FakeAgentContext(tmp_path, ai=FakeAI([_gate(release, _final("x"))]))
    runtime = AgentRuntime(tmp_path)
    task = runtime.start(context, CONFIG, CHAT, request_text="g", source="chat", admin=True)
    waiting = asyncio.create_task(runtime.wait_inline(context, task, 5))
    await wait_until(lambda: task.inline_waiter is not None and len(context.ai.calls) == 1)
    await runtime.stop(CHAT, task.task_id, ADMIN)
    outcome = await waiting
    assert outcome is not None and outcome.status == "cancelled"
    assert context.actions == []


@pytest.mark.asyncio
async def test_result_handed_to_a_cancelled_waiter_is_still_delivered(tmp_path: Path) -> None:
    context           = FakeAgentContext(tmp_path)
    runtime           = AgentRuntime(tmp_path)
    release           = asyncio.Event()
    context.ai.script = [_gate(release, _final("结果"))]
    task = runtime.start(context, CONFIG, CHAT, request_text="g", source="chat", admin=True)
    waiting = asyncio.create_task(runtime.wait_inline(context, task, 5))
    await wait_until(lambda: task.inline_waiter is not None)

    # 模拟结果刚交给等待方、等待方随即被取消的竞态。
    task.inline_waiter.set_result(Outcome("结果"))
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await wait_until(lambda: sent_texts(context) == ["结果"])
    release.set()
    await wait_until(lambda: task.runner.done())


@pytest.mark.asyncio
async def test_running_task_limit_and_lookup(tmp_path: Path) -> None:
    release = asyncio.Event()
    context = FakeAgentContext(
        tmp_path, ai=FakeAI([_gate(release, _final("a")), _gate(release, _final("b"))])
    )
    runtime = AgentRuntime(tmp_path)
    config = AgentConfig(enabled=True, max_admin_tasks=2)
    other = ChatRef(ADMIN_ID, GROUP_ID)
    first = runtime.start(context, config, CHAT, request_text="一", source="chat", admin=True)
    second = runtime.start(context, config, other, request_text="二", source="chat", admin=True)
    refused = runtime.start(context, config, CHAT, request_text="三", source="chat", admin=True)
    assert isinstance(refused, str) and "2 个任务" in refused

    assert runtime.running_in(CHAT) == [first]
    assert runtime.select(CHAT, second.task_id, ADMIN) == [second]
    assert runtime.select(CHAT, "all", ADMIN) == [first]
    assert runtime.select(CHAT, "", ADMIN) == [first]
    assert runtime.select(CHAT, "zzzzzz", ADMIN) == "没有找到运行中的任务 zzzzzz。"
    status = runtime.status_text(CHAT, "", ADMIN)
    assert first.task_id in status and "一" in status and second.task_id not in status
    assert "没有找到" in runtime.status_text(CHAT, "zzzzzz", ADMIN)

    release.set()
    await wait_until(lambda: not runtime.running)
    await wait_until(lambda: len(context.actions) == 2)
    # 群里的结果先 @ 发起人，正文前多一个空格。
    assert sorted(text.strip() for text in sent_texts(context)) == ["a", "b"]


@pytest.mark.asyncio
async def test_status_shows_the_plan(tmp_path: Path) -> None:
    release = asyncio.Event()
    plan    = ai_response(
        "",
        [
            tool_call(
                "update_plan",
                {
                    "steps": [
                        {"step": "抓数据", "status": "done"},
                        {"step": "画图", "status": "in_progress"},
                    ]
                },
            )
        ],
    )
    context = FakeAgentContext(tmp_path, ai=FakeAI([plan, _gate(release, _final("ok"))]))
    runtime = AgentRuntime(tmp_path)
    task = runtime.start(context, CONFIG, CHAT, request_text="画图任务", source="chat", admin=True)
    await wait_until(lambda: task.plan != [] and len(context.ai.calls) == 2)

    status = runtime.status_text(CHAT, "", ADMIN)
    assert "✓ 抓数据" in status and "▶ 画图" in status and "第 2 步" in status
    release.set()
    await wait_until(lambda: task.runner.done())


@pytest.mark.asyncio
async def test_crashing_loop_is_reported_not_raised(tmp_path: Path) -> None:
    context = FakeAgentContext(tmp_path, ai=FakeAI([KeyError("boom")]))
    runtime = AgentRuntime(tmp_path)
    task = runtime.start(context, CONFIG, CHAT, request_text="g", source="chat", admin=True)
    await wait_until(lambda: task.runner.done() and context.actions)
    assert task.status == "failed"
    assert task.error.startswith("KeyError")
    assert "出了点问题" in sent_texts(context)[0]


@pytest.mark.asyncio
async def test_shutdown_interrupts_tasks_and_restart_sweeps_leftovers(tmp_path: Path) -> None:
    release = asyncio.Event()
    context = FakeAgentContext(tmp_path, ai=FakeAI([_gate(release, _final("x"))]))
    runtime = AgentRuntime(tmp_path)
    task = runtime.start(context, CONFIG, CHAT, request_text="g", source="chat", admin=True)
    await wait_until(lambda: len(context.ai.calls) == 1)

    await runtime.shutdown()
    assert task.status == "interrupted"
    assert _records(tmp_path)[-1]["status"] == "interrupted"
    refused = runtime.start(context, CONFIG, CHAT, request_text="g", source="chat", admin=True)
    assert isinstance(refused, str) and "关闭" in refused

    # 模拟进程被直接杀掉：记录停在 running，下次启动时统一记为中断。
    store = TaskStore(tmp_path)
    path  = tmp_path / "tasks" / "u10001.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["tasks"][-1]["status"] = "running"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert await store.mark_interrupted() == 1
    assert _records(tmp_path)[-1]["status"] == "interrupted"
    assert await store.mark_interrupted() == 0


@pytest.mark.asyncio
async def test_a_new_session_starts_from_recent_records(tmp_path: Path) -> None:
    from dataclasses import replace

    context = FakeAgentContext(
        tmp_path,
        ai=FakeAI([_final("画好了 sin.png", ["sin.png"]), _final("改成红色了")]),
    )
    runtime = AgentRuntime(tmp_path)
    work    = tmp_path / "workspaces" / CHAT.key
    work.mkdir(parents=True)
    (work / "sin.png").write_bytes(PNG)

    first = runtime.start(context, CONFIG, CHAT, request_text="画 sin", source="chat", admin=True)
    assert await runtime.wait_inline(context, first, 5) is not None
    await wait_until(lambda: first.runner.done())

    # 会话过期后开新会话：最近的任务记录给出前面做过什么。
    second = runtime.start(
        context,
        replace(CONFIG, session_idle_minutes=0),
        CHAT,
        request_text = "把刚才的图改成红色",
        source       = "chat",
        admin        = True,
    )
    assert await runtime.wait_inline(context, second, 5) is not None
    prompt = context.ai.calls[1][1][-1]["content"]
    assert f"[{first.task_id}] done · 画 sin → 画好了 sin.png" in prompt
    assert "sin.png" in prompt

    history = await runtime.history_text(CHAT)
    assert history.index(second.task_id) < history.index(first.task_id)
    assert "这个会话还没有" in await runtime.history_text(ChatRef(ADMIN_ID, GROUP_ID))


@pytest.mark.asyncio
async def test_failed_delivery_is_recorded_and_the_result_can_be_resent(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("plugins.xiaoqing_agent.runtime._UPLOAD_AFTER_REPLY_SECONDS", 0.01)
    release = asyncio.Event()
    work    = tmp_path / "workspaces" / CHAT.key
    work.mkdir(parents=True)
    (work / "table.csv").write_text("a,b", encoding="utf-8")
    context = FakeAgentContext(
        tmp_path, ai=FakeAI([_gate(release, _final("表做好了", ["table.csv"]))])
    )
    context.send_results = [False, True]
    runtime              = AgentRuntime(tmp_path)

    task = runtime.start(context, CONFIG, CHAT, request_text="做表", source="chat", admin=True)
    release.set()
    await wait_until(lambda: task.runner.done())
    assert task.status == "done" and task.delivery == "failed"
    record = _records(tmp_path)[-1]
    assert record["status"] == "done" and record["delivery"] == "failed"
    assert "（结果没发出去）" in await runtime.history_text(CHAT)

    context.actions.clear()
    resent = await runtime.result_message(context, CHAT, task.task_id)
    assert not isinstance(resent, str)
    assert resent[0] == {"type": "text", "data": {"text": "表做好了"}}
    await wait_until(lambda: len(context.actions) == 1)
    assert context.actions[0]["action"] == "upload_private_file"
    assert "没有任务" in await runtime.result_message(context, CHAT, "zzzzzz")


@pytest.mark.asyncio
async def test_shutdown_waits_for_a_result_that_is_being_sent(tmp_path: Path) -> None:
    sending = asyncio.Event()
    finish  = asyncio.Event()
    context = FakeAgentContext(tmp_path, ai=FakeAI([_final("做完了")]))
    original = context.send_action

    async def slow_send(action):
        sending.set()
        await finish.wait()
        return await original(action)

    context.send_action = slow_send
    runtime             = AgentRuntime(tmp_path)
    task = runtime.start(context, CONFIG, CHAT, request_text="g", source="chat", admin=True)
    await sending.wait()
    assert runtime.running == {}

    closing = asyncio.create_task(runtime.shutdown())
    await asyncio.sleep(0.05)
    assert not closing.done()
    finish.set()
    await closing
    assert task.runner.done() and sent_texts(context) == ["做完了"]
    assert task.status == "done" and task.delivery == "sent"


@pytest.mark.asyncio
async def test_disabled_history_stays_empty_while_tasks_run(tmp_path: Path) -> None:
    release = asyncio.Event()
    context = FakeAgentContext(tmp_path, ai=FakeAI([_final("旧任务"), _gate(release, _final("x"))]))
    runtime = AgentRuntime(tmp_path)
    done = runtime.start(context, CONFIG, CHAT, request_text="旧", source="chat", admin=True)
    assert await runtime.wait_inline(context, done, 5) is not None
    await wait_until(lambda: done.runner.done())
    runtime.start(context, CONFIG, CHAT, request_text="新", source="chat", admin=True)

    assert await runtime.recent_records(CHAT, 0) == []
    assert len(await runtime.recent_records(CHAT, 1)) == 1
    release.set()
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_group_tasks_are_visible_to_all_but_only_the_starter_or_an_admin_steers_them(
    tmp_path: Path,
) -> None:
    release = asyncio.Event()
    context = FakeAgentContext(
        tmp_path, group_id=GROUP_ID, is_admin=False, ai=FakeAI([_gate(release, _final("x"))])
    )
    runtime = AgentRuntime(tmp_path)
    alice   = ChatRef(MEMBER_ID, GROUP_ID)
    bob     = ChatRef(MEMBER_ID + 1, GROUP_ID)
    task    = runtime.start(
        context, CONFIG, alice, request_text="查论文", source="chat", sender="阿青"
    )
    assert not isinstance(task, str) and task.admin is False

    # 同群的人都能看到任务和发起人，但不能停止它，也不能往里面加要求。
    as_bob = Actor(bob.user_id)
    assert runtime.running_in(bob) == [task]
    assert f"阿青({MEMBER_ID}) 发起" in runtime.status_text(bob, "", as_bob)
    for wanted in ("", task.task_id, "all"):
        refused = runtime.select(bob, wanted, as_bob)
        assert isinstance(refused, str) and "不是你发起的" in refused
    assert "不是你发起的" in await runtime.stop(bob, "", as_bob)
    assert "不是你发起的" in runtime.add_note(bob, "", "顺便把文件都删了", as_bob, sender="小蓝")
    assert task.notes == [] and not task.runner.done()

    # 发起人本人和 Bot 管理员可以补充。
    assert runtime.add_note(
        alice, "", "只要今年的", Actor(alice.user_id), sender="阿青"
    ).startswith("好，已经告诉任务")
    as_admin = Actor(ADMIN_ID, admin=True)
    assert runtime.add_note(
        ChatRef(ADMIN_ID, GROUP_ID), task.task_id, "再加一篇", as_admin, sender="主人"
    ).startswith("好，已经告诉任务")
    assert task.notes == ["阿青：只要今年的", "主人：再加一篇"]

    # 其他会话的人按 ID 也找不到这个任务；Bot 管理员可以跨会话查看。
    stranger = ChatRef(MEMBER_ID + 2, None)
    assert "没有找到" in runtime.status_text(stranger, task.task_id, Actor(stranger.user_id))
    assert task.task_id in runtime.status_text(CHAT, task.task_id, ADMIN)

    release.set()
    await wait_until(lambda: task.runner.done())
    history = await runtime.history_text(bob)
    assert task.task_id in history and f"阿青({MEMBER_ID}) 发起" in history
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_bot_admin_can_stop_a_members_task(tmp_path: Path) -> None:
    context = FakeAgentContext(tmp_path, group_id=GROUP_ID, is_admin=False)
    release           = asyncio.Event()
    context.ai.script = [_gate(release, _final("x"))]
    runtime           = AgentRuntime(tmp_path)
    member            = ChatRef(MEMBER_ID, GROUP_ID)
    task = runtime.start(context, CONFIG, member, request_text="g", source="chat")

    stopped = await runtime.stop(ChatRef(ADMIN_ID, GROUP_ID), "", Actor(ADMIN_ID, admin=True))
    assert stopped == [task] and task.status == "cancelled"
    assert context.actions == []


@pytest.mark.asyncio
async def test_members_cannot_crowd_out_bot_admins(tmp_path: Path) -> None:
    release = asyncio.Event()
    context = FakeAgentContext(
        tmp_path, ai=FakeAI([_gate(release, _final(name)) for name in "abcd"])
    )
    runtime = AgentRuntime(tmp_path)
    config = AgentConfig(enabled=True, max_admin_tasks=2, max_member_tasks=2)

    def start(chat: ChatRef, goal: str, *, admin: bool = False):
        return runtime.start(context, config, chat, request_text=goal, source="chat", admin=admin)

    first = start(ChatRef(MEMBER_ID, GROUP_ID), "一")
    assert not isinstance(first, str)
    # 同一个人换个会话也算在一起。
    again = start(ChatRef(MEMBER_ID, None), "二")
    assert isinstance(again, str) and "你已经有 1 个任务在跑了" in again
    assert not isinstance(start(ChatRef(MEMBER_ID + 1, GROUP_ID), "三"), str)
    full = start(ChatRef(MEMBER_ID + 2, GROUP_ID), "四")
    assert isinstance(full, str) and "排满了" in full

    # 其他人的名额满了，Bot 管理员照样能开任务；管理员自己的名额另算。
    admin_tasks = [start(CHAT, goal, admin=True) for goal in ("五", "六")]
    assert all(not isinstance(task, str) for task in admin_tasks)
    over = start(CHAT, "七", admin=True)
    assert isinstance(over, str) and "已经有 2 个任务" in over

    release.set()
    await wait_until(lambda: not runtime.running)
    await runtime.shutdown()


def test_member_tasks_can_be_switched_off(tmp_path: Path) -> None:
    runtime = AgentRuntime(tmp_path)
    context = FakeAgentContext(tmp_path, is_admin=False)
    refused = runtime.start(
        context,
        AgentConfig(enabled=True, max_member_tasks=0),
        ChatRef(MEMBER_ID, None),
        request_text = "g",
        source       = "chat",
    )
    assert refused == "现在只有 Bot 管理员能让我动手做事。"
    assert runtime.running == {}


@pytest.mark.asyncio
async def test_background_results_in_a_group_mention_the_starter(tmp_path: Path) -> None:
    release = asyncio.Event()
    context = FakeAgentContext(tmp_path, group_id=GROUP_ID, is_admin=False)
    context.ai.script = [_gate(release, _final("论文找到了"))]
    runtime           = AgentRuntime(tmp_path)
    member            = ChatRef(MEMBER_ID, GROUP_ID)
    task = runtime.start(context, CONFIG, member, request_text="g", source="chat")
    assert await runtime.wait_inline(context, task, 0.05) is None

    release.set()
    await wait_until(lambda: len(context.actions) == 1)
    message = context.actions[0]["params"]["message"]
    assert message[0] == {"type": "at", "data": {"qq": str(MEMBER_ID)}}
    assert "".join(seg["data"].get("text", "") for seg in message[1:]) == " 论文找到了"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_follow_up_that_arrives_after_the_last_step_is_reported_as_unused(
    tmp_path: Path, monkeypatch
) -> None:
    import plugins.xiaoqing_agent.runtime as agent_runtime

    real_loop = agent_runtime.run_agent_loop

    async def loop_then_late_note(env, tools, messages, **kwargs):
        outcome = await real_loop(env, tools, messages, **kwargs)
        env.task.notes.append("换成红色")
        return outcome

    monkeypatch.setattr(agent_runtime, "run_agent_loop", loop_then_late_note)
    context = FakeAgentContext(tmp_path, ai=FakeAI([_final("蓝色的好了")]))
    runtime = AgentRuntime(tmp_path)
    task = runtime.start(context, CONFIG, CHAT, request_text="画图", source="chat", admin=True)
    await wait_until(lambda: task.runner.done())

    [text] = sent_texts(context)
    assert text.startswith("蓝色的好了") and "“换成红色”送到时任务已经做完" in text
    assert task.notes == []


def _record(data_dir: Path, task_id: str) -> dict:
    return next(item for item in _records(data_dir) if item["task_id"] == task_id)


@pytest.mark.asyncio
async def test_inline_reply_delivery_is_confirmed_by_the_chat_side(tmp_path: Path) -> None:
    context = FakeAgentContext(tmp_path, ai=FakeAI([_final("算好了"), _final("又算好了")]))
    runtime = AgentRuntime(tmp_path)

    async def answer_inline(goal: str):
        task = runtime.start(context, CONFIG, CHAT, request_text=goal, source="chat", admin=True)
        outcome = await runtime.wait_inline(context, task, 5)
        await runtime.inline_reply(context, task, outcome)
        await wait_until(lambda: task.runner.done())
        return task

    lost = await answer_inline("一")
    await runtime.report_delivery(lost.task_id, False)
    assert _record(tmp_path, lost.task_id)["delivery"] == "failed"
    assert "结果没发出去" in await runtime.history_text(CHAT)

    sent = await answer_inline("二")
    await runtime.report_delivery(sent.task_id, True)
    # 同一个任务只认第一次回执；没登记过的任务直接忽略。
    await runtime.report_delivery(sent.task_id, False)
    await runtime.report_delivery("zzzzzz", False)
    await runtime.shutdown()
    assert _record(tmp_path, sent.task_id)["delivery"] == "sent"


@pytest.mark.asyncio
async def test_resent_results_are_complete_and_record_their_own_delivery(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("plugins.xiaoqing_agent.runtime._UPLOAD_AFTER_REPLY_SECONDS", 0.01)
    work = tmp_path / "workspaces" / CHAT.key
    work.mkdir(parents=True)
    (work / "table.csv").write_text("a,b", encoding="utf-8")
    long_text = "结果" * 3000
    context   = FakeAgentContext(
        tmp_path, ai=FakeAI([_final(long_text), _final("表在这", ["table.csv"])])
    )
    runtime = AgentRuntime(tmp_path)
    task = runtime.start(context, CONFIG, CHAT, request_text="长结果", source="chat", admin=True)
    await wait_until(lambda: task.runner.done() and len(context.actions) == 1)

    resent = await runtime.result_message(context, CHAT, task.task_id)
    assert isinstance(resent, DeliverySegments)
    assert resent[0]["data"]["text"] == long_text
    await resent.delivery_receipt.record(False)
    assert _record(tmp_path, task.task_id)["delivery"] == "failed"
    again = await runtime.result_message(context, CHAT, task.task_id)
    await again.delivery_receipt.record(True)
    assert _record(tmp_path, task.task_id)["delivery"] == "sent"

    # 文字发出了、附件没传上去：仍然记为没送达。
    table = runtime.start(context, CONFIG, CHAT, request_text="做表", source="chat", admin=True)
    await wait_until(lambda: table.runner.done() and len(context.actions) == 3)
    context.send_results = [False]
    with_file            = await runtime.result_message(context, CHAT, table.task_id)
    await with_file.delivery_receipt.record(True)
    await wait_until(lambda: len(context.actions) == 4)
    await runtime.shutdown()
    assert _record(tmp_path, table.task_id)["delivery"] == "failed"


class _ReceiptLog:
    """记下命令回执最后被怎么结算：True 提交、False 回滚、None 未知。"""

    def __init__(self) -> None:
        self.outcomes: dict[str, list[bool | None]] = {}

    def make(self, name: str) -> DeliveryReceipt:
        log = self.outcomes.setdefault(name, [])
        return DeliveryReceipt(
            expected_actions = 1,
            commit           = lambda: log.append(True),
            rollback         = lambda: log.append(False),
            unknown          = lambda: log.append(None),
        )


def _command_runner(tmp_path: Path, receipts: _ReceiptLog) -> tuple[SimpleNamespace, Path]:
    picture = tmp_path / "twimg.png"
    picture.write_bytes(PNG)
    replies = iter(
        [
            DeliverySegments(
                [{"type": "text", "data": {"text": "今日推荐三篇"}}], receipts.make("arxiv")
            ),
            DeliverySegments(
                [{"type": "image", "data": {"file": str(picture)}}], receipts.make("twimg")
            ),
        ]
    )
    return SimpleNamespace(run=AsyncMock(side_effect=lambda *_a, **_k: next(replies))), picture


def _command_steps(files: list[str]) -> list[dict]:
    return [
        ai_response("", [tool_call("xiaoqing_command", {"commands": ["/arxiv"]})]),
        ai_response("", [tool_call("xiaoqing_command", {"commands": ["/twimg"]})]),
        _final("图来了", files),
        ai_response("图来了"),
    ]


@pytest.mark.asyncio
async def test_command_receipts_settle_with_the_files_they_brought(tmp_path: Path) -> None:
    """取图这类命令的回执随图片真正发出时提交；只有文字的命令原消息不会发出，当场释放。"""

    receipts = _ReceiptLog()
    runner, picture = _command_runner(tmp_path, receipts)
    context = FakeAgentContext(
        tmp_path, ai=FakeAI(_command_steps([str(picture)])), command_runner=runner
    )
    runtime = AgentRuntime(tmp_path)
    task = runtime.start(context, CONFIG, CHAT, request_text="取图", source="chat", admin=True)
    await wait_until(lambda: task.runner.done() and bool(context.actions))
    await runtime.shutdown()

    assert receipts.outcomes == {"arxiv": [False], "twimg": [True]}
    # 每次命令调用都有自己的操作标识，同一任务里的两次调用不会被插件当成重复提交。
    operation_ids = [call.kwargs["operation_id"] for call in runner.run.await_args_list]
    assert operation_ids == [f"{task.task_id}-1", f"{task.task_id}-2"]


@pytest.mark.asyncio
async def test_command_receipts_are_released_when_their_files_never_go_out(tmp_path: Path) -> None:
    receipts = _ReceiptLog()
    runner, picture = _command_runner(tmp_path, receipts)
    del picture
    context = FakeAgentContext(tmp_path, ai=FakeAI(_command_steps([])), command_runner=runner)
    runtime = AgentRuntime(tmp_path)
    task = runtime.start(context, CONFIG, CHAT, request_text="取图", source="chat", admin=True)
    await wait_until(lambda: task.runner.done() and bool(context.actions))
    await runtime.shutdown()

    assert receipts.outcomes == {"arxiv": [False], "twimg": [False]}


@pytest.mark.asyncio
async def test_inline_image_receipt_follows_the_chat_sides_report(tmp_path: Path) -> None:
    receipts = _ReceiptLog()
    runner, picture = _command_runner(tmp_path, receipts)
    context = FakeAgentContext(
        tmp_path, ai=FakeAI(_command_steps([str(picture)])), command_runner=runner
    )
    runtime = AgentRuntime(tmp_path)
    task = runtime.start(context, CONFIG, CHAT, request_text="取图", source="chat", admin=True)
    outcome = await runtime.wait_inline(context, task, 5)
    assert outcome is not None
    message = await runtime.inline_reply(context, task, outcome)
    assert any(segment["type"] == "image" for segment in message)
    assert receipts.outcomes["twimg"] == []

    await runtime.report_delivery(task.task_id, False)
    await runtime.shutdown()
    assert receipts.outcomes == {"arxiv": [False], "twimg": [False]}


@pytest.mark.asyncio
async def test_stopping_a_task_releases_its_command_receipts(tmp_path: Path) -> None:
    receipts = _ReceiptLog()
    runner, picture = _command_runner(tmp_path, receipts)
    del picture
    release = asyncio.Event()
    steps   = _command_steps([])
    context = FakeAgentContext(
        tmp_path,
        ai             = FakeAI([steps[0], steps[1], _gate(release, steps[2])]),
        command_runner = runner,
    )
    runtime = AgentRuntime(tmp_path)
    task = runtime.start(context, CONFIG, CHAT, request_text="取图", source="chat", admin=True)
    await wait_until(lambda: runner.run.await_count == 2 and len(context.ai.calls) == 3)
    assert receipts.outcomes["twimg"] == []

    await runtime.stop(CHAT, task.task_id, ADMIN)
    await runtime.shutdown()
    assert receipts.outcomes == {"arxiv": [False], "twimg": [False]}


@pytest.mark.asyncio
async def test_each_turn_carries_only_the_chat_since_the_previous_turn(tmp_path: Path) -> None:
    import time

    context = FakeAgentContext(tmp_path, ai=FakeAI([_final("好"), _final("改好了")]))
    runtime = AgentRuntime(tmp_path)
    old     = {
        "role"   : "user",
        "name"   : "主人",
        "user_id": ADMIN_ID,
        "text"   : "之前聊的",
        "ts"     : time.time() - 60,
    }
    first = runtime.start(
        context, CONFIG, CHAT, request_text="建个多节点事件", source="chat", history=[old]
    )
    await first.runner
    new = {
        "role"   : "user",
        "name"   : "主人",
        "user_id": ADMIN_ID,
        "text"   : "顺便说一句",
        "ts"     : time.time() + 1,
    }
    second = runtime.start(
        context, CONFIG, CHAT, request_text="改", source="chat", history=[old, new]
    )
    await second.runner
    assert "主人（本人）: 之前聊的" in context.ai.calls[0][1][-1]["content"]
    turn = context.ai.calls[1][1][-1]["content"]
    assert "顺便说一句" in turn and "之前聊的" not in turn
    assert turn.splitlines()[-1] == "本轮原话：改"
    assert _records(tmp_path)[-1]["continues"] == first.task_id
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_needs_user_task_is_settled_and_not_swept_as_interrupted(tmp_path: Path) -> None:
    runner = SimpleNamespace(
        run=AsyncMock(return_value=[{"type": "text", "data": {"text": "有两条同名记录"}}])
    )
    ai = FakeAI(
        [
            _command("/shop"),
            ai_response(
                "", [tool_call("final_answer", {"text": "要改哪一条？", "state": "needs_user"})]
            ),
        ]
    )
    context = FakeAgentContext(tmp_path, ai=ai, command_runner=runner)
    runtime = AgentRuntime(tmp_path)
    task    = runtime.start(
        context, CONFIG, CHAT, request_text="改同名记录", source="chat", admin=True
    )
    outcome = await runtime.wait_inline(context, task, 5)
    await task.runner
    assert outcome is not None and outcome.text == "要改哪一条？"
    assert task.status == outcome.status == "needs_user"
    # 等待发起人回答的任务已经收尾，重启时不会被当成中断。
    assert await runtime.store.mark_interrupted() == 0
    assert _records(tmp_path)[-1]["status"] == "needs_user"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_finished_task_logs_model_and_tool_timing(tmp_path: Path, caplog) -> None:
    context = FakeAgentContext(tmp_path, ai=FakeAI([_final("好")]))
    runtime = AgentRuntime(tmp_path)
    with caplog.at_level("INFO", logger="plugins.xiaoqing_agent.runtime"):
        task = runtime.start(context, CONFIG, CHAT, request_text="好", source="chat", admin=True)
        await wait_until(lambda: task.runner.done())
    finished = [
        record.getMessage() for record in caplog.records if "task finished" in record.getMessage()
    ]
    assert finished and "model_calls=1 " in finished[0] and "tool_s=" in finished[0]
    await runtime.shutdown()
