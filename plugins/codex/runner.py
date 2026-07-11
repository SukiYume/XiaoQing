from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .artifacts import IMAGE_EXTENSIONS, default_generated_images_dir
from .config import CodexPluginConfig

logger = logging.getLogger(__name__)


@dataclass
class CodexRunResult:
    exit_code: int | None
    thread_id: str | None
    final_text: str
    stdout_tail: str
    stderr_tail: str
    timed_out: bool = False
    cancelled: bool = False
    output_limited: bool = False
    output_path: str | None = None
    image_paths: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessTreeTerminationResult:
    tree_confirmed: bool
    parent_reaped: bool
    forced: bool = False
    helper_error: str | None = None


class OutputBudgetExceeded(RuntimeError):
    """Raised internally when a subprocess stream or output file exceeds policy."""


def _tail(text: str, limit: int = 1600) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def _looks_like_image_path(value: str) -> bool:
    lowered = value.strip().strip("\"'`").lower()
    return any(lowered.endswith(ext) for ext in IMAGE_EXTENSIONS)


def _extract_image_paths(
    value: Any,
    *,
    max_items: int = 100,
    _depth: int = 0,
) -> list[str]:
    if max_items <= 0 or _depth > 16:
        return []
    paths: list[str] = []
    if isinstance(value, str):
        if _looks_like_image_path(value):
            paths.append(value)
    elif isinstance(value, dict):
        for child in value.values():
            paths.extend(
                _extract_image_paths(
                    child,
                    max_items=max_items - len(paths),
                    _depth=_depth + 1,
                )
            )
            if len(paths) >= max_items:
                break
    elif isinstance(value, list):
        for child in value:
            paths.extend(
                _extract_image_paths(
                    child,
                    max_items=max_items - len(paths),
                    _depth=_depth + 1,
                )
            )
            if len(paths) >= max_items:
                break
    return paths


def _parse_json_events(
    stdout: str,
) -> tuple[str | None, list[str], dict[str, Any] | None, list[str]]:
    thread_id: str | None = None
    messages: list[str] = []
    usage: dict[str, Any] | None = None
    image_paths: list[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")
        image_paths.extend(_extract_image_paths(event))
        if event_type == "thread.started":
            thread_id = event.get("thread_id") or thread_id
        elif event_type == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                messages.append(str(item["text"]))
        elif event_type == "turn.completed":
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else usage
    unique_paths = list(dict.fromkeys(image_paths))
    return thread_id, messages, usage, unique_paths


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value
    clipped = encoded[: max(0, max_bytes - 32)].decode("utf-8", errors="ignore")
    return f"{clipped}\n...[output truncated]"


class _ByteTail:
    def __init__(self, limit: int = 8 * 1024) -> None:
        self.limit = max(256, int(limit))
        self.data = bytearray()

    def feed(self, chunk: bytes) -> None:
        if len(chunk) >= self.limit:
            self.data[:] = chunk[-self.limit :]
            return
        self.data.extend(chunk)
        overflow = len(self.data) - self.limit
        if overflow > 0:
            del self.data[:overflow]

    def text(self) -> str:
        return _tail(bytes(self.data).decode("utf-8", errors="replace"))


class _StdoutEventAccumulator:
    def __init__(self, config: CodexPluginConfig) -> None:
        self.config = config
        self.total_bytes = 0
        self.pending = bytearray()
        self.tail = _ByteTail()
        self.thread_id: str | None = None
        self.last_message = ""
        self.usage: dict[str, Any] = {}
        self.image_paths: list[str] = []
        self._seen_image_paths: set[str] = set()

    def feed(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        if self.total_bytes > self.config.max_stdout_bytes:
            raise OutputBudgetExceeded(f"stdout exceeded {self.config.max_stdout_bytes} bytes")
        self.tail.feed(chunk)
        self.pending.extend(chunk)
        while True:
            newline = self.pending.find(b"\n")
            if newline < 0:
                if len(self.pending) > self.config.max_json_line_bytes:
                    raise OutputBudgetExceeded(
                        f"stdout JSON line exceeded {self.config.max_json_line_bytes} bytes"
                    )
                break
            raw_line = bytes(self.pending[:newline])
            del self.pending[: newline + 1]
            self._consume_line(raw_line)

    def finish(self) -> None:
        if self.pending:
            self._consume_line(bytes(self.pending))
            self.pending.clear()

    def _consume_line(self, raw_line: bytes) -> None:
        if len(raw_line) > self.config.max_json_line_bytes:
            raise OutputBudgetExceeded(
                f"stdout JSON line exceeded {self.config.max_json_line_bytes} bytes"
            )
        line = raw_line.strip()
        if not line.startswith(b"{"):
            return
        try:
            event = json.loads(line.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeError):
            return
        if not isinstance(event, dict):
            return
        event_type = event.get("type")
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                self.thread_id = thread_id[:512]
        elif event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text:
                    self.last_message = _truncate_utf8(
                        text,
                        self.config.max_final_output_bytes,
                    )
        elif event_type == "turn.completed":
            raw_usage = event.get("usage")
            if isinstance(raw_usage, dict):
                self.usage = {
                    key: raw_usage[key]
                    for key in ("input_tokens", "cached_input_tokens", "output_tokens")
                    if isinstance(raw_usage.get(key), (int, float))
                }

        remaining = max(0, self.config.max_image_artifacts * 4 - len(self.image_paths))
        for image_path in _extract_image_paths(event, max_items=remaining):
            key = image_path.casefold()
            if key in self._seen_image_paths:
                continue
            self._seen_image_paths.add(key)
            self.image_paths.append(image_path)


class _StderrAccumulator:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.total_bytes = 0
        self.tail = _ByteTail()

    def feed(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        if self.total_bytes > self.max_bytes:
            raise OutputBudgetExceeded(f"stderr exceeded {self.max_bytes} bytes")
        self.tail.feed(chunk)


@dataclass(frozen=True)
class _StreamIoOutcome:
    timed_out: bool = False
    cancelled: bool = False
    output_limited: bool = False
    limit_reason: str = ""


@dataclass(frozen=True)
class _FinalOutputCapture:
    text: str
    archive_path: str | None = None
    limited: bool = False
    reason: str = ""


async def _read_stdout_stream(
    stream: asyncio.StreamReader,
    accumulator: _StdoutEventAccumulator,
) -> None:
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            accumulator.finish()
            return
        accumulator.feed(chunk)


async def _read_stderr_stream(
    stream: asyncio.StreamReader,
    accumulator: _StderrAccumulator,
) -> None:
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            return
        accumulator.feed(chunk)


async def _write_process_stdin(
    writer: asyncio.StreamWriter,
    payload: bytes,
) -> None:
    try:
        writer.write(payload)
        await writer.drain()
    finally:
        writer.close()
        wait_closed = getattr(writer, "wait_closed", None)
        if callable(wait_closed):
            try:
                await wait_closed()
            except (BrokenPipeError, ConnectionError, RuntimeError):
                pass


async def _monitor_output_file(path: Path, max_bytes: int) -> None:
    while True:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > max_bytes:
            raise OutputBudgetExceeded(f"final output exceeded {max_bytes} bytes")
        await asyncio.sleep(0.1)


def _has_streaming_pipes(process: asyncio.subprocess.Process) -> bool:
    stdin = getattr(process, "stdin", None)
    stdout = getattr(process, "stdout", None)
    stderr = getattr(process, "stderr", None)
    return (
        callable(getattr(stdin, "write", None))
        and callable(getattr(stdin, "drain", None))
        and callable(getattr(stdout, "read", None))
        and callable(getattr(stderr, "read", None))
    )


def _qq_preview(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    marker = "\n\n...[QQ preview truncated]...\n\n"
    available = max(0, max_chars - len(marker))
    head_length = available * 2 // 3
    tail_length = available - head_length
    return f"{text[:head_length]}{marker}{text[-tail_length:] if tail_length else ''}"


def _archive_destination(output_dir: Path, job: Any) -> Path:
    label = (
        "".join(
            character
            for character in str(getattr(job, "label", "job"))
            if character.isascii() and (character.isalnum() or character in "_-")
        )[:32]
        or "job"
    )
    job_id = int(getattr(job, "job_id", 0) or 0)
    descriptor, name = tempfile.mkstemp(
        prefix=f"codex-{label}-job-{job_id:04d}-",
        suffix=".txt",
        dir=output_dir,
    )
    os.close(descriptor)
    destination = Path(name)
    destination.unlink(missing_ok=True)
    return destination


def _bounded_file_bytes(path: Path, max_bytes: int) -> tuple[bytes, int]:
    """Read at most max_bytes, keeping both the beginning and the end."""

    size = path.stat().st_size
    if size <= max_bytes:
        with path.open("rb") as handle:
            return handle.read(max_bytes + 1)[:max_bytes], size

    marker = (
        f"\n\n...[file size {size} bytes; bytes omitted to enforce "
        f"the {max_bytes}-byte limit]...\n\n"
    ).encode()
    payload_budget = max(0, max_bytes - len(marker))
    head_size = payload_budget * 2 // 3
    tail_size = payload_budget - head_size
    with path.open("rb") as handle:
        head = handle.read(head_size)
        if tail_size:
            handle.seek(max(0, size - tail_size))
            tail = handle.read(tail_size)
        else:
            tail = b""
    return head + marker + tail, size


def _capture_final_output(
    path: Path,
    *,
    output_dir: Path,
    job: Any,
    config: CodexPluginConfig,
) -> _FinalOutputCapture:
    try:
        size = path.stat().st_size
    except OSError:
        return _FinalOutputCapture(text="")
    if size <= 0:
        return _FinalOutputCapture(text="")

    payload, observed_size = _bounded_file_bytes(path, config.max_final_output_bytes)
    decoded = (
        payload.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n").strip()
    )
    exceeds_disk_budget = observed_size > config.max_final_output_bytes
    exceeds_qq_budget = len(decoded) > config.max_qq_text_chars
    if not exceeds_disk_budget and not exceeds_qq_budget:
        return _FinalOutputCapture(text=decoded)

    destination = _archive_destination(output_dir, job)
    if exceeds_disk_budget:
        archive_payload = decoded.encode("utf-8", errors="replace")
        if len(archive_payload) > config.max_final_output_bytes:
            archive_payload = (
                archive_payload[: config.max_final_output_bytes]
                .decode(
                    "utf-8",
                    errors="ignore",
                )
                .encode("utf-8")
            )
        destination.write_bytes(archive_payload)
        path.unlink(missing_ok=True)
        reason = (
            f"final output was {observed_size} bytes; archived a bounded "
            f"{len(archive_payload)}-byte head/tail capture"
        )
    else:
        os.replace(path, destination)
        reason = (
            f"final output exceeded the QQ preview limit of {config.max_qq_text_chars} characters"
        )
    preview = _qq_preview(decoded, config.max_qq_text_chars)
    notice = f"\n\n[完整/受控输出已保存到: {destination.resolve()}]"
    preview_budget = max(1, config.max_qq_text_chars - len(notice))
    return _FinalOutputCapture(
        text=f"{_qq_preview(preview, preview_budget)}{notice}",
        archive_path=str(destination.resolve()),
        limited=True,
        reason=reason,
    )


def _archive_large_message(
    text: str,
    *,
    output_dir: Path,
    job: Any,
    config: CodexPluginConfig,
) -> _FinalOutputCapture:
    text = text.strip()
    encoded = text.encode("utf-8", errors="replace")
    limited_bytes = encoded[: config.max_final_output_bytes]
    bounded_text = limited_bytes.decode("utf-8", errors="ignore")
    if len(text) <= config.max_qq_text_chars and len(encoded) <= config.max_final_output_bytes:
        return _FinalOutputCapture(text=text)

    destination = _archive_destination(output_dir, job)
    destination.write_bytes(limited_bytes)
    notice = f"\n\n[受控输出已保存到: {destination.resolve()}]"
    preview_budget = max(1, config.max_qq_text_chars - len(notice))
    return _FinalOutputCapture(
        text=f"{_qq_preview(bounded_text, preview_budget)}{notice}",
        archive_path=str(destination.resolve()),
        limited=True,
        reason="agent message exceeded a configured output budget",
    )


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_for_parent(
    process: asyncio.subprocess.Process,
    timeout_seconds: float,
) -> bool:
    if process.returncode is not None:
        return True
    try:
        await asyncio.wait_for(process.wait(), timeout=max(0.01, timeout_seconds))
    except asyncio.TimeoutError:
        return process.returncode is not None
    return True


async def _terminate_windows_process_tree(
    process: asyncio.subprocess.Process,
    *,
    helper_timeout_seconds: float,
    kill_timeout_seconds: float,
) -> ProcessTreeTerminationResult:
    helper_error: str | None = None
    helper_succeeded = False
    helper: asyncio.subprocess.Process | None = None
    try:
        helper = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            ),
            timeout=max(0.01, helper_timeout_seconds),
        )
        try:
            helper_code = await asyncio.wait_for(
                helper.wait(),
                timeout=max(0.01, helper_timeout_seconds),
            )
        except asyncio.TimeoutError:
            helper_error = "taskkill timed out"
            if helper.returncode is None:
                helper.kill()
                await _wait_for_parent(helper, kill_timeout_seconds)
        else:
            helper_succeeded = helper_code == 0
            if not helper_succeeded:
                helper_error = f"taskkill exited with code {helper_code}"
    except asyncio.TimeoutError:
        helper_error = "taskkill spawn timed out"
    except (OSError, RuntimeError) as exc:
        helper_error = f"taskkill failed: {exc}"

    parent_reaped = await _wait_for_parent(process, kill_timeout_seconds)
    forced = False
    if not parent_reaped:
        forced = True
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            pass
        parent_reaped = await _wait_for_parent(process, kill_timeout_seconds)

    return ProcessTreeTerminationResult(
        tree_confirmed=helper_succeeded,
        parent_reaped=parent_reaped,
        forced=forced,
        helper_error=helper_error,
    )


async def _terminate_posix_process_tree(
    process: asyncio.subprocess.Process,
    *,
    term_grace_seconds: float,
    kill_timeout_seconds: float,
) -> ProcessTreeTerminationResult:
    process_group_id = process.pid
    forced = False
    parent_wait_task = asyncio.create_task(process.wait()) if process.returncode is None else None

    async def finish_parent_wait() -> bool:
        if parent_wait_task is None:
            return True
        try:
            await asyncio.wait_for(
                asyncio.shield(parent_wait_task),
                timeout=max(0.01, kill_timeout_seconds),
            )
        except asyncio.TimeoutError:
            parent_wait_task.cancel()
            await asyncio.gather(parent_wait_task, return_exceptions=True)
            return process.returncode is not None
        finally:
            if parent_wait_task.done():
                try:
                    parent_wait_task.result()
                except (asyncio.CancelledError, OSError, RuntimeError):
                    pass
        return True

    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        return ProcessTreeTerminationResult(
            tree_confirmed=False,
            parent_reaped=await finish_parent_wait(),
            helper_error=f"SIGTERM permission denied: {exc}",
        )

    loop = asyncio.get_running_loop()
    term_deadline = loop.time() + max(0.0, term_grace_seconds)
    while _process_group_exists(process_group_id) and loop.time() < term_deadline:
        await asyncio.sleep(0.05)

    if _process_group_exists(process_group_id):
        forced = True
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            return ProcessTreeTerminationResult(
                tree_confirmed=False,
                parent_reaped=await finish_parent_wait(),
                forced=True,
                helper_error=f"SIGKILL permission denied: {exc}",
            )

    kill_deadline = loop.time() + max(0.01, kill_timeout_seconds)
    while _process_group_exists(process_group_id) and loop.time() < kill_deadline:
        await asyncio.sleep(0.05)
    parent_reaped = await finish_parent_wait()
    if parent_wait_task is not None and not parent_wait_task.done():
        parent_wait_task.cancel()
        await asyncio.gather(parent_wait_task, return_exceptions=True)
    return ProcessTreeTerminationResult(
        tree_confirmed=not _process_group_exists(process_group_id),
        parent_reaped=parent_reaped,
        forced=forced,
    )


async def terminate_process_tree(
    process: asyncio.subprocess.Process,
    *,
    term_grace_seconds: float = 5,
    kill_timeout_seconds: float = 5,
    helper_timeout_seconds: float = 10,
) -> ProcessTreeTerminationResult:
    """Terminate the complete process tree with bounded platform-specific waits."""

    if sys.platform == "win32":
        return await _terminate_windows_process_tree(
            process,
            helper_timeout_seconds=helper_timeout_seconds,
            kill_timeout_seconds=kill_timeout_seconds,
        )
    return await _terminate_posix_process_tree(
        process,
        term_grace_seconds=term_grace_seconds,
        kill_timeout_seconds=kill_timeout_seconds,
    )


def _close_process_pipe_transports(process: asyncio.subprocess.Process) -> None:
    stdin = getattr(process, "stdin", None)
    if stdin is not None:
        try:
            stdin.close()
        except (AttributeError, RuntimeError):
            pass
    transport = getattr(process, "_transport", None)
    get_pipe_transport = getattr(transport, "get_pipe_transport", None)
    if not callable(get_pipe_transport):
        return
    for descriptor in (0, 1, 2):
        try:
            pipe_transport = get_pipe_transport(descriptor)
            if pipe_transport is not None:
                pipe_transport.close()
        except (AttributeError, RuntimeError):
            continue


async def _drain_process_after_termination(
    process: asyncio.subprocess.Process,
    *,
    timeout_seconds: float = 5,
) -> tuple[bytes, bytes]:
    try:
        return await asyncio.wait_for(
            process.communicate(),
            timeout=max(0.01, timeout_seconds),
        )
    except (asyncio.TimeoutError, RuntimeError):
        _close_process_pipe_transports(process)
        return b"", b""


async def _terminate_and_drain_process(
    process: asyncio.subprocess.Process,
) -> tuple[ProcessTreeTerminationResult, bytes, bytes]:
    termination = await terminate_process_tree(process)
    if not termination.tree_confirmed or not termination.parent_reaped:
        logger.error(
            "Codex process tree termination was not fully confirmed: pid=%s result=%s",
            getattr(process, "pid", None),
            termination,
        )
    stdout, stderr = await _drain_process_after_termination(process)
    return termination, stdout, stderr


def _consume_task_exception(task: asyncio.Task[Any], *, name: str) -> BaseException | None:
    if task.cancelled():
        return None
    try:
        return task.exception()
    except asyncio.CancelledError:
        return None


async def _settle_stream_tasks(
    process: asyncio.subprocess.Process,
    tasks: dict[str, asyncio.Task[Any]],
    *,
    timeout_seconds: float = 5,
) -> list[tuple[str, BaseException]]:
    pending = {task for task in tasks.values() if not task.done()}
    if pending:
        _, still_pending = await asyncio.wait(
            pending,
            timeout=max(0.01, timeout_seconds),
        )
        if still_pending:
            termination = await terminate_process_tree(process)
            if not termination.tree_confirmed or not termination.parent_reaped:
                logger.error(
                    "Codex process left pipe tasks open after parent exit: pid=%s result=%s",
                    getattr(process, "pid", None),
                    termination,
                )
            _close_process_pipe_transports(process)
            for task in still_pending:
                task.cancel()
            await asyncio.gather(*still_pending, return_exceptions=True)

    failures: list[tuple[str, BaseException]] = []
    for name, task in tasks.items():
        error = _consume_task_exception(task, name=name)
        if error is not None:
            failures.append((name, error))
    return failures


async def _terminate_streaming_process(
    process: asyncio.subprocess.Process,
    tasks: dict[str, asyncio.Task[Any]],
) -> list[tuple[str, BaseException]]:
    for name in ("stdin", "monitor", "cancel"):
        task = tasks.get(name)
        if task is not None and not task.done():
            task.cancel()
    termination = await terminate_process_tree(process)
    if not termination.tree_confirmed or not termination.parent_reaped:
        logger.error(
            "Codex streaming process tree termination was not fully confirmed: pid=%s result=%s",
            getattr(process, "pid", None),
            termination,
        )
    return await _settle_stream_tasks(process, tasks)


async def _run_streaming_io(
    process: asyncio.subprocess.Process,
    *,
    prompt_payload: bytes,
    output_path: Path,
    cancel_event: asyncio.Event | None,
    config: CodexPluginConfig,
) -> tuple[_StdoutEventAccumulator, _StderrAccumulator, _StreamIoOutcome]:
    """Drive subprocess pipes concurrently while enforcing every hard budget."""

    stdout = process.stdout
    stderr = process.stderr
    stdin = process.stdin
    if stdout is None or stderr is None or stdin is None:
        raise RuntimeError("streaming Codex process is missing a configured pipe")

    stdout_accumulator = _StdoutEventAccumulator(config)
    stderr_accumulator = _StderrAccumulator(config.max_stderr_bytes)
    tasks: dict[str, asyncio.Task[Any]] = {
        "stdout": asyncio.create_task(
            _read_stdout_stream(stdout, stdout_accumulator),
            name="codex-stdout-reader",
        ),
        "stderr": asyncio.create_task(
            _read_stderr_stream(stderr, stderr_accumulator),
            name="codex-stderr-reader",
        ),
        "stdin": asyncio.create_task(
            _write_process_stdin(stdin, prompt_payload),
            name="codex-stdin-writer",
        ),
        "wait": asyncio.create_task(process.wait(), name="codex-process-wait"),
        "monitor": asyncio.create_task(
            _monitor_output_file(output_path, config.max_final_output_bytes),
            name="codex-output-monitor",
        ),
    }
    if cancel_event is not None:
        tasks["cancel"] = asyncio.create_task(
            cancel_event.wait(),
            name="codex-cancel-wait",
        )

    deadline = asyncio.get_running_loop().time() + config.job_timeout_seconds
    outcome = _StreamIoOutcome()
    termination_required = False
    try:
        while not tasks["wait"].done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                outcome = _StreamIoOutcome(timed_out=True)
                termination_required = True
                break
            active = {task for task in tasks.values() if not task.done()}
            if not active:
                break
            done, _ = await asyncio.wait(
                active,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                outcome = _StreamIoOutcome(timed_out=True)
                termination_required = True
                break

            if "cancel" in tasks and tasks["cancel"] in done:
                outcome = _StreamIoOutcome(cancelled=True)
                termination_required = True

            for name in ("stdout", "stderr", "monitor", "stdin"):
                task = tasks[name]
                if task not in done:
                    continue
                error = _consume_task_exception(task, name=name)
                if error is None:
                    continue
                if isinstance(error, OutputBudgetExceeded):
                    outcome = _StreamIoOutcome(
                        output_limited=True,
                        limit_reason=str(error),
                    )
                    termination_required = True
                    break
                if name == "stdin" and isinstance(
                    error,
                    (BrokenPipeError, ConnectionError),
                ):
                    continue
                raise error
            if termination_required:
                break

        if termination_required:
            failures = await _terminate_streaming_process(process, tasks)
        else:
            monitor_task = tasks["monitor"]
            if not monitor_task.done():
                monitor_task.cancel()
            cancel_task = tasks.get("cancel")
            if cancel_task is not None and not cancel_task.done():
                cancel_task.cancel()
            failures = await _settle_stream_tasks(process, tasks)

        for name, error in failures:
            if isinstance(error, OutputBudgetExceeded):
                if not outcome.output_limited:
                    outcome = _StreamIoOutcome(
                        output_limited=True,
                        limit_reason=str(error),
                    )
                    if not termination_required:
                        await terminate_process_tree(process)
                continue
            if name in {"stdin", "cancel", "monitor"} and isinstance(
                error,
                (asyncio.CancelledError, BrokenPipeError, ConnectionError),
            ):
                continue
            if name in {"monitor", "cancel"} and isinstance(error, asyncio.CancelledError):
                continue
            raise error
        return stdout_accumulator, stderr_accumulator, outcome
    except asyncio.CancelledError:
        for name in ("stdin", "monitor", "cancel"):
            task = tasks.get(name)
            if task is not None and not task.done():
                task.cancel()
        await asyncio.shield(_terminate_streaming_process(process, tasks))
        raise
    finally:
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


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

    def _prompt_with_artifact_instruction(self, prompt: str, artifact_dir: Path | None) -> str:
        if artifact_dir is None:
            return prompt
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_dir.resolve().as_posix()
        generated_images_path = default_generated_images_dir().resolve().as_posix()
        return (
            f"{prompt.rstrip()}\n\n"
            "[Codex 插件默认图片输出约定]\n"
            f"如果本次任务生成、导出、截图或保存图片，请把图片文件保存到这个目录: {artifact_path}\n"
            f"内置 imagegen 工具默认可能先保存到 {generated_images_path}；如果图片在那边，请在最终回复前复制到上述目录。\n"
            "最终回复里请用 Markdown 图片语法 `![说明](图片路径)`，或单独一行 `图片: 图片路径` 标出每张图片。\n"
            "如果本次任务没有生成图片，忽略这段约定。"
        )

    def _build_args(
        self,
        cwd: Path,
        prompt: str,
        thread_id: str | None,
        output_path: Path,
        artifact_dir: Path | None = None,
    ) -> list[str]:
        args = self._base_args(cwd)
        if thread_id:
            args.extend(["exec", "resume", thread_id, "--json"])
        else:
            args.extend(["exec", "--json"])
        if self.config.skip_git_repo_check:
            args.append("--skip-git-repo-check")
        args.extend(["-o", str(output_path), "-"])
        return args

    async def run(
        self,
        *,
        cwd: Path,
        prompt: str,
        thread_id: str | None,
        job: Any,
        artifact_dir: Path | None = None,
        process_handoff: Callable[
            [asyncio.subprocess.Process | None],
            Awaitable[bool],
        ]
        | None = None,
        prompt_handoff: Callable[[], Awaitable[bool]] | None = None,
    ) -> CodexRunResult:
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

        prompt_payload = self._prompt_with_artifact_instruction(prompt, artifact_dir)
        args = self._build_args(cwd, prompt, thread_id, output_path, artifact_dir)
        logger.info(
            "Starting Codex CLI: label=%s thread=%s cwd=%s prompt_chars=%d prompt_lines=%d",
            getattr(job, "label", "?"),
            thread_id or "new",
            cwd,
            len(prompt_payload),
            len(prompt_payload.splitlines()),
        )
        try:
            try:
                process = await asyncio.wait_for(
                    asyncio.create_subprocess_exec(
                        *args,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        **kwargs,
                    ),
                    timeout=self.config.spawn_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    f"Codex process spawn timed out after {self.config.spawn_timeout_seconds}s"
                ) from exc

            try:
                if process_handoff is None:
                    job.process = process
                    may_continue = not bool(getattr(job, "cancel_requested", False))
                else:
                    may_continue = await process_handoff(process)
            except BaseException:
                await _terminate_and_drain_process(process)
                raise
            if not may_continue:
                await _terminate_and_drain_process(process)
                return CodexRunResult(
                    exit_code=process.returncode,
                    thread_id=thread_id,
                    final_text="任务在 Codex 进程登记后、prompt 发送前已取消。",
                    stdout_tail="",
                    stderr_tail="",
                    cancelled=True,
                )

            try:
                if prompt_handoff is None:
                    may_send_prompt = not bool(getattr(job, "cancel_requested", False))
                    if may_send_prompt:
                        job.prompt_started = True
                else:
                    may_send_prompt = await prompt_handoff()
            except BaseException:
                await _terminate_and_drain_process(process)
                raise
            if not may_send_prompt:
                await _terminate_and_drain_process(process)
                return CodexRunResult(
                    exit_code=process.returncode,
                    thread_id=thread_id,
                    final_text="任务在 prompt 发送前已取消。",
                    stdout_tail="",
                    stderr_tail="",
                    cancelled=True,
                )

            cancel_event = getattr(job, "cancel_event", None)
            typed_cancel_event = cancel_event if isinstance(cancel_event, asyncio.Event) else None
            timed_out = False
            cancelled_while_running = False
            output_limited = False
            limit_reason = ""
            new_thread_id: str | None = None
            message_text = ""
            usage: dict[str, Any] = {}
            image_paths: list[str] = []
            stdout_tail = ""
            stderr_tail = ""

            if _has_streaming_pipes(process):
                stdout_accumulator, stderr_accumulator, io_outcome = await _run_streaming_io(
                    process,
                    prompt_payload=prompt_payload.encode("utf-8"),
                    output_path=output_path,
                    cancel_event=typed_cancel_event,
                    config=self.config,
                )
                timed_out = io_outcome.timed_out
                cancelled_while_running = io_outcome.cancelled
                output_limited = io_outcome.output_limited
                limit_reason = io_outcome.limit_reason
                new_thread_id = stdout_accumulator.thread_id
                message_text = stdout_accumulator.last_message
                usage = stdout_accumulator.usage
                image_paths = stdout_accumulator.image_paths
                stdout_tail = stdout_accumulator.tail.text()
                stderr_tail = stderr_accumulator.tail.text()
            else:
                # Compatibility path for lightweight unit-test doubles. Real subprocesses
                # created above always expose StreamReader/StreamWriter pipes.
                communicate_task = asyncio.create_task(
                    process.communicate(prompt_payload.encode("utf-8"))
                )
                cancel_wait_task = (
                    asyncio.create_task(typed_cancel_event.wait())
                    if typed_cancel_event is not None
                    else None
                )
                try:
                    waiters = {communicate_task}
                    if cancel_wait_task is not None:
                        waiters.add(cancel_wait_task)
                    done, _ = await asyncio.wait(
                        waiters,
                        timeout=self.config.job_timeout_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if communicate_task in done:
                        stdout_bytes, stderr_bytes = communicate_task.result()
                    else:
                        cancelled_while_running = bool(
                            cancel_wait_task is not None and cancel_wait_task in done
                        )
                        timed_out = not cancelled_while_running
                        communicate_task.cancel()
                        await asyncio.gather(communicate_task, return_exceptions=True)
                        _, stdout_bytes, stderr_bytes = await _terminate_and_drain_process(process)
                except asyncio.CancelledError:
                    communicate_task.cancel()
                    await asyncio.gather(communicate_task, return_exceptions=True)
                    await asyncio.shield(_terminate_and_drain_process(process))
                    raise
                finally:
                    if cancel_wait_task is not None and not cancel_wait_task.done():
                        cancel_wait_task.cancel()
                    if cancel_wait_task is not None:
                        await asyncio.gather(cancel_wait_task, return_exceptions=True)

                if len(stdout_bytes) > self.config.max_stdout_bytes:
                    output_limited = True
                    limit_reason = f"stdout exceeded {self.config.max_stdout_bytes} bytes"
                elif len(stderr_bytes) > self.config.max_stderr_bytes:
                    output_limited = True
                    limit_reason = f"stderr exceeded {self.config.max_stderr_bytes} bytes"
                stdout_bytes = stdout_bytes[-self.config.max_stdout_bytes :]
                stderr_bytes = stderr_bytes[-self.config.max_stderr_bytes :]
                stdout_text = stdout_bytes.decode("utf-8", errors="replace")
                stderr_text = stderr_bytes.decode("utf-8", errors="replace")
                new_thread_id, messages, parsed_usage, image_paths = _parse_json_events(stdout_text)
                message_text = messages[-1] if messages else ""
                usage = parsed_usage or {}
                stdout_tail = _tail(stdout_text)
                stderr_tail = _tail(stderr_text)

            file_capture = await asyncio.to_thread(
                _capture_final_output,
                output_path,
                output_dir=self.output_dir,
                job=job,
                config=self.config,
            )
            output_limited = output_limited or file_capture.limited
            limit_reason = limit_reason or file_capture.reason

            result_capture = file_capture
            if not result_capture.text and message_text:
                result_capture = await asyncio.to_thread(
                    _archive_large_message,
                    message_text,
                    output_dir=self.output_dir,
                    job=job,
                    config=self.config,
                )
                output_limited = output_limited or result_capture.limited
                limit_reason = limit_reason or result_capture.reason

            if output_limited and limit_reason:
                budget_notice = f"Codex output budget exceeded: {limit_reason}."
                if result_capture.text:
                    final_text = f"{budget_notice}\n\n{result_capture.text}"
                else:
                    final_text = budget_notice
            else:
                final_text = result_capture.text
            if not final_text:
                if cancelled_while_running:
                    final_text = "Codex 任务已取消。"
                elif timed_out:
                    final_text = "Codex 执行超时。"
                else:
                    final_text = stderr_tail or stdout_tail or "Codex 没有返回文本结果。"
            final_text = _qq_preview(final_text, self.config.max_qq_text_chars)

            return CodexRunResult(
                exit_code=process.returncode,
                thread_id=new_thread_id or thread_id,
                final_text=final_text,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                timed_out=timed_out,
                cancelled=(
                    cancelled_while_running or bool(getattr(job, "cancel_requested", False))
                ),
                output_limited=output_limited,
                output_path=result_capture.archive_path,
                image_paths=image_paths,
                usage=usage,
            )
        finally:
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass
