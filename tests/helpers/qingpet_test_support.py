"""QingPet 测试辅助：在真实 SQLite 库上制造存储层写入失败。"""

from __future__ import annotations

from plugins.qingpet.services.database import Database


def fail_updates_to(database: Database, table: str) -> None:
    """让之后对 ``table`` 的所有更新都失败，模拟存储层写入失败；事务应整体回滚。"""

    connection = database._get_connection()
    connection.execute(
        f"""CREATE TRIGGER fail_{table}_write BEFORE UPDATE ON {table}
            BEGIN SELECT RAISE(ABORT, 'injected {table} write failure'); END"""
    )
    connection.commit()
