"""QingPet 测试数据库 fixture。"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def qingpet_db(tmp_path: Path):
    """创建独立 QingPet 数据库，并统一走公开清理入口。"""

    from plugins.qingpet.services.database import Database

    database = Database(str(tmp_path / "qingpet.db"))
    try:
        yield database
    finally:
        database.cleanup()
