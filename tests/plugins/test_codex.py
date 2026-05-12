from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from core.constants import MAX_MESSAGE_TEXT_LENGTH
from plugins.codex import main as codex_main
from plugins.codex.config import load_plugin_config
from plugins.codex.manager import CodexQueueManager, reset_manager_for_tests
from plugins.codex.runner import CodexRunner
from plugins.codex.runner import CodexRunResult


PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


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
    def __init__(
        self,
        *,
        result_text: str | None = None,
        artifact_name: str | None = None,
        generated_image_name: str | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.started: list[str] = []
        self.release = asyncio.Event()
        self.result_text = result_text
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
    ) -> CodexRunResult:
        self.calls.append((job.label, prompt, thread_id))
        self.started.append(job.label)
        if "block" in prompt:
            await self.release.wait()
        await asyncio.sleep(0)
        if self.artifact_name and artifact_dir is not None:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / self.artifact_name).write_bytes(PNG_BYTES)
        if self.generated_image_name:
            generated_dir = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "generated_images"
            generated_dir.mkdir(parents=True, exist_ok=True)
            (generated_dir / self.generated_image_name).write_bytes(PNG_BYTES)
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
    assert any(event.get("role") == "user" and event.get("content") == "user prompt" for event in events)
    assert any(event.get("role") == "assistant" and event.get("content") == "assistant reply" for event in events)


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


@pytest.mark.asyncio
async def test_generated_images_dir_is_used_when_result_has_no_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    context = FakeContext(tmp_path)
    runner = FakeRunner(result_text="已生成。", generated_image_name="cyberpunk.png")
    manager = _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create img", {"user_id": 1, "group_id": 2}, context)
    await codex_main.handle("codex", "img $imagegen draw", {"user_id": 1, "group_id": 2}, context)
    await manager.wait_idle()

    message = context.actions[-1]["params"]["message"]
    assert any(seg.get("type") == "image" for seg in message)
    copied = context.data_dir / "session" / "img" / "images" / "job-0001-01.png"
    assert copied.exists()


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


def test_runner_injects_default_image_artifact_prompt(tmp_path: Path):
    context = FakeContext(tmp_path)
    config = load_plugin_config(context)
    runner = CodexRunner(config, tmp_path / "outputs")
    artifact_dir = tmp_path / "session" / "aaa" / "jobs" / "job-0001" / "artifacts"

    args = runner._build_args(  # noqa: SLF001 - regression coverage for CLI prompt composition.
        tmp_path,
        "画一张图",
        None,
        tmp_path / "out.txt",
        artifact_dir,
    )

    prompt = args[-1]
    assert prompt.startswith("画一张图")
    assert "Codex 插件默认图片输出约定" in prompt
    assert "generated_images" in prompt
    assert artifact_dir.resolve().as_posix() in prompt
