from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.constants import MAX_MESSAGE_TEXT_LENGTH
from core.interfaces import PluginCapabilities, PluginPrincipal
from plugins.codex import arxiv_summary as codex_arxiv_summary
from plugins.codex import main as codex_main
from plugins.codex.artifacts import CodexImageArtifact
from plugins.codex.config import load_plugin_config
from plugins.codex.manager import CodexQueueManager, reset_manager_for_tests
from plugins.codex.runner import CodexRunner, CodexRunResult, ProcessTreeTerminationResult

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


def _valid_arxiv_summary(date: str, link: str, text: str = "summary") -> str:
    return (
        f"## {date}\n\n"
        f"1. [Example Paper Title]({link})\n\n"
        "   > Radio, Survey\n\n"
        f"   {text} 第一段。\n\n"
        f"   {text} 第二段。"
    )


class FakeContext:
    def __init__(self, tmp_path: Path, *, max_parallel_jobs: int = 2) -> None:
        self.data_dir = tmp_path / "plugin-data"
        self.plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / "codex"
        self.current_user_id = None
        self.current_group_id = None
        self.plugin_name = "codex"
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
        self.principal = PluginPrincipal(kind="lifecycle")
        self.capabilities = PluginCapabilities()

    async def send_action(self, action: dict[str, Any]) -> None:
        self.actions.append(action)


class FakeRunner:
    def __init__(
        self,
        *,
        result_text: str | None = None,
        exit_code: int = 0,
        artifact_name: str | None = None,
        generated_image_name: str | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.started: list[str] = []
        self.release = asyncio.Event()
        self.result_text = result_text
        self.exit_code = exit_code
        self.artifact_name = artifact_name
        self.generated_image_name = generated_image_name

    async def run(
        self,
        *,
        cwd: Path,
        prompt: str,
        thread_id: str | None,
        job: Any,
        artifact_dir: Path | None = None,
        process_handoff=None,
        prompt_handoff=None,
    ) -> CodexRunResult:
        self.calls.append((job.label, prompt, thread_id))
        self.started.append(job.label)
        if process_handoff is not None and not await process_handoff(None):
            return CodexRunResult(
                exit_code=None,
                thread_id=thread_id,
                final_text="cancelled before fake runner start",
                stdout_tail="",
                stderr_tail="",
                cancelled=True,
            )
        if prompt_handoff is not None and not await prompt_handoff():
            return CodexRunResult(
                exit_code=None,
                thread_id=thread_id,
                final_text="cancelled before fake prompt",
                stdout_tail="",
                stderr_tail="",
                cancelled=True,
            )
        if "block" in prompt:
            release_task = asyncio.create_task(self.release.wait())
            cancel_task = asyncio.create_task(job.cancel_event.wait())
            await asyncio.wait(
                {release_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in (release_task, cancel_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(release_task, cancel_task, return_exceptions=True)
            if job.cancel_requested:
                return CodexRunResult(
                    exit_code=None,
                    thread_id=thread_id,
                    final_text="cancelled fake runner",
                    stdout_tail="",
                    stderr_tail="",
                    cancelled=True,
                )
        await asyncio.sleep(0)
        if self.artifact_name and artifact_dir is not None:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / self.artifact_name).write_bytes(PNG_BYTES)
        if self.generated_image_name:
            generated_dir = (
                Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "generated_images"
            )
            generated_dir.mkdir(parents=True, exist_ok=True)
            (generated_dir / self.generated_image_name).write_bytes(PNG_BYTES)
        return CodexRunResult(
            exit_code=self.exit_code,
            thread_id=thread_id or f"thread-{job.label}",
            final_text=self.result_text if self.result_text is not None else f"done: {prompt}",
            stdout_tail="",
            stderr_tail="",
        )


class _RaceProcess:
    def __init__(self, pid: int = 43210) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.communicate_inputs: list[bytes | None] = []
        self.prompt_sent = asyncio.Event()
        self.terminated = asyncio.Event()

    async def communicate(self, input: bytes | None = None):
        self.communicate_inputs.append(input)
        if input:
            self.prompt_sent.set()
        await self.terminated.wait()
        return b"", b""

    async def wait(self):
        await self.terminated.wait()
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9
        self.terminated.set()


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


def _install_actual_runner_manager(context: FakeContext) -> CodexQueueManager:
    import plugins.codex.manager as manager_module

    config = load_plugin_config(context)
    manager = CodexQueueManager(
        context,
        config=config,
        runner=CodexRunner(config, context.data_dir / "outputs"),
    )
    manager_module._MANAGER = manager
    return manager


def _patch_race_termination(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    import plugins.codex.manager as manager_module
    import plugins.codex.runner as runner_module

    async def terminate(process: _RaceProcess) -> ProcessTreeTerminationResult:
        process.returncode = -15
        process.terminated.set()
        return ProcessTreeTerminationResult(tree_confirmed=True, parent_reaped=True)

    mocked = AsyncMock(side_effect=terminate)
    monkeypatch.setattr(manager_module, "terminate_process_tree", mocked)
    monkeypatch.setattr(runner_module, "terminate_process_tree", mocked)
    return mocked


def _arxiv_addon(manager: CodexQueueManager) -> codex_arxiv_summary.ArxivSummaryAddon:
    return codex_arxiv_summary.ArxivSummaryAddon(manager)


@pytest.fixture(autouse=True)
def reset_codex_manager():
    reset_manager_for_tests()
    yield
    reset_manager_for_tests()


def test_codex_manifest_restricts_every_command_to_admins() -> None:
    manifest_path = Path(__file__).resolve().parents[2] / "plugins" / "codex" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    commands = manifest.get("commands")
    assert commands, "Codex manifest must declare at least one command"
    assert all(command.get("admin_only") is True for command in commands)


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

    result = await codex_main.handle(
        "codex", f"create aaa cwd:{outside.as_posix()}", {"user_id": 1}, context
    )

    assert "不在允许范围" in str(result)


@pytest.mark.asyncio
async def test_same_label_queue_runs_serially_and_sends_results(tmp_path: Path):
    context = FakeContext(tmp_path, max_parallel_jobs=2)
    runner = FakeRunner()
    manager = _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create aaa", {"user_id": 1, "group_id": 2}, context)
    first = await codex_main.handle(
        "codex", "aaa block first", {"user_id": 1, "group_id": 2}, context
    )
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
async def test_private_job_result_stays_private_when_session_created_in_group(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner(result_text="private reply")
    manager = _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create aaa", {"user_id": 1, "group_id": 2}, context)
    await codex_main.handle(
        "codex", "aaa hello", {"message_type": "private", "user_id": 1}, context
    )
    await manager.wait_idle()

    assert len(context.actions) == 1
    action = context.actions[0]
    assert action["action"] == "send_private_msg"
    assert action["params"]["user_id"] == 1
    assert "group_id" not in action["params"]
    assert "private reply" in str(action["params"]["message"])


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
async def test_cancel_while_waiting_for_global_slot_never_spawns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    context = FakeContext(tmp_path)
    context.default_cwd.mkdir(parents=True)
    manager = _install_actual_runner_manager(context)
    await manager.create_session("race", None, user_id=1, group_id=2)
    for _ in range(manager.config.max_parallel_jobs):
        await manager.global_sem.acquire()
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    try:
        await manager.enqueue(
            "race",
            "must not spawn",
            user_id=1,
            group_id=2,
            context=context,
        )
        await _wait_until(lambda: "race" in manager.running)
        job = manager.running["race"]

        response = await asyncio.wait_for(manager.cancel("race", job.job_id), timeout=2)
        await manager.wait_idle()
    finally:
        for _ in range(manager.config.max_parallel_jobs):
            manager.global_sem.release()

    assert "已取消" in response
    spawn.assert_not_awaited()
    assert job.status == "cancelled"
    assert job.prompt_started is False
    assert job.finished_event.is_set()
    assert manager.running == {}


@pytest.mark.asyncio
async def test_cancel_during_spawn_waits_for_handoff_and_never_sends_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    context = FakeContext(tmp_path)
    context.default_cwd.mkdir(parents=True)
    manager = _install_actual_runner_manager(context)
    await manager.create_session("race", None, user_id=1, group_id=2)
    process = _RaceProcess()
    spawn_started = asyncio.Event()
    allow_spawn = asyncio.Event()

    async def delayed_spawn(*_args, **_kwargs):
        spawn_started.set()
        await allow_spawn.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    terminate = _patch_race_termination(monkeypatch)

    await manager.enqueue("race", "side effect", user_id=1, group_id=2, context=context)
    await spawn_started.wait()
    job = manager.running["race"]
    assert job.status == "starting"
    cancel_task = asyncio.create_task(manager.cancel("race", job.job_id))
    await asyncio.sleep(0)
    assert cancel_task.done() is False

    allow_spawn.set()
    response = await asyncio.wait_for(cancel_task, timeout=2)
    await manager.wait_idle()

    assert "已取消" in response
    assert not any(process.communicate_inputs)
    assert process.prompt_sent.is_set() is False
    assert job.prompt_started is False
    assert job.status == "cancelled"
    assert job.result is not None and job.result.cancelled is True
    assert job.spawn_handoff.is_set() and job.finished_event.is_set()
    assert terminate.await_count >= 1
    history = [
        json.loads(line)
        for line in (context.data_dir / "session" / "race" / "conversation.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assistant_events = [
        event
        for event in history
        if event.get("role") == "assistant" and event.get("job_id") == job.job_id
    ]
    assert len(assistant_events) == 1
    assert assistant_events[0]["status"] == "cancelled"
    assert assistant_events[0]["cancelled"] is True


@pytest.mark.asyncio
async def test_cancel_after_process_registration_but_before_prompt_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    context = FakeContext(tmp_path)
    context.default_cwd.mkdir(parents=True)
    manager = _install_actual_runner_manager(context)
    await manager.create_session("race", None, user_id=1, group_id=2)
    process = _RaceProcess()
    before_prompt = asyncio.Event()
    allow_prompt = asyncio.Event()
    original_authorize = manager._authorize_job_prompt

    async def delayed_authorize(label, job):
        before_prompt.set()
        await allow_prompt.wait()
        return await original_authorize(label, job)

    manager._authorize_job_prompt = delayed_authorize  # type: ignore[method-assign]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    terminate = _patch_race_termination(monkeypatch)

    await manager.enqueue("race", "side effect", user_id=1, group_id=2, context=context)
    await before_prompt.wait()
    job = manager.running["race"]
    assert job.spawn_handoff.is_set()
    assert job.process is process
    assert job.prompt_started is False

    cancel_task = asyncio.create_task(manager.cancel("race", job.job_id))
    await _wait_until(lambda: job.cancel_requested)
    allow_prompt.set()
    response = await asyncio.wait_for(cancel_task, timeout=2)
    await manager.wait_idle()

    assert "已取消" in response
    assert not any(process.communicate_inputs)
    assert process.prompt_sent.is_set() is False
    assert job.status == "cancelled"
    assert job.result is not None and job.result.cancelled is True
    assert terminate.await_count >= 1


@pytest.mark.asyncio
async def test_cancel_after_prompt_commit_terminates_and_finishes_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    context = FakeContext(tmp_path)
    context.default_cwd.mkdir(parents=True)
    manager = _install_actual_runner_manager(context)
    await manager.create_session("race", None, user_id=1, group_id=2)
    process = _RaceProcess()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    _patch_race_termination(monkeypatch)

    await manager.enqueue("race", "running body", user_id=1, group_id=2, context=context)
    await process.prompt_sent.wait()
    job = manager.running["race"]
    assert job.status == "running"
    assert job.prompt_started is True

    response = await asyncio.wait_for(manager.cancel("race", job.job_id), timeout=2)
    await manager.wait_idle()

    assert "已取消" in response
    assert len([payload for payload in process.communicate_inputs if payload]) == 1
    assert job.status == "cancelled"
    assert job.result is not None and job.result.cancelled is True
    assert manager.running == {}


@pytest.mark.asyncio
async def test_spawn_failure_completes_handoff_and_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    context = FakeContext(tmp_path)
    context.default_cwd.mkdir(parents=True)
    manager = _install_actual_runner_manager(context)
    await manager.create_session("race", None, user_id=1, group_id=2)
    spawn_started = asyncio.Event()
    fail_spawn = asyncio.Event()

    async def failing_spawn(*_args, **_kwargs):
        spawn_started.set()
        await fail_spawn.wait()
        raise OSError("spawn failed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failing_spawn)
    await manager.enqueue("race", "body", user_id=1, group_id=2, context=context)
    await spawn_started.wait()
    job = manager.running["race"]
    fail_spawn.set()
    await manager.wait_idle()

    assert job.status == "failed"
    assert job.result is not None and "spawn failed" in job.result.final_text
    assert job.spawn_handoff.is_set() and job.finished_event.is_set()
    assert job.process is None
    assert not list((context.data_dir / "outputs").glob("codex-last-*.txt"))


@pytest.mark.asyncio
async def test_shutdown_during_spawn_uses_same_handoff_without_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    context = FakeContext(tmp_path)
    context.default_cwd.mkdir(parents=True)
    manager = _install_actual_runner_manager(context)
    await manager.create_session("race", None, user_id=1, group_id=2)
    process = _RaceProcess()
    spawn_started = asyncio.Event()
    allow_spawn = asyncio.Event()

    async def delayed_spawn(*_args, **_kwargs):
        spawn_started.set()
        await allow_spawn.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    _patch_race_termination(monkeypatch)
    await manager.enqueue("race", "shutdown body", user_id=1, group_id=2, context=context)
    await spawn_started.wait()
    job = manager.running["race"]

    shutdown_task = asyncio.create_task(manager.shutdown())
    await asyncio.sleep(0)
    assert shutdown_task.done() is False
    allow_spawn.set()
    await asyncio.wait_for(shutdown_task, timeout=2)

    assert not any(process.communicate_inputs)
    assert process.prompt_sent.is_set() is False
    assert job.status == "cancelled"
    assert job.finished_event.is_set()
    assert manager.running == {}
    assert manager.workers == {}


@pytest.mark.asyncio
async def test_spawn_timeout_is_bounded_and_finalizes_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    context = FakeContext(tmp_path)
    context.config["plugins"]["codex"]["spawn_timeout_seconds"] = 1
    context.default_cwd.mkdir(parents=True)
    manager = _install_actual_runner_manager(context)
    await manager.create_session("race", None, user_id=1, group_id=2)
    never = asyncio.Event()

    async def stuck_spawn(*_args, **_kwargs):
        await never.wait()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", stuck_spawn)
    await manager.enqueue("race", "body", user_id=1, group_id=2, context=context)
    await _wait_until(lambda: "race" in manager.running)
    job = manager.running["race"]

    await asyncio.wait_for(manager.wait_idle(), timeout=2)

    assert job.status == "failed"
    assert job.result is not None and "spawn timed out" in job.result.final_text
    assert job.spawn_handoff.is_set() and job.finished_event.is_set()
    assert manager.running == {}


@pytest.mark.asyncio
async def test_result_is_not_truncated_by_plugin(tmp_path: Path):
    context = FakeContext(tmp_path)
    long_text = "x" * 5000
    runner = FakeRunner(result_text=long_text)
    manager = _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create aaa", {"user_id": 1, "group_id": 2}, context)
    await codex_main.handle("codex", "aaa long answer", {"user_id": 1, "group_id": 2}, context)
    await manager.wait_idle()

    sent_text = "".join(
        seg["data"]["text"]
        for action in context.actions
        for seg in action["params"]["message"]
        if seg.get("type") == "text"
    )
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

    history_path = context.data_dir / "session" / "aaa" / "conversation.jsonl"
    events = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]

    assert events[0]["type"] == "session.created"
    assert any(
        event.get("role") == "user" and event.get("content") == "user prompt" for event in events
    )
    assert any(
        event.get("role") == "assistant" and event.get("content") == "assistant reply"
        for event in events
    )


@pytest.mark.asyncio
async def test_markdown_image_result_is_copied_and_sent(tmp_path: Path):
    context = FakeContext(tmp_path)
    context.default_cwd.mkdir(parents=True)
    image_path = context.default_cwd / "plot.png"
    image_path.write_bytes(PNG_BYTES)
    runner = FakeRunner(result_text=f"生成完成\n![plot]({image_path.as_posix()})")
    manager = _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create aaa", {"user_id": 1, "group_id": 2}, context)
    await codex_main.handle("codex", "aaa draw a plot", {"user_id": 1, "group_id": 2}, context)
    await manager.wait_idle()

    message = context.actions[-1]["params"]["message"]
    image_segments = [seg for seg in message if seg.get("type") == "image"]
    assert image_segments
    copied = context.data_dir / "session" / "aaa" / "images" / "job-0001-01.png"
    assert copied.exists()
    assert image_segments[0]["data"]["file"] == copied.resolve().as_uri()

    history_path = context.data_dir / "session" / "aaa" / "conversation.jsonl"
    events = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]
    assistant_event = [event for event in events if event.get("role") == "assistant"][-1]
    assert assistant_event["images"][0]["path"] == "images/job-0001-01.png"


@pytest.mark.asyncio
async def test_artifact_directory_images_are_sent_without_text_marker(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner(result_text="生成完成", artifact_name="chart.png")
    manager = _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create aaa", {"user_id": 1, "group_id": 2}, context)
    await codex_main.handle("codex", "aaa draw a chart", {"user_id": 1, "group_id": 2}, context)
    await manager.wait_idle()

    message = context.actions[-1]["params"]["message"]
    assert any(seg.get("type") == "image" for seg in message)
    assert (context.data_dir / "session" / "aaa" / "images" / "job-0001-01.png").exists()
    assert not (context.data_dir / "session" / "aaa" / "jobs" / "job-0001" / "artifacts").exists()


def test_result_message_batches_enforce_qq_image_limit(tmp_path: Path):
    context = FakeContext(tmp_path)
    context.config["plugins"]["codex"]["max_qq_images"] = 1
    manager = _install_fake_manager(context, FakeRunner())
    artifacts = [
        CodexImageArtifact(
            path=f"images/{index}.png",
            absolute_path=str(tmp_path / f"{index}.png"),
            source="artifact",
            original_path=f"{index}.png",
        )
        for index in range(3)
    ]

    batches = manager._result_message_batches("done", artifacts)  # noqa: SLF001

    assert sum(segment.get("type") == "image" for batch in batches for segment in batch) == 1


@pytest.mark.asyncio
async def test_generated_images_dir_is_not_guessed_without_explicit_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    context = FakeContext(tmp_path)
    runner = FakeRunner(result_text="已生成。", generated_image_name="cyberpunk.png")
    manager = _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create img", {"user_id": 1, "group_id": 2}, context)
    await codex_main.handle("codex", "img $imagegen draw", {"user_id": 1, "group_id": 2}, context)
    await manager.wait_idle()

    message = context.actions[-1]["params"]["message"]
    assert not any(seg.get("type") == "image" for seg in message)
    copied = context.data_dir / "session" / "img" / "images" / "job-0001-01.png"
    assert not copied.exists()


@pytest.mark.asyncio
async def test_long_text_with_image_is_split_before_image(tmp_path: Path):
    context = FakeContext(tmp_path)
    context.default_cwd.mkdir(parents=True)
    image_path = context.default_cwd / "plot.png"
    image_path.write_bytes(PNG_BYTES)
    long_text = "x" * (MAX_MESSAGE_TEXT_LENGTH + 10)
    runner = FakeRunner(result_text=f"{long_text}\n图片: {image_path.as_posix()}")
    manager = _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create aaa", {"user_id": 1, "group_id": 2}, context)
    await codex_main.handle("codex", "aaa draw long report", {"user_id": 1, "group_id": 2}, context)
    await manager.wait_idle()

    assert len(context.actions) >= 2
    assert all(seg.get("type") == "text" for seg in context.actions[0]["params"]["message"])
    assert any(seg.get("type") == "image" for seg in context.actions[-1]["params"]["message"])


@pytest.mark.asyncio
async def test_arxiv_summary_auto_creates_astro_ph_and_runs(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner(
        result_text=_valid_arxiv_summary(
            "2026-05-19",
            "https://arxiv.org/abs/2605.16917",
        )
    )
    manager = _install_fake_manager(context, runner)

    result = await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-19",
        links=["https://arxiv.org/abs/2605.16917"],
        user_id=1,
        group_id=2,
        context=context,
    )
    await manager.wait_idle()

    assert "已投递" in result
    assert "astro-ph" in manager.sessions
    assert manager.sessions["astro-ph"].cwd == str(context.default_cwd.resolve(strict=False))
    assert len(runner.calls) == 2
    init_label, init_prompt, init_thread_id = runner.calls[0]
    summary_label, summary_prompt, summary_thread_id = runner.calls[1]
    assert init_label == "astro-ph"
    assert init_thread_id is None
    assert "arxiv-summary-methodology.md" in init_prompt
    assert "这条消息只用于初始化会话规则" in init_prompt
    assert "https://arxiv.org/abs/2605.16917" not in init_prompt
    assert summary_label == "astro-ph"
    assert summary_thread_id == "thread-astro-ph"
    assert "请先读取当前工作目录下的 `arxiv-summary-methodology.md`" in summary_prompt
    assert "不要把方法论文件内容复述出来" in summary_prompt
    assert "## 2026-05-19\nhttps://arxiv.org/abs/2605.16917" in summary_prompt
    sent_text = str(context.actions)
    assert "[codex:astro-ph #1]" not in sent_text
    assert "[codex:astro-ph #2] 完成" in sent_text
    assert "summary" in sent_text


@pytest.mark.asyncio
async def test_arxiv_summary_public_entrypoint_uses_addon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    context = FakeContext(tmp_path)
    context.principal = PluginPrincipal(kind="scheduled_system")
    context.capabilities = PluginCapabilities(is_system=True)
    runner = FakeRunner(
        result_text=_valid_arxiv_summary(
            "2026-05-19",
            "https://arxiv.org/abs/2605.16917",
            "entrypoint summary",
        )
    )
    manager = _install_fake_manager(context, runner)

    async def fake_get_manager(_context: Any) -> CodexQueueManager:
        return manager

    import plugins.codex.manager as manager_module

    monkeypatch.setattr(manager_module, "get_manager", fake_get_manager)
    result = await codex_arxiv_summary.enqueue_or_replay_arxiv_summary(
        context,
        date="2026-05-19",
        links=["https://arxiv.org/abs/2605.16917"],
        user_id=1,
        group_id=2,
    )
    await manager.wait_idle()

    assert "已投递" in result
    assert len(runner.calls) == 2
    assert "entrypoint summary" in str(context.actions)


@pytest.mark.asyncio
async def test_arxiv_summary_public_entrypoint_rejects_unprivileged_or_wrong_context(
    tmp_path: Path,
):
    context = FakeContext(tmp_path)
    context.principal = PluginPrincipal(kind="user", user_id=1, is_private=True)

    with pytest.raises(PermissionError, match="authorization"):
        await codex_arxiv_summary.enqueue_or_replay_arxiv_summary(
            context,
            date="2026-05-19",
            links=["https://arxiv.org/abs/2605.16917"],
            user_id=1,
            group_id=2,
        )

    context.capabilities = PluginCapabilities(is_bot_admin=True)
    context.plugin_name = "arxiv_filter"
    with pytest.raises(PermissionError, match="Codex-scoped"):
        await codex_arxiv_summary.enqueue_or_replay_arxiv_summary(
            context,
            date="2026-05-19",
            links=["https://arxiv.org/abs/2605.16917"],
            user_id=1,
            group_id=2,
        )


@pytest.mark.asyncio
async def test_arxiv_summary_replays_existing_success_without_rerun(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner(
        result_text=_valid_arxiv_summary(
            "2026-05-19",
            "https://arxiv.org/abs/2605.16917",
            "cached summary",
        )
    )
    manager = _install_fake_manager(context, runner)

    await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-19",
        links=["https://arxiv.org/abs/2605.16917"],
        user_id=1,
        group_id=2,
        context=context,
    )
    await manager.wait_idle()
    context.actions.clear()

    result = await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-19",
        links=["https://arxiv.org/abs/2605.16917"],
        user_id=1,
        group_id=2,
        context=context,
    )

    assert "已重发" in result
    assert len(runner.calls) == 2
    sent_text = str(context.actions)
    assert "[codex:astro-ph #2] 完成" in sent_text
    assert "cached summary" in sent_text


@pytest.mark.asyncio
async def test_arxiv_summary_duplicate_inflight_reports_status(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner()
    manager = _install_fake_manager(context, runner)

    await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-20",
        links=["block"],
        user_id=1,
        group_id=2,
        context=context,
    )
    await _wait_until(lambda: runner.started == ["astro-ph", "astro-ph"])
    context.actions.clear()

    result = await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-20",
        links=["block"],
        user_id=1,
        group_id=2,
        context=context,
    )

    assert "已在队列或运行中" in result
    assert "已在运行中" in str(context.actions)
    runner.release.set()
    await manager.wait_idle()


@pytest.mark.asyncio
async def test_arxiv_summary_failed_history_is_retried(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner(result_text="network failed", exit_code=1)
    manager = _install_fake_manager(context, runner)

    await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-21",
        links=["https://arxiv.org/abs/2605.18513"],
        user_id=1,
        group_id=2,
        context=context,
    )
    await manager.wait_idle()
    assert "2026-05-21 arXiv 总结失败" in str(context.actions)

    retry_runner = FakeRunner(
        result_text=_valid_arxiv_summary(
            "2026-05-21",
            "https://arxiv.org/abs/2605.18513",
            "retry summary",
        )
    )
    manager.runner = retry_runner  # type: ignore[assignment]
    await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-21",
        links=["https://arxiv.org/abs/2605.18513"],
        user_id=1,
        group_id=2,
        context=context,
    )
    await manager.wait_idle()

    assert len(retry_runner.calls) == 1
    assert "retry summary" in str(context.actions)


@pytest.mark.asyncio
async def test_arxiv_summary_queue_full_reuses_manager_limit(tmp_path: Path):
    context = FakeContext(tmp_path)
    context.config["plugins"]["codex"]["per_session_queue_limit"] = 1
    runner = FakeRunner()
    manager = _install_fake_manager(context, runner)

    await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-22",
        links=["block"],
        user_id=1,
        group_id=2,
        context=context,
    )
    await _wait_until(lambda: runner.started == ["astro-ph", "astro-ph"])

    queued = await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-23",
        links=["https://arxiv.org/abs/2605.16917"],
        user_id=1,
        group_id=2,
        context=context,
    )
    full = await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-24",
        links=["https://arxiv.org/abs/2605.18050"],
        user_id=1,
        group_id=2,
        context=context,
    )

    assert "已投递" in queued
    assert "已投递" in full
    runner.release.set()
    await manager.wait_idle()


@pytest.mark.asyncio
async def test_protected_astro_ph_requires_explicit_protected_delete(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner()
    _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create astro-ph", {"user_id": 1, "group_id": 2}, context)

    denied = await codex_main.handle(
        "codex",
        "delete astro-ph --force",
        {"user_id": 1, "group_id": 2},
        context,
    )
    allowed = await codex_main.handle(
        "codex",
        "delete astro-ph --force --protected",
        {"user_id": 1, "group_id": 2},
        context,
    )

    assert "受保护" in str(denied)
    assert "已删除" in str(allowed)
    assert "历史已归档" in str(allowed)
    assert not (context.data_dir / "session" / "astro-ph").exists()
    archives = list((context.data_dir / "deleted_sessions").glob("astro-ph-*"))
    assert len(archives) == 1
    archived_history = (archives[0] / "conversation.jsonl").read_text(encoding="utf-8")
    assert "session.created" in archived_history
    assert "session.deleted" in archived_history


@pytest.mark.asyncio
async def test_delete_archives_history_and_recreate_uses_clean_session(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner()
    _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create aaa", {"user_id": 1, "group_id": 2}, context)
    deleted = await codex_main.handle(
        "codex", "delete aaa --force", {"user_id": 1, "group_id": 2}, context
    )
    recreated = await codex_main.handle(
        "codex", "create aaa", {"user_id": 1, "group_id": 2}, context
    )

    assert "已删除" in str(deleted)
    assert "已创建" in str(recreated)
    active_history = (context.data_dir / "session" / "aaa" / "conversation.jsonl").read_text(
        encoding="utf-8"
    )
    assert "session.created" in active_history
    assert "session.deleted" not in active_history
    archives = list((context.data_dir / "deleted_sessions").glob("aaa-*"))
    assert len(archives) == 1
    archived_history = (archives[0] / "conversation.jsonl").read_text(encoding="utf-8")
    assert "session.deleted" in archived_history


@pytest.mark.asyncio
async def test_recreated_astro_ph_does_not_replay_archived_summary(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner(
        result_text=_valid_arxiv_summary(
            "2026-05-19",
            "https://arxiv.org/abs/2605.16917",
            "old summary",
        )
    )
    manager = _install_fake_manager(context, runner)

    await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-19",
        links=["https://arxiv.org/abs/2605.16917"],
        user_id=1,
        group_id=2,
        context=context,
    )
    await manager.wait_idle()
    context.actions.clear()

    await codex_main.handle(
        "codex",
        "delete astro-ph --force --protected",
        {"user_id": 1, "group_id": 2},
        context,
    )

    retry_runner = FakeRunner(
        result_text=_valid_arxiv_summary(
            "2026-05-19",
            "https://arxiv.org/abs/2605.16917",
            "new summary",
        )
    )
    manager.runner = retry_runner  # type: ignore[assignment]
    result = await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-19",
        links=["https://arxiv.org/abs/2605.16917"],
        user_id=1,
        group_id=2,
        context=context,
    )
    await manager.wait_idle()

    assert "已投递" in result
    assert len(retry_runner.calls) == 2
    sent_text = str(context.actions)
    assert "new summary" in sent_text
    assert "old summary" not in sent_text


def test_runner_injects_default_image_artifact_prompt(tmp_path: Path):
    context = FakeContext(tmp_path)
    config = load_plugin_config(context)
    runner = CodexRunner(config, tmp_path / "outputs")
    artifact_dir = tmp_path / "session" / "aaa" / "jobs" / "job-0001" / "artifacts"

    args = runner._build_args(  # noqa: SLF001 - regression coverage for CLI prompt transport.
        tmp_path,
        "画一张图",
        None,
        tmp_path / "out.txt",
        artifact_dir,
    )
    prompt = runner._prompt_with_artifact_instruction(  # noqa: SLF001
        "画一张图",
        artifact_dir,
    )

    assert args[-1] == "-"
    assert "画一张图" not in args
    assert prompt.startswith("画一张图")
    assert "Codex 插件默认图片输出约定" in prompt
    assert "generated_images" in prompt
    assert artifact_dir.resolve().as_posix() in prompt


@pytest.mark.asyncio
async def test_runner_sends_multiline_prompt_via_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    context = FakeContext(tmp_path)
    config = load_plugin_config(context)
    runner = CodexRunner(config, tmp_path / "outputs")
    captured: dict[str, Any] = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, input: bytes | None = None):
            captured["stdin"] = input.decode("utf-8") if input else ""
            output_path = Path(captured["args"][captured["args"].index("-o") + 1])
            output_path.write_text("## 2026-05-19\nsummary", encoding="utf-8")
            stdout = b'{"type":"thread.started","thread_id":"thread-1"}\n'
            return stdout, b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = list(args)
        captured["stdin_pipe"] = kwargs.get("stdin")
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    prompt = "## 2026-05-19\nhttps://arxiv.org/abs/2605.16917\nhttps://arxiv.org/abs/2605.18050"
    result = await runner.run(
        cwd=tmp_path,
        prompt=prompt,
        thread_id="thread-old",
        job=SimpleNamespace(label="astro-ph", cancel_requested=False),
        artifact_dir=None,
    )

    assert captured["args"][-1] == "-"
    assert prompt not in captured["args"]
    assert captured["stdin_pipe"] == asyncio.subprocess.PIPE
    assert captured["stdin"].startswith(prompt)
    assert "https://arxiv.org/abs/2605.18050" in captured["stdin"]
    assert result.final_text == "## 2026-05-19\nsummary"
