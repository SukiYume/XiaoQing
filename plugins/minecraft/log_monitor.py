"""有界读取 Minecraft 日志，并提取需要转发的玩家事件。"""

from __future__ import annotations

import logging
import os
import re
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO

from core.atomic_store import AtomicJsonStore
from core.sensitive_audit import summarize_sensitive

from .audit import audit_error_type

logger = logging.getLogger(__name__)

_PLAYER_PATTERN = r"[A-Za-z0-9_]{1,16}"


class LogEventType(str, Enum):
    """插件会转发的日志事件类型。"""

    CHAT = "chat"
    JOIN = "join"
    LEAVE = "leave"
    DEATH = "death"
    ADVANCEMENT = "advancement"


@dataclass(frozen=True, slots=True)
class LogEvent:
    """已解析且与原始日志行解耦的最小事件。"""

    event_type: LogEventType
    player: str
    message: str | None = None


@dataclass(slots=True)
class LogBatch:
    """一次有界轮询的事件、丢弃统计和待提交游标。"""

    events: list[LogEvent]
    matched_total: int = 0
    dropped_events: int = 0
    skipped_bytes: int = 0
    skipped_lines: int | None = 0
    cursor_before: int | None = None
    cursor_after: int | None = None
    file_identity: str | None = None


@dataclass(frozen=True, slots=True)
class _ReadWindow:
    content: bytes
    cursor_before: int
    cursor_after: int
    file_identity: str
    skipped_bytes: int = 0
    skipped_lines: int | None = 0


class LogMonitor:
    """跟踪一个日志文件；只有投递确认后才持久化读取位置。"""

    LOG_PREFIX_PATTERN = re.compile(
        r"^\[\d{1,2}:\d{2}:\d{2}\] \[[^\]\r\n]+/INFO\]: (?P<body>[^\r\n]*)\Z"
    )
    CHAT_PATTERN = re.compile(rf"^<(?P<player>{_PLAYER_PATTERN})> (?P<message>.+)\Z")
    PRESENCE_PATTERN = re.compile(
        rf"^(?P<player>{_PLAYER_PATTERN}) (?P<action>joined|left) the game\Z"
    )
    ADVANCEMENT_PATTERN = re.compile(
        rf"^(?P<player>{_PLAYER_PATTERN}) has "
        r"(?:made the advancement|completed the challenge|reached the goal) "
        r"\[(?P<message>.+)\]\Z"
    )
    DEATH_PATTERN = re.compile(
        rf"^(?P<player>{_PLAYER_PATTERN}) (?P<message>"
        r"(?:was slain|was shot|drowned|burned|fell|hit the ground|was blown up|"
        r"was killed|died|withered away|was squashed|was pricked|walked into a cactus|"
        r"suffocated|starved|was impaled|was fireballed|was pummeled|was stung|froze|"
        r"was skewered|was obliterated)(?: .*)?)\Z"
    )

    MAX_READ_BYTES = 1024 * 1024
    MAX_EVENTS_PER_CHECK = 1000
    MAX_SKIPPED_LINE_SCAN_BYTES = 4 * 1024 * 1024

    def __init__(self, log_path: str, *, state_path: str | Path | None = None) -> None:
        self.log_path = Path(log_path)
        self.state_path = Path(state_path) if state_path is not None else None
        self._last_position = 0
        self._file_identity: str | None = None
        self._initialized = False

    @staticmethod
    def _identity(stat: os.stat_result) -> str:
        return f"{int(stat.st_dev)}:{int(stat.st_ino)}"

    def _load_saved_position(self, *, file_identity: str, current_size: int) -> int | None:
        if self.state_path is None or not self.state_path.is_file():
            return None
        try:
            state = AtomicJsonStore(self.state_path).read({}, raise_on_error=True)
            if not isinstance(state, dict) or state.get("version") != 1:
                raise ValueError("Minecraft cursor state version is invalid")
            position = state.get("position")
            if isinstance(position, bool) or not isinstance(position, int) or position < 0:
                raise ValueError("Minecraft cursor position is invalid")
            if state.get("file_identity") != file_identity or position > current_size:
                return 0
            return position
        except Exception as exc:
            # 状态损坏时从当前文件末尾恢复，避免重放整份历史日志造成洪泛。
            logger.error(
                "Minecraft cursor state rejected error_type=%s",
                audit_error_type(exc),
            )
            return current_size

    def _persist_position(self, position: int, file_identity: str) -> None:
        if self.state_path is None:
            return
        AtomicJsonStore(self.state_path).write(
            {
                "version": 1,
                "position": position,
                "file_identity": file_identity,
            }
        )

    def initialize(self) -> bool:
        """读取文件身份；无历史游标时从末尾开始，不回放旧日志。"""

        path_audit = summarize_sensitive(str(self.log_path))
        try:
            with self.log_path.open("rb") as file:
                stat = os.fstat(file.fileno())
            file_identity = self._identity(stat)
            saved_position = self._load_saved_position(
                file_identity=file_identity,
                current_size=stat.st_size,
            )
            self._last_position = stat.st_size if saved_position is None else saved_position
            self._file_identity = file_identity
            self._initialized = True
            logger.info(
                "sensitive_audit operation=minecraft.log_monitor status=initialized "
                "payload_kind=%s payload_length=%d payload_bytes=%d payload_fingerprint=%s "
                "position=%d",
                path_audit.kind,
                path_audit.length,
                path_audit.byte_length,
                path_audit.fingerprint,
                self._last_position,
            )
            return True
        except FileNotFoundError:
            logger.warning(
                "sensitive_audit operation=minecraft.log_monitor status=missing "
                "payload_kind=%s payload_length=%d payload_bytes=%d payload_fingerprint=%s",
                path_audit.kind,
                path_audit.length,
                path_audit.byte_length,
                path_audit.fingerprint,
            )
        except Exception as exc:
            logger.error(
                "sensitive_audit operation=minecraft.log_monitor status=failed "
                "payload_kind=%s payload_length=%d payload_bytes=%d payload_fingerprint=%s "
                "error_type=%s",
                path_audit.kind,
                path_audit.length,
                path_audit.byte_length,
                path_audit.fingerprint,
                audit_error_type(exc),
            )
        self._initialized = False
        return False

    def _count_skipped_lines(self, file: BinaryIO, start: int, end: int) -> int | None:
        byte_count = max(0, end - start)
        if byte_count <= 0:
            return 0
        if byte_count > self.MAX_SKIPPED_LINE_SCAN_BYTES:
            return None
        count = 0
        file.seek(start)
        remaining = byte_count
        while remaining > 0:
            chunk = file.read(min(64 * 1024, remaining))
            if not chunk:
                break
            count += chunk.count(b"\n")
            remaining -= len(chunk)
        return count

    def _bounded_read_start(
        self,
        file: BinaryIO,
        *,
        read_position: int,
        current_size: int,
    ) -> tuple[int, int, int | None]:
        if current_size - read_position <= self.MAX_READ_BYTES:
            return read_position, 0, 0
        read_start = current_size - self.MAX_READ_BYTES
        skipped_lines = self._count_skipped_lines(file, read_position, read_start)
        logger.warning(
            "Minecraft 日志积压超过 %d 字节，仅处理最新 tail",
            self.MAX_READ_BYTES,
        )
        return read_start, read_start - read_position, skipped_lines

    @staticmethod
    def _drop_partial_prefix(
        file: BinaryIO,
        *,
        read_start: int,
        content: bytes,
    ) -> tuple[bytes, int]:
        if read_start <= 0 or not content:
            return content, 0
        file.seek(read_start - 1)
        if file.read(1) == b"\n":
            return content, 0
        newline = content.find(b"\n")
        discarded = len(content) if newline < 0 else newline + 1
        return (b"" if newline < 0 else content[newline + 1 :]), discarded

    @staticmethod
    def _keep_complete_lines(content: bytes, *, content_start: int) -> tuple[bytes, int]:
        if not content:
            return b"", content_start
        last_newline = content.rfind(b"\n")
        if last_newline < 0:
            return b"", content_start
        cursor_after = content_start + last_newline + 1
        return content[: last_newline + 1], cursor_after

    def _read_window(self) -> _ReadWindow:
        # fstat 与读取使用同一文件句柄，避免日志轮换时把两个不同文件拼在一起。
        with self.log_path.open("rb") as file:
            stat = os.fstat(file.fileno())
            current_size = stat.st_size
            current_identity = self._identity(stat)
            cursor_before = self._last_position
            rotated = current_identity != self._file_identity or current_size < cursor_before
            read_position = 0 if rotated else cursor_before
            if rotated:
                logger.info("检测到日志文件轮换，重新开始监控")
            if current_size == read_position:
                return _ReadWindow(
                    b"",
                    cursor_before,
                    current_size,
                    current_identity,
                )

            read_start, skipped_bytes, skipped_lines = self._bounded_read_start(
                file,
                read_position=read_position,
                current_size=current_size,
            )
            file.seek(read_start)
            content = file.read(current_size - read_start)
            content, prefix_bytes = self._drop_partial_prefix(
                file,
                read_start=read_start,
                content=content,
            )
            if prefix_bytes:
                skipped_bytes += prefix_bytes
                if skipped_lines is not None:
                    skipped_lines += 1
            content_start = read_start + prefix_bytes
            content, cursor_after = self._keep_complete_lines(
                content,
                content_start=content_start,
            )
            return _ReadWindow(
                content,
                cursor_before,
                cursor_after,
                current_identity,
                skipped_bytes,
                skipped_lines,
            )

    def check_updates(self) -> LogBatch:
        """读取一个有界窗口；异常时返回不可提交的空批次。"""

        if not self._initialized and not self.initialize():
            return LogBatch(events=[])
        try:
            window = self._read_window()
            events, matched_total = self._parse_events(window.content)
        except Exception as exc:
            self._initialized = False
            path_audit = summarize_sensitive(str(self.log_path))
            logger.error(
                "sensitive_audit operation=minecraft.log_poll status=failed "
                "payload_kind=%s payload_length=%d payload_bytes=%d payload_fingerprint=%s "
                "error_type=%s",
                path_audit.kind,
                path_audit.length,
                path_audit.byte_length,
                path_audit.fingerprint,
                audit_error_type(exc),
            )
            return LogBatch(events=[])

        return LogBatch(
            events=events,
            matched_total=matched_total,
            dropped_events=max(0, matched_total - len(events)),
            skipped_bytes=window.skipped_bytes,
            skipped_lines=window.skipped_lines,
            cursor_before=window.cursor_before,
            cursor_after=window.cursor_after,
            file_identity=window.file_identity,
        )

    def commit(self, batch: LogBatch) -> bool:
        """仅提交仍与当前内存游标衔接的批次，拒绝乱序或重复确认。"""

        if (
            batch.cursor_before is None
            or batch.cursor_after is None
            or batch.file_identity is None
            or batch.cursor_before != self._last_position
            or batch.cursor_after < 0
        ):
            return False
        self._persist_position(batch.cursor_after, batch.file_identity)
        self._last_position = batch.cursor_after
        self._file_identity = batch.file_identity
        self._initialized = True
        return True

    def _parse_events(self, content: bytes) -> tuple[list[LogEvent], int]:
        retained: deque[LogEvent] = deque(maxlen=self.MAX_EVENTS_PER_CHECK)
        matched_total = 0
        for line in content.decode("utf-8", errors="replace").splitlines():
            event = self._parse_line(line)
            if event is not None:
                matched_total += 1
                retained.append(event)
        return list(retained), matched_total

    def _parse_line(self, line: str) -> LogEvent | None:
        prefix = self.LOG_PREFIX_PATTERN.fullmatch(line)
        if prefix is None:
            return None
        body = prefix.group("body")

        match = self.CHAT_PATTERN.fullmatch(body)
        if match is not None:
            return LogEvent(LogEventType.CHAT, match.group("player"), match.group("message"))

        match = self.PRESENCE_PATTERN.fullmatch(body)
        if match is not None:
            event_type = (
                LogEventType.JOIN if match.group("action") == "joined" else LogEventType.LEAVE
            )
            return LogEvent(event_type, match.group("player"))

        match = self.ADVANCEMENT_PATTERN.fullmatch(body)
        if match is not None:
            return LogEvent(
                LogEventType.ADVANCEMENT,
                match.group("player"),
                match.group("message"),
            )

        match = self.DEATH_PATTERN.fullmatch(body)
        if match is not None:
            return LogEvent(LogEventType.DEATH, match.group("player"), match.group("message"))
        return None


__all__ = ["LogBatch", "LogEvent", "LogEventType", "LogMonitor"]
