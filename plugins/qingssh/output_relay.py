"""把 SSH 完整输出排空到受控归档，并向 QQ 投影有界的首尾摘要。"""

from __future__ import annotations

import asyncio
import math
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, cast

from core.plugin_base import bounded_external_text

_FINAL_RESERVE_CHARS       = 512
_ARCHIVE_MARKER            = b"\n\n--- output exceeded archive budget; middle omitted ---\n\n"
_ARCHIVE_WRITE_BATCH_BYTES = 256 * 1024


def _integer_option(options: Mapping[str, Any], name: str, default: int) -> int:
    value = options.get(name, default)
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{name} must be an integer")
    return parsed


def _float_option(options: Mapping[str, Any], name: str, default: float) -> float:
    value = options.get(name, default)
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    return parsed


@dataclass(frozen=True)
class SSHOutputPolicy:
    """单条命令的消息与归档预算，不限制远端命令本身。"""

    command_timeout_seconds: float  = 30.0
    qq_max_actions: int             = 6
    qq_max_text_chars: int          = 10_000
    qq_max_message_chars: int       = 1_800
    qq_head_chars: int              = 6_000
    qq_tail_chars: int              = 2_000
    qq_send_interval_seconds: float = 0.35
    qq_send_timeout_seconds: float  = 5.0
    archive_max_bytes: int          = 64 * 1024 * 1024
    archive_tail_bytes: int         = 1024 * 1024
    archive_retention_files: int    = 20

    @classmethod
    def from_context(cls, context: Any) -> SSHOutputPolicy:
        options = context.get_settings_snapshot().plugin_config("qingssh")
        policy  = cls(
            command_timeout_seconds=_float_option(
                options,
                "command_timeout_seconds",
                cls.command_timeout_seconds,
            ),
            qq_max_actions    = _integer_option(options, "qq_max_actions", cls.qq_max_actions),
            qq_max_text_chars = _integer_option(
                options,
                "qq_max_text_chars",
                cls.qq_max_text_chars,
            ),
            qq_max_message_chars=_integer_option(
                options,
                "qq_max_message_chars",
                cls.qq_max_message_chars,
            ),
            qq_head_chars            = _integer_option(options, "qq_head_chars", cls.qq_head_chars),
            qq_tail_chars            = _integer_option(options, "qq_tail_chars", cls.qq_tail_chars),
            qq_send_interval_seconds = _float_option(
                options,
                "qq_send_interval_seconds",
                cls.qq_send_interval_seconds,
            ),
            qq_send_timeout_seconds=_float_option(
                options,
                "qq_send_timeout_seconds",
                cls.qq_send_timeout_seconds,
            ),
            archive_max_bytes=_integer_option(
                options,
                "archive_max_bytes",
                cls.archive_max_bytes,
            ),
            archive_tail_bytes=_integer_option(
                options,
                "archive_tail_bytes",
                cls.archive_tail_bytes,
            ),
            archive_retention_files=_integer_option(
                options,
                "archive_retention_files",
                cls.archive_retention_files,
            ),
        )
        policy.validate()
        return policy

    def validate(self) -> None:
        if self.command_timeout_seconds < 0:
            raise ValueError("command_timeout_seconds must be zero or positive")
        if not 2 <= self.qq_max_actions <= 20:
            raise ValueError("qq_max_actions must be between 2 and 20")
        if not 1024 <= self.qq_max_text_chars <= 60_000:
            raise ValueError("qq_max_text_chars must be between 1024 and 60000")
        if not 256 <= self.qq_max_message_chars <= 3_000:
            raise ValueError("qq_max_message_chars must be between 256 and 3000")
        if not 256 <= self.qq_head_chars:
            raise ValueError("qq_head_chars must be at least 256")
        if not 128 <= self.qq_tail_chars:
            raise ValueError("qq_tail_chars must be at least 128")
        if self.qq_head_chars > self.qq_max_message_chars * (self.qq_max_actions - 1):
            raise ValueError("qq_head_chars exceeds the reserved output action capacity")
        if self.qq_head_chars + self.qq_tail_chars + _FINAL_RESERVE_CHARS > self.qq_max_text_chars:
            raise ValueError("QQ head/tail budgets exceed qq_max_text_chars")
        if not 0 <= self.qq_send_interval_seconds <= 60:
            raise ValueError("qq_send_interval_seconds must be between 0 and 60")
        if not 0 < self.qq_send_timeout_seconds <= 300:
            raise ValueError("qq_send_timeout_seconds must be between 0 and 300")
        if not 1024 * 1024 <= self.archive_max_bytes <= 1024 * 1024 * 1024:
            raise ValueError("archive_max_bytes must be between 1 MiB and 1 GiB")
        if not 64 * 1024 <= self.archive_tail_bytes <= self.archive_max_bytes // 2:
            raise ValueError(
                "archive_tail_bytes must be between 64 KiB and half the archive budget"
            )
        if not 1 <= self.archive_retention_files <= 1000:
            raise ValueError("archive_retention_files must be between 1 and 1000")


@dataclass(frozen=True)
class SSHOutputSummary:
    """命令输出结束后可安全记录的传输统计。"""

    total_chars: int
    total_bytes: int
    qq_truncated: bool
    archive_path: Path | None
    archive_truncated: bool
    actions_attempted: int
    text_chars_attempted: int
    delivery_errors: int


class SSHOutputRelay:
    """快速排空 SSH 输出，同时按速率和总量限制发送 QQ 摘要。"""

    def __init__(
        self,
        *,
        output_dir: Path,
        policy: SSHOutputPolicy,
        send_text: Callable[[str], Awaitable[None]],
    ) -> None:
        policy.validate()
        self.policy     = policy
        self._send_text = send_text
        self._queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=policy.qq_max_actions + 1)
        self._sender_task = asyncio.create_task(
            self._sender_loop(),
            name="qingssh-output-sender",
        )
        self._head_parts: list[str]          = []
        self._head_chars                     = 0
        self._queued_head_actions            = 0
        self._tail_parts: deque[str]         = deque()
        self._tail_chars                     = 0
        self._total_chars                    = 0
        self._total_visible_chars            = 0
        self._total_bytes                    = 0
        self._actions_attempted              = 0
        self._text_chars_attempted           = 0
        self._delivery_errors                = 0
        self._last_send_at                   = 0.0
        self._closed                         = False
        self._finished                       = False
        self._committed_archive: Path | None = None

        self._output_dir = Path(output_dir)
        self._archive_file: BinaryIO | None = None
        self._archive_temp: Path | None = None
        self._archive_error: str | None = None
        self._archive_started = False
        self._created_output_dir = False
        self._archive_pending: list[bytes] = []
        self._archive_write_buffer = bytearray()
        self._archive_io_lock = asyncio.Lock()
        self._archive_thread_lock = threading.Lock()
        self._archive_head_bytes = policy.archive_max_bytes - policy.archive_tail_bytes
        self._archive_head_written = 0
        self._archive_tail_parts: deque[bytes] = deque()
        self._archive_tail_size = 0

    def _open_archive(self, initial_payload: bytes = b"") -> None:
        try:
            output_dir_existed = self._output_dir.exists()
            self._output_dir.mkdir(parents=True, exist_ok=True)
            self._created_output_dir = not output_dir_existed
            path                     = self._output_dir / f".ssh-output-{uuid.uuid4().hex}.tmp"
            self._archive_file       = path.open("xb")
            self._archive_temp       = path
            if os.name != "nt":
                path.chmod(0o600)
        except OSError as exc:
            self._archive_file  = None
            self._archive_temp  = None
            self._archive_error = type(exc).__name__

        self._archive(initial_payload)

    def _run_archive_operation(self, operation: Callable[..., Any], *args: Any) -> Any:
        """串行化线程内归档操作，连取消后的后台线程也不能互相踩踏。"""

        with self._archive_thread_lock:
            return operation(*args)

    async def _run_archive_io(self, operation: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.to_thread(self._run_archive_operation, operation, *args)

    async def _record_archive_payload(self, payload: bytes) -> None:
        """在事件循环中有界缓存，并批量把阻塞文件写入交给工作线程。"""

        async with self._archive_io_lock:
            if not self._archive_started:
                self._archive_pending.append(payload)
                if self._total_chars <= self.policy.qq_head_chars:
                    return
                self._archive_started = True
                initial_payload       = b"".join(self._archive_pending)
                self._archive_pending.clear()
                await self._run_archive_io(self._open_archive, initial_payload)
                return

            if not self._archive_write_buffer and len(payload) >= _ARCHIVE_WRITE_BATCH_BYTES:
                batch = payload
            else:
                self._archive_write_buffer.extend(payload)
                if len(self._archive_write_buffer) < _ARCHIVE_WRITE_BATCH_BYTES:
                    return
                batch = bytes(self._archive_write_buffer)
                self._archive_write_buffer.clear()
            await self._run_archive_io(self._archive, batch)

    @staticmethod
    def _safe_utf8_prefix(payload: bytes, limit: int) -> bytes:
        if len(payload) <= limit:
            return payload
        return payload[:limit].decode("utf-8", errors="ignore").encode("utf-8")

    def _append_archive_tail(self, payload: bytes) -> None:
        if not payload:
            return
        self._archive_tail_parts.append(payload)
        self._archive_tail_size += len(payload)
        while self._archive_tail_size > self.policy.archive_tail_bytes and self._archive_tail_parts:
            excess = self._archive_tail_size - self.policy.archive_tail_bytes
            first  = self._archive_tail_parts[0]
            if len(first) <= excess:
                self._archive_tail_parts.popleft()
                self._archive_tail_size -= len(first)
                continue
            kept = first[excess:].decode("utf-8", errors="ignore").encode("utf-8")
            self._archive_tail_parts[0] = kept
            self._archive_tail_size -= len(first) - len(kept)
            break

    def _archive(self, payload: bytes) -> None:
        handle = self._archive_file
        if handle is None:
            return
        try:
            remaining_head = self._archive_head_bytes - self._archive_head_written
            if remaining_head > 0:
                prefix = self._safe_utf8_prefix(payload, remaining_head)
                handle.write(prefix)
                self._archive_head_written += len(prefix)
                payload = payload[len(prefix) :]
            self._append_archive_tail(payload)
        except OSError as exc:
            self._archive_error = type(exc).__name__
            self._discard_archive()

    def _append_tail(self, text: str) -> None:
        if not text:
            return
        self._tail_parts.append(text)
        self._tail_chars += len(text)
        while self._tail_chars > self.policy.qq_tail_chars and self._tail_parts:
            excess = self._tail_chars - self.policy.qq_tail_chars
            first  = self._tail_parts[0]
            if len(first) <= excess:
                self._tail_parts.popleft()
                self._tail_chars -= len(first)
                continue
            self._tail_parts[0] = first[excess:]
            self._tail_chars -= excess
            break

    def _enqueue_head(self, *, force: bool) -> None:
        if not self._head_parts:
            return
        content = "".join(self._head_parts)
        while content and self._queued_head_actions < self.policy.qq_max_actions - 1:
            if not force and len(content) < self.policy.qq_max_message_chars:
                break
            chunk   = content[: self.policy.qq_max_message_chars]
            content = content[len(chunk) :]
            self._queue.put_nowait(chunk)
            self._queued_head_actions += 1
        self._head_parts = [content] if content else []

    async def feed(self, text: Any) -> None:
        if self._closed:
            return
        value = "" if text is None else str(text)
        if not value:
            return
        encoded = value.encode("utf-8", errors="replace")
        self._total_chars += len(value)
        self._total_bytes += len(encoded)
        await self._record_archive_payload(encoded)
        visible = bounded_external_text(
            value,
            max_chars = max(1, len(value)),
            max_bytes = max(1, len(encoded)),
            suffix    = "",
            strip     = False,
        )
        self._total_visible_chars += len(visible)
        self._append_tail(visible)

        remaining = self.policy.qq_head_chars - self._head_chars
        if remaining > 0:
            kept = visible[:remaining]
            self._head_parts.append(kept)
            self._head_chars += len(kept)
            self._enqueue_head(force=False)

    def _tail_without_head_overlap(self) -> str:
        tail    = "".join(self._tail_parts)
        overlap = max(0, self.policy.qq_head_chars + len(tail) - self._total_visible_chars)
        return tail[overlap:]

    def _discard_archive(self) -> None:
        handle, self._archive_file = self._archive_file, None
        if handle is not None:
            # 清理路径不能用次生异常覆盖原始命令结果。
            with suppress(OSError):
                handle.close()
        path, self._archive_temp = self._archive_temp, None
        if path is not None:
            with suppress(OSError):
                path.unlink(missing_ok=True)
        if self._created_output_dir:
            with suppress(OSError):
                self._output_dir.rmdir()
            self._created_output_dir = False

    def _commit_archive(self) -> tuple[Path | None, bool]:
        handle = self._archive_file
        temp   = self._archive_temp
        if handle is None or temp is None:
            self._discard_archive()
            return None, False
        archive_truncated = self._total_bytes > self.policy.archive_max_bytes
        try:
            tail = b"".join(self._archive_tail_parts)
            if archive_truncated:
                tail_budget = max(0, self.policy.archive_tail_bytes - len(_ARCHIVE_MARKER))
                tail = tail[-tail_budget:].decode("utf-8", errors="replace").encode("utf-8")
                handle.write(_ARCHIVE_MARKER)
            handle.write(tail)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            self._archive_file                           = None
            archive_entries: list[tuple[int, str, Path]] = []
            for path in self._output_dir.glob("ssh-output-*.txt"):
                try:
                    if path.is_file() and not path.is_symlink():
                        archive_entries.append((path.stat().st_mtime_ns, path.name, path))
                except OSError:
                    continue
            archives = [entry[2] for entry in sorted(archive_entries)]
            excess   = len(archives) - self.policy.archive_retention_files + 1
            for stale in archives[: max(0, excess)]:
                with suppress(OSError):
                    stale.unlink(missing_ok=True)
            stamp       = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            destination = self._output_dir / f"ssh-output-{stamp}-{uuid.uuid4().hex}.txt"
            os.replace(temp, destination)
            self._archive_temp      = None
            self._committed_archive = destination.resolve()
            return self._committed_archive, archive_truncated
        except OSError as exc:
            self._archive_error = type(exc).__name__
            self._discard_archive()
            return None, False

    async def _finish_archive(self, *, keep: bool) -> tuple[Path | None, bool]:
        async with self._archive_io_lock:
            if not keep:
                self._archive_pending.clear()
                self._archive_write_buffer.clear()
                await self._run_archive_io(self._discard_archive)
                return None, False

            if self._archive_write_buffer:
                batch = bytes(self._archive_write_buffer)
                self._archive_write_buffer.clear()
                await self._run_archive_io(self._archive, batch)
            return cast(
                tuple[Path | None, bool],
                await self._run_archive_io(self._commit_archive),
            )

    def _abort_archive(self) -> None:
        committed, self._committed_archive = self._committed_archive, None
        if committed is not None:
            with suppress(OSError):
                committed.unlink(missing_ok=True)
        self._discard_archive()

    async def _sender_loop(self) -> None:
        while True:
            content = await self._queue.get()
            try:
                if content is None:
                    return
                if self._actions_attempted >= self.policy.qq_max_actions:
                    continue
                remaining_chars = self.policy.qq_max_text_chars - self._text_chars_attempted
                if remaining_chars <= 0:
                    continue
                content = content[: min(remaining_chars, self.policy.qq_max_message_chars)]
                if not content:
                    continue
                elapsed = time.monotonic() - self._last_send_at
                delay   = self.policy.qq_send_interval_seconds - elapsed
                if self._last_send_at and delay > 0:
                    await asyncio.sleep(delay)
                self._actions_attempted += 1
                self._text_chars_attempted += len(content)
                try:
                    await asyncio.wait_for(
                        self._send_text(content),
                        timeout=self.policy.qq_send_timeout_seconds,
                    )
                except Exception:
                    self._delivery_errors += 1
                self._last_send_at = time.monotonic()
            finally:
                self._queue.task_done()

    def _final_message(
        self,
        status: str,
        *,
        archive_path: Path | None,
        archive_truncated: bool,
    ) -> str:
        lines        = [status.strip()]
        qq_truncated = self._total_visible_chars > self.policy.qq_head_chars
        if qq_truncated:
            lines.append(f"⚠️ QQ 输出已截断（原始 {self._total_chars} 字符），以上为开头摘要。")
            if archive_path is not None:
                label = "受控首尾归档" if archive_truncated else "完整输出"
                lines.append(f"{label}文件：{archive_path.name}（完整路径仅写入日志）")
            else:
                reason = self._archive_error or "unavailable"
                lines.append(f"完整输出归档失败（{reason}）")
            prefix = "\n".join(part for part in lines if part)
            tail   = self._tail_without_head_overlap()
            if tail:
                heading   = "\n末尾摘要：\n"
                available = self.policy.qq_max_message_chars - len(prefix) - len(heading)
                if available > 0:
                    prefix += heading + tail[-available:]
            return prefix
        return "\n".join(part for part in lines if part)

    async def finish(self, status: str) -> SSHOutputSummary:
        if self._closed:
            raise RuntimeError("SSH output relay is already closed")
        self._closed = True
        self._enqueue_head(force=True)
        qq_truncated              = self._total_visible_chars > self.policy.qq_head_chars
        archive_path: Path | None = None
        archive_truncated         = False
        archive_path, archive_truncated = await self._finish_archive(keep=qq_truncated)
        self._committed_archive = archive_path
        final_message           = self._final_message(
            status,
            archive_path      = archive_path,
            archive_truncated = archive_truncated,
        )
        self._queue.put_nowait(final_message)
        self._queue.put_nowait(None)
        await asyncio.shield(self._sender_task)
        summary = SSHOutputSummary(
            total_chars          = self._total_chars,
            total_bytes          = self._total_bytes,
            qq_truncated         = qq_truncated,
            archive_path         = archive_path,
            archive_truncated    = archive_truncated,
            actions_attempted    = self._actions_attempted,
            text_chars_attempted = self._text_chars_attempted,
            delivery_errors      = self._delivery_errors,
        )
        self._finished = True
        return summary

    async def abort(self) -> None:
        if self._finished:
            return
        self._closed = True
        async with self._archive_io_lock:
            self._archive_pending.clear()
            self._archive_write_buffer.clear()
            await self._run_archive_io(self._abort_archive)
        if not self._sender_task.done():
            self._sender_task.cancel()
        await asyncio.gather(self._sender_task, return_exceptions=True)


__all__ = ["SSHOutputPolicy", "SSHOutputRelay", "SSHOutputSummary"]
