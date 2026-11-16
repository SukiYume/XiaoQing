"""QingPet 测试数据库与插件运行时 fixture。"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio


@pytest.fixture
def qingpet_db(tmp_path: Path):
    """创建独立 QingPet 数据库，并统一走公开清理入口。"""

    from plugins.qingpet.services.database import Database

    database = Database(str(tmp_path / "qingpet.db"))
    try:
        yield database
    finally:
        database.cleanup()


@pytest_asyncio.fixture
async def qingpet_runtime(tmp_path: Path):
    """经插件自己的 init 打开真实数据库；测试另开一个句柄读写同一个库文件。"""

    from plugins.qingpet import main as qingpet_main
    from plugins.qingpet.services.database import Database

    context = SimpleNamespace(data_dir=tmp_path, logger=logging.getLogger("test.qingpet"))
    await qingpet_main.init(context)
    assert qingpet_main._db_instance is not None
    database = Database(str(tmp_path / "qingpet" / "qingpet.db"))
    try:
        yield context, database
    finally:
        database.cleanup()
        await qingpet_main.cleanup(context)
