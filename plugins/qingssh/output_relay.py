"""Bounded QQ projection and local archive handling for SSH command output."""

from __future__ import annotations

import asyncio
import math
import os
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

_FINAL_RESERVE_CHARS = 512
_ARCHIVE_MARKER = b"\n\n--- output exceeded archive budget; middle omitted ---\n\n"


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
    """Per-command transport policy; it never restricts the remote command itself."""

    command_timeout_seconds: float = 30.0
    qq_max_actions: int = 6
    qq_max_text_chars: int = 10_000
    qq_max_message_chars: int = 1_800
    qq_head_chars: int = 6_000
    qq_tail_chars: int = 2_000
    qq_send_interval_seconds: float = 0.35
    qq_send_timeout_seconds: float = 5.0
    archive_max_bytes: int = 64 * 1024 * 1024
    archive_tail_bytes: int = 1024 * 1024
    archive_retention_files: int = 20

    @classmethod
    def from_context(cls, context: Any) -> SSHOutputPolicy:
        config = getattr(context, "config", None)
        options: Mapping[str, Any] = {}
        if isinstance(config, Mapping):
            plugins = config.get("plugins", {})
            if isinstance(plugins, Mapping):
                candidate = plugins.get("qingssh", {})
                if isinstance(candidate, Mapping):
                    options = candidate
        policy = cls(
            command_timeout_seconds=_float_option(
                options,
                "command_timeout_seconds",
                cls.command_timeout_seconds,
            ),
            qq_max_actions=_integer_option(options, "qq_max_actions", cls.qq_max_actions),
            qq_max_text_chars=_integer_option(
                options,
                "qq_max_text_chars",
                cls.qq_max_text_chars,
            ),
            qq_max_message_chars=_integer_option(
                options,
                "qq_max_message_chars",
                cls.qq_max_message_chars,
            ),
            qq_head_chars=_integer_option(options, "qq_head_chars", cls.qq_head_chars),
            qq_tail_chars=_integer_option(options, "qq_tail_chars", cls.qq_tail_chars),
            qq_send_interval_seconds=_float_option(
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
            raise ValueError("archive_tail_bytes must be between 64 KiB and half the archive budget")
        if not 1 <= self.archive_retention_files <= 1000:
            raise ValueError("archive_retention_files must be between 1 and 1000")


@dataclass(frozen=True)
class SSHOutputSummary:
    total_chars: int
    total_bytes: int
    qq_truncated: bool
    archive_path: Path | None
    archive_truncated: bool
    actions_attempted: int
    text_chars_attempted: int
    delivery_errors: int


class SSHOutputRelay:
    """Drain SSH output quickly while sending a bounded, rate-limited QQ view."""

    def __init__(
        self,
        *,
        output_dir: Path,
        policy: SSHOutputPolicy,
        send_text: Callable[[str], Awaitable[None]],
    ) -> None:
        policy.validate()
        self.policy = policy
        self._send_text = send_text
        self._queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=policy.qq_max_actions + 1)
        self._sender_task = asyncio.create_task(
            self._sender_loop(),
            name="qingssh-output-sender",
        )
        self._head_parts: list[str] = []
        self._head_chars = 0
        self._queued_head_actions = 0
        self._tail_parts: deque[str] = deque()
        self._tail_chars = 0
        self._total_chars = 0
        self._total_bytes = 0
        self._actions_attempted = 0
        self._text_chars_attempted = 0
        self._delivery_errors = 0
        self._last_send_at = 0.0
        self._closed = False
        self._finished = False
        self._committed_archive: Path | None = None

        self._output_dir = Path(output_dir)
        self._archive_file: BinaryIO | None = None
        self._archive_temp: Path | None = None
        self._archive_error: str | None = None
        self._archive_head_bytes = policy.archive_max_bytes - policy.archive_tail_bytes
        self._archive_head_written = 0
        self._archive_tail_parts: deque[bytes] = deque()
        self._archive_tail_size = 0
        self._open_archive()

    def _open_archive(self) -> None:
        try:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            path = self._output_dir / f".ssh-output-{uuid.uuid4().hex}.tmp"
            self._archive_file = path.open("xb")
            self._archive_temp = path
            if os.name != "nt":
                path.chmod(0o600)
        except OSError as exc:
            self._archive_file = None
            self._archive_temp = None
            self._archive_error = type(exc).__name__

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
            first = self._archive_tail_parts[0]
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
                payload = payload[len(prefix):]
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
            first = self._tail_parts[0]
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
            chunk = content[: self.policy.qq_max_message_chars]
            content = content[len(chunk):]
            self._queue.put_nowait(chunk)
            self._queued_head_actions += 1
        self._head_parts = [content] if content else []

    async def feed(self, text: Any) -> None:
        if self._closed:
            return
        value = str(text or "")
        if not value:
            return
        encoded = value.encode("utf-8", errors="replace")
        self._total_chars += len(value)
        self._total_bytes += len(encoded)
        self._archive(encoded)
        self._append_tail(value)

        remaining = self.policy.qq_head_chars - self._head_chars
        if remaining > 0:
            kept = value[:remaining]
            self._head_parts.append(kept)
            self._head_chars += len(kept)
            self._enqueue_head(force=False)

    def _tail_without_head_overlap(self) -> str:
        tail = "".join(self._tail_parts)
        overlap = max(0, self.policy.qq_head_chars + len(tail) - self._total_chars)
        return tail[overlap:]

    def _discard_archive(self) -> None:
        handle, self._archive_file = self._archive_file, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        path, self._archive_temp = self._archive_temp, None
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _commit_archive(self) -> tuple[Path | None, bool]:
        handle = self._archive_file
        temp = self._archive_temp
        if handle is None or temp is None:
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
            self._archive_file = None
            archives = sorted(
                (
                    path
                    for path in self._output_dir.glob("ssh-output-*.txt")
                    if path.is_file() and not path.is_symlink()
                ),
                key=lambda path: (path.stat().st_mtime_ns, path.name),
            )
            excess = len(archives) - self.policy.archive_retention_files + 1
            for stale in archives[: max(0, excess)]:
                stale.unlink(missing_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            destination = self._output_dir / f"ssh-output-{stamp}-{uuid.uuid4().hex}.txt"
            os.replace(temp, destination)
            self._archive_temp = None
            if os.name != "nt":
                destination.chmod(0o600)
            return destination.resolve(), archive_truncated
        except OSError as exc:
            self._archive_error = type(exc).__name__
            self._discard_archive()
            return None, False

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
                delay = self.policy.qq_send_interval_seconds - elapsed
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
        lines = [status.strip()]
        qq_truncated = self._total_chars > self.policy.qq_head_chars
        if qq_truncated:
            lines.append(f"⚠️ QQ 输出已截断（原始 {self._total_chars} 字符），以上为开头摘要。")
            if archive_path is not None:
                label = "受控首尾归档" if archive_truncated else "完整输出"
                lines.append(f"{label}：{archive_path}")
            else:
                reason = self._archive_error or "unavailable"
                lines.append(f"完整输出归档失败（{reason}）")
            prefix = "\n".join(part for part in lines if part)
            tail = self._tail_without_head_overlap()
            if tail:
                heading = "\n末尾摘要：\n"
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
        qq_truncated = self._total_chars > self.policy.qq_head_chars
        archive_path: Path | None = None
        archive_truncated = False
        if qq_truncated:
            archive_path, archive_truncated = self._commit_archive()
            self._committed_archive = archive_path
        else:
            self._discard_archive()
        final_message = self._final_message(
            status,
            archive_path=archive_path,
            archive_truncated=archive_truncated,
        )
        self._queue.put_nowait(final_message)
        self._queue.put_nowait(None)
        await asyncio.shield(self._sender_task)
        summary = SSHOutputSummary(
            total_chars=self._total_chars,
            total_bytes=self._total_bytes,
            qq_truncated=qq_truncated,
            archive_path=archive_path,
            archive_truncated=archive_truncated,
            actions_attempted=self._actions_attempted,
            text_chars_attempted=self._text_chars_attempted,
            delivery_errors=self._delivery_errors,
        )
        self._finished = True
        return summary

    async def abort(self) -> None:
        if self._finished:
            return
        self._closed = True
        self._discard_archive()
        if self._committed_archive is not None:
            try:
                self._committed_archive.unlink(missing_ok=True)
            except OSError:
                pass
            self._committed_archive = None
        if not self._sender_task.done():
            self._sender_task.cancel()
        await asyncio.gather(self._sender_task, return_exceptions=True)


__all__ = ["SSHOutputPolicy", "SSHOutputRelay", "SSHOutputSummary"]
