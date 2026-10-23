"""Codex 为 xiaoqing_agent 提供的任务入口：交回 Task，结果交还调用方，不直接投递。"""

from __future__ import annotations

import tests.helpers.codex_test_support as _fixture_support
from tests.helpers.codex_test_support import (
    FakeContext,
    FakeRunner,
    Path,
    _install_fake_manager,
    _wait_until,
    asyncio,
    codex_main,
    json,
    pytest,
)

reset_codex_manager = _fixture_support.reset_codex_manager


async def _run(request: dict, context: FakeContext) -> dict:
    # 服务只在插件执行通道里启动任务，等待由调用方（Core）在通道外完成。
    pending = await codex_main.agent_task_service(request, context)
    assert isinstance(pending, asyncio.Task)
    return await pending


@pytest.mark.asyncio
async def test_agent_task_creates_session_runs_and_returns_text_and_images(tmp_path: Path):
    context                 = FakeContext(tmp_path)
    context.current_user_id = 101
    runner = FakeRunner(result_text="画好了", artifact_name="chart.png")
    _install_fake_manager(context, runner)

    result = await _run(
        {"session": "agent-u101", "prompt": "画一张图", "cwd": None, "timeout_seconds": 30},
        context,
    )

    assert result["status"] == "done"
    assert result["final_text"].startswith("画好了")
    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert len(result["images"]) == 1 and Path(result["images"][0]).is_file()
    assert runner.calls == [("agent-u101", "画一张图", None)]
    # 结果交给 agent 自己说，不能再由 Codex 往聊天里发一遍。
    assert context.actions == []

    history = context.data_dir / "session" / "agent-u101" / "conversation.jsonl"
    events = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines()]
    queued = [event for event in events if event.get("status") == "queued"][-1]
    assert queued["metadata"] == {"source": "xiaoqing_agent", "suppress_delivery": True}


@pytest.mark.asyncio
async def test_agent_task_reuses_existing_session_thread(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner  = FakeRunner()
    _install_fake_manager(context, runner)

    first  = {"session": "agent-g9", "prompt": "第一步", "cwd": None, "timeout_seconds": 30}
    second = {"session": "agent-g9", "prompt": "第二步", "cwd": None, "timeout_seconds": 30}
    await _run(first, context)
    await _run(second, context)

    assert runner.calls == [("agent-g9", "第一步", None), ("agent-g9", "第二步", "thread-agent-g9")]


@pytest.mark.asyncio
async def test_agent_task_timeout_cancels_the_running_job(tmp_path: Path):
    context = FakeContext(tmp_path)
    manager = _install_fake_manager(context, FakeRunner())

    result = await _run(
        {"session": "agent-u1", "prompt": "block forever", "cwd": None, "timeout_seconds": 0.2},
        context,
    )

    assert result["status"] == "timeout"
    assert result["timed_out"] is True
    assert "agent-u1" not in manager.running
    assert context.actions == []


@pytest.mark.asyncio
async def test_agent_task_caller_cancellation_cancels_the_job(tmp_path: Path):
    context = FakeContext(tmp_path)
    manager = _install_fake_manager(context, FakeRunner())

    waiter = await codex_main.agent_task_service(
        {"session": "agent-u2", "prompt": "block", "cwd": None, "timeout_seconds": 30},
        context,
    )
    await _wait_until(lambda: "agent-u2" in manager.running)
    job = manager.running["agent-u2"]
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert job.finished_event.is_set()
    assert job.status == "cancelled"


@pytest.mark.asyncio
async def test_agent_task_still_waiting_is_cancelled_by_manager_shutdown(tmp_path: Path):
    context = FakeContext(tmp_path)
    manager = _install_fake_manager(context, FakeRunner())

    waiter = await codex_main.agent_task_service(
        {"session": "agent-u5", "prompt": "block", "cwd": None, "timeout_seconds": 30},
        context,
    )
    await _wait_until(lambda: "agent-u5" in manager.running)
    await manager.shutdown()

    assert waiter.done()
    assert manager.agent_tasks == set()


@pytest.mark.asyncio
async def test_agent_task_rejects_bad_labels_and_empty_prompts(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner  = FakeRunner()
    _install_fake_manager(context, runner)

    bad_label = await _run(
        {"session": "bad label!", "prompt": "x", "cwd": None, "timeout_seconds": 5},
        context,
    )
    empty = await _run(
        {"session": "agent-u3", "prompt": "  ", "cwd": None, "timeout_seconds": 5},
        context,
    )
    outside = await _run(
        {
            "session": "agent-u4",
            "prompt": "x",
            "cwd": "C:/definitely/outside",
            "timeout_seconds": 5,
        },
        context,
    )

    assert bad_label["status"] == "error"
    assert empty["status"] == "error"
    assert outside["status"] == "error"
    assert runner.calls == []
