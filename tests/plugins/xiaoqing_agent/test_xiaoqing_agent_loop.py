"""agent 执行循环：工具调用往返、交付、预算收尾、模型失败与上下文压缩。"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.ai import AIRequestError
from core.delivery import CommandOutcome, DeliverySegments
from plugins.xiaoqing_agent.config import AgentConfig
from plugins.xiaoqing_agent.loop import (
    AGENT_ROUTE,
    compact_messages,
    parse_tool_calls,
    run_agent_loop,
)
from plugins.xiaoqing_agent.shells import resolve_shell
from plugins.xiaoqing_agent.tasks import AgentTask, ChatRef
from plugins.xiaoqing_agent.tools import Tool, ToolEnv, ToolResult, build_tools
from tests.helpers.xiaoqing_agent_test_support import (
    ADMIN_ID,
    FakeAgentContext,
    FakeAI,
    ai_response,
    tool_call,
)


def _setup(tmp_path: Path, script: list, *, config: AgentConfig | None = None):
    ai = FakeAI(script)
    context = FakeAgentContext(tmp_path / "data", ai=ai)
    task = AgentTask("t1", ChatRef(ADMIN_ID, None), "原话", "chat", admin=True)
    work = tmp_path / "ws"
    (work / "out" / "t1").mkdir(parents=True)

    async def send_progress(_text: str, _files: list[str]) -> bool:
        return True

    env = ToolEnv(
        context=context,
        config=config or AgentConfig(enabled=True),
        task          = task,
        workspace     = work,
        out_dir       = work / "out" / "t1",
        python        = sys.executable,
        shell         = resolve_shell(""),
        deadline      = time.monotonic() + 600,
        send_progress = send_progress,
    )
    tools    = build_tools(env)
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "做事"}]
    return ai, env, tools, messages


@pytest.mark.asyncio
async def test_command_results_go_back_to_the_model_before_the_reply(tmp_path):
    """命令回报不原样发出：交回模型，由 final_answer 按人设写成回复。"""

    def persona(_route, messages, _kwargs):
        assert messages[-1]["role"] == "tool" and messages[-1]["content"].startswith(
            "五篇论文及链接"
        )
        return ai_response(
            "", [tool_call("final_answer", {"text": "找到五篇，链接都在这", "state": "done"})]
        )

    ai, env, _, messages = _setup(
        tmp_path,
        [
            ai_response("", [tool_call("xiaoqing_command", {"commands": ["/search topic"]})]),
            persona,
        ],
    )
    runner = AsyncMock(return_value=[{"type": "text", "data": {"text": "五篇论文及链接"}}])
    env.context.capabilities = replace(
        env.context.capabilities, command_runner=SimpleNamespace(run=runner)
    )
    result = await run_agent_loop(env, build_tools(env), messages)
    assert (result.text, result.status) == ("找到五篇，链接都在这", "done")
    assert runner.await_count == 1 and len(ai.calls) == 2


@pytest.mark.asyncio
async def test_the_third_identical_call_ends_the_loop_before_the_rest_of_the_batch(tmp_path):
    ai, env, _, messages = _setup(
        tmp_path,
        [
            ai_response(
                "",
                [
                    tool_call(
                        "xiaoqing_command",
                        {"commands": ["/paper search FRB"]},
                        str(index),
                    )
                    for index in range(4)
                ],
            ),
            ai_response("同一个查询重复了几次，结果没有变化。"),
        ],
    )
    runner = AsyncMock(
        return_value=DeliverySegments(
            [{"type": "text", "data": {"text": "五篇论文及链接"}}],
            outcome=CommandOutcome("completed"),
        )
    )
    env.context.capabilities = replace(
        env.context.capabilities, command_runner=SimpleNamespace(run=runner)
    )
    result = await run_agent_loop(env, build_tools(env), messages)
    assert result.status == "failed"
    assert "没有变化" in result.text
    assert runner.await_count == 3 and len(ai.calls) == 2


@pytest.mark.asyncio
async def test_repeated_identical_calls_get_a_hint_then_wrap_up(tmp_path):
    """同一调用得到相同结果：第二次在回报里提醒，第三次由代码收尾，最后一次不带工具的调用交代结果。"""

    script = [
        ai_response("", [tool_call("xiaoqing_command", {"commands": ["/paper search FRB"]})])
        for _ in range(3)
    ]
    script.append(ai_response("返回内容一直没变，只拿到了这两条。"))
    ai, env, _, messages = _setup(tmp_path, script)
    runner = AsyncMock(return_value=[{"type": "text", "data": {"text": "固定的两条结果"}}])
    env.context.capabilities = replace(
        env.context.capabilities, command_runner=SimpleNamespace(run=runner)
    )
    outcome = await run_agent_loop(env, build_tools(env), messages)
    assert runner.await_count == 3 and env.task.steps == 3
    assert "重复不会得到不同结果" in ai.calls[2][1][-1]["content"]
    assert outcome.status == "failed" and "一直没变" in outcome.text
    assert ai.calls[-1][2]["tools"] is None


@pytest.mark.asyncio
async def test_background_acceptance_is_reported_and_other_work_continues(tmp_path):
    script = [
        ai_response("", [tool_call("xiaoqing_command", {"commands": ["/tw_fetch"]})]),
        ai_response("", [tool_call("xiaoqing_command", {"commands": ["/pendo todo list"]})]),
        ai_response("抓图已经开始，完成后会通知；今天有一项待办。"),
    ]
    ai, env, _, messages = _setup(tmp_path, script)
    runner = AsyncMock(
        side_effect=[
            DeliverySegments(
                [{"type": "text", "data": {"text": "后台已接收，完成后通知"}}],
                outcome=CommandOutcome("accepted"),
            ),
            [{"type": "text", "data": {"text": "今天有一项待办"}}],
        ]
    )
    env.context.capabilities = replace(
        env.context.capabilities, command_runner=SimpleNamespace(run=runner)
    )
    outcome = await run_agent_loop(env, build_tools(env), messages)
    # 后台还没做完：直接回复的文字照常交付，任务记录不算完成。
    assert outcome.status == "unfinished" and runner.await_count == 2
    assert "不用等待" in ai.calls[1][1][-1]["content"]
    assert "今天有一项" in outcome.text


@pytest.mark.asyncio
async def test_new_pages_keep_advancing(tmp_path):
    script = [
        *[
            ai_response(
                "",
                [tool_call("xiaoqing_command", {"commands": [f"/job page {i}"]})],
            )
            for i in range(4)
        ],
        ai_response("四页都查到了。"),
    ]
    _, env, _, messages = _setup(tmp_path, script)
    runner = AsyncMock(
        side_effect=[
            *[[{"type": "text", "data": {"text": f"第{i}页"}}] for i in range(4)],
        ]
    )
    env.context.capabilities = replace(
        env.context.capabilities, command_runner=SimpleNamespace(run=runner)
    )
    outcome = await run_agent_loop(env, build_tools(env), messages)
    assert outcome.status == "done" and runner.await_count == 4


@pytest.mark.asyncio
async def test_loop_runs_tools_until_final_answer(tmp_path: Path) -> None:
    ai, env, tools, messages = _setup(
        tmp_path,
        [
            ai_response(
                "先写个文件",
                [tool_call("write_file", {"path": "out/t1/r.txt", "content": "42"}, "c1")],
                reasoning_content="需要先把结果写下来",
            ),
            ai_response(
                "",
                [tool_call("final_answer", {"text": "答案是 42", "files": ["out/t1/r.txt"]}, "c2")],
            ),
        ],
    )

    outcome = await run_agent_loop(env, tools, messages)

    assert outcome.text == "答案是 42"
    assert outcome.status == "done"
    assert outcome.files == [str(env.out_dir / "r.txt")]
    assert env.task.steps == 2
    route, second_request, kwargs = ai.calls[1]
    assert route == AGENT_ROUTE
    assert kwargs["tool_choice"] == "auto"
    assert {tool["function"]["name"] for tool in kwargs["tools"]} == set(tools)
    assistant = second_request[2]
    assert assistant["role"] == "assistant"
    assert assistant["reasoning_content"] == "需要先把结果写下来"
    assert assistant["tool_calls"][0]["function"]["name"] == "write_file"
    tool_message = second_request[3]
    assert tool_message == {
        "role"        : "tool",
        "tool_call_id": "c1",
        "name"        : "write_file",
        "content"     : tool_message["content"],
    }
    assert "已写入" in tool_message["content"]


@pytest.mark.asyncio
async def test_plain_reply_without_tools_is_the_final_answer(tmp_path: Path) -> None:
    _ai, env, tools, messages = _setup(tmp_path, [ai_response("这个不用算，直接告诉你：是 7。")])
    outcome = await run_agent_loop(env, tools, messages)
    assert outcome.text == "这个不用算，直接告诉你：是 7。"
    assert outcome.files == []


@pytest.mark.asyncio
async def test_unknown_tools_and_bad_arguments_are_fed_back(tmp_path: Path) -> None:
    ai, env, tools, messages = _setup(
        tmp_path,
        [
            ai_response(
                "", [tool_call("delete_everything", {}, "a"), tool_call("read_file", "{bad", "b")]
            ),
            ai_response("", [tool_call("final_answer", {"text": "好了"}, "c")]),
        ],
    )
    outcome = await run_agent_loop(env, tools, messages)
    assert outcome.text == "好了"
    feedback = [item["content"] for item in ai.calls[1][1] if item["role"] == "tool"]
    assert "没有名为 delete_everything" in feedback[0]
    assert "合法 JSON" in feedback[1]


@pytest.mark.asyncio
async def test_calls_after_final_answer_are_not_executed(tmp_path: Path) -> None:
    _ai, env, tools, messages = _setup(
        tmp_path,
        [
            ai_response(
                "",
                [
                    tool_call("final_answer", {"text": "完成"}, "a"),
                    tool_call("write_file", {"path": "late.txt", "content": "x"}, "b"),
                ],
            )
        ],
    )
    outcome = await run_agent_loop(env, tools, messages)
    assert outcome.text == "完成"
    assert not (env.workspace / "late.txt").exists()
    assert messages[-1]["content"] == "已交付，忽略后续调用"


@pytest.mark.asyncio
async def test_empty_replies_are_nudged_then_fail(tmp_path: Path) -> None:
    ai, env, tools, messages = _setup(tmp_path, [ai_response("") for _ in range(3)])
    outcome = await run_agent_loop(env, tools, messages)
    assert outcome.status == "failed"
    assert env.task.error == "模型没有给出结果"
    assert len(ai.calls) == 3
    assert ai.calls[-1][1][-1]["content"].startswith("（系统提示）请继续")


@pytest.mark.asyncio
async def test_step_budget_triggers_a_tool_free_wrap_up(tmp_path: Path) -> None:
    looping = ai_response(
        "", [tool_call("update_plan", {"steps": [{"step": "想", "status": "in_progress"}]})]
    )
    ai, env, tools, messages = _setup(
        tmp_path,
        [looping, looping, ai_response("做到第二步了，还差画图。")],
        config=AgentConfig(enabled=True, max_steps=2),
    )
    outcome = await run_agent_loop(env, tools, messages)
    assert outcome.status == "failed"
    assert outcome.text == "做到第二步了，还差画图。"
    assert env.task.error == "超出步数预算"
    _route, wrap_messages, wrap_kwargs = ai.calls[-1]
    assert wrap_kwargs["tools"] is None and wrap_kwargs["tool_choice"] is None
    assert "预算已经用完" in wrap_messages[-1]["content"]


@pytest.mark.asyncio
async def test_time_budget_wraps_up_even_if_the_summary_call_fails(tmp_path: Path) -> None:
    ai, env, tools, messages = _setup(tmp_path, [AIRequestError("timeout")])
    env.deadline = time.monotonic() + 1
    outcome      = await run_agent_loop(env, tools, messages)
    assert outcome.status == "failed"
    assert "没能在预算内做完" in outcome.text
    assert env.task.error == "超出时间预算"
    assert len(ai.calls) == 1


@pytest.mark.asyncio
async def test_error_wrap_up_failure_keeps_the_real_stop_reason(tmp_path):
    script = [
        ai_response("", [tool_call("xiaoqing_command", {"commands": ["/demo"]})]) for _ in range(3)
    ] + [AIRequestError("timeout")]
    _, env, _, messages = _setup(tmp_path, script)
    runner = AsyncMock(return_value=[{"type": "text", "data": {"text": "❌ 暂时不可用"}}])
    env.context.capabilities = replace(
        env.context.capabilities, command_runner=SimpleNamespace(run=runner)
    )
    outcome = await run_agent_loop(env, build_tools(env), messages)
    assert outcome.status == "failed" and "三次得到相同结果" in env.task.error
    assert "预算" not in outcome.text and "操作保存在任务记录" in outcome.text


@pytest.mark.asyncio
async def test_model_failure_ends_the_task_with_a_plain_message(tmp_path: Path) -> None:
    _ai, env, tools, messages = _setup(tmp_path, [AIRequestError("server_error", status=500)])
    outcome = await run_agent_loop(env, tools, messages)
    assert outcome.status == "failed"
    assert "模型" in outcome.text
    assert env.task.error.startswith("模型调用失败")


def test_parse_tool_calls_repairs_ids_and_skips_malformed_entries() -> None:
    calls = parse_tool_calls(
        {
            "tool_calls": [
                {"id": "x", "function": {"name": "a", "arguments": "{}"}},
                {"id": "x", "function": {"name": "b", "arguments": {"k": 1}}},
                {"function": {"name": ""}},
                "garbage",
                {"id": "", "function": {"name": "c"}},
            ]
        }
    )
    assert [call.name for call in calls] == ["a", "b", "c"]
    assert len({call.call_id for call in calls}) == 3
    assert parse_tool_calls({"tool_calls": "nope"}) == []


def test_compaction_keeps_recent_tool_output_and_structure() -> None:
    messages = [{"role": "system", "content": "s"}]
    for index in range(12):
        messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": str(index)}]})
        messages.append(
            {"role": "tool", "tool_call_id": str(index), "content": f"{index}:" + "x" * 9000}
        )
    compact_messages(messages)
    tool_contents = [item["content"] for item in messages if item["role"] == "tool"]
    assert all("较早的输出已省略" in text for text in tool_contents[:4])
    assert all(len(text) > 9000 for text in tool_contents[4:])
    assert sum(1 for item in messages if item["role"] == "assistant") == 12

    small = [{"role": "tool", "content": "short"}]
    compact_messages(small)
    assert small == [{"role": "tool", "content": "short"}]


def test_compaction_counts_tool_arguments_and_reasoning() -> None:
    script               = "print(1)\n" * 2000
    messages: list[dict] = [{"role": "system", "content": "s"}]
    for index in range(10):
        messages.append(
            {
                "role"             : "assistant",
                "content"          : "",
                "reasoning_content": "想" * 5000,
                "tool_calls"       : [
                    {
                        "id"      : str(index),
                        "type"    : "function",
                        "function": {
                            "name"     : "run_python",
                            "arguments": json.dumps({"code": script}),
                        },
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": str(index), "content": "ok"})
    original = [dict(item) for item in messages]

    compact_messages(messages)

    assistants = [item for item in messages if item["role"] == "assistant"]
    early, recent = assistants[:-4], assistants[-4:]
    for item in early:
        arguments = item["tool_calls"][0]["function"]["arguments"]
        assert "较早调用的参数已省略" in json.loads(arguments)["omitted"]
        assert item["reasoning_content"].endswith("（较早的推理已省略）")
        assert item["tool_calls"][0]["id"]
    for item in recent:
        assert len(item["reasoning_content"]) == 5000
        assert json.loads(item["tool_calls"][0]["function"]["arguments"])["code"] == script
    # 压缩换的是新对象，原来的消息（执行记录里保存的那份）保持完整。
    assert original[1]["reasoning_content"] == "想" * 5000
    assert json.loads(original[1]["tool_calls"][0]["function"]["arguments"])["code"] == script


def test_compaction_retains_command_help_and_delivered_answers() -> None:
    answer        = "已保存的结果和后续需要的编号。" * 100
    help_message  = {"role": "tool", "name": "xiaoqing_commands", "content": "完整命令说明" * 100}
    final_message = {
        "role"      : "assistant",
        "tool_calls": [tool_call("final_answer", {"text": answer})],
    }
    messages = [help_message, final_message]
    for index in range(12):
        messages.extend(
            [
                {"role": "assistant", "content": str(index)},
                {"role": "tool", "content": "x" * 9000},
            ]
        )
    compact_messages(messages)
    assert messages[0] == help_message
    assert json.loads(messages[1]["tool_calls"][0]["function"]["arguments"])["text"] == answer


@pytest.mark.asyncio
async def test_transcript_keeps_full_messages_while_the_model_sees_compacted_ones(
    tmp_path: Path,
    monkeypatch,
) -> None:
    big = "y" * 15_000

    async def output(_env, _argv, _timeout):
        return ToolResult(big)

    # 本例验证上下文压缩；真实子进程由使用 Proactor 循环的工具测试覆盖。
    monkeypatch.setattr("plugins.xiaoqing_agent.tools._run_in_workspace", output)
    steps = [
        ai_response("", [tool_call("run_python", {"code": f"print('{big}')"}, f"c{index}")])
        for index in range(6)
    ]
    ai, env, tools, messages = _setup(
        tmp_path,
        [*steps, ai_response("", [tool_call("final_answer", {"text": "好了"})])],
        config=AgentConfig(enabled=True, max_tool_output_chars=40_000),
    )
    transcript = list(messages)
    outcome = await run_agent_loop(env, tools, messages, transcript=transcript)

    assert outcome.text == "好了"
    assert len(transcript) == len(messages)
    sent_last = ai.calls[-1][1]
    assert any("较早调用的参数已省略" in json.dumps(item, ensure_ascii=False) for item in sent_last)
    assert all(
        big in call["function"]["arguments"]
        for item in transcript
        if item["role"] == "assistant"
        for call in item.get("tool_calls", [])
        if call["function"]["name"] == "run_python"
    )


@pytest.mark.asyncio
async def test_follow_up_notes_reach_the_model_before_delivery(tmp_path: Path) -> None:
    ai, env, tools, messages = _setup(tmp_path, [])

    async def plan_then_note(_route, _messages, _kwargs):
        env.task.notes.append("换成红色")
        return ai_response("", [tool_call("final_answer", {"text": "蓝色的好了"})])

    ai.script = [
        plan_then_note,
        ai_response("", [tool_call("final_answer", {"text": "红色的好了"})]),
    ]
    outcome = await run_agent_loop(env, tools, messages)

    assert outcome.text == "红色的好了"
    second_call = ai.calls[1][1]
    # 补充在模型给出交付之后、执行之前到达：这次交付没有执行，交给模型按补充重来。
    assert any("没有执行" in str(item.get("content")) for item in second_call)
    assert second_call[-1]["role"] == "user" and "换成红色" in second_call[-1]["content"]
    assert env.task.notes == []


@pytest.mark.asyncio
async def test_a_follow_up_during_a_batch_stops_the_calls_that_have_not_started(
    tmp_path: Path,
) -> None:
    """模型一次排了“先查再改”；查的时候发起人说“第二步先别做”，改的那一步就不能再执行。"""

    ai, env, tools, messages = _setup(tmp_path, [])
    executed: list[str] = []

    async def query(task_env: ToolEnv, _args: dict) -> ToolResult:
        executed.append("query")
        task_env.task.notes.append("第二步先别执行")
        return ToolResult("查到 3 条")

    async def modify(_env: ToolEnv, _args: dict) -> ToolResult:
        executed.append("modify")
        return ToolResult("已修改")

    empty = {"type": "object", "properties": {}}
    tools = {
        **tools,
        "query" : Tool("query", "查询", empty, query),
        "modify": Tool("modify", "修改", empty, modify),
    }
    ai.script = [
        ai_response("", [tool_call("query", {}, "c1"), tool_call("modify", {}, "c2")]),
        ai_response("", [tool_call("final_answer", {"text": "查到 3 条，按你说的没有改"})]),
    ]
    outcome = await run_agent_loop(env, tools, messages)

    assert executed == ["query"]
    assert outcome.text == "查到 3 条，按你说的没有改"
    second  = ai.calls[1][1]
    skipped = next(item for item in second if item.get("tool_call_id") == "c2")
    assert skipped["role"] == "tool" and "没有执行" in skipped["content"]
    assert second[-1]["role"] == "user" and "第二步先别执行" in second[-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [ai_response("直接答复"), ai_response("", [tool_call("final_answer", {"text": "交付"})])],
)
async def test_direct_text_and_final_answer_both_end_the_task_and_calls_are_counted(
    tmp_path, reply
):
    ai, env, _, messages = _setup(tmp_path, [reply])
    result = await run_agent_loop(env, build_tools(env), messages)
    assert result.text in {"直接答复", "交付"} and result.status == "done"
    assert env.model_calls == len(ai.calls) == 1
    assert env.model_seconds >= 0 and env.tool_seconds >= 0
