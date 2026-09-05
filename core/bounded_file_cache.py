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
    max_bytes: int
    ttl_seconds: float

    def __post_init__(self) -> None:
        if self.max_entries <= 0 or self.max_bytes <= 0 or self.ttl_seconds <= 0:
            raise ValueError("file cache limits must be positive")


class BoundedFileCache:
    """Manage one dedicated cache directory without an in-memory index.

    Each operation enumerates the dedicated directory and stats its visible
    regular files while holding the process-local path lock.  That intentional
    O(n) work keeps TTL/LRU/byte limits correct even when callers construct
    separate cache instances or another worker has changed the directory;
    cache limits should therefore stay in the low-thousands range, and large
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
        entries                                     = self._entries_unlocked()
        retained: list[tuple[Path, os.stat_result]] = []
        for path, stat in entries:
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

    def put(self, name: str, payload: bytes) -> Path | None:
        validated = self._validate_name(name)
        if len(payload) > self.limits.max_bytes:
            return None
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / validated
        with keyed_path_lock(self._lock_path):
            atomic_write_bytes(path, payload)
            now = time.time()
            os.utime(path, (now, now))
            self._prune_unlocked(now=now, protect=path)
            if not path.is_file():
                return None
        return path

    def put_if_absent(self, name: str, payload: bytes) -> tuple[Path | None, bool]:
        """Atomically store a content-addressed entry and report whether it was new."""
        validated = self._validate_name(name)
        if len(payload) > self.limits.max_bytes:
            return None, False
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / validated
        with keyed_path_lock(self._lock_path):
            now = time.time()
            if not path.is_symlink() and path.is_file():
                os.utime(path, (now, now))
                self._prune_unlocked(now=now, protect=path)
                return path, False
            atomic_write_bytes(path, payload)
            os.utime(path, (now, now))
            self._prune_unlocked(now=now, protect=path)
            if not path.is_file():
                return None, False
        return path, True

    def prune(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with keyed_path_lock(self._lock_path):
            self._prune_unlocked(now=time.time())
