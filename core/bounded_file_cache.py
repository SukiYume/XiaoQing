# 有界文件缓存：原子落盘配合过期时间、最近访问顺序和字节容量。
"""Small crash-safe disk cache with TTL, LRU, entry and byte limits."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .atomic_store import atomic_write_bytes, keyed_path_lock

_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class FileCacheLimits:
    max_entries: int
    max_bytes  : int
    ttl_seconds: float

    def __post_init__(self) -> None:
        if self.max_entries <= 0 or self.max_bytes <= 0 or self.ttl_seconds <= 0:
            raise ValueError("file cache limits must be positive")


class BoundedFileCache:
    """Manage one dedicated cache directory without an in-memory index.

    Each operation enumerates the dedicated directory and stats its visible
    regular files while holding the process-local path lock.  That intentional
    O(n) work keeps TTL/LRU/byte limits correct even when callers construct
    separate cache instances or another worker has changed the directory.
    Cache limits should therefore stay in the low-thousands range, and large
    cache directories should use the explicit ``prune()`` maintenance path.
    """

    def __init__(self, directory: Path, limits: FileCacheLimits) -> None:
        self.directory  = Path(directory)
        self.limits     = limits
        self._lock_path = self.directory / ".budget.lock"

    @staticmethod
    def _validate_name(name: str) -> str:
        if not isinstance(name, str) or _SAFE_NAME.fullmatch(name) is None:
            raise ValueError("unsafe cache filename")
        return name

    def _entries_unlocked(self) -> list[tuple[Path, os.stat_result]]:
        """列出缓存目录中的普通文件，跳过隐藏文件（锁文件）和符号链接。"""

        entries: list[tuple[Path, os.stat_result]] = []
        try:
            children = list(self.directory.iterdir())
        except FileNotFoundError:
            return entries
        for path in children:
            if path.name.startswith(".") or path.is_symlink():
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            if path.is_file():
                entries.append((path, stat))
        return entries

    def _prune_unlocked(self, *, now: float, protect: Path | None = None) -> None:
        """先删过期条目，再按最旧优先淘汰到条目数和字节数都达标；protect 永不淘汰。"""

        retained: list[tuple[Path, os.stat_result]] = []
        for path, stat in self._entries_unlocked():
            if path != protect and now - stat.st_mtime > self.limits.ttl_seconds:
                path.unlink(missing_ok=True)
            else:
                retained.append((path, stat))

        total_bytes = sum(stat.st_size for _, stat in retained)
        retained.sort(key=lambda item: (item[1].st_mtime_ns, item[0].name))
        while len(retained) > self.limits.max_entries or total_bytes > self.limits.max_bytes:
            victim_index = next(
                (index for index, (path, _) in enumerate(retained) if path != protect),
                None,
            )
            if victim_index is None:
                break
            path, stat = retained.pop(victim_index)
            path.unlink(missing_ok=True)
            total_bytes -= stat.st_size

    def get_any(self, names: tuple[str, ...]) -> Path | None:
        validated = tuple(self._validate_name(name) for name in names)
        self.directory.mkdir(parents=True, exist_ok=True)
        with keyed_path_lock(self._lock_path):
            now = time.time()
            self._prune_unlocked(now=now)
            for name in validated:
                path = self.directory / name
                if path.is_symlink() or not path.is_file():
                    continue
                os.utime(path, (now, now))
                return path
        return None

    def _prepare(self, name: str, payload: bytes) -> Path | None:
        """校验文件名并确保目录存在；超过总容量的单条内容直接拒绝缓存。"""

        validated = self._validate_name(name)
        if len(payload) > self.limits.max_bytes:
            return None
        self.directory.mkdir(parents=True, exist_ok=True)
        return self.directory / validated

    def _store_unlocked(self, path: Path, payload: bytes, now: float) -> Path | None:
        """写入并刷新访问时间，随后按容量淘汰；被淘汰时返回 None。"""

        atomic_write_bytes(path, payload)
        os.utime(path, (now, now))
        self._prune_unlocked(now=now, protect=path)
        return path if path.is_file() else None

    def put(self, name: str, payload: bytes) -> Path | None:
        path = self._prepare(name, payload)
        if path is None:
            return None
        with keyed_path_lock(self._lock_path):
            return self._store_unlocked(path, payload, time.time())

    def put_if_absent(self, name: str, payload: bytes) -> tuple[Path | None, bool]:
        """Atomically store a content-addressed entry and report whether it was new."""

        path = self._prepare(name, payload)
        if path is None:
            return None, False
        with keyed_path_lock(self._lock_path):
            now = time.time()
            # 已存在的内容寻址条目只刷新访问时间，不重写文件。
            if not path.is_symlink() and path.is_file():
                os.utime(path, (now, now))
                self._prune_unlocked(now=now, protect=path)
                return path, False
            stored = self._store_unlocked(path, payload, now)
        return stored, stored is not None

    def prune(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with keyed_path_lock(self._lock_path):
            self._prune_unlocked(now=time.time())
