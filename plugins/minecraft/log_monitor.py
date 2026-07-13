"""
Minecraft 服务器日志监控

监控服务器日志文件，提取玩家聊天消息和事件。
"""

import asyncio
import logging
import re
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, overload

from core.sensitive_audit import summarize_sensitive

from .audit import audit_error_type

logger = logging.getLogger(__name__)


class LogEventType(Enum):
    """日志事件类型"""

    CHAT = "chat"  # 玩家聊天
    JOIN = "join"  # 玩家加入
    LEAVE = "leave"  # 玩家离开
    DEATH = "death"  # 玩家死亡
    ADVANCEMENT = "advancement"  # 获得成就
    UNKNOWN = "unknown"


@dataclass
class LogEvent:
    """日志事件"""

    event_type: LogEventType
    player: str | None
    message: str | None
    raw_line: str
    timestamp: str | None = None


@dataclass
class LogBatch:
    """One bounded log poll and explicit information about discarded input."""

    events: list[LogEvent]
    matched_total: int = 0
    dropped_events: int = 0
    skipped_bytes: int = 0
    skipped_lines: int | None = 0

    def __len__(self) -> int:
        return len(self.events)

    def __iter__(self) -> Iterator[LogEvent]:
        return iter(self.events)

    @overload
    def __getitem__(self, index: int) -> LogEvent: ...

    @overload
    def __getitem__(self, index: slice) -> list[LogEvent]: ...

    def __getitem__(self, index: int | slice) -> Any:
        return self.events[index]


class LogMonitor:
    """
    日志文件监控器

    追踪日志文件的变化，解析新增的日志行。
    """

    # 日志行模式
    # 格式: [HH:MM:SS] [Thread/INFO]: <Player> Message
    # Paper 服务器可能使用异步线程: [Async Chat Thread - #N/INFO]
    CHAT_PATTERN = re.compile(r"\[[\d:]+\] \[[^\]]+/INFO\]: <(\w+)> (.+)")

    # 玩家加入: Player joined the game
    JOIN_PATTERN = re.compile(r"\[[\d:]+\] \[[^\]]+/INFO\]: (\w+) joined the game")

    # 玩家离开: Player left the game
    LEAVE_PATTERN = re.compile(r"\[[\d:]+\] \[[^\]]+/INFO\]: (\w+) left the game")

    # 玩家死亡 (各种死亡消息)
    DEATH_PATTERNS = [
        re.compile(
            r"\[[\d:]+\] \[[^\]]+/INFO\]: (\w+) (was slain|was shot|drowned|burned|fell|hit the ground|was blown up|was killed|died|withered away|was squashed|was pricked|walked into a cactus|suffocated|starved|was impaled|was fireballed|was pummeled|was stung|froze|was skewered|was obliterated)"
        ),
    ]

    # 获得成就
    ADVANCEMENT_PATTERN = re.compile(
        r"\[[\d:]+\] \[[^\]]+/INFO\]: (\w+) has (made the advancement|completed the challenge|reached the goal) \[(.+)\]"
    )

    # 时间戳提取
    TIMESTAMP_PATTERN = re.compile(r"\[([\d:]+)\]")
    MAX_READ_BYTES = 1024 * 1024
    MAX_EVENTS_PER_CHECK = 1000
    MAX_SKIPPED_LINE_SCAN_BYTES = 4 * 1024 * 1024

    def __init__(self, log_path: str):
        self.log_path = Path(log_path)
        self._last_position = 0
        self._last_size = 0
        self._initialized = False

    def initialize(self) -> bool:
        """初始化监控器，定位到文件末尾"""
        path_audit = summarize_sensitive(str(self.log_path))
        if not self.log_path.exists():
            logger.warning(
                "sensitive_audit operation=minecraft.log_monitor status=missing "
                "payload_kind=%s payload_length=%d payload_bytes=%d payload_fingerprint=%s",
                path_audit.kind,
                path_audit.length,
                path_audit.byte_length,
                path_audit.fingerprint,
            )
            return False

        try:
            self._last_size = self.log_path.stat().st_size
            self._last_position = self._last_size  # 从文件末尾开始
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
            return False

    def _count_skipped_lines(self, start: int, end: int) -> int | None:
        byte_count = max(0, end - start)
        if byte_count <= 0:
            return 0
        if byte_count > self.MAX_SKIPPED_LINE_SCAN_BYTES:
            return None
        count = 0
        with self.log_path.open("rb") as file:
            file.seek(start)
            remaining = byte_count
            while remaining > 0:
                chunk = file.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                count += chunk.count(b"\n")
                remaining -= len(chunk)
        return count

    def check_updates(self) -> LogBatch:
        """
        检查日志文件更新

        Returns:
            新日志事件列表
        """
        if not self._initialized:
            if not self.initialize():
                return LogBatch(events=[])

        if not self.log_path.exists():
            logger.warning("日志文件不存在")
            return LogBatch(events=[])

        retained_events: deque[LogEvent] = deque(maxlen=self.MAX_EVENTS_PER_CHECK)
        matched_total = 0
        skipped_bytes = 0
        skipped_lines: int | None = 0

        try:
            current_size = self.log_path.stat().st_size

            # 文件可能被轮换（新的 latest.log）
            if current_size < self._last_position:
                logger.info("检测到日志文件轮换，重新开始监控")
                self._last_position = 0

            if current_size == self._last_position:
                return LogBatch(events=[])  # 没有新内容

            # 只读取最新的有界 tail，避免一次更新把整个日志装入内存。
            read_start = self._last_position
            if current_size - read_start > self.MAX_READ_BYTES:
                read_start = current_size - self.MAX_READ_BYTES
                skipped_bytes = read_start - self._last_position
                skipped_lines = self._count_skipped_lines(self._last_position, read_start)
                logger.warning(
                    "Minecraft 日志积压超过 %d 字节，仅处理最新 tail",
                    self.MAX_READ_BYTES,
                )
            with open(self.log_path, "rb") as f:
                f.seek(read_start)
                new_content = f.read(self.MAX_READ_BYTES)
            self._last_position = current_size

            if read_start > 0 and new_content:
                with self.log_path.open("rb") as file:
                    file.seek(read_start - 1)
                    starts_at_line_boundary = file.read(1) == b"\n"
                if not starts_at_line_boundary:
                    newline = new_content.find(b"\n")
                    discarded = len(new_content) if newline < 0 else newline + 1
                    skipped_bytes += discarded
                    if skipped_lines is not None:
                        skipped_lines += 1
                    new_content = b"" if newline < 0 else new_content[newline + 1 :]

            # 解析每一行
            decoded = new_content.decode("utf-8", errors="replace")
            for line in decoded.splitlines():
                if line.strip():
                    event = self._parse_line(line)
                    if event and event.event_type != LogEventType.UNKNOWN:
                        matched_total += 1
                        retained_events.append(event)

            self._last_size = current_size

        except Exception as exc:
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

        events = list(retained_events)
        return LogBatch(
            events=events,
            matched_total=matched_total,
            dropped_events=max(0, matched_total - len(events)),
            skipped_bytes=skipped_bytes,
            skipped_lines=skipped_lines,
        )

    async def check_updates_async(self) -> LogBatch:
        """在线程池中检查更新，避免阻塞事件循环"""
        return await asyncio.to_thread(self.check_updates)

    def _parse_line(self, line: str) -> LogEvent | None:
        """解析单行日志"""
        # 提取时间戳
        timestamp = None
        ts_match = self.TIMESTAMP_PATTERN.search(line)
        if ts_match:
            timestamp = ts_match.group(1)

        # 尝试匹配聊天消息
        match = self.CHAT_PATTERN.search(line)
        if match:
            return LogEvent(
                event_type=LogEventType.CHAT,
                player=match.group(1),
                message=match.group(2),
                raw_line=line,
                timestamp=timestamp,
            )

        # 尝试匹配玩家加入
        match = self.JOIN_PATTERN.search(line)
        if match:
            return LogEvent(
                event_type=LogEventType.JOIN,
                player=match.group(1),
                message=None,
                raw_line=line,
                timestamp=timestamp,
            )

        # 尝试匹配玩家离开
        match = self.LEAVE_PATTERN.search(line)
        if match:
            return LogEvent(
                event_type=LogEventType.LEAVE,
                player=match.group(1),
                message=None,
                raw_line=line,
                timestamp=timestamp,
            )

        # 尝试匹配死亡消息
        for pattern in self.DEATH_PATTERNS:
            match = pattern.search(line)
            if match:
                return LogEvent(
                    event_type=LogEventType.DEATH,
                    player=match.group(1),
                    message=match.group(2),
                    raw_line=line,
                    timestamp=timestamp,
                )

        # 尝试匹配成就
        match = self.ADVANCEMENT_PATTERN.search(line)
        if match:
            return LogEvent(
                event_type=LogEventType.ADVANCEMENT,
                player=match.group(1),
                message=match.group(3),
                raw_line=line,
                timestamp=timestamp,
            )

        return None

    def reset(self) -> None:
        """重置监控位置到文件末尾"""
        self._initialized = False
        self.initialize()
