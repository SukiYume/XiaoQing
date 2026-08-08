"""有界执行 Codex CLI，并管理流式输出、进程树和临时文件。"""

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
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, TypeVar, cast

from core.plugin_base import head_tail_preview

from .artifacts import IMAGE_EXTENSIONS
from .config import CodexPluginConfig

logger = logging.getLogger(__name__)
_T = TypeVar("_T")
_USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens")
_MAX_THREAD_ID_CHARS = 512
_MAX_EVENT_COUNTER = 2**63 - 1


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
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessTreeTerminationResult:
    tree_confirmed: bool
    parent_reaped: bool
    forced: bool = False
    helper_error: str | None = None


class OutputBudgetExceeded(RuntimeError):
    """子进程输出流或结果文件超过预算时使用的内部异常。"""


def _tail(text: str, limit: int = 1600) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


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
        cleaned = value.strip().strip("\"'`").strip()
        lowered = cleaned.lower()
        if (
            cleaned
            and len(cleaned) <= 32_768
            and not any(ord(char) < 32 for char in cleaned)
            and any(lowered.endswith(ext) for ext in IMAGE_EXTENSIONS)
        ):
            paths.append(cleaned)
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


def _valid_thread_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if (
        not cleaned
        or len(cleaned) > _MAX_THREAD_ID_CHARS
        or any(ord(char) < 32 for char in cleaned)
    ):
        return None
    return cleaned


def _valid_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    usage = {
        key: item
        for key in _USAGE_KEYS
        if type(item := value.get(key)) is int and 0 <= item <= _MAX_EVENT_COUNTER
    }
    return usage or None


@dataclass(frozen=True)
class _ParsedCodexEvent:
    thread_id: str | None = None
    message: str | None = None
    usage: dict[str, int] | None = None
    image_paths: list[str] = field(default_factory=list)


def _parse_codex_event(
    event: Any,
    *,
    max_message_bytes: int,
    max_image_paths: int,
) -> _ParsedCodexEvent | None:
    if not isinstance(event, dict):
        return None

    event_type = event.get("type")
    thread_id = _valid_thread_id(event.get("thread_id")) if event_type == "thread.started" else None
    message = None
    if event_type == "item.completed":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text:
                message = _truncate_utf8(text, max_message_bytes)
    usage = _valid_usage(event.get("usage")) if event_type == "turn.completed" else None
    return _ParsedCodexEvent(
        thread_id=thread_id,
        message=message,
        usage=usage,
        image_paths=_extract_image_paths(event, max_items=max_image_paths),
    )


@dataclass
class _EventSummary:
    thread_id: str | None = None
    last_message: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    image_paths: list[str] = field(default_factory=list)
    seen_image_paths: set[str] = field(default_factory=set, repr=False)

    def apply(self, event: _ParsedCodexEvent) -> None:
        if event.thread_id is not None:
            self.thread_id = event.thread_id
        if event.message is not None:
            self.last_message = event.message
        if event.usage is not None:
            self.usage = event.usage
        for image_path in event.image_paths:
            key = image_path.casefold()
            if key not in self.seen_image_paths:
                self.seen_image_paths.add(key)
                self.image_paths.append(image_path)


def _truncate_utf8(value: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value
    marker = b"\n...[output truncated]"
    if max_bytes <= len(marker):
        return marker[:max_bytes].decode("utf-8", errors="ignore")
    clipped = encoded[: max_bytes - len(marker)].decode("utf-8", errors="ignore")
    return f"{clipped}{marker.decode()}"


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
        self.summary = _EventSummary()

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
        parsed = _parse_codex_event(
            event,
            max_message_bytes=self.config.max_final_output_bytes,
            max_image_paths=max(
                0,
                self.config.max_image_artifacts * 4 - len(self.summary.image_paths),
            ),
        )
        if parsed is not None:
            self.summary.apply(parsed)


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


@dataclass(frozen=True)
class _OwnedTaskOutcome(Generic[_T]):
    result: _T | None = None
    error: BaseException | None = None
    cancellation: asyncio.CancelledError | None = None


@dataclass
class _RunResources:
    output_path: Path | None = None
    process: asyncio.subprocess.Process | None = None
    process_cleanup_required: bool = False
    result_committed: bool = False
    orphan_archives: set[Path] = field(default_factory=set)


@dataclass
class _CodexExecution:
    timed_out: bool = False
    cancelled: bool = False
    output_limited: bool = False
    limit_reason: str = ""
    thread_id: str | None = None
    message_text: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    image_paths: list[str] = field(default_factory=list)
    stdout_tail: str = ""
    stderr_tail: str = ""
    forced_stop: bool = False


async def _settle_owned_task(task: asyncio.Task[_T]) -> _OwnedTaskOutcome[_T]:
    """等待自有任务结束，并单独保留调用者的取消状态。"""

    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    try:
        result = task.result()
    except BaseException as exc:  # 把任务失败交给调用方按资源顺序统一处理。
        return _OwnedTaskOutcome(error=exc, cancellation=cancellation)
    return _OwnedTaskOutcome(result=result, cancellation=cancellation)


async def _run_cleanup_cancellation_resistant(
    awaitable: Coroutine[Any, Any, _T],
) -> _T:
    """先完整执行清理，再恢复取消语义或抛出清理失败。"""

    task = asyncio.create_task(awaitable, name="codex-resource-cleanup")
    outcome = await _settle_owned_task(task)
    if outcome.cancellation is not None:
        if outcome.error is not None:
            logger.warning(
                "Codex resource cleanup also failed during cancellation: error_type=%s",
                type(outcome.error).__name__,
            )
        raise outcome.cancellation
    if outcome.error is not None:
        raise outcome.error
    return cast(_T, outcome.result)


async def _remove_output_path_with_retry(
    path: Path,
    *,
    attempts: int = 5,
    delay_seconds: float = 0.05,
) -> bool:
    """等待 Windows 句柄关闭后，删除临时或已放弃的输出文件。"""

    attempts = max(1, int(attempts))
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(delay_seconds * (attempt + 1))
    logger.warning(
        "Failed to remove Codex output after bounded retries: name=%s error_type=%s",
        path.name,
        type(last_error).__name__ if last_error is not None else "OSError",
    )
    return False


def _remove_partial_archive(path: Path) -> None:
    """尽力回滚从未发布到结果中的半成品归档。"""

    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "Failed to roll back partial Codex archive: name=%s error_type=%s",
            path.name,
            type(exc).__name__,
        )


async def _capture_in_thread(
    function: Callable[..., _FinalOutputCapture],
    /,
    *args: Any,
    orphan_archives: set[Path],
    **kwargs: Any,
) -> _FinalOutputCapture:
    """在线程中执行有界文件读取，避免取消与 Windows 删除文件形成竞态。"""

    task = asyncio.create_task(
        asyncio.to_thread(function, *args, **kwargs),
        name="codex-output-capture",
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        outcome = await _settle_owned_task(task)
        if outcome.error is not None:
            logger.warning(
                "Codex output capture failed while cancellation was settling: error_type=%s",
                type(outcome.error).__name__,
            )
        else:
            capture = outcome.result
            assert capture is not None
            if capture.archive_path:
                orphan_archives.add(Path(capture.archive_path))
        raise cancellation


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
    delay = 0.1
    while True:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > max_bytes:
            raise OutputBudgetExceeded(f"final output exceeded {max_bytes} bytes")
        await asyncio.sleep(delay)
        delay = min(2.0, delay * 2)


def _qq_preview(text: str, max_chars: int) -> str:
    text = text.strip()
    marker = "\n\n...[QQ preview truncated]...\n\n"
    return head_tail_preview(text, max_chars, marker=marker)


def _archive_destination(output_dir: Path, job: Any) -> Path:
    label = (
        "".join(
            character
            for character in str(getattr(job, "label", "job"))
            if character.isascii() and (character.isalnum() or character in "_-")
        )[:32]
        or "job"
    )
    raw_job_id = getattr(job, "job_id", 0)
    job_id = raw_job_id if type(raw_job_id) is int and raw_job_id >= 0 else 0
    descriptor, name = tempfile.mkstemp(
        prefix=f"codex-{label}-job-{job_id:04d}-",
        suffix=".txt",
        dir=output_dir,
    )
    destination = Path(name)
    try:
        os.close(descriptor)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        _remove_partial_archive(destination)
        raise
    return destination


def _bounded_file_bytes(path: Path, max_bytes: int) -> tuple[bytes, int]:
    """最多读取 ``max_bytes``，同时保留文件开头和结尾。"""

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
    try:
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
                "final output exceeded the QQ preview limit of "
                f"{config.max_qq_text_chars} characters"
            )
        resolved_destination = destination.resolve()
        preview = _qq_preview(decoded, config.max_qq_text_chars)
        notice = f"\n\n[完整/受控输出已保存到文件: {destination.name}]"
        preview_budget = max(1, config.max_qq_text_chars - len(notice))
        return _FinalOutputCapture(
            text=f"{_qq_preview(preview, preview_budget)}{notice}",
            archive_path=str(resolved_destination),
            limited=True,
            reason=reason,
        )
    except BaseException:
        _remove_partial_archive(destination)
        raise


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
    try:
        destination.write_bytes(limited_bytes)
        resolved_destination = destination.resolve()
        notice = f"\n\n[受控输出已保存到文件: {destination.name}]"
        preview_budget = max(1, config.max_qq_text_chars - len(notice))
        return _FinalOutputCapture(
            text=f"{_qq_preview(bounded_text, preview_budget)}{notice}",
            archive_path=str(resolved_destination),
            limited=True,
            reason="agent message exceeded a configured output budget",
        )
    except BaseException:
        _remove_partial_archive(destination)
        raise


def _signal_process_group(process_group_id: int, group_signal: int) -> None:
    kill_group = getattr(os, "killpg", None)
    if not callable(kill_group):
        raise OSError("process-group signals are unavailable on this platform")
    kill_group(process_group_id, group_signal)


def _process_group_exists(process_group_id: int) -> bool:
    try:
        _signal_process_group(process_group_id, 0)
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
    except (OSError, RuntimeError):
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
    helper_code: int | None = None
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
                try:
                    helper.kill()
                except (ProcessLookupError, OSError, RuntimeError):
                    pass
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
        except (ProcessLookupError, OSError, RuntimeError):
            pass
        parent_reaped = await _wait_for_parent(process, kill_timeout_seconds)

    # Windows 的 taskkill 在目标 PID 已经消失时返回 128。若父进程在兜底
    # kill 之前就已确认回收，这表示清理目标已经不存在，不应再记录 ERROR。
    # 其他非零返回码、超时，以及依赖兜底 kill 才结束的情况仍保持失败语义，
    # 避免把可能残留的子进程误报为已完整终止。
    if helper_code == 128 and parent_reaped and not forced:
        helper_succeeded = True
        helper_error = None

    return ProcessTreeTerminationResult(
        tree_confirmed=helper_succeeded,
        parent_reaped=parent_reaped,
        forced=forced,
        helper_error=helper_error,
    )


async def _wait_for_process_group_exit(process_group_id: int, timeout_seconds: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout_seconds)
    while _process_group_exists(process_group_id) and loop.time() < deadline:
        await asyncio.sleep(0.05)


async def _finish_posix_parent_wait(
    process: asyncio.subprocess.Process,
    parent_wait_task: asyncio.Task[int] | None,
    timeout_seconds: float,
) -> bool:
    if parent_wait_task is None:
        return True
    try:
        await asyncio.wait_for(
            asyncio.shield(parent_wait_task),
            timeout=max(0.01, timeout_seconds),
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


async def _terminate_posix_process_tree(
    process: asyncio.subprocess.Process,
    *,
    term_grace_seconds: float,
    kill_timeout_seconds: float,
) -> ProcessTreeTerminationResult:
    process_group_id = process.pid
    forced = False
    parent_wait_task = asyncio.create_task(process.wait()) if process.returncode is None else None

    try:
        _signal_process_group(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        return ProcessTreeTerminationResult(
            tree_confirmed=False,
            parent_reaped=await _finish_posix_parent_wait(
                process,
                parent_wait_task,
                kill_timeout_seconds,
            ),
            helper_error=f"SIGTERM permission denied: {exc}",
        )

    await _wait_for_process_group_exit(process_group_id, term_grace_seconds)

    if _process_group_exists(process_group_id):
        forced = True
        try:
            _signal_process_group(process_group_id, getattr(signal, "SIGKILL", 9))
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            return ProcessTreeTerminationResult(
                tree_confirmed=False,
                parent_reaped=await _finish_posix_parent_wait(
                    process,
                    parent_wait_task,
                    kill_timeout_seconds,
                ),
                forced=True,
                helper_error=f"SIGKILL permission denied: {exc}",
            )

    await _wait_for_process_group_exit(process_group_id, kill_timeout_seconds)
    parent_reaped = await _finish_posix_parent_wait(
        process,
        parent_wait_task,
        kill_timeout_seconds,
    )
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
    """按平台执行有界等待，并终止完整进程树。"""

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
        except (AttributeError, OSError, RuntimeError):
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
        except (AttributeError, OSError, RuntimeError):
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
    except Exception as exc:  # noqa: BLE001 - 即使读取失败，清理路径也必须关闭全部管道。
        logger.warning(
            "Codex process pipe drain failed: pid=%s error_type=%s",
            getattr(process, "pid", None),
            type(exc).__name__,
        )
        _close_process_pipe_transports(process)
        return b"", b""


async def _terminate_and_drain_process(
    process: asyncio.subprocess.Process,
) -> tuple[ProcessTreeTerminationResult, bytes, bytes]:
    try:
        termination = await terminate_process_tree(process)
    except Exception as exc:  # noqa: BLE001 - 最终所有权兜底仍必须回收父进程。
        logger.error(
            "Codex process-tree helper failed; using parent fallback: pid=%s error_type=%s",
            getattr(process, "pid", None),
            type(exc).__name__,
        )
        forced = False
        if getattr(process, "returncode", None) is None:
            try:
                process.kill()
                forced = True
            except (ProcessLookupError, OSError, RuntimeError):
                pass
        parent_reaped = await _wait_for_parent(process, 5)
        termination = ProcessTreeTerminationResult(
            tree_confirmed=False,
            parent_reaped=parent_reaped,
            forced=forced,
            helper_error=type(exc).__name__,
        )
    if not termination.tree_confirmed or not termination.parent_reaped:
        logger.error(
            "Codex process tree termination was not fully confirmed: pid=%s result=%s",
            getattr(process, "pid", None),
            termination,
        )
    try:
        stdout, stderr = await _drain_process_after_termination(process)
    finally:
        _close_process_pipe_transports(process)
    return termination, stdout, stderr


def _consume_task_exception(task: asyncio.Task[Any]) -> BaseException | None:
    """读取已完成任务的异常；任务自身取消不视为流处理失败。"""

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
        error = _consume_task_exception(task)
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


async def _wait_for_stream_trigger(
    tasks: dict[str, asyncio.Task[Any]],
    *,
    deadline: float,
) -> _StreamIoOutcome:
    """等待退出、取消、超时或任一管道失败。"""

    while not tasks["wait"].done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return _StreamIoOutcome(timed_out=True)
        active = {task for task in tasks.values() if not task.done()}
        if not active:
            return _StreamIoOutcome()
        done, _ = await asyncio.wait(
            active,
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            return _StreamIoOutcome(timed_out=True)

        failure_outcome = _completed_stream_failure(tasks, done)
        if failure_outcome is not None:
            return failure_outcome
        if "cancel" in tasks and tasks["cancel"] in done:
            return _StreamIoOutcome(cancelled=True)
    return _StreamIoOutcome()


def _completed_stream_failure(
    tasks: dict[str, asyncio.Task[Any]],
    done: set[asyncio.Task[Any]],
) -> _StreamIoOutcome | None:
    for name in ("stdout", "stderr", "monitor", "stdin"):
        task = tasks[name]
        if task not in done:
            continue
        error = _consume_task_exception(task)
        if error is None:
            continue
        if isinstance(error, OutputBudgetExceeded):
            return _StreamIoOutcome(output_limited=True, limit_reason=str(error))
        if name == "stdin" and isinstance(error, (BrokenPipeError, ConnectionError)):
            continue
        raise error
    return None


def _cancel_stream_waiters(tasks: dict[str, asyncio.Task[Any]]) -> None:
    for name in ("monitor", "cancel"):
        task = tasks.get(name)
        if task is not None and not task.done():
            task.cancel()


async def _apply_stream_failures(
    process: asyncio.subprocess.Process,
    failures: list[tuple[str, BaseException]],
    *,
    outcome: _StreamIoOutcome,
    process_was_terminated: bool,
) -> _StreamIoOutcome:
    for name, error in failures:
        if isinstance(error, OutputBudgetExceeded):
            if not outcome.output_limited:
                outcome = _StreamIoOutcome(output_limited=True, limit_reason=str(error))
                if not process_was_terminated:
                    await terminate_process_tree(process)
            continue
        if name in {"stdin", "cancel", "monitor"} and isinstance(
            error,
            (asyncio.CancelledError, BrokenPipeError, ConnectionError),
        ):
            continue
        raise error
    return outcome


async def _run_streaming_io(
    process: asyncio.subprocess.Process,
    *,
    prompt_payload: bytes,
    output_path: Path,
    cancel_event: asyncio.Event | None,
    config: CodexPluginConfig,
) -> tuple[_StdoutEventAccumulator, _StderrAccumulator, _StreamIoOutcome]:
    """并发驱动子进程管道，并执行全部硬预算。"""

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

    try:
        outcome = await _wait_for_stream_trigger(
            tasks,
            deadline=asyncio.get_running_loop().time() + config.job_timeout_seconds,
        )
        termination_required = outcome.timed_out or outcome.cancelled or outcome.output_limited
        if termination_required:
            failures = await _terminate_streaming_process(process, tasks)
        else:
            _cancel_stream_waiters(tasks)
            failures = await _settle_stream_tasks(process, tasks)
        outcome = await _apply_stream_failures(
            process,
            failures,
            outcome=outcome,
            process_was_terminated=termination_required,
        )
        return stdout_accumulator, stderr_accumulator, outcome
    except asyncio.CancelledError as cancellation:
        for name in ("stdin", "monitor", "cancel"):
            task = tasks.get(name)
            if task is not None and not task.done():
                task.cancel()
        try:
            await _run_cleanup_cancellation_resistant(_terminate_streaming_process(process, tasks))
        except BaseException as exc:
            logger.warning(
                "Codex streaming cancellation cleanup failed: pid=%s error_type=%s",
                getattr(process, "pid", None),
                type(exc).__name__,
            )
        raise cancellation
    finally:
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


class CodexRunner:
    def __init__(self, config: CodexPluginConfig, output_dir: Path):
        self.config = config
        self.output_dir = output_dir

    def _base_args(self, cwd: Path) -> list[str]:
        codex_bin = shutil.which(self.config.codex_bin) or self.config.codex_bin
        args = [
            codex_bin,
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
        return (
            f"{prompt.rstrip()}\n\n"
            "[Codex 插件默认图片输出约定]\n"
            f"如果本次任务生成、导出、截图或保存图片，请把图片文件保存到这个目录: {artifact_path}\n"
            "如果图片工具先保存到其他位置，请在最终回复中引用它，或在最终回复前复制到上述目录。\n"
            "最终回复里请用 Markdown 图片语法 `![说明](图片路径)`，或单独一行 `图片: 图片路径` 标出每张图片。\n"
            "如果本次任务没有生成图片，忽略这段约定。"
        )

    def _build_args(
        self,
        cwd: Path,
        thread_id: str | None,
        output_path: Path,
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

    def _create_output_path(self) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # 必须使用 delete=False：Codex 需要在 Windows 上重新打开该路径。创建进程前先关闭
        # 句柄，但生成的文件名始终由本次执行独占。
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".txt",
            prefix="codex-last-",
            dir=self.output_dir,
            delete=False,
        ) as temporary:
            return Path(temporary.name)

    async def _spawn_process(
        self,
        *,
        cwd: Path,
        prompt_payload: str,
        thread_id: str | None,
        output_path: Path,
        job: Any,
    ) -> asyncio.subprocess.Process:
        platform_kwargs: dict[str, Any]
        if sys.platform == "win32":
            platform_kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            platform_kwargs = {"start_new_session": True}
        args = self._build_args(cwd, thread_id, output_path)
        logger.info(
            "Starting Codex CLI: label=%s thread=%s cwd_name=%s prompt_chars=%d prompt_lines=%d",
            getattr(job, "label", "?"),
            thread_id or "new",
            cwd.name,
            len(prompt_payload),
            len(prompt_payload.splitlines()),
        )
        try:
            return await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **platform_kwargs,
                ),
                timeout=self.config.spawn_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"Codex process spawn timed out after {self.config.spawn_timeout_seconds}s"
            ) from exc

    async def _perform_handoff(
        self,
        *,
        process: asyncio.subprocess.Process,
        callback: Callable[[], Awaitable[bool]] | None,
        default_decision: bool,
        stage: str,
        resources: _RunResources,
    ) -> bool:
        try:
            may_continue = await callback() if callback is not None else default_decision
        except BaseException as handoff_error:
            try:
                termination, _, _ = await _run_cleanup_cancellation_resistant(
                    _terminate_and_drain_process(process)
                )
            except BaseException as cleanup_error:
                logger.warning(
                    "Codex %s handoff cleanup failed: pid=%s error_type=%s",
                    stage,
                    getattr(process, "pid", None),
                    type(cleanup_error).__name__,
                )
            else:
                resources.process_cleanup_required = not (
                    termination.tree_confirmed and termination.parent_reaped
                )
            raise handoff_error
        if may_continue:
            return True
        termination, _, _ = await _run_cleanup_cancellation_resistant(
            _terminate_and_drain_process(process)
        )
        resources.process_cleanup_required = not (
            termination.tree_confirmed and termination.parent_reaped
        )
        return False

    @staticmethod
    def _cancelled_before_execution_result(
        process: asyncio.subprocess.Process,
        thread_id: str | None,
        message: str,
    ) -> CodexRunResult:
        return CodexRunResult(
            exit_code=process.returncode,
            thread_id=thread_id,
            final_text=message,
            stdout_tail="",
            stderr_tail="",
            cancelled=True,
        )

    async def _execute_process(
        self,
        *,
        process: asyncio.subprocess.Process,
        prompt_payload: bytes,
        output_path: Path,
        cancel_event: asyncio.Event | None,
    ) -> _CodexExecution:
        stdout_accumulator, stderr_accumulator, outcome = await _run_streaming_io(
            process,
            prompt_payload=prompt_payload,
            output_path=output_path,
            cancel_event=cancel_event,
            config=self.config,
        )
        return _CodexExecution(
            timed_out=outcome.timed_out,
            cancelled=outcome.cancelled,
            output_limited=outcome.output_limited,
            limit_reason=outcome.limit_reason,
            thread_id=stdout_accumulator.summary.thread_id,
            message_text=stdout_accumulator.summary.last_message,
            usage=stdout_accumulator.summary.usage,
            image_paths=stdout_accumulator.summary.image_paths,
            stdout_tail=stdout_accumulator.tail.text(),
            stderr_tail=stderr_accumulator.tail.text(),
            forced_stop=outcome.timed_out or outcome.cancelled or outcome.output_limited,
        )

    async def _capture_result(
        self,
        *,
        execution: _CodexExecution,
        output_path: Path,
        process: asyncio.subprocess.Process,
        thread_id: str | None,
        job: Any,
        resources: _RunResources,
    ) -> CodexRunResult:
        file_capture = await _capture_in_thread(
            _capture_final_output,
            output_path,
            orphan_archives=resources.orphan_archives,
            output_dir=self.output_dir,
            job=job,
            config=self.config,
        )
        if file_capture.archive_path:
            resources.orphan_archives.add(Path(file_capture.archive_path))
        execution.output_limited = execution.output_limited or file_capture.limited
        execution.limit_reason = execution.limit_reason or file_capture.reason

        result_capture = file_capture
        if not result_capture.text and execution.message_text:
            result_capture = await _capture_in_thread(
                _archive_large_message,
                execution.message_text,
                orphan_archives=resources.orphan_archives,
                output_dir=self.output_dir,
                job=job,
                config=self.config,
            )
            if result_capture.archive_path:
                resources.orphan_archives.add(Path(result_capture.archive_path))
            execution.output_limited = execution.output_limited or result_capture.limited
            execution.limit_reason = execution.limit_reason or result_capture.reason

        final_text = result_capture.text
        if execution.output_limited and execution.limit_reason:
            budget_notice = f"Codex output budget exceeded: {execution.limit_reason}."
            final_text = f"{budget_notice}\n\n{final_text}" if final_text else budget_notice
        if not final_text:
            if execution.cancelled:
                final_text = "Codex 任务已取消。"
            elif execution.timed_out:
                final_text = "Codex 执行超时。"
            else:
                final_text = (
                    execution.stderr_tail or execution.stdout_tail or "Codex 没有返回文本结果。"
                )
        return CodexRunResult(
            exit_code=process.returncode,
            thread_id=execution.thread_id or thread_id,
            final_text=_qq_preview(final_text, self.config.max_qq_text_chars),
            stdout_tail=execution.stdout_tail,
            stderr_tail=execution.stderr_tail,
            timed_out=execution.timed_out,
            cancelled=execution.cancelled or bool(getattr(job, "cancel_requested", False)),
            output_limited=execution.output_limited,
            output_path=result_capture.archive_path,
            image_paths=execution.image_paths,
            usage=execution.usage,
        )

    @staticmethod
    async def _cleanup_process_resource(
        process: asyncio.subprocess.Process,
    ) -> asyncio.CancelledError | None:
        try:
            await _run_cleanup_cancellation_resistant(_terminate_and_drain_process(process))
        except asyncio.CancelledError as exc:
            return exc
        except Exception as exc:  # 清理失败不能跳过后续文件回收。
            logger.error(
                "Final Codex process cleanup failed: pid=%s error_type=%s",
                getattr(process, "pid", None),
                type(exc).__name__,
            )
        return None

    @staticmethod
    async def _cleanup_path_resource(
        path: Path,
        *,
        orphan_archive: bool,
    ) -> asyncio.CancelledError | None:
        try:
            await _run_cleanup_cancellation_resistant(_remove_output_path_with_retry(path))
        except asyncio.CancelledError as exc:
            return exc
        except Exception as exc:  # 同一轮仍要继续回收其他自有资源。
            logger.warning(
                "%s: name=%s error_type=%s",
                (
                    "Failed to discard orphan Codex archive"
                    if orphan_archive
                    else "Failed to finalize Codex transient output"
                ),
                path.name,
                type(exc).__name__,
            )
        return None

    async def _cleanup_run_resources(self, resources: _RunResources) -> None:
        cleanup_cancellation: asyncio.CancelledError | None = None
        if resources.process_cleanup_required and resources.process is not None:
            cleanup_cancellation = await self._cleanup_process_resource(resources.process)
        if resources.output_path is not None:
            path_cancellation = await self._cleanup_path_resource(
                resources.output_path,
                orphan_archive=False,
            )
            cleanup_cancellation = cleanup_cancellation or path_cancellation
        # Python 进入本清理阶段前结果尚未提交；此时如果收到取消，新建归档就没有接收方。
        if not resources.result_committed or cleanup_cancellation is not None:
            for archive_path in sorted(resources.orphan_archives):
                path_cancellation = await self._cleanup_path_resource(
                    archive_path,
                    orphan_archive=True,
                )
                cleanup_cancellation = cleanup_cancellation or path_cancellation
        if cleanup_cancellation is not None:
            raise cleanup_cancellation

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
        resources = _RunResources()
        try:
            resources.output_path = self._create_output_path()
            prompt_payload = self._prompt_with_artifact_instruction(prompt, artifact_dir)
            resources.process = await self._spawn_process(
                cwd=cwd,
                prompt_payload=prompt_payload,
                thread_id=thread_id,
                output_path=resources.output_path,
                job=job,
            )
            process = resources.process
            resources.process_cleanup_required = True

            if process_handoff is None:
                job.process = process
                process_callback = None
            else:

                async def process_callback() -> bool:
                    return await process_handoff(process)

            may_continue = await self._perform_handoff(
                process=process,
                callback=process_callback,
                default_decision=not bool(getattr(job, "cancel_requested", False)),
                stage="process",
                resources=resources,
            )
            if not may_continue:
                result = self._cancelled_before_execution_result(
                    process,
                    thread_id,
                    "任务在 Codex 进程登记后、prompt 发送前已取消。",
                )
                resources.result_committed = True
                return result

            may_send_by_default = not bool(getattr(job, "cancel_requested", False))
            if prompt_handoff is None:
                prompt_callback = None
                if may_send_by_default:
                    job.prompt_started = True
            else:
                prompt_callback = prompt_handoff
            may_send_prompt = await self._perform_handoff(
                process=process,
                callback=prompt_callback,
                default_decision=may_send_by_default,
                stage="prompt",
                resources=resources,
            )
            if not may_send_prompt:
                result = self._cancelled_before_execution_result(
                    process,
                    thread_id,
                    "任务在 prompt 发送前已取消。",
                )
                resources.result_committed = True
                return result

            cancel_event = getattr(job, "cancel_event", None)
            typed_cancel_event = cancel_event if isinstance(cancel_event, asyncio.Event) else None
            execution = await self._execute_process(
                process=process,
                prompt_payload=prompt_payload.encode("utf-8"),
                output_path=resources.output_path,
                cancel_event=typed_cancel_event,
            )
            result = await self._capture_result(
                execution=execution,
                output_path=resources.output_path,
                process=process,
                thread_id=thread_id,
                job=job,
                resources=resources,
            )
            resources.result_committed = True
            resources.process_cleanup_required = execution.forced_stop or bool(
                getattr(job, "cancel_requested", False)
            )
            return result
        finally:
            await self._cleanup_run_resources(resources)
