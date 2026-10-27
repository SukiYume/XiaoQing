"""任务运行时：当轮交付、后台主动发送、停止、并发上限、关闭与重启收尾。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.ai import AIError
from core.delivery import CommandOutcome, DeliveryReceipt, DeliverySegments
from core.interfaces import ACTION_BYPASS_SINK_KEY
from plugins.xiaoqing_agent.config import AgentConfig
from plugins.xiaoqing_agent.runtime import AgentRuntime
from plugins.xiaoqing_agent.tasks import Actor, ChatRef, Outcome, TaskStore
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


def _final(text: str, files: list[str] | None = None) -> dict:
    return ai_response("", [tool_call("final_answer", {"text": text, "files": files or []})])


def _gate(release: asyncio.Event, response: dict):
    async def wait(_route, _messages, _kwargs):
        await release.wait()
        return response

    return wait


def _records(data_dir: Path, chat: ChatRef = CHAT) -> list[dict]:
    path = data_dir / "tasks" / f"{chat.key}.json"
    return json.loads(path.read_text(encoding="utf-8"))["tasks"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pending,failed", [("买2个苹果", False), ("查CHIME目录", False), ("买2个苹果", True)]
)
async def test_delivery_repairs_only_unfinished_original_request_once(tmp_path, pending, failed):
    first = DeliverySegments(
        [
            {
                "type": "text",
                "data": {"text": "商店：苹果10金币一个" if not failed else "商店连接超时"},
            }
        ],
        outcome=CommandOutcome("error" if failed else "completed"),
    )
    runner = SimpleNamespace(
        run=AsyncMock(
            side_effect=[first, [{"type": "text", "data": {"text": "购买成功：苹果2个"}}]]
        )
    )
    script = [
        ai_response("", [tool_call("xiaoqing_command", {"command": "/shop", "delivery": "reply"})]),
        ai_response(
            json.dumps(
                {"reply": "只查到了商店，购买还没完成。", "unfinished_request": pending},
                ensure_ascii=False,
            )
        ),
    ]
    repaired = pending == "买2个苹果" and not failed
    if repaired:
        script.extend(
            [
                ai_response(
                    "",
                    [
                        tool_call(
                            "xiaoqing_command", {"command": "/buy apple 2", "delivery": "reply"}
                        )
                    ],
                ),
                # 再次声称未完成也只能结束本轮，避免核对器造成循环购买。
                ai_response(
                    json.dumps(
                        {"reply": "买好了，2个苹果。", "unfinished_request": pending},
                        ensure_ascii=False,
                    )
                ),
            ]
        )
    script.append(ai_response('{"remove":[]}'))
    context = FakeAgentContext(tmp_path, ai=FakeAI(script), command_runner=runner)
    runtime = AgentRuntime(tmp_path)
    task    = runtime.start(
        context,
        CONFIG,
        CHAT,
        goal         = "给宠物买2个苹果",
        request_text = "给宠物买2个苹果",
        source       = "chat",
        admin        = True,
    )
    outcome = await runtime.wait_inline(context, task, 5)
    assert outcome is not None
    await wait_until(lambda: task.runner.done())
    assert runner.run.await_count == (2 if repaired else 1)
    assert all("CHIME" not in call.args[0] for call in runner.run.await_args_list)
    if repaired:
        assert outcome.text == "买好了，2个苹果。"
    if failed:
        assert outcome.status == "failed"


@pytest.mark.asyncio
async def test_fast_task_is_handed_to_the_waiting_turn(tmp_path: Path) -> None:
    context = FakeAgentContext(
        tmp_path, ai=FakeAI([_final("2 的 100 次方是 1267650600228229401496703205376")])
    )
    runtime = AgentRuntime(tmp_path)

    task = runtime.start(
        context, CONFIG, CHAT, goal="算 2^100", request_text="算一下", source="chat", admin=True
    )
    assert not isinstance(task, str)
    outcome = await runtime.wait_inline(context, task, 5)

    assert outcome is not None and outcome.text.startswith("2 的 100 次方")
    await wait_until(lambda: task.runner.done())
    assert context.actions == []
    assert runtime.running == {}
    record = _records(tmp_path)[-1]
    assert record["status"] == "done" and record["goal"] == "算 2^100"
    transcript = json.loads((tmp_path / "transcripts" / f"{task.task_id}.json").read_text("utf-8"))
    assert transcript["messages"][0]["role"] == "system"
    assert "算 2^100" in transcript["messages"][1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "review",
    [
        ai_response(
            json.dumps(
                {"reply": "指令发过去了，设备还没确认灯的状态。要我再帮你调亮度就说。"},
                ensure_ascii=False,
            )
        ),
        ai_response(),
        AIError("offline"),
    ],
)
async def test_delivery_review_uses_tool_facts_and_preserves_files_without_repeating_writes(
    tmp_path: Path,
    review,
) -> None:
    work = tmp_path / "workspaces" / CHAT.key
    work.mkdir(parents=True)
    evidence = work / "device.txt"
    evidence.write_text("已接收，状态待确认", encoding="utf-8")
    tool_reply = (
        "已接收；状态待确认。" + "设备回报。" * 300 + "中段事实仍需核对" + "设备回报。" * 300
    )
    runner = SimpleNamespace(
        run=AsyncMock(return_value=[{"type": "text", "data": {"text": tool_reply}}])
    )
    ai = FakeAI(
        [
            ai_response(
                "",
                [
                    tool_call(
                        "xiaoqing_command", {"delivery": "continue", "command": "/米家 调用 {}"}
                    )
                ],
            ),
            _final("灯关了", ["device.txt"]),
            review,
            ai_response(json.dumps({"remove": ["要我再帮你调亮度就说。"]})),
        ]
    )
    context = FakeAgentContext(tmp_path, ai=ai, command_runner=runner)
    runtime = AgentRuntime(tmp_path)
    task    = runtime.start(
        context,
        CONFIG,
        CHAT,
        goal         = "关灯",
        request_text = "关灯",
        source       = "chat",
        admin        = True,
        persona      = "你是小青，说话自然、直接。",
    )
    outcome = await runtime.wait_inline(context, task, 5)
    assert outcome is not None
    await task.runner

    assert "灯关了" not in outcome.text
    assert "确认" in outcome.text
    assert "要我再帮你" not in outcome.text
    assert outcome.files == [str(evidence.resolve())]
    valid_review = isinstance(review, dict) and bool(review["choices"][0]["message"]["content"])
    assert outcome.status == "done" and task.steps == (4 if valid_review else 3)
    runner.run.assert_awaited_once()
    assert context.actions == []
    _, messages, kwargs = ai.calls[-2 if valid_review else -1]
    assert kwargs.get("tools") is None and kwargs["temperature"] == 0.1
    assert "你是小青" in messages[0]["content"]
    assert any("中段事实仍需核对" in str(message.get("content")) for message in messages)
    assert "中段事实仍需核对" not in task.command_results[0]
    assert kwargs["extra_payload"]["response_format"] == {"type": "json_object"}
    assert len(messages) == 2
    assert "发起人原话：关灯" in messages[-1]["content"]
    assert "灯关了" not in messages[-1]["content"]
    assert "待发送正文" not in messages[-1]["content"]
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_stopped_task_reviews_unsupported_claims_without_retrying_commands(tmp_path):
    runner = SimpleNamespace(
        run=AsyncMock(
            return_value=[
                {"type": "text", "data": {"text": "检索结果只有占位内容，缺少真实标题和链接"}}
            ]
        )
    )
    ai = FakeAI(
        [
            *[
                ai_response(
                    "",
                    [
                        tool_call(
                            "xiaoqing_command",
                            {"delivery": "continue", "command": f"/paper search {q}"},
                        )
                    ],
                )
                for q in ["FRB", "repeater", "review"]
            ],
            ai_response("", [tool_call("wait", {"seconds": 600})]),
            ai_response("没查到，不过另一条查询通道肯定可用，还有一篇我凭记忆补充的论文。"),
            ai_response(
                json.dumps(
                    {
                        "reply": "这次没拿到真实的论文标题和链接，返回内容一直没变，我就停了。",
                    },
                    ensure_ascii=False,
                )
            ),
            ai_response('{"remove": []}'),
        ]
    )
    context = FakeAgentContext(tmp_path, ai=ai, command_runner=runner)
    runtime = AgentRuntime(tmp_path)
    task    = runtime.start(
        context, CONFIG, CHAT, goal="找论文", request_text="找论文", source="chat", admin=True
    )
    outcome = await runtime.wait_inline(context, task, 5)
    assert outcome is not None
    await task.runner
    assert outcome.status == "failed" and task.status == "failed"
    assert "真实的论文标题和链接" in outcome.text and "肯定可用" not in outcome.text
    assert runner.run.await_count == 3 and len(ai.calls) == 7
    assert ai.calls[-1][2].get("tools") is None
    assert "缺少真实标题和链接" in str(ai.calls[-1][1])
    assert "肯定可用" not in str(ai.calls[-1][1])
    assert "执行状态：failed" in str(ai.calls[-1][1])
    await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "screened",
    [
        ai_response('{"remove": []}'),
        ai_response('{"remove": ["this text is absent"]}'),
        ai_response('{"remove": "wrong type"}'),
        AIError("offline"),
    ],
)
async def test_delivery_filter_preserves_required_questions_and_falls_back_to_full_facts(
    tmp_path, screened
):
    question = "查到了两个同名记录，你要改哪一个？"
    facts    = "记录 A：" + "甲" * 1400 + "；记录 B：" + "乙" * 1400 + "末尾完整事实"
    runner   = SimpleNamespace(
        run=AsyncMock(return_value=[{"type": "text", "data": {"text": facts}}])
    )
    ai = FakeAI(
        [
            ai_response(
                "", [tool_call("xiaoqing_command", {"command": "/records", "delivery": "reply"})]
            ),
            ai_response(json.dumps({"reply": question})),
            screened,
        ]
    )
    context = FakeAgentContext(tmp_path, ai=ai, command_runner=runner)
    runtime = AgentRuntime(tmp_path)
    task    = runtime.start(
        context,
        CONFIG,
        CHAT,
        goal         = "改同名记录",
        request_text = "改同名记录",
        source       = "chat",
        admin        = True,
    )
    outcome = await runtime.wait_inline(context, task, 5)
    await task.runner
    assert runner.run.await_count == 1
    if screened == ai_response('{"remove": []}'):
        assert outcome.text == question
    else:
        assert facts in outcome.text and "末尾完整事实" in outcome.text
    assert "筛选候选回复" in ai.calls[-1][1][1]["content"]
    await runtime.shutdown()


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

    task = runtime.start(
        context, CONFIG, chat, goal="画图", request_text="画图", source="chat", admin=True
    )
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
        "file": str((out / "table.csv").resolve()),
        "name": "table.csv",
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
    task    = runtime.start(
        context, CONFIG, CHAT, goal="g", request_text="g", source="chat", admin=True
    )
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
    task    = runtime.start(
        context, CONFIG, CHAT, goal="长任务", request_text="长任务", source="chat", admin=True
    )
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
    task    = runtime.start(
        context, CONFIG, CHAT, goal="g", request_text="g", source="chat", admin=True
    )
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
    task              = runtime.start(
        context, CONFIG, CHAT, goal="g", request_text="g", source="chat", admin=True
    )
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
    first = runtime.start(
        context, config, CHAT, goal="一", request_text="一", source="chat", admin=True
    )
    second = runtime.start(
        context, config, other, goal="二", request_text="二", source="chat", admin=True
    )
    refused = runtime.start(
        context, config, CHAT, goal="三", request_text="三", source="chat", admin=True
    )
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
    task    = runtime.start(
        context, CONFIG, CHAT, goal="画图任务", request_text="画图", source="chat", admin=True
    )
    await wait_until(lambda: task.plan != [] and len(context.ai.calls) == 2)

    status = runtime.status_text(CHAT, "", ADMIN)
    assert "✓ 抓数据" in status and "▶ 画图" in status and "第 2 步" in status
    release.set()
    await wait_until(lambda: task.runner.done())


@pytest.mark.asyncio
async def test_crashing_loop_is_reported_not_raised(tmp_path: Path) -> None:
    context = FakeAgentContext(tmp_path, ai=FakeAI([KeyError("boom")]))
    runtime = AgentRuntime(tmp_path)
    task    = runtime.start(
        context, CONFIG, CHAT, goal="g", request_text="g", source="chat", admin=True
    )
    await wait_until(lambda: task.runner.done() and context.actions)
    assert task.status == "failed"
    assert task.error.startswith("KeyError")
    assert "出了点问题" in sent_texts(context)[0]


@pytest.mark.asyncio
async def test_shutdown_interrupts_tasks_and_restart_sweeps_leftovers(tmp_path: Path) -> None:
    release = asyncio.Event()
    context = FakeAgentContext(tmp_path, ai=FakeAI([_gate(release, _final("x"))]))
    runtime = AgentRuntime(tmp_path)
    task    = runtime.start(
        context, CONFIG, CHAT, goal="g", request_text="g", source="chat", admin=True
    )
    await wait_until(lambda: len(context.ai.calls) == 1)

    await runtime.shutdown()
    assert task.status == "interrupted"
    assert _records(tmp_path)[-1]["status"] == "interrupted"
    refused = runtime.start(
        context, CONFIG, CHAT, goal="g", request_text="g", source="chat", admin=True
    )
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
async def test_recent_records_feed_follow_up_tasks(tmp_path: Path) -> None:
    context = FakeAgentContext(
        tmp_path,
        ai=FakeAI([_final("画好了 sin.png", ["sin.png"]), _final("改成红色了")]),
    )
    runtime = AgentRuntime(tmp_path)
    work    = tmp_path / "workspaces" / CHAT.key
    work.mkdir(parents=True)
    (work / "sin.png").write_bytes(PNG)

    first = runtime.start(
        context, CONFIG, CHAT, goal="画 sin", request_text="画 sin", source="chat", admin=True
    )
    assert await runtime.wait_inline(context, first, 5) is not None
    await wait_until(lambda: first.runner.done())

    second = runtime.start(
        context,
        CONFIG,
        CHAT,
        goal         = "把刚才的图改成红色",
        request_text = "改红色",
        source       = "chat",
        admin        = True,
    )
    assert await runtime.wait_inline(context, second, 5) is not None
    prompt = context.ai.calls[1][1][1]["content"]
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

    task = runtime.start(
        context, CONFIG, CHAT, goal="做表", request_text="做表", source="chat", admin=True
    )
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
    task                = runtime.start(
        context, CONFIG, CHAT, goal="g", request_text="g", source="chat", admin=True
    )
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
    done    = runtime.start(
        context, CONFIG, CHAT, goal="旧", request_text="旧", source="chat", admin=True
    )
    assert await runtime.wait_inline(context, done, 5) is not None
    await wait_until(lambda: done.runner.done())
    runtime.start(context, CONFIG, CHAT, goal="新", request_text="新", source="chat", admin=True)

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
        context, CONFIG, alice, goal="查论文", request_text="查论文", source="chat", sender="阿青"
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
    task = runtime.start(context, CONFIG, member, goal="g", request_text="g", source="chat")

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
        return runtime.start(
            context, config, chat, goal=goal, request_text=goal, source="chat", admin=admin
        )

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
        goal         = "g",
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
    task = runtime.start(context, CONFIG, member, goal="g", request_text="g", source="chat")
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
    task    = runtime.start(
        context, CONFIG, CHAT, goal="画图", request_text="画图", source="chat", admin=True
    )
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
        task = runtime.start(
            context, CONFIG, CHAT, goal=goal, request_text=goal, source="chat", admin=True
        )
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
    task    = runtime.start(
        context, CONFIG, CHAT, goal="长结果", request_text="长结果", source="chat", admin=True
    )
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
    table = runtime.start(
        context, CONFIG, CHAT, goal="做表", request_text="做表", source="chat", admin=True
    )
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
        ai_response(
            "", [tool_call("xiaoqing_command", {"delivery": "continue", "command": "/arxiv"})]
        ),
        ai_response(
            "", [tool_call("xiaoqing_command", {"delivery": "continue", "command": "/twimg"})]
        ),
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
    task    = runtime.start(
        context, CONFIG, CHAT, goal="取图", request_text="取图", source="chat", admin=True
    )
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
    task    = runtime.start(
        context, CONFIG, CHAT, goal="取图", request_text="取图", source="chat", admin=True
    )
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
    task    = runtime.start(
        context, CONFIG, CHAT, goal="取图", request_text="取图", source="chat", admin=True
    )
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
    task    = runtime.start(
        context, CONFIG, CHAT, goal="取图", request_text="取图", source="chat", admin=True
    )
    await wait_until(lambda: runner.run.await_count == 2 and len(context.ai.calls) == 3)
    assert receipts.outcomes["twimg"] == []

    await runtime.stop(CHAT, task.task_id, ADMIN)
    await runtime.shutdown()
    assert receipts.outcomes == {"arxiv": [False], "twimg": [False]}
