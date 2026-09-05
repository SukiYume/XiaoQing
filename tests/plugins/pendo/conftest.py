"""Pendo 测试数据库、Web 客户端与连接生命周期门禁。"""

from __future__ import annotations

import functools
from pathlib import Path

import pytest

from tests.helpers.pendo_leak_guard import (
    PendoDatabaseTracker,
    enforce_pendo_database_cleanup,
    pendo_test_origin,
)
from tests.helpers.pendo_test_support import managed_pendo_database


@pytest.fixture(name="db")
def pendo_db(tmp_path: Path):
    """为只需要基础数据库的测试提供统一 fixture。"""

    with managed_pendo_database(tmp_path) as database:
        yield database


@pytest.fixture
def temp_db(db):
    """为 Pendo Web 路由和服务测试提供统一隔离数据库。"""

    return db


@functools.lru_cache(maxsize=1)
def _pendo_web_test_dependencies():
    """只解析一次 Web 依赖，避免模块隔离测试污染后续 fixture。"""

    from fastapi.testclient import TestClient

    from plugins.pendo.web.server import create_app

    return TestClient, create_app


@pytest.fixture
def client(temp_db):
    """创建并关闭 Pendo FastAPI 测试客户端。"""

    from plugins.pendo.utils import db_ops
    from plugins.pendo.web import auth, deps

    try:
        test_client_class, create_app = _pendo_web_test_dependencies()
    except ModuleNotFoundError:
        pytest.skip("fastapi is not installed in this environment")
    except RuntimeError as exc:
        if "requires the httpx package" not in str(exc):
            raise
        pytest.skip("httpx is not installed in this environment")

    previous_singleton = db_ops._db_singleton
    previous_web_db    = deps._db_instance
    with auth._AUTH_LOCK:
        previous_auth_db = auth._AUTH_DATABASE
    try:
        with test_client_class(create_app(temp_db)) as test_client:
            yield test_client
    finally:
        db_ops.set_database_singleton(previous_singleton)
        deps._db_instance = previous_web_db
        with auth._AUTH_LOCK:
            auth._AUTH_DATABASE = previous_auth_db


@pytest.fixture(autouse=True)
def pendo_database_leak_guard():
    """测试结束时检查并紧急关闭遗漏的 Pendo 数据库连接。"""

    from plugins.pendo.services.db import Database

    tracker       = PendoDatabaseTracker()
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
