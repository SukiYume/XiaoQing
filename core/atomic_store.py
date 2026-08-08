"""Core 与插件共用的崩溃安全本地持久化原语。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")
MISSING_ETAG = "missing"
_ATOMIC_REPLACE_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08)


@dataclass
class _LockEntry:
    lock: threading.RLock
    users: int = 0


_POOL_GUARD = threading.RLock()
_PATH_LOCKS: dict[Path, _LockEntry] = {}


def _canonical_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


@contextmanager
def keyed_path_lock(path: Path) -> Iterator[None]:
    """锁定规范化路径，并在最后一个使用者退出后回收锁条目。"""
    key = _canonical_path(path)
    with _POOL_GUARD:
        entry = _PATH_LOCKS.get(key)
        if entry is None:
            entry = _LockEntry(threading.RLock())
            _PATH_LOCKS[key] = entry
        entry.users += 1
    try:
        with entry.lock:
            yield
    finally:
        with _POOL_GUARD:
            entry.users -= 1
            if entry.users == 0:
                _PATH_LOCKS.pop(key, None)


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """通过同目录临时文件、fsync 和原子替换写入字节。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # Windows 的杀毒软件或索引器可能短暂占用刚关闭的目标文件。只对
        # PermissionError 做有限退避；其他写盘错误仍立即暴露给调用方。
        for delay in _ATOMIC_REPLACE_RETRY_DELAYS:
            try:
                os.replace(temp_name, path)
                break
            except PermissionError:
                time.sleep(delay)
        else:
            os.replace(temp_name, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: Path, payload: str) -> None:
    atomic_write_bytes(path, payload.encode("utf-8"))


class AtomicJsonStore:
    """带有效备份恢复的原子 JSON 读写器。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.backup_path = self.path.with_name(f"{self.path.name}.bak")

    @staticmethod
    def _decode(payload: bytes) -> Any:
        return json.loads(payload.decode("utf-8"))

    @staticmethod
    def _encode(value: Any) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ).encode("utf-8")

    def _read_payload_unlocked(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    def _read_unlocked(self, default: T, *, raise_on_error: bool) -> T | Any:
        payload = self._read_payload_unlocked()
        if payload is None:
            return default
        try:
            return self._decode(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as primary_error:
            if raise_on_error:
                # Security-sensitive callers use strict reads so a stale backup
                # cannot silently roll back a newer authorization decision.
                raise primary_error from None
            try:
                backup_payload = self.backup_path.read_bytes()
                recovered = self._decode(backup_payload)
            except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError):
                return default
            atomic_write_bytes(self.path, backup_payload)
            return recovered

    def read(self, default: T, *, raise_on_error: bool = False) -> T | Any:
        with keyed_path_lock(self.path):
            return self._read_unlocked(default, raise_on_error=raise_on_error)

    def _write_unlocked(self, value: Any) -> None:
        payload = self._encode(value)
        current = self._read_payload_unlocked()
        if current is not None:
            try:
                self._decode(current)
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            else:
                atomic_write_bytes(self.backup_path, current)
        atomic_write_bytes(self.path, payload)

    def write(self, value: Any) -> None:
        with keyed_path_lock(self.path):
            self._write_unlocked(value)

    def write_with_backup(self, value: Any) -> None:
        """Atomically publish a value and make the backup an explicit copy of it."""

        payload = self._encode(value)
        with keyed_path_lock(self.path):
            atomic_write_bytes(self.path, payload)
            atomic_write_bytes(self.backup_path, payload)
