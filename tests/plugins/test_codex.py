from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from plugins.codex import main as codex_main
from plugins.codex.config import load_plugin_config
from plugins.codex.manager import CodexQueueManager, reset_manager_for_tests
from plugins.codex.runner import CodexRunResult


class FakeContext:
    def __init__(self, tmp_path: Path, *, max_parallel_jobs: int = 2) -> None:
        self.data_dir = tmp_path / "plugin-data"
        self.plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / "codex"
        self.current_user_id = None
        self.current_group_id = None
        self.actions: list[dict[str, Any]] = []
        self.default_cwd = tmp_path / "default-cwd"
        self.config = {
            "plugins": {
                "codex": {
                    "default_cwd": str(self.default_cwd),
                    "allowed_cwd_roots": [str(tmp_path)],
                    "max_parallel_jobs": max_parallel_jobs,
                    "job_timeout_seconds": 30,
                }
            }
        }
        self.secrets: dict[str, Any] = {}

    async def send_action(self, action: dict[str, Any]) -> None:
        self.actions.append(action)


class FakeRunner:
    def __init__(self, *, result_text: str | None = None) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.started: list[str] = []
        self.release = asyncio.Event()
        self.result_text = result_text

    async def run(self, *, cwd: Path, prompt: str, thread_id: str | None, job: Any) -> CodexRunResult:
        self.calls.append((job.label, prompt, thread_id))
        self.started.append(job.label)
        if "block" in prompt:
            await self.release.wait()
        await asyncio.sleep(0)
        return CodexRunResult(
            exit_code=0,
            thread_id=thread_id or f"thread-{job.label}",
            final_text=self.result_text if self.result_text is not None else f"done: {prompt}",
            stdout_tail="",
            stderr_tail="",
        )


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached before timeout")
        await asyncio.sleep(0.01)


def _install_fake_manager(context: FakeContext, runner: FakeRunner) -> CodexQueueManager:
    import plugins.codex.manager as manager_module

    manager = CodexQueueManager(
        context,
        config=load_plugin_config(context),
        runner=runner,  # type: ignore[arg-type]
    )
    manager_module._MANAGER = manager
    return manager


@pytest.fixture(autouse=True)
def reset_codex_manager():
    reset_manager_for_tests()
    yield
    reset_manager_for_tests()


@pytest.mark.asyncio
async def test_create_session_uses_default_cwd_and_creates_directory(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner()
    _install_fake_manager(context, runner)

    result = await codex_main.handle("codex", "create aaa", {"user_id": 1, "group_id": 2}, context)

    assert context.default_cwd.exists()
    assert "已创建" in str(result)
    assert "aaa" in str(result)


@pytest.mark.asyncio
async def test_create_session_rejects_cwd_outside_allowed_roots(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner()
    _install_fake_manager(context, runner)
    outside = tmp_path.parent / "outside-codex-test"
    outside.mkdir(exist_ok=True)

    result = await codex_main.handle("codex", f"create aaa cwd:{outside.as_posix()}", {"user_id": 1}, context)

    assert "不在允许范围" in str(result)


@pytest.mark.asyncio
async def test_same_label_queue_runs_serially_and_sends_results(tmp_path: Path):
    context = FakeContext(tmp_path, max_parallel_jobs=2)
    runner = FakeRunner()
    manager = _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create aaa", {"user_id": 1, "group_id": 2}, context)
    first = await codex_main.handle("codex", "aaa block first", {"user_id": 1, "group_id": 2}, context)
    second = await codex_main.handle("codex", "aaa second", {"user_id": 1, "group_id": 2}, context)

    await _wait_until(lambda: runner.started == ["aaa"])
    assert "开始后台执行" in str(first)
    assert "前面还有 1 个任务" in str(second)

    runner.release.set()
    await manager.wait_idle()

    assert [call[1] for call in runner.calls] == ["block first", "second"]
    sent_text = str(context.actions)
    assert "[codex:aaa #1] 完成" in sent_text
    assert "[codex:aaa #2] 完成" in sent_text


@pytest.mark.asyncio
async def test_different_labels_can_run_in_parallel(tmp_path: Path):
    context = FakeContext(tmp_path, max_parallel_jobs=2)
    runner = FakeRunner()
    manager = _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create aaa", {"user_id": 1, "group_id": 2}, context)
    await codex_main.handle("codex", "create bbb", {"user_id": 1, "group_id": 2}, context)
    await codex_main.handle("codex", "aaa block one", {"user_id": 1, "group_id": 2}, context)
    await codex_main.handle("codex", "bbb block two", {"user_id": 1, "group_id": 2}, context)

    await _wait_until(lambda: runner.started.count("aaa") == 1 and runner.started.count("bbb") == 1)
    assert set(runner.started) == {"aaa", "bbb"}

    runner.release.set()
    await manager.wait_idle()
    assert len(context.actions) == 2


@pytest.mark.asyncio
async def test_result_is_not_truncated_by_plugin(tmp_path: Path):
    context = FakeContext(tmp_path)
    long_text = "x" * 5000
    runner = FakeRunner(result_text=long_text)
    manager = _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create aaa", {"user_id": 1, "group_id": 2}, context)
    await codex_main.handle("codex", "aaa long answer", {"user_id": 1, "group_id": 2}, context)
    await manager.wait_idle()

    sent_text = context.actions[-1]["params"]["message"][0]["data"]["text"]
    assert long_text in sent_text
    assert "已截断" not in sent_text


@pytest.mark.asyncio
async def test_conversation_history_is_saved_per_session(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner(result_text="assistant reply")
    manager = _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create aaa", {"user_id": 1, "group_id": 2}, context)
    await codex_main.handle("codex", "aaa user prompt", {"user_id": 1, "group_id": 2}, context)
    await manager.wait_idle()

    history_path = context.data_dir / "conversations" / "aaa.jsonl"
    events = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]

    assert events[0]["type"] == "session.created"
    assert any(event.get("role") == "user" and event.get("content") == "user prompt" for event in events)
    assert any(event.get("role") == "assistant" and event.get("content") == "assistant reply" for event in events)
