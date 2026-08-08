"""Pendo SQLite 表结构、迁移、索引和连接级 SQL 函数。"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, cast
from zoneinfo import ZoneInfo

from ..config import PendoConfig
from ..models.item import ItemType
from ..utils.time_utils import TimezoneHelper, utc_now_iso

logger = logging.getLogger(__name__)

_ADD_COLUMN_NAME = re.compile(
    r"\bADD\s+COLUMN\s+(?:\"(?P<double>[^\"]+)\"|`(?P<backtick>[^`]+)`|"
    r"\[(?P<bracket>[^\]]+)\]|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))",
    re.IGNORECASE,
)


def _sqlite_casefold(value: Any) -> str:
    """为 SQLite 查询提供与 Python 过滤路径一致的 Unicode casefold。"""

    return str(value or "").casefold()


def _sqlite_local_date(value: Any, timezone_name: Any) -> str:
    """按显式时区解释时间戳，并返回 ISO 日期。"""

    text = str(value or "").strip()
    zone_name = str(timezone_name or "").strip()
    if not text or not zone_name:
        return ""
    try:
        zone = ZoneInfo(zone_name)
        parsed = cast(datetime, TimezoneHelper.parse(text, zone))
        return parsed.date().isoformat()
    except (TypeError, ValueError):
        return ""


def _sqlite_utc_epoch(value: Any, timezone_name: Any) -> float | None:
    """按显式时区把历史墙钟时间转换为绝对排序键。"""

    text = str(value or "").strip()
    zone_name = str(timezone_name or "").strip()
    if not text or not zone_name:
        return None
    try:
        parsed = cast(datetime, TimezoneHelper.parse(text, ZoneInfo(zone_name)))
        return parsed.timestamp()
    except (OverflowError, TypeError, ValueError):
        return None


_ADD_COLUMN_MIGRATIONS = (
    "ALTER TABLE items ADD COLUMN notes TEXT",
    "ALTER TABLE items ADD COLUMN amount REAL",
    "ALTER TABLE items ADD COLUMN amount_cents INTEGER",
    "ALTER TABLE items ADD COLUMN currency TEXT",
    "ALTER TABLE items ADD COLUMN transaction_type TEXT",
    "ALTER TABLE items ADD COLUMN ledger_category TEXT",
    "ALTER TABLE items ADD COLUMN ledger_date TEXT",
    "ALTER TABLE items ADD COLUMN account_name TEXT",
    "ALTER TABLE items ADD COLUMN counter_account_name TEXT",
    "ALTER TABLE items ADD COLUMN merchant TEXT",
    "ALTER TABLE items ADD COLUMN remark TEXT",
    'ALTER TABLE items ADD COLUMN "references" TEXT',
    "ALTER TABLE items ADD COLUMN last_viewed TEXT",
    "ALTER TABLE items ADD COLUMN related_items TEXT",
    "ALTER TABLE items ADD COLUMN reminder_rules TEXT",
    "ALTER TABLE items ADD COLUMN event_role TEXT",
    "ALTER TABLE items ADD COLUMN event_collection_id TEXT",
    "ALTER TABLE items ADD COLUMN event_collection_kind TEXT",
    "ALTER TABLE items ADD COLUMN event_index INTEGER",
    "ALTER TABLE items ADD COLUMN event_node_key TEXT",
    "ALTER TABLE items ADD COLUMN source_item_id TEXT",
    "ALTER TABLE items ADD COLUMN plan_date TEXT",
    "ALTER TABLE items ADD COLUMN deadline_at TEXT",
    "ALTER TABLE items ADD COLUMN repeat_rule TEXT",
    "ALTER TABLE items ADD COLUMN cancelled_at TEXT",
    "ALTER TABLE items ADD COLUMN entry_time TEXT",
    "ALTER TABLE items ADD COLUMN template_answers TEXT",
    "ALTER TABLE items ADD COLUMN is_favorite INTEGER DEFAULT 0",
    "ALTER TABLE items ADD COLUMN version INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE reminder_logs ADD COLUMN repeat_count INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE reminder_logs ADD COLUMN last_sent_at TEXT",
    "ALTER TABLE reminder_logs ADD COLUMN state TEXT NOT NULL DEFAULT 'pending'",
    "ALTER TABLE reminder_logs ADD COLUMN claim_token TEXT",
    "ALTER TABLE reminder_logs ADD COLUMN claim_expires_at TEXT",
    "ALTER TABLE reminder_logs ADD COLUMN next_attempt_at TEXT",
    "ALTER TABLE reminder_logs ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE user_settings ADD COLUMN version INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE operation_logs ADD COLUMN undone_at TEXT",
    "ALTER TABLE operation_logs ADD COLUMN undo_log_id INTEGER",
    "ALTER TABLE reminder_logs ADD COLUMN fire_at_utc TEXT",
)
_ITEM_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_owner_type ON items(owner_id, type, deleted)",
    f"CREATE INDEX IF NOT EXISTS idx_start_time ON items(start_time) WHERE type='{ItemType.EVENT.value}'",
    f"CREATE INDEX IF NOT EXISTS idx_task_plan_date ON items(plan_date) WHERE type='{ItemType.TASK.value}'",
    f"CREATE INDEX IF NOT EXISTS idx_task_deadline_at ON items(deadline_at) WHERE type='{ItemType.TASK.value}'",
    f"CREATE INDEX IF NOT EXISTS idx_diary_date ON items(diary_date) WHERE type='{ItemType.DIARY.value}'",
    f"CREATE INDEX IF NOT EXISTS idx_diary_entry_time ON items(entry_time) WHERE type='{ItemType.DIARY.value}'",
    f"CREATE INDEX IF NOT EXISTS idx_ledger_date ON items(ledger_date) WHERE type='{ItemType.LEDGER.value}'",
    f"CREATE INDEX IF NOT EXISTS idx_ledger_account ON items(owner_id, account_name, deleted) WHERE type='{ItemType.LEDGER.value}'",
    f"CREATE INDEX IF NOT EXISTS idx_ledger_transaction_type ON items(owner_id, transaction_type, deleted) WHERE type='{ItemType.LEDGER.value}'",
    "CREATE INDEX IF NOT EXISTS idx_items_event_collection ON items(event_collection_id, event_index) WHERE type='event' AND deleted = 0",
    "CREATE INDEX IF NOT EXISTS idx_items_event_role ON items(owner_id, event_role, deleted) WHERE type='event'",
)


def _is_expected_duplicate_column_error(
    error: sqlite3.OperationalError,
    sql: str,
) -> bool:
    """仅当报错列名正是当前 ADD COLUMN 目标时返回真。"""
    prefix = "duplicate column name:"
    message = str(error).strip()
    if not message.casefold().startswith(prefix):
        return False
    match = _ADD_COLUMN_NAME.search(sql)
    if match is None:
        return False
    expected = next(value for value in match.groupdict().values() if value is not None)
    actual = message[len(prefix) :].strip().strip('"`[]')
    return actual.casefold() == expected.casefold()


def _execute_add_column_migration(cursor: sqlite3.Cursor, sql: str) -> None:
    """执行一条加列迁移，只忽略该列已存在的可预期错误。"""
    try:
        cursor.execute(sql)
    except sqlite3.OperationalError as exc:
        if not _is_expected_duplicate_column_error(exc, sql):
            raise


def _create_core_schema(cursor: sqlite3.Cursor) -> None:
    """创建版本化加列迁移所依赖的基础表。"""

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            title TEXT,
            content TEXT,
            tags TEXT,
            category TEXT DEFAULT '未分类',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            context TEXT,
            visibility TEXT DEFAULT 'private',
            attachments TEXT,
            ai_meta TEXT,
            deleted INTEGER DEFAULT 0,
            deleted_at TEXT,
            start_time TEXT,
            end_time TEXT,
            timezone TEXT,
            location TEXT,
            participants TEXT,
            remind_times TEXT,
            reminder_rules TEXT,
            event_role TEXT,
            event_collection_id TEXT,
            event_collection_kind TEXT,
            event_index INTEGER,
            event_node_key TEXT,
            source_item_id TEXT,
            plan_date TEXT,
            deadline_at TEXT,
            priority INTEGER,
            status TEXT,
            repeat_rule TEXT,
            completed_at TEXT,
            cancelled_at TEXT,
            "references" TEXT,
            last_viewed TEXT,
            related_items TEXT,
            mood TEXT,
            mood_score INTEGER,
            weather TEXT,
            template_id TEXT,
            diary_date TEXT,
            entry_time TEXT,
            template_answers TEXT,
            is_favorite INTEGER DEFAULT 0,
            notes TEXT,
            amount REAL,
            amount_cents INTEGER,
            currency TEXT,
            transaction_type TEXT,
            ledger_category TEXT,
            ledger_date TEXT,
            account_name TEXT,
            counter_account_name TEXT,
            merchant TEXT,
            remark TEXT,
            version INTEGER NOT NULL DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reminder_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL,
            remind_time TEXT NOT NULL,
            sent_at TEXT,
            confirmed_at TEXT,
            user_action TEXT,
            repeat_count INTEGER NOT NULL DEFAULT 1,
            last_sent_at TEXT,
            state TEXT NOT NULL DEFAULT 'pending',
            claim_token TEXT,
            claim_expires_at TEXT,
            next_attempt_at TEXT,
            failure_count INTEGER NOT NULL DEFAULT 0,
            fire_at_utc TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id TEXT PRIMARY KEY,
            timezone TEXT DEFAULT 'Asia/Shanghai',
            quiet_hours_start TEXT DEFAULT '23:00',
            quiet_hours_end TEXT DEFAULT '07:00',
            daily_report_time TEXT DEFAULT '08:00',
            diary_remind_time TEXT DEFAULT '21:30',
            default_category TEXT DEFAULT '未分类',
            settings_json TEXT,
            updated_at TEXT,
            version INTEGER NOT NULL DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            sql TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
    """)


def _apply_schema_migrations(cursor: sqlite3.Cursor) -> None:
    """保证每条历史 ADD COLUMN 迁移只执行一次。"""

    applied_versions = {
        int(row[0]) for row in cursor.execute("SELECT version FROM schema_migrations").fetchall()
    }
    for version, sql in enumerate(_ADD_COLUMN_MIGRATIONS, start=1):
        if version in applied_versions:
            continue
        _execute_add_column_migration(cursor, sql)
        cursor.execute(
            """
            INSERT INTO schema_migrations (version, name, sql, applied_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                version,
                f"add-column-{version:03d}",
                sql,
                utc_now_iso(),
            ),
        )


def _create_item_indexes(cursor: sqlite3.Cursor) -> None:
    """在迁移补齐列后创建条目索引。"""

    for statement in _ITEM_INDEXES:
        cursor.execute(statement)


def _create_event_schema(cursor: sqlite3.Cursor) -> None:
    """创建日程集合存储表及查询索引。"""

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS event_collections (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('multi_node', 'recurring')),
            title TEXT NOT NULL,
            content TEXT DEFAULT '',
            category TEXT DEFAULT '未分类',
            location TEXT DEFAULT '',
            tags TEXT DEFAULT '[]',
            notes TEXT DEFAULT '',
            context TEXT DEFAULT '{}',
            visibility TEXT DEFAULT 'private',
            timezone TEXT DEFAULT 'Asia/Shanghai',
            rrule TEXT,
            reminder_rules TEXT DEFAULT '[]',
            start_time TEXT,
            end_time TEXT,
            source_item_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted INTEGER DEFAULT 0,
            deleted_at TEXT
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_collections_owner_kind "
        "ON event_collections(owner_id, kind, deleted)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_collections_time "
        "ON event_collections(owner_id, start_time, end_time) WHERE deleted = 0"
    )


def _create_search_schema(cursor: sqlite3.Cursor) -> None:
    """创建条目全文检索表。"""

    cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
            id UNINDEXED, title, content, tags, category
        )
    """)


def reminder_fire_at_utc(remind_time: str, timezone_name: str) -> str | None:
    """把提醒墙钟时间解析成可索引的 UTC ISO 时间。"""

    try:
        user_timezone = ZoneInfo(timezone_name)
        parsed = cast(datetime, TimezoneHelper.parse(remind_time, user_timezone))
    except (KeyError, TypeError, ValueError):
        logger.warning("Invalid reminder schedule while materializing queue")
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _materialize_existing_reminder_schedules(cursor: sqlite3.Cursor) -> None:
    """启动时为旧库当前仍有效的提醒补齐待处理队列行。"""

    rows = cursor.execute(
        """
        SELECT i.id AS item_id, CAST(reminder.value AS TEXT) AS remind_time,
               COALESCE(NULLIF(us.timezone, ''), ?) AS timezone_name
        FROM items AS i
        LEFT JOIN user_settings AS us ON us.user_id = i.owner_id
        JOIN json_each(
          CASE WHEN json_valid(i.remind_times) THEN i.remind_times ELSE '[]' END
        ) AS reminder
        WHERE i.deleted = 0 AND i.type IN ('event', 'task')
          AND (i.type != 'task' OR COALESCE(i.status, 'open') = 'open')
          AND reminder.type = 'text' AND TRIM(CAST(reminder.value AS TEXT)) != ''
        """,
        (PendoConfig.DEFAULT_TIMEZONE,),
    ).fetchall()
    for row in rows:
        remind_time = str(row["remind_time"])
        fire_at_utc = reminder_fire_at_utc(remind_time, str(row["timezone_name"]))
        if fire_at_utc is None:
            continue
        cursor.execute(
            """
            INSERT INTO reminder_logs
                (item_id, remind_time, fire_at_utc, state, repeat_count, failure_count)
            VALUES (?, ?, ?, 'pending', 0, 0)
            ON CONFLICT(item_id, remind_time) DO UPDATE SET
                fire_at_utc = excluded.fire_at_utc
            WHERE reminder_logs.sent_at IS NULL AND reminder_logs.confirmed_at IS NULL
            """,
            (str(row["item_id"]), remind_time, fire_at_utc),
        )


def _migrate_reminder_logs(cursor: sqlite3.Cursor) -> None:
    """合并历史重复提醒记录，并补齐领取状态。"""

    cursor.execute("""
        SELECT item_id, remind_time, COUNT(*) as cnt
        FROM reminder_logs GROUP BY item_id, remind_time HAVING cnt > 1
    """)
    if cursor.fetchone() is not None:
        cursor.execute("""
            CREATE TEMP TABLE _rl_merged AS
            SELECT
                MIN(id) AS id,
                item_id,
                remind_time,
                MIN(sent_at) AS sent_at,
                MAX(confirmed_at) AS confirmed_at,
                COALESCE(
                    MAX(CASE WHEN confirmed_at IS NOT NULL THEN user_action END),
                    MAX(user_action)
                ) AS user_action,
                COUNT(CASE WHEN sent_at IS NOT NULL THEN 1 END) AS repeat_count,
                MAX(sent_at) AS last_sent_at
            FROM reminder_logs
            GROUP BY item_id, remind_time
        """)
        cursor.execute("DELETE FROM reminder_logs")
        cursor.execute("""
            INSERT INTO reminder_logs
                (id, item_id, remind_time, sent_at, confirmed_at, user_action, repeat_count, last_sent_at)
            SELECT id, item_id, remind_time, sent_at, confirmed_at, user_action, repeat_count, last_sent_at
            FROM _rl_merged
        """)
        cursor.execute("DROP TABLE _rl_merged")

    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_reminder_logs_unique
        ON reminder_logs(item_id, remind_time)
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminder_logs_claim "
        "ON reminder_logs(state, claim_expires_at, next_attempt_at)"
    )
    cursor.execute(
        "UPDATE reminder_logs SET state = CASE "
        "WHEN confirmed_at IS NOT NULL THEN 'confirmed' "
        "WHEN sent_at IS NOT NULL THEN 'sent' ELSE 'pending' END "
        "WHERE state IS NULL OR state = 'pending'"
    )
    _materialize_existing_reminder_schedules(cursor)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminder_logs_pending_fire "
        "ON reminder_logs(fire_at_utc) "
        "WHERE sent_at IS NULL AND confirmed_at IS NULL"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminder_logs_unconfirmed_sent "
        "ON reminder_logs(sent_at) "
        "WHERE sent_at IS NOT NULL AND confirmed_at IS NULL"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminder_logs_confirmed_retention "
        "ON reminder_logs(confirmed_at) WHERE confirmed_at IS NOT NULL"
    )


def _create_scheduled_delivery_schema(cursor: sqlite3.Cursor) -> None:
    """创建持久化、基于租约的调度投递出站表。"""

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_delivery_outbox (
            task_name TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            period_key TEXT NOT NULL,
            delivery_key TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            claim_token TEXT,
            claim_expires_at TEXT,
            next_attempt_at TEXT,
            failure_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            sent_at TEXT,
            PRIMARY KEY (task_name, owner_id, period_key),
            UNIQUE (delivery_key)
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_scheduled_delivery_claim "
        "ON scheduled_delivery_outbox(state, claim_expires_at, next_attempt_at)"
    )


def _create_audit_schema(cursor: sqlite3.Cursor) -> None:
    """创建操作、传输和导入幂等性审计表。"""

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS operation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            action TEXT NOT NULL,
            item_type TEXT,
            item_id TEXT,
            details TEXT,
            created_at TEXT NOT NULL,
            undone_at TEXT,
            undo_log_id INTEGER
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transfer_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id TEXT NOT NULL,
            action TEXT NOT NULL,
            bundle_id TEXT,
            filename TEXT,
            types TEXT,
            record_count INTEGER DEFAULT 0,
            result_summary TEXT,
            created_at TEXT NOT NULL
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_transfer_logs_owner "
        "ON transfer_logs(owner_id, created_at DESC)"
    )
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS imported_bundles (
            owner_id TEXT NOT NULL,
            bundle_id TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, bundle_id)
        )
    """)


def _create_audit_indexes(cursor: sqlite3.Cursor) -> None:
    """创建用户撤销查询和保留期清理所需索引。"""

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_operation_logs_user_time "
        "ON operation_logs(user_id, created_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_operation_logs_created_at ON operation_logs(created_at)"
    )


def _create_web_auth_schema(cursor: sqlite3.Cursor) -> None:
    """创建持久化登录码、浏览器会话和 Widget Token 注册表。"""

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS login_code_registry (
            code_digest TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            issued_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            CHECK (expires_at > issued_at)
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_login_code_expiry
        ON login_code_registry(expires_at)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS web_session_registry (
            session_digest TEXT PRIMARY KEY,
            device_id TEXT NOT NULL UNIQUE,
            owner_id TEXT NOT NULL,
            csrf_token TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            demo INTEGER NOT NULL DEFAULT 0 CHECK (demo IN (0, 1)),
            CHECK (expires_at > created_at)
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_web_session_owner_expiry
        ON web_session_registry(owner_id, expires_at, created_at DESC)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_web_session_expiry
        ON web_session_registry(expires_at)
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS widget_token_registry (
            jti TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            issued_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            revoked_at INTEGER,
            CHECK (expires_at > issued_at)
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_widget_token_owner_active
        ON widget_token_registry(owner_id, revoked_at, expires_at)
    """)


def configure_connection(connection: sqlite3.Connection) -> None:
    """为每条线程本地连接注册确定性的查询辅助函数。"""

    connection.create_function("pendo_casefold", 1, _sqlite_casefold, deterministic=True)
    connection.create_function("pendo_local_date", 2, _sqlite_local_date, deterministic=True)
    connection.create_function("pendo_utc_epoch", 2, _sqlite_utc_epoch, deterministic=True)


def initialize_schema(cursor: sqlite3.Cursor) -> None:
    """按依赖顺序在调用方事务内创建表、执行迁移并补齐索引。"""

    _create_core_schema(cursor)
    # 审计表必须先存在，历史版本化 ADD COLUMN 才有明确目标。
    _create_audit_schema(cursor)
    _apply_schema_migrations(cursor)
    _create_item_indexes(cursor)
    _create_audit_indexes(cursor)
    _create_event_schema(cursor)
    _create_search_schema(cursor)
    _migrate_reminder_logs(cursor)
    _create_scheduled_delivery_schema(cursor)
    _create_web_auth_schema(cursor)
