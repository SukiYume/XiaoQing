"""Contract tests for Pendo's state-owning runtime service boundary."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.pendo.config import PendoRuntimeSettings
from plugins.pendo.services.runtime import PendoRuntimeService
from plugins.pendo.utils import db_ops


class _DummyDatabase:
    def __init__(self, path: str = ":memory:") -> None:
        self.db_path = path
        self.cleanup_count = 0

    def cleanup(self) -> None:
        self.cleanup_count += 1


def test_runtime_owns_database_path_and_closes_shared_object_once(tmp_path) -> None:
    db_ops.set_database_singleton(None)
    service = PendoRuntimeService()
    context = SimpleNamespace(data_dir=tmp_path / "pendo")

    database = service.open_database(context, _DummyDatabase)
    assert service.database is database
    assert service.database_path == context.data_dir / "pendo.db"
    assert db_ops.get_database(SimpleNamespace()) is database

    assert service.close_databases() == ()
    assert database.cleanup_count == 1
    assert service.database is None
    assert service.database_path is None


def test_runtime_never_overwrites_another_database_generation(tmp_path) -> None:
    existing = _DummyDatabase("existing")
    candidates: list[_DummyDatabase] = []
    db_ops.set_database_singleton(existing)
    service = PendoRuntimeService()

    def factory(path: str) -> _DummyDatabase:
        candidate = _DummyDatabase(path)
        candidates.append(candidate)
        return candidate

    try:
        with pytest.raises(RuntimeError, match="already owned"):
            service.open_database(SimpleNamespace(data_dir=tmp_path), factory)

        assert service.database is None
        assert len(candidates) == 1
        assert candidates[0].cleanup_count == 1
        assert db_ops.get_database(SimpleNamespace()) is existing
    finally:
        db_ops.set_database_singleton(None)


def test_runtime_unsubscribes_once_even_when_unsubscribe_fails() -> None:
    service = PendoRuntimeService()
    calls: list[str] = []

    def unsubscribe() -> None:
        calls.append("unsubscribe")
        raise RuntimeError("subscription cleanup failed")

    assert service.bind_config_subscription(unsubscribe) is True
    assert service.has_config_subscription is True
    with pytest.raises(RuntimeError, match="subscription cleanup failed"):
        service.unsubscribe_config()

    assert service.has_config_subscription is False
    service.unsubscribe_config()
    assert calls == ["unsubscribe"]


def test_runtime_reconfigures_web_endpoint_without_dual_start() -> None:
    calls: list[object] = []
    state = {"running": True}

    class _WebBackend:
        @staticmethod
        def is_running() -> bool:
            return state["running"]

        @staticmethod
        def stop() -> bool:
            calls.append("stop")
            state["running"] = False
            return True

        @staticmethod
        def start(database) -> bool:
            calls.append(("start", database))
            state["running"] = True
            return True

    service = PendoRuntimeService(web_backend_factory=_WebBackend)
    database = _DummyDatabase()
    before = PendoRuntimeSettings(web_enabled=True, web_port=12001)
    after = PendoRuntimeSettings(web_enabled=True, web_port=12002)

    service.reconfigure_web(database, before, after)

    assert calls == ["stop", ("start", database)]
    assert state["running"] is True
