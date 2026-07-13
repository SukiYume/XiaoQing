"""Pendo-only resource lifecycle guard."""

from __future__ import annotations

import functools
from pathlib import Path

import pytest

from tests.helpers.pendo_leak_guard import (
    PendoDatabaseTracker,
    enforce_pendo_database_cleanup,
    pendo_test_origin,
)


def _is_pendo_test(request: pytest.FixtureRequest) -> bool:
    path = Path(str(request.node.path)).resolve()
    return path.parent == Path(__file__).resolve().parent and path.name.startswith("test_pendo")


@pytest.fixture(autouse=True)
def pendo_database_leak_guard(request: pytest.FixtureRequest):
    """Fail after emergency cleanup when a Pendo test leaves registered connections."""
    if not _is_pendo_test(request):
        yield None
        return

    from plugins.pendo.services.db import Database

    tracker = PendoDatabaseTracker()
    original_init = Database.__init__

    @functools.wraps(original_init)
    def tracked_init(database, db_path, *args, **kwargs):
        origin = pendo_test_origin()
        try:
            original_init(database, db_path, *args, **kwargs)
        finally:
            tracker.record(database, db_path, origin)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(Database, "__init__", tracked_init)
        try:
            yield tracker
        finally:
            enforce_pendo_database_cleanup(tracker.records)
