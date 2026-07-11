"""Crash-safe local persistence primitives shared by core and plugins."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar


T = TypeVar("T")
MISSING_ETAG = "missing"


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
    """Lock one canonical path and remove idle lock entries after use."""
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


def active_keyed_lock_count() -> int:
    """Expose pool size for leak regression tests and diagnostics."""
    with _POOL_GUARD:
        return len(_PATH_LOCKS)


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Write bytes with same-directory temp, fsync and atomic replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
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


def _etag(payload: bytes | None) -> str:
    return hashlib.sha256(payload).hexdigest() if payload is not None else MISSING_ETAG


class AtomicJsonStore:
    """Atomic JSON store with backup recovery, mutate and content CAS."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.backup_path = self.path.with_name(f"{self.path.name}.bak")

    @staticmethod
    def _decode(payload: bytes) -> Any:
        return json.loads(payload.decode("utf-8"))

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
            try:
                backup_payload = self.backup_path.read_bytes()
                recovered = self._decode(backup_payload)
            except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError):
                if raise_on_error:
                    raise primary_error
                return default
            atomic_write_bytes(self.path, backup_payload)
            return recovered

    def read(self, default: T, *, raise_on_error: bool = False) -> T | Any:
        with keyed_path_lock(self.path):
            return self._read_unlocked(default, raise_on_error=raise_on_error)

    def read_versioned(self, default: T) -> tuple[T | Any, str]:
        with keyed_path_lock(self.path):
            value = self._read_unlocked(default, raise_on_error=False)
            return value, _etag(self._read_payload_unlocked())

    def _write_unlocked(self, value: Any) -> str:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ).encode("utf-8")
        current = self._read_payload_unlocked()
        if current is not None:
            try:
                self._decode(current)
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            else:
                atomic_write_bytes(self.backup_path, current)
        atomic_write_bytes(self.path, payload)
        return _etag(payload)

    def write(self, value: Any) -> str:
        with keyed_path_lock(self.path):
            return self._write_unlocked(value)

    def compare_and_swap(self, expected_etag: str, value: Any) -> tuple[bool, str]:
        with keyed_path_lock(self.path):
            current_etag = _etag(self._read_payload_unlocked())
            if current_etag != expected_etag:
                return False, current_etag
            return True, self._write_unlocked(value)

    def mutate(self, default: T, callback: Callable[[T | Any], Any]) -> Any:
        with keyed_path_lock(self.path):
            value = self._read_unlocked(default, raise_on_error=False)
            result = callback(value)
            self._write_unlocked(value if result is None else result)
            return value if result is None else result
