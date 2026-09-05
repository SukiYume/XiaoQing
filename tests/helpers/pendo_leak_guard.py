# 数据库泄漏防护：跟踪测试创建的 Pendo 实例并验证连接回收。
"""Fail-closed tracking helpers for Pendo Database test instances."""

from __future__ import annotations

import sys
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest


@dataclass(frozen=True)
class PendoDatabaseRecord:
    """Strongly retain one Database and the test source that constructed it."""

    database: Any
    db_path: str
    origin: str


def pendo_test_origin() -> str:
    """Return the closest Pendo test frame without retaining frame objects."""
    for frame in reversed(traceback.extract_stack()[:-1]):
        filename = Path(frame.filename).name
        if filename.startswith("test_pendo"):
            return f"{filename}:{frame.lineno} in {frame.name}"
    return "unknown Pendo test source"


def _connection_thread_ids(database: Any) -> tuple[int, ...]:
    lock     = getattr(database, "_lock", None)
    registry = getattr(database, "_all_connections", None)
    if lock is None or not isinstance(registry, dict):
        raise RuntimeError("Database lifecycle registry is unavailable or malformed")
    with lock:
        thread_ids: list[int] = []
        for slot in registry.values():
            if not isinstance(slot, tuple) or len(slot) != 2:
                raise RuntimeError("Database lifecycle registry contains a malformed slot")
            thread_ids.append(int(slot[0]))
        return tuple(sorted(thread_ids))


def enforce_pendo_database_cleanup(records: list[PendoDatabaseRecord]) -> None:
    """Raise a leak failure and emergency-clean without masking that failure."""
    leaked: list[tuple[PendoDatabaseRecord, tuple[int, ...], str | None]] = []
    seen: set[int]                                                        = set()
    for record in records:
        identity = id(record.database)
        if identity in seen:
            continue
        seen.add(identity)
        try:
            thread_ids = _connection_thread_ids(record.database)
        except Exception as exc:  # pragma: no cover - exact variants covered by behavior test
            leaked.append((record, (), f"{type(exc).__name__}: {exc}"))
        else:
            if thread_ids:
                leaked.append((record, thread_ids, None))

    if not leaked:
        return

    slot_count = sum(len(thread_ids) for _record, thread_ids, _error in leaked)
    details    = []
    for record, thread_ids, inspection_error in leaked:
        detail = (
            f"origin={record.origin} db_path={record.db_path!r} "
            f"slot_count={len(thread_ids)} thread_ids={list(thread_ids)!r}"
        )
        if inspection_error:
            detail += f" lifecycle_inspection_failed={inspection_error}"
        details.append(detail)
    message = (
        "Pendo Database leak guard detected "
        f"{slot_count} unclosed connection slot(s) across {len(leaked)} instance(s); "
        + "; ".join(details)
    )
    try:
        pytest.fail(message, pytrace=False)
    finally:
        cleanup_errors: list[str] = []
        for record, _thread_ids, _inspection_error in leaked:
            try:
                record.database.cleanup()
            except BaseException as exc:  # cleanup must never mask the recorded leak failure
                cleanup_errors.append(f"origin={record.origin} error={type(exc).__name__}: {exc}")
        active_failure = sys.exc_info()[1]
        if cleanup_errors and active_failure is not None:
            note     = "Emergency Pendo cleanup also failed: " + "; ".join(cleanup_errors)
            add_note = getattr(active_failure, "add_note", None)
            if callable(add_note):
                try:
                    add_note(note)
                except BaseException:  # pragma: no cover - never mask the leak failure
                    print(note, file=sys.stderr)
            else:  # pragma: no cover - Python 3.10 compatibility
                print(note, file=sys.stderr)


class PendoDatabaseTracker:
    """Thread-safe strong-reference registry populated by the patched constructor."""

    def __init__(self) -> None:
        self.records: list[PendoDatabaseRecord] = []
        self._lock                              = threading.Lock()

    def record(self, database: Any, db_path: object, origin: str) -> None:
        with self._lock:
            self.records.append(
                PendoDatabaseRecord(
                    database = database,
                    db_path  = str(db_path),
                    origin   = origin,
                )
            )
