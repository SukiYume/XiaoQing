from __future__ import annotations

import gc
import subprocess
import sys
import threading
import weakref
from pathlib import Path

import pytest

from plugins.pendo.services.db import Database
from tests.helpers.pendo_leak_guard import (
    PendoDatabaseRecord,
    PendoDatabaseTracker,
    enforce_pendo_database_cleanup,
)

ROOT = Path(__file__).resolve().parents[2]


class _FakeDatabase:
    def __init__(self, slots: int) -> None:
        self._lock = threading.Lock()
        self._all_connections = {index: (1000 + index, object()) for index in range(slots)}
        self.cleanup_calls = 0
        self.failure_was_active_during_cleanup = False

    def cleanup(self) -> None:
        self.cleanup_calls += 1
        self.failure_was_active_during_cleanup = isinstance(
            sys.exc_info()[1], pytest.fail.Exception
        )
        self._all_connections.clear()


class _MalformedDatabase(_FakeDatabase):
    def __init__(self) -> None:
        super().__init__(slots=0)
        self._all_connections = None

    def cleanup(self) -> None:
        self.cleanup_calls += 1
        self._all_connections = {}


class _CleanupFailureDatabase(_FakeDatabase):
    def cleanup(self) -> None:
        self.cleanup_calls += 1
        self.failure_was_active_during_cleanup = isinstance(
            sys.exc_info()[1], pytest.fail.Exception
        )
        raise KeyboardInterrupt("cleanup failure must not mask the leak")


def test_leak_guard_detects_then_emergency_cleans_and_still_fails() -> None:
    database = _FakeDatabase(slots=2)
    records = [PendoDatabaseRecord(database, "leaked.db", "test_source.py:12")]

    with pytest.raises(pytest.fail.Exception, match=r"2 unclosed connection slot.*test_source"):
        enforce_pendo_database_cleanup(records)

    assert database.cleanup_calls == 1
    assert database.failure_was_active_during_cleanup
    assert database._all_connections == {}


def test_leak_guard_does_not_cleanup_an_already_clean_database() -> None:
    database = _FakeDatabase(slots=0)

    enforce_pendo_database_cleanup([PendoDatabaseRecord(database, "clean.db", "test_source.py:20")])

    assert database.cleanup_calls == 0


def test_leak_guard_fails_closed_when_registry_cannot_be_inspected() -> None:
    database = _MalformedDatabase()

    with pytest.raises(pytest.fail.Exception, match="lifecycle_inspection_failed"):
        enforce_pendo_database_cleanup(
            [PendoDatabaseRecord(database, "malformed.db", "test_source.py:25")]
        )

    assert database.cleanup_calls == 1


def test_emergency_cleanup_base_exception_does_not_mask_leak_failure() -> None:
    database = _CleanupFailureDatabase(slots=1)

    with pytest.raises(pytest.fail.Exception, match="1 unclosed connection slot"):
        enforce_pendo_database_cleanup(
            [PendoDatabaseRecord(database, "cleanup-fails.db", "test_source.py:28")]
        )

    assert database.cleanup_calls == 1
    assert database.failure_was_active_during_cleanup


def test_tracker_strongly_retains_database_until_teardown() -> None:
    tracker = PendoDatabaseTracker()
    database = _FakeDatabase(slots=0)
    reference = weakref.ref(database)
    tracker.record(database, "retained.db", "test_source.py:30")

    del database
    gc.collect()

    assert reference() is tracker.records[0].database


@pytest.fixture
def managed_database(tmp_path):
    database = Database(str(tmp_path / "managed.db"))
    try:
        yield database
    finally:
        database.cleanup()


def test_yield_fixture_cleanup_runs_before_autouse_leak_check(managed_database) -> None:
    assert managed_database._all_connections


def test_request_finalizer_cleanup_runs_before_autouse_leak_check(tmp_path, request) -> None:
    database = Database(str(tmp_path / "finalizer-managed.db"))
    request.addfinalizer(database.cleanup)
    assert database._all_connections


def test_autouse_fixture_tracks_real_database_instance(tmp_path, pendo_database_leak_guard) -> None:
    database = Database(str(tmp_path / "tracked.db"))
    try:
        assert any(record.database is database for record in pendo_database_leak_guard.records)
    finally:
        database.cleanup()


def test_non_pendo_plugin_path_does_not_import_database_module() -> None:
    probe = Path(__file__).with_name("_pendo_guard_nonpendo_probe.py")
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", str(probe), "-q"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
