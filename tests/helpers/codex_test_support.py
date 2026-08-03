"""Codex 插件测试共享 fixture、导入和私有 helper。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.constants import MAX_MESSAGE_TEXT_LENGTH
from core.interfaces import (
    DeliveryTarget,
    PluginCapabilities,
    PluginPrincipal,
    PluginSettingsSnapshot,
)
from plugins.codex import arxiv_summary as codex_arxiv_summary
from plugins.codex import main as codex_main
from plugins.codex.artifacts import CodexImageArtifact
from plugins.codex.config import load_plugin_config
from plugins.codex.manager import CodexQueueManager, reset_manager_for_tests
from plugins.codex.paths import CwdError, normalize_cwd
from plugins.codex.runner import CodexRunner, CodexRunResult, ProcessTreeTerminationResult
from tests.codex_fakes import CallbackStreamingProcess

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
        self.settings_revision = 0
        self.principal = PluginPrincipal(kind="lifecycle")
        self.capabilities = PluginCapabilities()

    def get_settings_snapshot(self) -> PluginSettingsSnapshot:
        return PluginSettingsSnapshot(
            config=self.config,
            secrets=self.secrets,
            revision=self.settings_revision,
        )

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
        block_summary: bool = False,
    ) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.started: list[str] = []
        self.release = asyncio.Event()
        self.result_text = result_text
        self.exit_code = exit_code
        self.artifact_name = artifact_name
        self.generated_image_name = generated_image_name
        self.block_summary = block_summary

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
        if "block" in prompt or (self.block_summary and "本次要总结的 arXiv 链接" in prompt):
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


class _RaceProcess(CallbackStreamingProcess):
    def __init__(self, pid: int = 43210) -> None:
        self.stdin_inputs: list[bytes] = []
        self.prompt_sent = asyncio.Event()
        self.terminated = asyncio.Event()

        async def exchange(payload: bytes) -> tuple[bytes, bytes]:
            self.stdin_inputs.append(payload)
            if payload:
                self.prompt_sent.set()
            await self.terminated.wait()
            return b"", b""

        super().__init__(exchange, pid=pid)

    def kill(self) -> None:
        self.returncode = -9
        if self.stdin_payload:
            self.prompt_sent.set()
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


def _persisted_session(label: str, cwd: Path, **updates: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "label": label,
        "cwd": str(cwd),
        "owner_user_id": 101,
        "target_group_id": 202,
        "thread_id": None,
        "created_at": time.time(),
        "updated_at": time.time(),
        "total_jobs": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
    }
    payload.update(updates)
    return payload


__all__ = (
    "Any",
    "AsyncMock",
    "CallbackStreamingProcess",
    "CodexImageArtifact",
    "CodexQueueManager",
    "CodexRunResult",
    "CodexRunner",
    "CwdError",
    "DeliveryTarget",
    "FakeContext",
    "FakeRunner",
    "MAX_MESSAGE_TEXT_LENGTH",
    "PNG_BYTES",
    "Path",
    "PluginCapabilities",
    "PluginPrincipal",
    "PluginSettingsSnapshot",
    "ProcessTreeTerminationResult",
    "SimpleNamespace",
    "_RaceProcess",
    "_arxiv_addon",
    "_install_actual_runner_manager",
    "_install_fake_manager",
    "_patch_race_termination",
    "_persisted_session",
    "_valid_arxiv_summary",
    "_wait_until",
    "asyncio",
    "base64",
    "codex_arxiv_summary",
    "codex_main",
    "json",
    "load_plugin_config",
    "normalize_cwd",
    "os",
    "pytest",
    "reset_codex_manager",
    "reset_manager_for_tests",
    "time",
)
