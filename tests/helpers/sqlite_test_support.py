"""SQLite 测试辅助：记录连接上实际执行的语句。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def trace_sql_statements(connection: sqlite3.Connection) -> Iterator[list[str]]:
    """在 SQLite 连接上记录实际执行的语句，退出时取消记录；缓存命中等场景用它断言“没有查询”。"""

    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    try:
        yield statements
    finally:
        connection.set_trace_callback(None)
