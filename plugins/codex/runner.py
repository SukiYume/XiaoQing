from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import CodexPluginConfig


@dataclass
class CodexRunResult:
    exit_code: int | None
    thread_id: str | None
    final_text: str
    stdout_tail: str
    stderr_tail: str
    timed_out: bool = False
    cancelled: bool = False


def _tail(text: str, limit: int = 1600) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def _parse_json_events(stdout: str) -> tuple[str | None, list[str], dict[str, Any] | None]:
    thread_id: str | None = None
    messages: list[str] = []
    usage: dict[str, Any] | None = None
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")
        if event_type == "thread.started":
            thread_id = event.get("thread_id") or thread_id
        elif event_type == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                messages.append(str(item["text"]))
        elif event_type == "turn.completed":
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else usage
    return thread_id, messages, usage


async def terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if sys.platform == "win32":
        await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        if process.returncode is None:
            process.kill()
            await process.wait()


class CodexRunner:
    def __init__(self, config: CodexPluginConfig, output_dir: Path):
        self.config = config
        self.output_dir = output_dir

    def _codex_bin(self) -> str:
        configured = self.config.codex_bin
        found = shutil.which(configured)
        return found or configured

    def _base_args(self, cwd: Path) -> list[str]:
        args = [
            self._codex_bin(),
            "-C",
            str(cwd),
            "--sandbox",
            self.config.sandbox,
        ]
        if self.config.approval_policy:
            args.extend(["-c", f"approval_policy='{self.config.approval_policy}'"])
        return args

    def _build_args(self, cwd: Path, prompt: str, thread_id: str | None, output_path: Path) -> list[str]:
        args = self._base_args(cwd)
        if thread_id:
            args.extend(["exec", "resume", thread_id, "--json"])
        else:
            args.extend(["exec", "--json"])
        if self.config.skip_git_repo_check:
            args.append("--skip-git-repo-check")
        args.extend(["-o", str(output_path), prompt])
        return args

    async def run(self, *, cwd: Path, prompt: str, thread_id: str | None, job: Any) -> CodexRunResult:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".txt",
            prefix="codex-last-",
            dir=self.output_dir,
            delete=False,
        ) as tmp:
            output_path = Path(tmp.name)

        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        process = await asyncio.create_subprocess_exec(
            *self._build_args(cwd, prompt, thread_id, output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        job.process = process

        timed_out = False
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=self.config.job_timeout_seconds,
            )
        except asyncio.TimeoutError:
            timed_out = True
            await terminate_process_tree(process)
            stdout_bytes, stderr_bytes = await process.communicate()

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        new_thread_id, messages, _usage = _parse_json_events(stdout)

        file_text = ""
        try:
            file_text = output_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            file_text = ""
        finally:
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass

        final_text = file_text or (messages[-1] if messages else "")
        if not final_text:
            final_text = _tail(stderr) or _tail(stdout) or "Codex 没有返回文本结果。"

        return CodexRunResult(
            exit_code=process.returncode,
            thread_id=new_thread_id or thread_id,
            final_text=final_text,
            stdout_tail=_tail(stdout),
            stderr_tail=_tail(stderr),
            timed_out=timed_out,
            cancelled=bool(getattr(job, "cancel_requested", False)),
        )
