"""Pendo 离线 SQLite 迁移共享的文件、模式与 JSON 工具。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any


def backup_sqlite_database(db_path: Path, backup_path: Path) -> None:
    """一致备份现有数据库，并包含尚未 checkpoint 的 WAL 页面。"""

    source_path = db_path.resolve()
    target_path = backup_path.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"待备份的 SQLite 数据库不存在: {db_path}")
    if source_path == target_path:
        raise ValueError("SQLite 备份路径不能与源数据库相同")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    # closing 会真正关闭连接；sqlite3.Connection 自身的上下文管理器只负责事务。
    with closing(sqlite3.connect(str(source_path))) as source:
        with closing(sqlite3.connect(str(target_path))) as target:
            source.backup(target)


def connect_sqlite_database(db_path: Path) -> sqlite3.Connection:
    """打开迁移连接，并让查询结果支持按列名读取。"""

    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    return connection


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    """判断主 SQLite schema 中是否存在指定表。"""

    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    """返回已有表的列名；表不存在时返回空集合。"""

    if not table_exists(connection, table):
        return set()
    quoted_table = '"' + table.replace('"', '""') + '"'
    return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({quoted_table})")}


def load_json_field(value: Any, default: Any) -> Any:
    """解码旧 JSON 字段；空值、非字符串或损坏文本回退到默认值。"""

    if value in (None, ""):
        return default
    if isinstance(value, (list, dict)):
        return value
    if not isinstance(value, str):
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def dump_json_field(value: Any) -> str:
    """编码迁移字段，同时保留便于人工检查的 Unicode 字符。"""

    return json.dumps(value, ensure_ascii=False)


def normalize_iso_seconds(value: Any) -> str | None:
    """把可解析时间统一为无微秒的 ISO 字符串，无效值返回 ``None``。"""

    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).strip()).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None
