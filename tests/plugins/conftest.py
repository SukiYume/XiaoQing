"""插件测试共享 fixture 与 Pendo 资源生命周期门禁。"""

from __future__ import annotations

import functools
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.helpers.pendo_leak_guard import (
    PendoDatabaseTracker,
    enforce_pendo_database_cleanup,
    pendo_test_origin,
)


@pytest.fixture
def temp_data_dir():
    """提供短路径的独立数据目录，避免超长参数化名称进入 Windows 路径。"""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield Path(temp_dir)


@pytest.fixture
def qingpet_db(tmp_path: Path):
    """为 QingPet 测试提供独立数据库，并统一走公开清理入口。"""
    from plugins.qingpet.services.database import Database

    database = Database(str(tmp_path / "qingpet.db"))
    try:
        yield database
    finally:
        database.cleanup()


@contextmanager
def _managed_pendo_database(tmp_path: Path):
    """延迟导入并统一关闭 Pendo 数据库，不污染非 Pendo 测试的模块空间。"""

    from plugins.pendo.services.db import Database

    database = Database(str(tmp_path / "pendo.db"))
    try:
        yield database
    finally:
        database.cleanup()


@pytest.fixture(name="db")
def pendo_db(tmp_path: Path):
    """为仅需基础数据库的 Pendo 模块提供统一 fixture。"""

    with _managed_pendo_database(tmp_path) as database:
        yield database


@pytest.fixture
def temp_db(db):
    """为 Pendo Web 路由和服务测试提供统一的隔离数据库。"""

    return db


@functools.lru_cache(maxsize=1)
def _pendo_web_test_dependencies():
    """只解析一次 Web 测试依赖，避免模块隔离用例替换后污染后续 fixture。"""
    from fastapi.testclient import TestClient

    from plugins.pendo.web.server import create_app

    return TestClient, create_app


@pytest.fixture
def client(temp_db):
    """为 Pendo Web 模块统一创建并关闭 FastAPI 测试客户端。"""
    try:
        test_client_class, create_app = _pendo_web_test_dependencies()
    except ModuleNotFoundError:
        pytest.skip("fastapi is not installed in this environment")
    except RuntimeError as exc:
        if "requires the httpx package" not in str(exc):
            raise
        pytest.skip("httpx is not installed in this environment")

    with test_client_class(create_app(temp_db)) as test_client:
        yield test_client


def _is_pendo_test(request: pytest.FixtureRequest) -> bool:
    path = Path(str(request.node.path)).resolve()
    return path.parent == Path(__file__).resolve().parent and (
        path.name.startswith("test_pendo") or "db" in request.fixturenames
    )


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
