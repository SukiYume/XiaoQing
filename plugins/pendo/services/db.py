"""Pendo 的 SQLite 存储、缓存、全文检索、审计和提醒租约服务。"""

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import PendoConfig
from ..core.exceptions import ItemNotFoundException
from ..models.item import (
    ITEM_TYPE_CLASS_MAP,
    EventItem,
    Item,
    ItemType,
    TaskItem,
    TaskStatus,
)
from ..utils.settings_utils import normalize_settings_json
from ..utils.time_utils import (
    TimezoneHelper,
    normalize_datetime_for_storage,
    normalize_event_collection_datetimes_for_storage,
    normalize_item_datetimes_for_storage,
    require_canonical_utc_storage,
    require_canonical_utc_timestamp,
    utc_now_iso,
)
from ..utils.validators import (
    ledger_amount_filter_to_cents,
    normalize_bool_flag,
    sanitize_search_keyword,
    validate_item_data,
)

logger = logging.getLogger(__name__)
_CACHE_MISS = object()
_SQLITE_ID_BATCH_SIZE = 500
_SQL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ADD_COLUMN_NAME = re.compile(
    r"\bADD\s+COLUMN\s+(?:\"(?P<double>[^\"]+)\"|`(?P<backtick>[^`]+)`|"
    r"\[(?P<bracket>[^\]]+)\]|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))",
    re.IGNORECASE,
)


def _sqlite_casefold(value: Any) -> str:
    """为 SQLite 查询提供与 Python 过滤路径一致的 Unicode casefold。"""

    return str(value or "").casefold()


def _sqlite_local_date(value: Any, timezone_name: Any) -> str:
    """Return an ISO day after interpreting a timestamp in an explicit zone."""

    text = str(value or "").strip()
    zone_name = str(timezone_name or "").strip()
    if not text or not zone_name:
        return ""
    try:
        zone = ZoneInfo(zone_name)
        return TimezoneHelper.parse(text, zone).date().isoformat()
    except (TypeError, ValueError):
        return ""


def _sqlite_utc_epoch(value: Any, timezone_name: Any) -> float | None:
    """Return an absolute sort key using an explicit zone for legacy wall times."""

    text = str(value or "").strip()
    zone_name = str(timezone_name or "").strip()
    if not text or not zone_name:
        return None
    try:
        return TimezoneHelper.parse(text, ZoneInfo(zone_name)).timestamp()
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


def _reminder_fire_at_utc(remind_time: str, timezone_name: str) -> str | None:
    """把提醒墙钟时间解析成可索引的 UTC ISO 时间。"""

    try:
        user_timezone = ZoneInfo(timezone_name)
        parsed = TimezoneHelper.parse(remind_time, user_timezone)
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
        fire_at_utc = _reminder_fire_at_utc(remind_time, str(row["timezone_name"]))
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
    """Create indexes for owner undo lookup and retention pruning."""

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_operation_logs_user_time "
        "ON operation_logs(user_id, created_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_operation_logs_created_at ON operation_logs(created_at)"
    )


def _create_web_auth_schema(cursor: sqlite3.Cursor) -> None:
    """Create the persistent Widget-token registry and revocation index."""

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


class DuplicateBundleImportError(RuntimeError):
    """同一数据包未启用强制模式却被重复导入。"""


class Database:
    """集中管理 Pendo 的线程连接、事务、缓存和各类持久化操作。"""

    CACHE_TTL = 30
    CACHE_MAX_SIZE = 1024
    ALLOWED_DATE_FIELDS: ClassVar[set[str]] = {
        "start_time",
        "end_time",
        "plan_date",
        "deadline_at",
        "completed_at",
        "cancelled_at",
        "diary_date",
        "entry_time",
        "created_at",
        "ledger_date",
    }
    _DATE_ONLY_FIELDS = frozenset({"plan_date", "diary_date", "ledger_date"})
    _TIMESTAMP_FIELDS = frozenset(ALLOWED_DATE_FIELDS - _DATE_ONLY_FIELDS)
    _LEDGER_AMOUNT_CENTS_EXPR = (
        "COALESCE(amount_cents, CAST(ROUND(COALESCE(amount, 0) * 100.0) AS INTEGER), 0)"
    )
    _EXACT_FILTER_FIELDS: ClassVar[tuple[str, ...]] = (
        "type",
        "category",
        "ledger_category",
        "transaction_type",
        "counter_account_name",
        "merchant",
        "status",
        "priority",
        "plan_date",
    )

    # JSON 字段必须按容器类型分别降级，避免损坏值把 context 变成列表，或把
    # attachments 变成字典。
    _JSON_LIST_FIELDS = frozenset(
        {
            "tags",
            "attachments",
            "participants",
            "remind_times",
            "reminder_rules",
            "references",
            "related_items",
            "template_answers",
        }
    )
    _JSON_OBJECT_FIELDS = frozenset({"context", "ai_meta"})
    _JSON_FIELDS = _JSON_LIST_FIELDS | _JSON_OBJECT_FIELDS
    _ITEM_FIELDS = frozenset(
        field_name
        for item_class in ITEM_TYPE_CLASS_MAP.values()
        for field_name in item_class.__dataclass_fields__
    )
    _IMMUTABLE_UPDATE_FIELDS = frozenset({"id", "owner_id", "type", "created_at", "version"})
    _EVENT_COLLECTION_JSON_FIELDS = frozenset({"tags", "context", "reminder_rules"})
    _EVENT_COLLECTION_FIELDS = frozenset(
        {
            "id",
            "owner_id",
            "kind",
            "title",
            "content",
            "category",
            "location",
            "tags",
            "notes",
            "context",
            "visibility",
            "timezone",
            "rrule",
            "reminder_rules",
            "start_time",
            "end_time",
            "source_item_id",
            "created_at",
            "updated_at",
            "deleted",
            "deleted_at",
        }
    )
    _EVENT_COLLECTION_IMPORT_UPDATE_FIELDS = _EVENT_COLLECTION_FIELDS - {
        "id",
        "owner_id",
        "kind",
    }
    _EVENT_COLLECTION_UPDATE_FIELDS = frozenset(
        {
            "title",
            "content",
            "category",
            "location",
            "tags",
            "notes",
            "context",
            "visibility",
            "timezone",
            "rrule",
            "reminder_rules",
            "start_time",
            "end_time",
            "source_item_id",
        }
    )

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._local = threading.local()
        # 查询连接仍按线程隔离；额外登记连接对象，保证生命周期线程能可靠关闭
        # 已退出工作线程留下的连接。以连接对象身份为键，避免线程 ID 重用冲突。
        self._all_connections: dict[int, tuple[int, sqlite3.Connection]] = {}
        self._lock = threading.Lock()
        self._settings_lock = threading.Lock()

        # 使用 OrderedDict 实现 LRU 缓存
        self._cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._cache_lock = threading.Lock()

        logger.debug("Initializing database at %s", db_path)
        self._init_database()

    # ==================== 连接管理 ====================

    def get_connection(self) -> sqlite3.Connection:
        """获取当前线程的数据库连接"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            # 查询不跨线程共享；check_same_thread=False 只用于统一生命周期关闭。
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.create_function("pendo_casefold", 1, _sqlite_casefold, deterministic=True)
            conn.create_function("pendo_local_date", 2, _sqlite_local_date, deterministic=True)
            conn.create_function("pendo_utc_epoch", 2, _sqlite_utc_epoch, deterministic=True)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
            with self._lock:
                thread_id = threading.get_ident()
                self._all_connections[id(conn)] = (thread_id, conn)
        return conn

    def close_all_connections(self) -> None:
        """关闭所有已登记连接；任一关闭失败时汇总为生命周期错误。"""
        with self._lock:
            connections = list(self._all_connections.values())
            self._all_connections.clear()

        failures: list[BaseException] = []
        for thread_id, conn in connections:
            try:
                conn.close()
            except sqlite3.Error as exc:
                logger.error(
                    "Failed to close Pendo SQLite connection thread=%s error_type=%s",
                    thread_id,
                    type(exc).__name__,
                )
                failures.append(exc)

        if hasattr(self._local, "conn"):
            self._local.conn = None
        if failures:
            raise RuntimeError(
                f"Failed to close {len(failures)} Pendo SQLite connection(s)"
            ) from failures[0]

    def cleanup(self) -> None:
        """清理资源"""
        self.close_all_connections()

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """事务上下文管理器"""
        conn = self.get_connection()
        try:
            if immediate and not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ==================== 缓存管理 ====================

    def _cache_key(self, *parts: Any) -> str:
        """生成缓存键"""

        def normalize(value: Any) -> str:
            if isinstance(value, (dict, list)):
                return json.dumps(value, sort_keys=True, default=str)
            return str(value)

        return "|".join(normalize(p) for p in parts)

    def _cache_get_or_miss(self, key: str) -> Any:
        """获取缓存；未命中时返回内部 sentinel。"""
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry:
                ts, val = entry
                if time.time() - ts <= self.CACHE_TTL:
                    self._cache.move_to_end(key)  # 维护真正的 LRU 顺序
                    # 缓存值可能是 dataclass、列表或设置字典；返回副本，避免调用方
                    # 的就地修改污染尚未提交的缓存状态。
                    return deepcopy(val)
                del self._cache[key]
        return _CACHE_MISS

    def _cache_set(self, key: str, value: Any) -> None:
        """设置缓存（使用 LRU 淘汰策略）"""
        with self._cache_lock:
            # 如果键已存在，先删除（为了更新位置）
            if key in self._cache:
                del self._cache[key]
            # 如果超过容量，删除最旧的项（OrderedDict 的第一个）
            elif len(self._cache) >= self.CACHE_MAX_SIZE:
                self._cache.popitem(last=False)
            # 添加新项（自动放到最后）
            self._cache[key] = (time.time(), deepcopy(value))

    def cache_clear(self) -> None:
        """清空缓存"""
        with self._cache_lock:
            self._cache.clear()

    def cache_invalidate(self, pattern: str | None = None) -> None:
        """失效缓存

        Args:
            pattern: 如果提供，只删除匹配此模式的缓存键；否则清空所有缓存
        """
        with self._cache_lock:
            if pattern:
                keys_to_delete = [k for k in self._cache.keys() if pattern in k]
                for key in keys_to_delete:
                    del self._cache[key]
            else:
                self._cache.clear()

    # ==================== 数据库初始化 ====================

    def _init_database(self) -> None:
        """在一个原子事务中初始化全部表、迁移和索引。"""

        with self.transaction() as conn:
            cursor = conn.cursor()
            _create_core_schema(cursor)
            # Audit tables must exist before their versioned ADD COLUMN migrations run.
            _create_audit_schema(cursor)
            _apply_schema_migrations(cursor)
            _create_item_indexes(cursor)
            _create_audit_indexes(cursor)
            _create_event_schema(cursor)
            _create_search_schema(cursor)
            _migrate_reminder_logs(cursor)
            _create_scheduled_delivery_schema(cursor)
            _create_web_auth_schema(cursor)

    # ==================== Web Widget authentication ====================

    @staticmethod
    def _validate_widget_token_record(
        jti: str,
        owner_id: str,
        issued_at: int,
        expires_at: int,
    ) -> tuple[str, str, int, int]:
        token_id = str(jti or "").strip()
        owner = str(owner_id or "").strip()
        if not token_id or len(token_id) > 256:
            raise ValueError("widget token jti is invalid")
        if not owner or len(owner) > 256:
            raise ValueError("widget token owner_id is invalid")
        if type(issued_at) is not int or type(expires_at) is not int:
            raise TypeError("widget token timestamps must be integers")
        if expires_at <= issued_at:
            raise ValueError("widget token expiry must follow issuance")
        return token_id, owner, issued_at, expires_at

    def register_widget_token(
        self,
        jti: str,
        owner_id: str,
        *,
        issued_at: int,
        expires_at: int,
    ) -> None:
        """Persist a newly issued JTI before the credential can be delivered."""

        token_id, owner, issued, expires = self._validate_widget_token_record(
            jti,
            owner_id,
            issued_at,
            expires_at,
        )
        with self.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM widget_token_registry WHERE expires_at <= ?", (issued,))
            conn.execute(
                """
                INSERT INTO widget_token_registry
                    (jti, owner_id, issued_at, expires_at, revoked_at)
                VALUES (?, ?, ?, ?, NULL)
                """,
                (token_id, owner, issued, expires),
            )

    def revoke_widget_tokens(self, owner_id: str, *, revoked_at: int | None = None) -> int:
        """Revoke every unexpired Widget credential owned by one user."""

        owner = str(owner_id or "").strip()
        if not owner or len(owner) > 256:
            raise ValueError("widget token owner_id is invalid")
        timestamp = int(time.time()) if revoked_at is None else revoked_at
        if type(timestamp) is not int or timestamp < 0:
            raise ValueError("widget token revocation time is invalid")
        with self.transaction(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE widget_token_registry
                SET revoked_at = ?
                WHERE owner_id = ? AND revoked_at IS NULL AND expires_at > ?
                """,
                (timestamp, owner, timestamp),
            )
            return max(0, cursor.rowcount)

    def is_widget_token_active(
        self,
        jti: str,
        owner_id: str,
        *,
        now: int | None = None,
    ) -> bool:
        """Return true only for a registered, unexpired and unrevoked JTI."""

        token_id = str(jti or "").strip()
        owner = str(owner_id or "").strip()
        if not token_id or not owner:
            return False
        timestamp = int(time.time()) if now is None else now
        if type(timestamp) is not int or timestamp < 0:
            raise ValueError("widget token verification time is invalid")
        row = (
            self.get_connection()
            .execute(
                """
            SELECT 1
            FROM widget_token_registry
            WHERE jti = ? AND owner_id = ? AND revoked_at IS NULL AND expires_at > ?
            """,
                (token_id, owner, timestamp),
            )
            .fetchone()
        )
        return row is not None

    # ==================== 条目操作 ====================

    def _log_operation_with_cursor(
        self,
        cursor: sqlite3.Cursor,
        *,
        user_id: str,
        action: str,
        item_type: str | None = None,
        item_id: str | None = None,
        details: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> int:
        """使用调用方的事务写入操作日志。"""
        if not user_id or not action:
            raise ValueError("operation log requires user_id and action")
        cursor.execute(
            """
            INSERT INTO operation_logs (user_id, action, item_type, item_id, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                action,
                item_type,
                item_id,
                json.dumps(details or {}, ensure_ascii=False),
                created_at or utc_now_iso(),
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("operation log insert did not return an id")
        return int(cursor.lastrowid)

    def _log_operation_from_spec(
        self,
        cursor: sqlite3.Cursor,
        operation_log: dict[str, Any] | None,
        *,
        default_item_id: str,
        default_item_type: str | None,
    ) -> None:
        if operation_log is None:
            return
        self._log_operation_with_cursor(
            cursor,
            user_id=str(operation_log.get("user_id") or ""),
            action=str(operation_log.get("action") or ""),
            item_type=operation_log.get("item_type", default_item_type),
            item_id=operation_log.get("item_id", default_item_id),
            details=operation_log.get("details"),
        )

    def insert_item(
        self,
        item_data: dict[str, Any] | Item,
        custom_id: str | None = None,
        *,
        operation_log: dict[str, Any] | None = None,
    ) -> str:
        """插入条目，支持dict或Item dataclass实例"""
        conn = self.get_connection()
        cursor = conn.cursor()

        # 在事务外完成数据准备，避免持锁时间过长
        # 如果是Item dataclass实例，转换为字典
        if isinstance(item_data, Item):
            item_dict = item_data.to_dict()
        else:
            item_dict = item_data

        # 验证输入数据
        item_dict = validate_item_data(item_dict)

        if custom_id:
            item_dict["id"] = custom_id
        elif "id" not in item_dict:
            item_dict["id"] = uuid.uuid4().hex

        item_dict.setdefault("created_at", utc_now_iso())
        item_dict.setdefault("updated_at", utc_now_iso())
        item_dict.setdefault("version", 0)
        owner = str(item_dict.get("owner_id") or "")
        if not owner:
            raise ValueError("item owner_id is required")
        user_timezone = TimezoneHelper.get_user_timezone(owner, self)
        if str(item_dict.get("type") or "") == ItemType.EVENT.value and not item_dict.get(
            "timezone"
        ):
            item_dict["timezone"] = user_timezone.key
        item_dict = normalize_item_datetimes_for_storage(item_dict, user_timezone)

        data = self._prepare_data(item_dict)
        columns = ", ".join(self._quote_col(k) for k in data.keys())
        placeholders = ", ".join(["?" for _ in data])

        # 连接上下文统一管理提交与回滚，避免和 sqlite3 隐式事务冲突。
        with conn:
            cursor.execute(
                f"INSERT INTO items ({columns}) VALUES ({placeholders})", list(data.values())
            )
            if "remind_times" in item_dict:
                self._sync_reminder_logs(cursor, str(item_dict["id"]), item_dict["remind_times"])
            self._update_fts(item_dict["id"], item_dict, conn)
            self._log_operation_from_spec(
                cursor,
                operation_log,
                default_item_id=item_dict["id"],
                default_item_type=str(item_dict.get("type") or "") or None,
            )
        # 精确失效：只清除与该条目相关的缓存
        self.cache_invalidate(item_dict["id"])
        self.cache_invalidate(f"items|{item_dict.get('owner_id', '')}")
        return str(item_dict["id"])

    # FTS 相关字段，只有这些字段被修改时才需要更新全文索引
    _FTS_FIELDS = frozenset({"title", "content", "tags", "category"})

    def update_item(
        self,
        item_id: str,
        updates: dict[str, Any] | Item,
        owner_id: str | None = None,
        *,
        expected_version: int | None = None,
        item_type: str | None = None,
        operation_log: dict[str, Any] | None = None,
        touch: bool = True,
    ) -> bool:
        """更新条目，支持 dict 或 Item dataclass 实例。"""
        conn = self.get_connection()
        cursor = conn.cursor()

        # 如果是Item dataclass实例，转换为字典
        if isinstance(updates, Item):
            update_dict = updates.to_dict()
        else:
            # 数据库入口不能把 updated_at 等内部字段写回调用方对象。
            update_dict = dict(updates)
        for field in self._IMMUTABLE_UPDATE_FIELDS:
            update_dict.pop(field, None)

        current_row = cursor.execute(
            "SELECT owner_id, type, timezone FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if current_row is None:
            return False
        current_owner = str(current_row["owner_id"] or "")
        user_timezone = TimezoneHelper.get_user_timezone(current_owner, self)
        if touch:
            update_dict["updated_at"] = utc_now_iso()
        normalization_context = {
            "owner_id": current_owner,
            "type": str(current_row["type"] or ""),
            "timezone": str(current_row["timezone"] or user_timezone.key),
            **update_dict,
        }
        normalized_context = normalize_item_datetimes_for_storage(
            normalization_context,
            user_timezone,
        )
        update_dict = {key: normalized_context[key] for key in update_dict}
        data = self._prepare_data(update_dict)
        set_clause = ", ".join(f"{self._quote_col(key)} = ?" for key in data)
        if touch:
            set_clause = f"{set_clause}, version = version + 1"

        # 连接上下文统一管理提交与回滚，避免和 sqlite3 隐式事务冲突。
        affected = 0
        with conn:
            where = ["id = ?", "deleted = 0"]
            params: list[Any] = [item_id]
            if owner_id:
                where.append("owner_id = ?")
                params.append(owner_id)
            if item_type:
                where.append("type = ?")
                params.append(item_type)
            if expected_version is not None:
                where.append("version = ?")
                params.append(expected_version)
            cursor.execute(
                f"UPDATE items SET {set_clause} WHERE {' AND '.join(where)}",
                list(data.values()) + params,
            )
            affected = cursor.rowcount

            if affected > 0 and "remind_times" in update_dict:
                self._sync_reminder_logs(cursor, item_id, update_dict.get("remind_times"))

            # 在同一事务内读取最新行更新 FTS，不能使用可能过期的缓存值。
            if affected > 0 and self._FTS_FIELDS & update_dict.keys():
                self._refresh_fts(item_id, conn)
            if affected > 0:
                self._log_operation_from_spec(
                    cursor,
                    operation_log,
                    default_item_id=item_id,
                    default_item_type=item_type,
                )

        if affected > 0:
            self._invalidate_item_cache(item_id, owner_id)
        return affected > 0

    def _invalidate_item_cache(self, item_id: str, owner_id: str | None = None) -> None:
        """失效单条缓存，并在调用方未给用户时补查所属用户的列表缓存。"""
        self.cache_invalidate(item_id)
        resolved_owner = owner_id
        if not resolved_owner:
            row = (
                self.get_connection()
                .execute("SELECT owner_id FROM items WHERE id = ?", (item_id,))
                .fetchone()
            )
            if row is not None:
                resolved_owner = str(row["owner_id"])
        if resolved_owner:
            self.cache_invalidate(f"items|{resolved_owner}")

    def _apply_batch_operations(
        self,
        cursor: sqlite3.Cursor,
        operations: list[tuple[str, dict[str, Any]]],
        owner_id: str,
    ) -> tuple[list[tuple[str, str, str | None]], list[str]]:
        results: list[tuple[str, str, str | None]] = []
        refreshed_item_ids: list[str] = []
        user_timezone = TimezoneHelper.get_user_timezone(owner_id, self)
        for action, raw_payload in operations:
            payload = dict(raw_payload)
            item_id = str(payload.get("id") or "")
            try:
                if not item_id:
                    raise ValueError("import item requires id")
                payload["owner_id"] = owner_id
                validated = validate_item_data(payload)
                validated.setdefault("created_at", utc_now_iso())
                validated.setdefault("updated_at", utc_now_iso())
                if str(validated.get("type") or "") == ItemType.EVENT.value and not validated.get(
                    "timezone"
                ):
                    validated["timezone"] = user_timezone.key
                validated = normalize_item_datetimes_for_storage(validated, user_timezone)
                data = self._prepare_data(validated)

                if action == "insert":
                    columns = ", ".join(self._quote_col(k) for k in data.keys())
                    placeholders = ", ".join("?" for _ in data)
                    cursor.execute(
                        f"INSERT INTO items ({columns}) VALUES ({placeholders})",
                        list(data.values()),
                    )
                    if "remind_times" in validated:
                        self._sync_reminder_logs(cursor, item_id, validated["remind_times"])
                    refreshed_item_ids.append(str(data.get("id") or item_id))
                elif action == "update":
                    update_data = {
                        key: value
                        for key, value in data.items()
                        if key not in {"id", "type", "owner_id", "version"}
                    }
                    if not update_data:
                        raise ValueError("import update requires mutable fields")
                    set_clause = ", ".join(f"{self._quote_col(key)} = ?" for key in update_data)
                    cursor.execute(
                        f"UPDATE items SET {set_clause}, version = version + 1 "
                        "WHERE id = ? AND owner_id = ? AND deleted = 0",
                        list(update_data.values()) + [item_id, owner_id],
                    )
                    if cursor.rowcount != 1:
                        raise ValueError("import update target not found")
                    if "remind_times" in validated:
                        self._sync_reminder_logs(cursor, item_id, validated.get("remind_times"))
                    refreshed_item_ids.append(item_id)
                else:
                    raise ValueError(f"unsupported import action: {action}")
                results.append((action, item_id, None))
            except Exception as exc:
                raise RuntimeError(f"导入记录 {item_id} 失败") from exc
        return results, refreshed_item_ids

    def _log_transfer_with_cursor(
        self,
        cursor: sqlite3.Cursor,
        *,
        owner_id: str,
        action: str,
        bundle_id: str | None = None,
        filename: str | None = None,
        types: list[str] | None = None,
        record_count: int = 0,
        result_summary: dict[str, Any] | None = None,
    ) -> int:
        cursor.execute(
            """INSERT INTO transfer_logs
               (owner_id, action, bundle_id, filename, types, record_count, result_summary, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                owner_id,
                action,
                bundle_id,
                filename,
                json.dumps(types or [], ensure_ascii=False),
                record_count,
                json.dumps(result_summary or {}, ensure_ascii=False),
                utc_now_iso(),
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("迁移日志写入后未返回主键")
        return cursor.lastrowid

    def batch_insert_or_update(
        self,
        operations: list[tuple[str, dict[str, Any]]],
        owner_id: str,
    ) -> list[tuple[str, str, str | None]]:
        """单事务批量插入/更新，用于数据导入"""
        if not operations:
            return []
        conn = self.get_connection()
        cursor = conn.cursor()

        with conn:
            results, refreshed_item_ids = self._apply_batch_operations(cursor, operations, owner_id)
            for refreshed_item_id in refreshed_item_ids:
                if refreshed_item_id:
                    self._refresh_fts(refreshed_item_id, conn)

        # 事务成功后清缓存（包括单条目和列表缓存）
        for _action, payload in operations:
            iid = payload.get("id", "")
            if iid:
                self.cache_invalidate(iid)
        self.cache_invalidate(f"items|{owner_id}")
        return results

    def execute_import_bundle(
        self,
        *,
        owner_id: str,
        bundle_id: str | None,
        operations: list[tuple[str, dict[str, Any]]],
        filename: str | None,
        types: list[str],
        record_count: int,
        result_summary: dict[str, Any],
        force: bool = False,
        collection_operations: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> None:
        """在同一写事务内导入集合、条目、幂等标记和迁移审计。"""
        with self.transaction(immediate=True) as conn:
            cursor = conn.cursor()
            self._reserve_import_bundle(cursor, owner_id, bundle_id, force=force)
            if collection_operations:
                self._apply_event_collection_import_operations(
                    cursor,
                    collection_operations,
                    owner_id,
                )
            self._apply_import_item_operations(cursor, conn, operations, owner_id)
            self._log_transfer_with_cursor(
                cursor,
                owner_id=owner_id,
                action="import",
                bundle_id=bundle_id,
                filename=filename,
                types=types,
                record_count=record_count,
                result_summary=result_summary,
            )

        self._invalidate_import_caches(owner_id, operations, collection_operations)

    @staticmethod
    def _reserve_import_bundle(
        cursor: sqlite3.Cursor,
        owner_id: str,
        bundle_id: str | None,
        *,
        force: bool,
    ) -> None:
        """在事务内占用 bundle ID；强制导入允许复用既有 ID。"""
        if not bundle_id:
            return
        cursor.execute(
            """
            INSERT INTO imported_bundles (owner_id, bundle_id, imported_at)
            VALUES (?, ?, ?)
            ON CONFLICT(owner_id, bundle_id) DO NOTHING
            """,
            (owner_id, bundle_id, utc_now_iso()),
        )
        if cursor.rowcount == 0 and not force:
            raise DuplicateBundleImportError(bundle_id)

    def _apply_import_item_operations(
        self,
        cursor: sqlite3.Cursor,
        conn: sqlite3.Connection,
        operations: list[tuple[str, dict[str, Any]]],
        owner_id: str,
    ) -> None:
        """应用条目导入操作，并在提交前同步全文索引。"""
        if not operations:
            return
        _, refreshed_item_ids = self._apply_batch_operations(cursor, operations, owner_id)
        for item_id in refreshed_item_ids:
            self._refresh_fts(item_id, conn)

    def _invalidate_import_caches(
        self,
        owner_id: str,
        operations: list[tuple[str, dict[str, Any]]],
        collection_operations: list[tuple[str, dict[str, Any]]] | None,
    ) -> None:
        """导入提交成功后精确失效条目、集合及所属用户列表缓存。"""
        for _action, payload in operations:
            item_id = str(payload.get("id") or "")
            if item_id:
                self.cache_invalidate(item_id)
        if operations:
            self.cache_invalidate(f"items|{owner_id}")
        for _action, payload in collection_operations or []:
            collection_id = str(payload.get("id") or "")
            if collection_id:
                self.cache_invalidate(collection_id)
        if collection_operations:
            self.cache_invalidate(f"event_collections|{owner_id}")

    def _apply_event_collection_import_operations(
        self,
        cursor: sqlite3.Cursor,
        operations: list[tuple[str, dict[str, Any]]],
        owner_id: str,
    ) -> None:
        now = utc_now_iso()
        user_timezone = TimezoneHelper.get_user_timezone(owner_id, self)
        for action, payload in operations:
            collection = dict(payload)
            collection["owner_id"] = owner_id
            if action == "insert":
                collection = self._prepare_new_event_collection(
                    collection,
                    now=now,
                    id_length=16,
                )
                collection = normalize_event_collection_datetimes_for_storage(
                    collection,
                    user_timezone,
                )
                data = self._prepare_event_collection_data(collection)
                columns = ", ".join(self._quote_col(k) for k in data.keys())
                placeholders = ", ".join(["?" for _ in data])
                cursor.execute(
                    f"INSERT INTO event_collections ({columns}) VALUES ({placeholders})",
                    list(data.values()),
                )
            elif action == "update":
                collection_id = str(collection.get("id") or "")
                if not collection_id:
                    raise ValueError("event collection update requires id")
                data = {
                    key: value
                    for key, value in collection.items()
                    if key in self._EVENT_COLLECTION_IMPORT_UPDATE_FIELDS
                }
                data["updated_at"] = collection.get("updated_at") or now
                data = normalize_event_collection_datetimes_for_storage(data, user_timezone)
                prepared = self._prepare_event_collection_data(data)
                set_clause = ", ".join(f"{self._quote_col(k)} = ?" for k in prepared.keys())
                cursor.execute(
                    f"""
                    UPDATE event_collections SET {set_clause}
                    WHERE id = ? AND owner_id = ? AND deleted = 0
                    """,
                    list(prepared.values()) + [collection_id, owner_id],
                )
            else:
                raise ValueError(f"Unsupported event collection import action: {action}")

    def batch_soft_delete(
        self,
        item_ids: list[str],
        owner_id: str,
        *,
        item_type: str | None = None,
        operation_action: str | None = None,
        details_factory: Callable[[str], dict[str, Any]] | None = None,
    ) -> int:
        """原子软删除真实匹配的条目，并同步附属数据和可选审计日志。"""
        unique_ids = list(dict.fromkeys(str(item_id) for item_id in item_ids if item_id))
        if not unique_ids:
            return 0
        now = utc_now_iso()
        affected = 0
        deleted_ids: list[str] = []
        with self.transaction(immediate=True) as conn:
            cursor = conn.cursor()
            active_ids = self._select_owner_item_ids(
                cursor,
                owner_id,
                unique_ids,
                deleted=False,
                item_type=item_type,
            )
            for offset in range(0, len(active_ids), _SQLITE_ID_BATCH_SIZE):
                batch = active_ids[offset : offset + _SQLITE_ID_BATCH_SIZE]
                placeholders = ",".join("?" for _ in batch)
                cursor.execute(
                    "UPDATE items SET deleted = 1, deleted_at = ?, updated_at = ?, "
                    f"version = version + 1 WHERE id IN ({placeholders}) "
                    "AND owner_id = ? AND deleted = 0",
                    [now, now, *batch, owner_id],
                )
                affected += cursor.rowcount
                if cursor.rowcount:
                    deleted_ids.extend(batch)
                cursor.execute(
                    f"DELETE FROM reminder_logs WHERE item_id IN ({placeholders})", batch
                )
                cursor.execute(f"DELETE FROM items_fts WHERE id IN ({placeholders})", batch)

            if operation_action:
                for item_id in deleted_ids:
                    details = (
                        details_factory(item_id)
                        if details_factory is not None
                        else {"soft_delete": True}
                    )
                    self._log_operation_with_cursor(
                        cursor,
                        user_id=owner_id,
                        action=operation_action,
                        item_type=item_type,
                        item_id=item_id,
                        details=details,
                        created_at=now,
                    )

        for iid in deleted_ids:
            self.cache_invalidate(iid)
        if deleted_ids:
            self.cache_invalidate(f"items|{owner_id}")
        return affected

    @staticmethod
    def _select_owner_item_ids(
        cursor: sqlite3.Cursor,
        owner_id: str,
        item_ids: list[str],
        *,
        deleted: bool | None = None,
        item_type: str | None = None,
    ) -> list[str]:
        """按用户、删除状态和可选类型筛 ID，并保持调用方原有顺序。"""
        matched: set[str] = set()
        for offset in range(0, len(item_ids), _SQLITE_ID_BATCH_SIZE):
            batch = item_ids[offset : offset + _SQLITE_ID_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            where = ["owner_id = ?", f"id IN ({placeholders})"]
            params: list[Any] = [owner_id, *batch]
            if deleted is not None:
                where.append("deleted = ?")
                params.append(int(deleted))
            if item_type is not None:
                where.append("type = ?")
                params.append(item_type)
            rows = cursor.execute(
                f"SELECT id FROM items WHERE {' AND '.join(where)}",
                params,
            ).fetchall()
            matched.update(str(row["id"]) for row in rows)
        return [item_id for item_id in item_ids if item_id in matched]

    def _prepare_event_collection_data(self, data: dict[str, Any]) -> dict[str, Any]:
        unknown = data.keys() - self._EVENT_COLLECTION_FIELDS
        if unknown:
            raise ValueError(f"Unsupported event collection field: {', '.join(sorted(unknown))}")
        require_canonical_utc_storage(data)
        prepared: dict[str, Any] = {}
        for key, value in data.items():
            if value is None:
                prepared[key] = None
            elif key in self._EVENT_COLLECTION_JSON_FIELDS and isinstance(value, (list, dict)):
                prepared[key] = json.dumps(value, ensure_ascii=False)
            else:
                prepared[key] = value
        return prepared

    @staticmethod
    def _prepare_new_event_collection(
        payload: dict[str, Any],
        *,
        now: str,
        id_length: int | None = None,
    ) -> dict[str, Any]:
        """补齐新集合的存储默认值，并统一校验身份和必填字段。"""
        collection = dict(payload)
        if not collection.get("id"):
            generated_id = uuid.uuid4().hex
            collection["id"] = generated_id[:id_length] if id_length else generated_id
        collection.setdefault("content", "")
        collection.setdefault("category", PendoConfig.DEFAULT_CATEGORY)
        collection.setdefault("location", "")
        collection.setdefault("tags", [])
        collection.setdefault("notes", "")
        collection.setdefault("context", {})
        collection.setdefault("visibility", "private")
        collection.setdefault("timezone", PendoConfig.DEFAULT_TIMEZONE)
        collection.setdefault("reminder_rules", [])
        collection.setdefault("created_at", now)
        collection.setdefault("updated_at", collection["created_at"])
        collection.setdefault("deleted", 0)
        if collection.get("kind") not in {"multi_node", "recurring"}:
            raise ValueError("Invalid event collection kind")
        if not collection.get("owner_id"):
            raise ValueError("event collection owner_id is required")
        if not collection.get("title"):
            raise ValueError("event collection title is required")
        return collection

    def _row_to_event_collection(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        for field in self._EVENT_COLLECTION_JSON_FIELDS:
            data[field] = self._decode_json_field(field, data.get(field))
        data["deleted"] = bool(data.get("deleted"))
        return data

    def create_event_collection(self, payload: dict[str, Any]) -> str:
        """创建不直接参与调度的日程集合头。"""
        now = utc_now_iso()
        owner_id = str(payload.get("owner_id") or "")
        user_timezone = TimezoneHelper.get_user_timezone(owner_id, self)
        collection = dict(payload)
        collection.setdefault("timezone", user_timezone.key)
        collection = self._prepare_new_event_collection(collection, now=now)
        collection = normalize_event_collection_datetimes_for_storage(
            collection,
            user_timezone,
        )

        data = self._prepare_event_collection_data(collection)
        columns = ", ".join(self._quote_col(k) for k in data.keys())
        placeholders = ", ".join(["?" for _ in data])
        conn = self.get_connection()
        with conn:
            conn.execute(
                f"INSERT INTO event_collections ({columns}) VALUES ({placeholders})",
                list(data.values()),
            )
        self.cache_invalidate(f"event_collections|{collection['owner_id']}")
        return str(collection["id"])

    def create_event_collection_with_children(
        self,
        payload: dict[str, Any],
        children: list[tuple[str, dict[str, Any] | EventItem]],
        *,
        operation_action: str = "create_event_collection",
    ) -> str:
        """原子创建集合、全部 leaf 条目及对应审计记录。"""
        now = utc_now_iso()
        owner_id = str(payload.get("owner_id") or "")
        user_timezone = TimezoneHelper.get_user_timezone(owner_id, self)
        collection_payload = dict(payload)
        collection_payload.setdefault("timezone", user_timezone.key)
        collection = self._prepare_new_event_collection(collection_payload, now=now)
        collection = normalize_event_collection_datetimes_for_storage(
            collection,
            user_timezone,
        )

        collection_data = self._prepare_event_collection_data(collection)
        collection_columns = ", ".join(self._quote_col(k) for k in collection_data)
        collection_placeholders = ", ".join("?" for _ in collection_data)
        child_ids: list[str] = []
        with self.transaction(immediate=True) as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"INSERT INTO event_collections ({collection_columns}) VALUES ({collection_placeholders})",
                list(collection_data.values()),
            )
            for child_id, child in children:
                if not child_id:
                    raise ValueError("event collection child id is required")
                item_data = child.to_dict() if isinstance(child, EventItem) else dict(child)
                item_data["id"] = child_id
                item_data["owner_id"] = collection["owner_id"]
                item_data["type"] = ItemType.EVENT.value
                item_data["event_collection_id"] = collection["id"]
                item_data["event_collection_kind"] = collection["kind"]
                item_data["event_role"] = (
                    "multi_node_child"
                    if collection["kind"] == "multi_node"
                    else "recurring_occurrence"
                )
                item_data.setdefault("created_at", now)
                item_data.setdefault("updated_at", now)
                validated = validate_item_data(item_data)
                validated.setdefault("timezone", str(collection["timezone"]))
                validated = normalize_item_datetimes_for_storage(validated, user_timezone)
                prepared = self._prepare_data(validated)
                columns = ", ".join(self._quote_col(k) for k in prepared)
                placeholders = ", ".join("?" for _ in prepared)
                cursor.execute(
                    f"INSERT INTO items ({columns}) VALUES ({placeholders})",
                    list(prepared.values()),
                )
                if "remind_times" in validated:
                    self._sync_reminder_logs(cursor, child_id, validated["remind_times"])
                self._update_fts(child_id, validated, conn)
                child_ids.append(child_id)
            self._log_operation_with_cursor(
                cursor,
                user_id=str(collection["owner_id"]),
                action=operation_action,
                item_type=ItemType.EVENT.value,
                item_id=str(collection["id"]),
                details={"child_ids": child_ids},
                created_at=now,
            )
        for child_id in child_ids:
            self.cache_invalidate(child_id)
        self.cache_invalidate(f"items|{collection['owner_id']}")
        self.cache_invalidate(f"event_collections|{collection['owner_id']}")
        return str(collection["id"])

    def get_event_collection(
        self, collection_id: str, owner_id: str | None = None
    ) -> dict[str, Any] | None:
        cache_key = self._cache_key("event_collection", collection_id, owner_id or "*")
        cached = self._cache_get_or_miss(cache_key)
        if cached is not _CACHE_MISS:
            return cast(dict[str, Any] | None, cached)

        conn = self.get_connection()
        if owner_id:
            row = conn.execute(
                """
                SELECT * FROM event_collections
                WHERE id = ? AND owner_id = ? AND deleted = 0
                """,
                (collection_id, owner_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM event_collections WHERE id = ? AND deleted = 0",
                (collection_id,),
            ).fetchone()
        collection = self._row_to_event_collection(row)
        if collection is not None:
            self._cache_set(cache_key, collection)
        return collection

    def get_event_collections_by_ids(
        self,
        owner_id: str,
        collection_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """分批读取指定用户的有效日程集合。"""
        unique_ids = list(dict.fromkeys(str(value) for value in collection_ids if value))
        collections: dict[str, dict[str, Any]] = {}
        conn = self.get_connection()
        for offset in range(0, len(unique_ids), _SQLITE_ID_BATCH_SIZE):
            batch = unique_ids[offset : offset + _SQLITE_ID_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT * FROM event_collections "
                f"WHERE owner_id = ? AND deleted = 0 AND id IN ({placeholders})",
                [owner_id, *batch],
            ).fetchall()
            for row in rows:
                collection = self._row_to_event_collection(row)
                if collection is None:
                    continue
                collection_id = str(collection["id"])
                collections[collection_id] = collection
                self._cache_set(
                    self._cache_key("event_collection", collection_id, owner_id),
                    collection,
                )
        return collections

    def update_event_collection(
        self,
        collection_id: str,
        updates: dict[str, Any],
        owner_id: str,
        *,
        operation_log: dict[str, Any] | None = None,
    ) -> bool:
        clean_updates = {
            key: value
            for key, value in updates.items()
            if key in self._EVENT_COLLECTION_UPDATE_FIELDS
        }
        if not clean_updates:
            return False
        conn = self.get_connection()
        current = conn.execute(
            """
            SELECT timezone FROM event_collections
            WHERE id = ? AND owner_id = ? AND deleted = 0
            """,
            (collection_id, owner_id),
        ).fetchone()
        if current is None:
            return False
        user_timezone = TimezoneHelper.get_user_timezone(owner_id, self)
        clean_updates["updated_at"] = utc_now_iso()
        context = {
            "timezone": str(current["timezone"] or user_timezone.key),
            **clean_updates,
        }
        normalized = normalize_event_collection_datetimes_for_storage(context, user_timezone)
        clean_updates = {key: normalized[key] for key in clean_updates}
        data = self._prepare_event_collection_data(clean_updates)
        set_clause = ", ".join(f"{self._quote_col(key)} = ?" for key in data)

        with conn:
            cursor = conn.execute(
                f"""
                UPDATE event_collections SET {set_clause}
                WHERE id = ? AND owner_id = ? AND deleted = 0
                """,
                list(data.values()) + [collection_id, owner_id],
            )
            if cursor.rowcount:
                # 内容与审计日志必须同成同败，不能留下无法撤销的半成功修改。
                self._log_operation_from_spec(
                    cursor,
                    operation_log,
                    default_item_id=collection_id,
                    default_item_type=ItemType.EVENT.value,
                )
        changed = cursor.rowcount > 0
        if changed:
            self.cache_invalidate(collection_id)
            self.cache_invalidate(f"event_collections|{owner_id}")
        return changed

    def update_event_collection_reminders(
        self,
        collection_id: str,
        owner_id: str,
        child_updates: dict[str, tuple[list[str], list[dict[str, int]]]],
        collection_rules: list[dict[str, int]] | None,
        *,
        collection_updates: dict[str, Any] | None = None,
        operation_log: dict[str, Any] | None = None,
    ) -> int:
        """原子更新集合字段、全部节点提醒及对应审计记录。"""

        ordered_updates = {
            str(item_id): (list(remind_times), list(reminder_rules))
            for item_id, (remind_times, reminder_rules) in child_updates.items()
            if item_id
        }
        clean_collection_updates = {
            key: value
            for key, value in (collection_updates or {}).items()
            if key in self._EVENT_COLLECTION_UPDATE_FIELDS
        }
        clean_collection_updates.update(
            {"reminder_rules": list(collection_rules)} if collection_rules is not None else {}
        )
        if not ordered_updates and not clean_collection_updates:
            return 0

        item_ids = list(ordered_updates)
        now = utc_now_iso()
        with self.transaction(immediate=True) as conn:
            cursor = conn.cursor()
            collection_row = cursor.execute(
                """
                SELECT timezone FROM event_collections
                WHERE id = ? AND owner_id = ? AND deleted = 0
                """,
                (collection_id, owner_id),
            ).fetchone()
            if collection_row is None:
                raise ItemNotFoundException(collection_id)
            user_timezone = TimezoneHelper.get_user_timezone(owner_id, self)
            collection_timezone = str(collection_row["timezone"] or user_timezone.key)
            if clean_collection_updates:
                collection_context = {
                    "timezone": collection_timezone,
                    **clean_collection_updates,
                    "updated_at": now,
                }
                normalized_collection = normalize_event_collection_datetimes_for_storage(
                    collection_context,
                    user_timezone,
                )
                clean_collection_updates = {
                    key: normalized_collection[key] for key in clean_collection_updates
                }
                collection_timezone = str(
                    normalized_collection.get("timezone") or collection_timezone
                )

            matched_ids: set[str] = set()
            for offset in range(0, len(item_ids), _SQLITE_ID_BATCH_SIZE):
                batch = item_ids[offset : offset + _SQLITE_ID_BATCH_SIZE]
                placeholders = ",".join("?" for _ in batch)
                rows = cursor.execute(
                    f"""
                    SELECT id FROM items
                    WHERE owner_id = ? AND type = ? AND deleted = 0
                      AND event_collection_id = ? AND id IN ({placeholders})
                    """,
                    [owner_id, ItemType.EVENT.value, collection_id, *batch],
                ).fetchall()
                matched_ids.update(str(row["id"]) for row in rows)
            missing_ids = set(item_ids) - matched_ids
            if missing_ids:
                raise ItemNotFoundException(min(missing_ids))

            for item_id, (remind_times, reminder_rules) in ordered_updates.items():
                normalized_child = normalize_item_datetimes_for_storage(
                    {
                        "type": ItemType.EVENT.value,
                        "timezone": collection_timezone,
                        "remind_times": remind_times,
                        "updated_at": now,
                    },
                    user_timezone,
                )
                require_canonical_utc_storage(normalized_child)
                remind_times = cast(list[str], normalized_child["remind_times"])
                cursor.execute(
                    """
                    UPDATE items
                    SET remind_times = ?, reminder_rules = ?, updated_at = ?,
                        version = version + 1
                    WHERE id = ? AND owner_id = ? AND type = ? AND deleted = 0
                      AND event_collection_id = ?
                    """,
                    (
                        json.dumps(remind_times, ensure_ascii=False),
                        json.dumps(reminder_rules, ensure_ascii=False),
                        now,
                        item_id,
                        owner_id,
                        ItemType.EVENT.value,
                        collection_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"event reminder update lost item: {item_id}")
                self._sync_reminder_logs(cursor, item_id, remind_times)

            if clean_collection_updates:
                clean_collection_updates["updated_at"] = now
                collection_data = self._prepare_event_collection_data(clean_collection_updates)
                set_clause = ", ".join(f"{self._quote_col(key)} = ?" for key in collection_data)
                cursor.execute(
                    f"""
                    UPDATE event_collections SET {set_clause}
                    WHERE id = ? AND owner_id = ? AND deleted = 0
                    """,
                    [*collection_data.values(), collection_id, owner_id],
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"event collection update lost collection: {collection_id}")

            self._log_operation_from_spec(
                cursor,
                operation_log,
                default_item_id=collection_id,
                default_item_type=ItemType.EVENT.value,
            )

        for item_id in item_ids:
            self.cache_invalidate(item_id)
        self.cache_invalidate(f"items|{owner_id}")
        self.cache_invalidate(collection_id)
        self.cache_invalidate(f"event_collections|{owner_id}")
        return len(item_ids)

    def get_collection_events(self, collection_id: str, owner_id: str) -> list[EventItem]:
        conn = self.get_connection()
        rows = conn.execute(
            """
            SELECT * FROM items
            WHERE owner_id = ? AND type = ? AND deleted = 0 AND event_collection_id = ?
            ORDER BY COALESCE(event_index, 999999), start_time, id
            """,
            (owner_id, ItemType.EVENT.value, collection_id),
        ).fetchall()
        return [item for row in rows if isinstance((item := self._row_to_item(row)), EventItem)]

    def delete_event_instance(self, item_id: str, owner_id: str) -> tuple[str, bool] | None:
        """原子删除单个日程；最后一个多节点日程同时删除空集合头。"""
        now = utc_now_iso()
        collection_id: str | None = None
        collection_deleted = False
        title = ""
        with self.transaction(immediate=True) as conn:
            cursor = conn.cursor()
            row = cursor.execute(
                """
                SELECT title, event_collection_id, event_collection_kind
                FROM items
                WHERE id = ? AND owner_id = ? AND type = ? AND deleted = 0
                """,
                (item_id, owner_id, ItemType.EVENT.value),
            ).fetchone()
            if row is None:
                return None

            title = str(row["title"] or "")
            if row["event_collection_id"]:
                collection_id = str(row["event_collection_id"])

            # 多节点集合不能在删除最后一个 leaf 后留下不可见的空壳。集合头与
            # leaf 共用一次删除日志，撤销时才能把二者一并恢复。
            if collection_id and row["event_collection_kind"] == "multi_node":
                sibling_count = int(
                    cursor.execute(
                        """
                        SELECT COUNT(*) FROM items
                        WHERE owner_id = ? AND type = ? AND deleted = 0
                          AND event_collection_id = ?
                        """,
                        (owner_id, ItemType.EVENT.value, collection_id),
                    ).fetchone()[0]
                )
                if sibling_count == 1:
                    cursor.execute(
                        """
                        UPDATE event_collections
                        SET deleted = 1, deleted_at = ?, updated_at = ?
                        WHERE id = ? AND owner_id = ? AND deleted = 0
                        """,
                        (now, now, collection_id, owner_id),
                    )
                    collection_deleted = cursor.rowcount == 1

            cursor.execute(
                """
                UPDATE items
                SET deleted = 1, deleted_at = ?, updated_at = ?, version = version + 1
                WHERE id = ? AND owner_id = ? AND type = ? AND deleted = 0
                """,
                (now, now, item_id, owner_id, ItemType.EVENT.value),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"event deletion lost item: {item_id}")
            cursor.execute("DELETE FROM reminder_logs WHERE item_id = ?", (item_id,))
            cursor.execute("DELETE FROM items_fts WHERE id = ?", (item_id,))

            if collection_deleted and collection_id:
                self._log_operation_with_cursor(
                    cursor,
                    user_id=owner_id,
                    action="delete_event_collection",
                    item_type=ItemType.EVENT.value,
                    item_id=collection_id,
                    details={"child_ids": [item_id]},
                    created_at=now,
                )
            else:
                self._log_operation_with_cursor(
                    cursor,
                    user_id=owner_id,
                    action="delete",
                    item_type=ItemType.EVENT.value,
                    item_id=item_id,
                    details={"soft_delete": True},
                    created_at=now,
                )

        self.cache_invalidate(item_id)
        self.cache_invalidate(f"items|{owner_id}")
        if collection_deleted and collection_id:
            self.cache_invalidate(collection_id)
            self.cache_invalidate(f"event_collections|{owner_id}")
        return title, collection_deleted

    def delete_event_collection(
        self,
        collection_id: str,
        owner_id: str,
        *,
        cascade: bool = True,
        operation_log: dict[str, Any] | None = None,
    ) -> bool:
        # 操作日志和现有撤销查询仍沿用本地 ISO 时间；这里保持相同格式，确保
        # 刚写入的集合删除日志能与其子日程按同一时间基准排序。
        now = utc_now_iso()
        with self.transaction(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE event_collections
                SET deleted = 1, deleted_at = ?, updated_at = ?
                WHERE id = ? AND owner_id = ? AND deleted = 0
                """,
                (now, now, collection_id, owner_id),
            )
            affected = cursor.rowcount
            child_ids: list[str] = []
            if affected and cascade:
                rows = cursor.execute(
                    """
                    SELECT id FROM items
                    WHERE owner_id = ? AND type = ? AND deleted = 0 AND event_collection_id = ?
                    """,
                    (owner_id, ItemType.EVENT.value, collection_id),
                ).fetchall()
                child_ids = [str(row["id"]) for row in rows]
                if child_ids:
                    placeholders = ",".join("?" for _ in child_ids)
                    cursor.execute(
                        f"UPDATE items SET deleted = 1, deleted_at = ?, updated_at = ? "
                        f"WHERE id IN ({placeholders}) AND owner_id = ?",
                        [now, now] + child_ids + [owner_id],
                    )
                    cursor.execute(f"DELETE FROM items_fts WHERE id IN ({placeholders})", child_ids)
                    cursor.execute(
                        f"DELETE FROM reminder_logs WHERE item_id IN ({placeholders})", child_ids
                    )
            if affected:
                self._log_operation_from_spec(
                    cursor,
                    operation_log,
                    default_item_id=collection_id,
                    default_item_type=ItemType.EVENT.value,
                )
        if affected:
            for child_id in child_ids:
                self.cache_invalidate(child_id)
            self.cache_invalidate(f"items|{owner_id}")
            self.cache_invalidate(collection_id)
            self.cache_invalidate(f"event_collections|{owner_id}")
        return affected > 0

    def get_item(self, item_id: str, owner_id: str | None = None) -> Item | None:
        """获取单个条目，返回Item dataclass实例"""
        cache_key = self._cache_key("item", item_id, owner_id or "*")
        cached = self._cache_get_or_miss(cache_key)
        if cached is not _CACHE_MISS:
            return cached

        conn = self.get_connection()
        cursor = conn.cursor()

        if owner_id:
            cursor.execute(
                "SELECT * FROM items WHERE id = ? AND owner_id = ? AND deleted = 0",
                (item_id, owner_id),
            )
        else:
            cursor.execute("SELECT * FROM items WHERE id = ? AND deleted = 0", (item_id,))

        row = cursor.fetchone()
        if row:
            item = self._row_to_item(row)
            if item:
                self._cache_set(cache_key, item)
                return item
        return None

    def get_items_by_ids(self, owner_id: str, item_ids: list[str]) -> dict[str, Item]:
        """分批读取该用户仍有效的条目，避免超过 SQLite 绑定参数上限。"""
        unique_ids = list(dict.fromkeys(str(item_id) for item_id in item_ids if item_id))
        if not unique_ids:
            return {}
        conn = self.get_connection()
        items: dict[str, Item] = {}
        for offset in range(0, len(unique_ids), _SQLITE_ID_BATCH_SIZE):
            batch = unique_ids[offset : offset + _SQLITE_ID_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT * FROM items WHERE owner_id = ? AND deleted = 0 "
                f"AND id IN ({placeholders})",
                [owner_id, *batch],
            ).fetchall()
            for row in rows:
                item = self._row_to_item(row)
                if item is not None:
                    items[str(item.id)] = item
        return items

    _ALLOWED_SORT_FIELDS: ClassVar[set[str]] = {
        "created_at",
        "updated_at",
        "ledger_date",
        "plan_date",
        "deadline_at",
        "completed_at",
        "cancelled_at",
        "start_time",
        "diary_date",
        "entry_time",
        "amount",
        "amount_cents",
    }

    def _item_list_conditions(
        self,
        owner_id: str,
        filters: dict[str, Any] | None,
    ) -> tuple[list[str], list[Any]]:
        """为列表、总数和金额汇总构造完全一致的过滤条件。"""

        where = ["owner_id = ?", "deleted = 0"]
        params: list[Any] = [owner_id]
        resolved_filters = self._resolve_item_filters(owner_id, filters)
        self._apply_filters(where, params, resolved_filters)
        self._apply_list_only_filters(where, params, resolved_filters)
        return where, params

    def _resolve_item_filters(
        self,
        owner_id: str,
        filters: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Copy filters and attach the explicit user zone needed by timestamp SQL."""

        resolved = dict(filters or {})
        date_field = str(resolved.get("date_field") or "")
        sort_field = str(resolved.get("sort_field") or "created_at")
        if date_field in self._TIMESTAMP_FIELDS or sort_field in self._TIMESTAMP_FIELDS:
            resolved["_user_timezone"] = TimezoneHelper.get_user_timezone(owner_id, self).key
        return resolved

    def get_items(
        self,
        owner_id: str,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
        offset: int = 0,
        *,
        use_cache: bool = True,
    ) -> list[Item]:
        """按白名单过滤、排序和分页读取一个用户的有效条目。"""
        if limit <= 0:
            return []
        if offset < 0:
            raise ValueError("offset must be non-negative")
        resolved_filters = self._resolve_item_filters(owner_id, filters)
        cache_key: str | None = None
        if use_cache:
            cache_key = self._cache_key("items", owner_id, resolved_filters, limit, offset)
            cached = self._cache_get_or_miss(cache_key)
            if cached is not _CACHE_MISS:
                return cast(list[Item], cached)

        conn = self.get_connection()
        cursor = conn.cursor()

        where, params = self._item_list_conditions(owner_id, resolved_filters)
        sort_field, sort_order, sort_params = self._resolve_item_sort(resolved_filters)

        sql = (
            f"SELECT * FROM items WHERE {' AND '.join(where)} "
            f"ORDER BY {sort_field} {sort_order} LIMIT ? OFFSET ?"
        )
        cursor.execute(sql, params + sort_params + [limit, offset])

        items: list[Item] = []
        for row in cursor.fetchall():
            item = self._row_to_item(row)
            if item:
                items.append(item)

        if cache_key is not None:
            self._cache_set(cache_key, items)
        return items

    def get_item_ids(
        self,
        owner_id: str,
        filters: dict[str, Any] | None = None,
    ) -> list[str]:
        """只读取匹配条目的 ID，供批量操作避免物化完整对象。"""

        where, params = self._item_list_conditions(owner_id, filters)
        rows = self.get_connection().execute(
            f"SELECT id FROM items WHERE {' AND '.join(where)} ORDER BY id",
            params,
        )
        return [str(row["id"]) for row in rows.fetchall()]

    def get_note_backlinks(
        self,
        owner_id: str,
        note_id: str,
        *,
        limit: int = 10,
    ) -> list[Item]:
        """用 JSON1 精确读取直接关联指定笔记的其他笔记。"""

        if limit <= 0:
            return []
        timezone_name = TimezoneHelper.get_user_timezone(owner_id, self).key
        rows = (
            self.get_connection()
            .execute(
                """
            SELECT i.*
            FROM items AS i
            WHERE i.owner_id = ? AND i.type = ? AND i.deleted = 0 AND i.id != ?
              AND (
                EXISTS (
                  SELECT 1
                  FROM json_each(
                    CASE WHEN json_valid(i.related_items) THEN i.related_items ELSE '[]' END
                  ) AS related
                  WHERE CAST(related.value AS TEXT) = ?
                )
                OR EXISTS (
                  SELECT 1
                  FROM json_each(
                    CASE WHEN json_valid(i."references") THEN i."references" ELSE '[]' END
                  ) AS reference
                  WHERE reference.type = 'object'
                    AND CAST(json_extract(reference.value, '$.id') AS TEXT) = ?
                )
              )
            ORDER BY pendo_utc_epoch(
                       COALESCE(NULLIF(i.updated_at, ''), i.created_at), ?
                     ) DESC,
                     pendo_utc_epoch(i.created_at, ?) DESC,
                     i.id DESC
            LIMIT ?
            """,
                (
                    owner_id,
                    ItemType.NOTE.value,
                    note_id,
                    note_id,
                    note_id,
                    timezone_name,
                    timezone_name,
                    limit,
                ),
            )
            .fetchall()
        )
        return [item for row in rows if (item := self._row_to_item(row)) is not None]

    def count_items(
        self,
        owner_id: str,
        filters: dict[str, Any] | None = None,
    ) -> int:
        """使用与 ``get_items`` 相同的过滤语义统计完整结果集。"""

        where, params = self._item_list_conditions(owner_id, filters)
        row = (
            self.get_connection()
            .execute(
                f"SELECT COUNT(*) FROM items WHERE {' AND '.join(where)}",
                params,
            )
            .fetchone()
        )
        return int(row[0] or 0) if row is not None else 0

    def aggregate_item_amounts(
        self,
        owner_id: str,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, tuple[int, int]]:
        """按交易类型汇总过滤结果的整数分金额与条目数。"""

        where, params = self._item_list_conditions(owner_id, filters)
        rows = (
            self.get_connection()
            .execute(
                f"""
            SELECT transaction_type,
                   COALESCE(SUM({self._LEDGER_AMOUNT_CENTS_EXPR}), 0) AS amount_cents,
                   COUNT(*) AS item_count
            FROM items
            WHERE {" AND ".join(where)}
            GROUP BY transaction_type
            """,
                params,
            )
            .fetchall()
        )
        return {
            str(row["transaction_type"] or ""): (
                int(row["amount_cents"] or 0),
                int(row["item_count"] or 0),
            )
            for row in rows
        }

    def aggregate_ledger_amounts_by_day(
        self,
        owner_id: str,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, tuple[int, int]]:
        """按账目日期汇总支出与收入分值，不物化账目行。"""

        where, params = self._item_list_conditions(owner_id, filters)
        rows = (
            self.get_connection()
            .execute(
                f"""
            SELECT ledger_date,
                   COALESCE(SUM(CASE WHEN transaction_type = 'expense'
                                     THEN {self._LEDGER_AMOUNT_CENTS_EXPR} ELSE 0 END), 0)
                     AS expense_cents,
                   COALESCE(SUM(CASE WHEN transaction_type = 'income'
                                     THEN {self._LEDGER_AMOUNT_CENTS_EXPR} ELSE 0 END), 0)
                     AS income_cents
            FROM items
            WHERE {" AND ".join(where)}
            GROUP BY ledger_date
            ORDER BY ledger_date
            """,
                params,
            )
            .fetchall()
        )
        return {
            str(row["ledger_date"]): (
                int(row["expense_cents"] or 0),
                int(row["income_cents"] or 0),
            )
            for row in rows
            if row["ledger_date"]
        }

    def get_active_task_preview(self, owner_id: str, *, limit: int = 8) -> list[Item]:
        """按看板业务顺序读取有界的未完成待办预览。"""

        if limit <= 0:
            return []
        rows = (
            self.get_connection()
            .execute(
                """
            SELECT * FROM items
            WHERE owner_id = ? AND type = ? AND deleted = 0
              AND COALESCE(status, 'open') = 'open'
            ORDER BY CASE WHEN priority BETWEEN 1 AND 5 THEN priority ELSE 3 END,
                     COALESCE(NULLIF(plan_date, ''), '9999-12-31'),
                     COALESCE(NULLIF(deadline_at, ''), '9999-12-31T23:59:59'),
                     created_at, id
            LIMIT ?
            """,
                (owner_id, ItemType.TASK.value, limit),
            )
            .fetchall()
        )
        return [item for row in rows if (item := self._row_to_item(row)) is not None]

    def _apply_list_only_filters(
        self,
        where: list[str],
        params: list[Any],
        filters: dict[str, Any],
    ) -> None:
        """追加普通列表查询特有的关键词和金额过滤条件。"""
        keyword = sanitize_search_keyword(str(filters.get("keyword") or ""))
        if keyword:
            like = self._like_contains_pattern(keyword)
            where.append(
                """(
                    title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' OR
                    category LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\' OR
                    ledger_category LIKE ? ESCAPE '\\' OR account_name LIKE ? ESCAPE '\\' OR
                    counter_account_name LIKE ? ESCAPE '\\' OR merchant LIKE ? ESCAPE '\\' OR
                    remark LIKE ? ESCAPE '\\' OR location LIKE ? ESCAPE '\\' OR
                    notes LIKE ? ESCAPE '\\' OR weather LIKE ? ESCAPE '\\'
                )"""
            )
            params.extend([like] * 12)
        if "amount_min" in filters:
            where.append(f"{self._LEDGER_AMOUNT_CENTS_EXPR} >= ?")
            params.append(ledger_amount_filter_to_cents(filters["amount_min"]))
        if "amount_max" in filters:
            where.append(f"{self._LEDGER_AMOUNT_CENTS_EXPR} <= ?")
            params.append(ledger_amount_filter_to_cents(filters["amount_max"]))

    def _resolve_item_sort(self, filters: dict[str, Any]) -> tuple[str, str, list[Any]]:
        """把外部排序值收敛到固定列名和 ASC/DESC。"""
        requested_field = str(filters.get("sort_field") or "created_at")
        requested_order = str(filters.get("sort_order") or "DESC").upper()
        sort_field = (
            requested_field if requested_field in self._ALLOWED_SORT_FIELDS else "created_at"
        )
        sort_order = requested_order if requested_order in {"ASC", "DESC"} else "DESC"
        if sort_field in self._TIMESTAMP_FIELDS:
            timezone_name = str(filters.get("_user_timezone") or "")
            try:
                ZoneInfo(timezone_name)
            except (TypeError, ValueError) as exc:
                raise ValueError("timestamp sorting requires a valid user timezone") from exc
            return f"pendo_utc_epoch({sort_field}, ?)", sort_order, [timezone_name]
        return sort_field, sort_order, []

    def get_all_items(
        self,
        owner_id: str,
        filters: dict[str, Any] | None = None,
        *,
        page_size: int = 200,
    ) -> list[Item]:
        """通过受限分页读取完整过滤结果。"""
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        results: list[Item] = []
        offset = 0
        while True:
            page = self.get_items(
                owner_id,
                filters=filters,
                limit=page_size,
                offset=offset,
                use_cache=False,
            )
            results.extend(page)
            if len(page) < page_size:
                return results
            offset += len(page)

    def get_active_user_ids(self) -> list[str]:
        """返回至少有一条未删除数据的去重用户 ID。"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT owner_id FROM items WHERE deleted = 0")
        return [str(row[0]) for row in cursor.fetchall()]

    def migrate_undone_tasks_to_date(
        self,
        user_id: str,
        source_date: str,
        target_date: str,
    ) -> int:
        """原子迁移未完成待办，并持久化当日迁移标记。

        立即事务会串行化多个调度实例。移动前依据完整业务条件和已读版本
        再次校验每条待办，防止旧快照覆盖已完成、已删除或已改期的状态。
        """
        migrated_ids: list[str] = []
        item_timestamp = utc_now_iso()
        settings_timestamp = item_timestamp
        marker_patch = json.dumps(
            {"last_todo_migrate_date": target_date},
            ensure_ascii=False,
        )

        with self._settings_lock, self.transaction(immediate=True) as conn:
            cursor = conn.cursor()
            settings_row = cursor.execute(
                "SELECT settings_json FROM user_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            raw_settings: dict[str, Any] = {}
            if settings_row and settings_row["settings_json"]:
                try:
                    decoded = json.loads(settings_row["settings_json"])
                    if isinstance(decoded, dict):
                        raw_settings = decoded
                except (TypeError, json.JSONDecodeError, ValueError):
                    raw_settings = {}
            if raw_settings.get("last_todo_migrate_date") == target_date:
                return 0

            rows = cursor.execute(
                """
                SELECT id, version
                FROM items
                WHERE owner_id = ? AND type = ? AND plan_date = ?
                  AND status = 'open' AND deleted = 0
                ORDER BY id
                """,
                (user_id, ItemType.TASK.value, source_date),
            ).fetchall()
            for row in rows:
                cursor.execute(
                    """
                    UPDATE items
                    SET plan_date = ?, updated_at = ?, version = version + 1
                    WHERE id = ? AND owner_id = ? AND type = ?
                      AND plan_date = ? AND status = 'open' AND deleted = 0
                      AND version = ?
                    """,
                    (
                        target_date,
                        item_timestamp,
                        row["id"],
                        user_id,
                        ItemType.TASK.value,
                        source_date,
                        row["version"],
                    ),
                )
                if cursor.rowcount == 1:
                    migrated_ids.append(str(row["id"]))

            if migrated_ids:
                self._log_operation_with_cursor(
                    cursor,
                    user_id=user_id,
                    action="migrate_todos",
                    item_type=ItemType.TASK.value,
                    details={
                        "source_date": source_date,
                        "target_date": target_date,
                        "item_ids": migrated_ids,
                        "count": len(migrated_ids),
                    },
                )

            cursor.execute(
                """
                INSERT INTO user_settings (user_id, settings_json, updated_at, version)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(user_id) DO UPDATE SET
                    settings_json = CASE
                        WHEN json_valid(COALESCE(user_settings.settings_json, '{}'))
                        THEN json_patch(COALESCE(user_settings.settings_json, '{}'), excluded.settings_json)
                        ELSE excluded.settings_json
                    END,
                    updated_at = excluded.updated_at,
                    version = user_settings.version + 1
                """,
                (user_id, marker_patch, settings_timestamp),
            )

        for item_id in migrated_ids:
            self.cache_invalidate(item_id)
        self.cache_invalidate(f"items|{user_id}")
        self.cache_invalidate(self._cache_key("settings", user_id))
        return len(migrated_ids)

    def get_last_unconfirmed_remind_time(self, item_id: str) -> str | None:
        """返回条目最近一次已发送但未确认的提醒时间。"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT remind_time FROM reminder_logs
            WHERE item_id = ? AND sent_at IS NOT NULL AND confirmed_at IS NULL
            ORDER BY COALESCE(last_sent_at, sent_at) DESC LIMIT 1
            """,
            (item_id,),
        )
        row = cursor.fetchone()
        return str(row[0]) if row else None

    def delete_item(
        self,
        item_id: str,
        soft: bool = True,
        *,
        owner_id: str,
        operation_log: dict[str, Any] | None = None,
    ) -> bool:
        """删除一个匹配用户的条目；未匹配时不得触碰其附属数据。"""
        conn = self.get_connection()
        cursor = conn.cursor()
        params = [item_id, owner_id]

        with conn:
            if soft:
                now = utc_now_iso()
                cursor.execute(
                    "UPDATE items SET deleted = 1, deleted_at = ?, updated_at = ?, "
                    "version = version + 1 WHERE id = ? AND owner_id = ? AND deleted = 0",
                    [now, now] + params,
                )
            else:
                cursor.execute("DELETE FROM items WHERE id = ? AND owner_id = ?", params)
            affected = cursor.rowcount
            if affected > 0:
                cursor.execute("DELETE FROM reminder_logs WHERE item_id = ?", (item_id,))
                cursor.execute("DELETE FROM items_fts WHERE id = ?", (item_id,))
                self._log_operation_from_spec(
                    cursor,
                    operation_log,
                    default_item_id=item_id,
                    default_item_type=None,
                )
        if affected > 0:
            self.cache_invalidate(item_id)
            self.cache_invalidate(f"items|{owner_id}")
        return affected > 0

    def _sync_reminder_logs(
        self,
        cursor: sqlite3.Cursor,
        item_id: str,
        remind_times: list[str] | None,
        *,
        timezone_name: str | None = None,
    ) -> None:
        """同步当前提醒点的物化 UTC 队列，并保留已发送历史。"""

        requested_times = sorted(
            {str(remind_time) for remind_time in (remind_times or []) if remind_time}
        )
        if timezone_name is None:
            row = cursor.execute(
                """
                SELECT COALESCE(NULLIF(us.timezone, ''), ?) AS timezone_name
                FROM items AS i
                LEFT JOIN user_settings AS us ON us.user_id = i.owner_id
                WHERE i.id = ?
                """,
                (PendoConfig.DEFAULT_TIMEZONE, item_id),
            ).fetchone()
            if row is None:
                return
            timezone_name = str(row["timezone_name"])
        schedules = [
            (remind_time, fire_at_utc)
            for remind_time in requested_times
            if (fire_at_utc := _reminder_fire_at_utc(remind_time, timezone_name)) is not None
        ]
        active_times = [remind_time for remind_time, _fire_at_utc in schedules]
        if not active_times:
            cursor.execute(
                "DELETE FROM reminder_logs WHERE item_id = ? AND sent_at IS NULL",
                (item_id,),
            )
            return

        placeholders = ",".join("?" for _ in active_times)
        cursor.execute(
            f"""DELETE FROM reminder_logs
                WHERE item_id = ? AND sent_at IS NULL
                AND remind_time NOT IN ({placeholders})""",
            [item_id] + active_times,
        )
        for remind_time, fire_at_utc in schedules:
            cursor.execute(
                """
                INSERT INTO reminder_logs
                    (item_id, remind_time, fire_at_utc, state, repeat_count, failure_count)
                VALUES (?, ?, ?, 'pending', 0, 0)
                ON CONFLICT(item_id, remind_time) DO UPDATE SET
                    fire_at_utc = excluded.fire_at_utc
                WHERE reminder_logs.sent_at IS NULL AND reminder_logs.confirmed_at IS NULL
                """,
                (item_id, remind_time, fire_at_utc),
            )

    def _apply_filters(
        self,
        where: list[str],
        params: list[Any],
        filters: dict[str, Any] | None,
        column_prefix: str = "",
    ) -> None:
        """将常用过滤条件追加到 where / params"""
        if filters:
            for key in self._EXACT_FILTER_FIELDS:
                if key in filters:
                    column = f"{column_prefix}{key}" if column_prefix else key
                    # 兼容清理规范化前写入的首尾空格，避免下拉框显示出的
                    # 分类值反而筛不到同一批旧条目。
                    trim_value = key in {"category", "ledger_category"}
                    column = f"TRIM({column})" if trim_value else column
                    value = str(filters[key]).strip() if trim_value else filters[key]
                    where.append(f"{column} = ?")
                    params.append(value)
            if "account_name" in filters:
                account_column = f"{column_prefix}account_name" if column_prefix else "account_name"
                counter_column = (
                    f"{column_prefix}counter_account_name"
                    if column_prefix
                    else "counter_account_name"
                )
                where.append(f"({account_column} = ? OR {counter_column} = ?)")
                params.extend([filters["account_name"], filters["account_name"]])
            if "tags" in filters:
                tags_column = f"{column_prefix}tags" if column_prefix else "tags"
                where.append(f"{tags_column} LIKE ? ESCAPE '\\'")
                params.append(self.tag_filter_pattern(filters["tags"]))

            date_field = filters.get("date_field")
            if date_field:
                if date_field not in Database.ALLOWED_DATE_FIELDS:
                    raise ValueError(f"Invalid date field: {date_field}")

                column = f"{column_prefix}{date_field}" if column_prefix else date_field
                if date_field in self._DATE_ONLY_FIELDS:
                    if "start_date" in filters:
                        where.append(f"{column} >= ?")
                        params.append(filters["start_date"])
                    if "end_date" in filters:
                        where.append(f"{column} <= ?")
                        params.append(filters["end_date"])
                    return

                timezone_name = str(filters.get("_user_timezone") or "")
                try:
                    user_timezone = ZoneInfo(timezone_name)
                except (TypeError, ValueError) as exc:
                    raise ValueError("timestamp filters require a valid user timezone") from exc
                start_value = filters.get("start_date")
                end_value = filters.get("end_date")
                if start_value is not None and end_value is not None:
                    start_at = TimezoneHelper.parse(str(start_value), user_timezone)
                    end_at = TimezoneHelper.parse(str(end_value), user_timezone)
                    lower_date = (start_at - timedelta(days=2)).date().isoformat()
                    upper_date = (end_at + timedelta(days=2)).date().isoformat()
                    where.append(
                        f"substr({column}, 1, 10) BETWEEN ? AND ? "
                        f"AND pendo_utc_epoch({column}, ?) BETWEEN ? AND ?"
                    )
                    params.extend(
                        [
                            lower_date,
                            upper_date,
                            timezone_name,
                            start_at.timestamp(),
                            end_at.timestamp(),
                        ]
                    )
                elif start_value is not None:
                    start_at = TimezoneHelper.parse(str(start_value), user_timezone)
                    lower_date = (start_at - timedelta(days=2)).date().isoformat()
                    where.append(
                        f"substr({column}, 1, 10) >= ? AND pendo_utc_epoch({column}, ?) >= ?"
                    )
                    params.extend([lower_date, timezone_name, start_at.timestamp()])
                elif end_value is not None:
                    end_at = TimezoneHelper.parse(str(end_value), user_timezone)
                    upper_date = (end_at + timedelta(days=2)).date().isoformat()
                    where.append(
                        f"substr({column}, 1, 10) <= ? AND pendo_utc_epoch({column}, ?) <= ?"
                    )
                    params.extend([upper_date, timezone_name, end_at.timestamp()])

    def _search_fts_ids(
        self,
        cursor: sqlite3.Cursor,
        owner_id: str,
        query: str,
        filters: dict[str, Any] | None,
    ) -> list[str]:
        """查询 FTS5 索引；需要字面 LIKE 转义的输入留给后续查询。"""
        if any(char in query for char in ("%", "_", "\\")):
            return []
        try:
            where = ["items_fts MATCH ?", "i.owner_id = ?", "i.deleted = 0"]
            params: list[Any] = [query, owner_id]
            self._apply_filters(where, params, filters, column_prefix="i.")
            timezone_name = str((filters or {}).get("_user_timezone") or "")
            rows = cursor.execute(
                f"""
                SELECT i.id
                FROM items_fts
                JOIN items i ON i.id = items_fts.id
                WHERE {" AND ".join(where)}
                ORDER BY bm25(items_fts),
                         pendo_utc_epoch(i.updated_at, ?) DESC,
                         pendo_utc_epoch(i.created_at, ?) DESC
                """,
                [*params, timezone_name, timezone_name],
            ).fetchall()
        except Exception as exc:
            logger.warning(
                "FTS search failed; falling back to LIKE error_type=%s",
                type(exc).__name__,
            )
            return []
        return [str(row[0]) for row in rows]

    def _search_item_ids(
        self,
        owner_id: str,
        query: str,
        filters: dict[str, Any] | None = None,
    ) -> list[str]:
        """执行搜索并返回按相关性去重后的条目 ID。"""
        conn = self.get_connection()
        cursor = conn.cursor()
        filters = self._resolve_item_filters(owner_id, filters)

        # 清洗搜索关键词
        query = sanitize_search_keyword(query)
        if not query:
            return []

        # LIKE 通配符在用户输入中是普通字符；FTS5 不支持这种转义，
        # 因此遇到这些字符时直接交给后面的字面 LIKE 搜索。
        fts_ids = self._search_fts_ids(cursor, owner_id, query, filters)

        # LIKE补充搜索（FTS的unicode61分词器对CJK子字符串匹配不完整，需要LIKE兜底）
        like = self._like_contains_pattern(query)
        like_where: list[str] = [
            "owner_id = ?",
            "deleted = 0",
            """(
                title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' OR
                tags LIKE ? ESCAPE '\\' OR category LIKE ? ESCAPE '\\' OR
                remark LIKE ? ESCAPE '\\' OR ledger_category LIKE ? ESCAPE '\\' OR
                account_name LIKE ? ESCAPE '\\' OR counter_account_name LIKE ? ESCAPE '\\' OR
                merchant LIKE ? ESCAPE '\\' OR location LIKE ? ESCAPE '\\' OR
                notes LIKE ? ESCAPE '\\' OR weather LIKE ? ESCAPE '\\'
            )""",
        ]
        like_params: list[Any] = [
            owner_id,
            like,
            like,
            like,
            like,
            like,
            like,
            like,
            like,
            like,
            like,
            like,
            like,
        ]
        self._apply_filters(like_where, like_params, filters)
        timezone_name = str((filters or {}).get("_user_timezone") or "")

        cursor.execute(
            f"""
            SELECT id
            FROM items
            WHERE {" AND ".join(like_where)}
            ORDER BY pendo_utc_epoch(updated_at, ?) DESC,
                     pendo_utc_epoch(created_at, ?) DESC
            """,
            [*like_params, timezone_name, timezone_name],
        )
        like_ids = [row[0] for row in cursor.fetchall()]

        collection_ids: list[str] = []
        if not filters or filters.get("type") in (None, ItemType.EVENT.value, "event"):
            collection_where: list[str] = [
                "c.owner_id = ?",
                "c.deleted = 0",
                f"i.type = '{ItemType.EVENT.value}'",
                "i.owner_id = ?",
                "i.deleted = 0",
                """(
                    c.title LIKE ? ESCAPE '\\' OR c.content LIKE ? ESCAPE '\\' OR
                    c.category LIKE ? ESCAPE '\\' OR c.location LIKE ? ESCAPE '\\' OR
                    c.notes LIKE ? ESCAPE '\\' OR c.tags LIKE ? ESCAPE '\\'
                )""",
            ]
            collection_params: list[Any] = [
                owner_id,
                owner_id,
                like,
                like,
                like,
                like,
                like,
                like,
            ]
            if filters:
                if filters.get("category"):
                    collection_where.append("(TRIM(i.category) = ? OR TRIM(c.category) = ?)")
                    collection_params.extend([filters["category"], filters["category"]])
                for key in (
                    "ledger_category",
                    "transaction_type",
                    "account_name",
                    "counter_account_name",
                    "merchant",
                    "status",
                    "priority",
                ):
                    if key in filters:
                        collection_where.append("1 = 0")

            cursor.execute(
                f"""
                SELECT i.id
                FROM event_collections c
                JOIN items i ON i.event_collection_id = c.id
                WHERE {" AND ".join(collection_where)}
                ORDER BY pendo_utc_epoch(i.updated_at, ?) DESC,
                         pendo_utc_epoch(i.created_at, ?) DESC
                """,
                [*collection_params, timezone_name, timezone_name],
            )
            collection_ids = [row[0] for row in cursor.fetchall()]

        # 合并去重：FTS结果优先（按rank排序），再补充LIKE独有的结果
        seen = set(fts_ids)
        merged_ids = list(fts_ids)
        for lid in like_ids + collection_ids:
            if lid not in seen:
                merged_ids.append(lid)
                seen.add(lid)

        return merged_ids

    def _load_items_for_search(
        self,
        owner_id: str,
        item_ids: list[str],
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Item]:
        """按 ID 批量读取搜索结果条目。"""
        if not item_ids:
            return {}

        conn = self.get_connection()
        base_where = ["owner_id = ?", "deleted = 0"]
        base_params: list[Any] = [owner_id]
        load_filters = self._resolve_item_filters(owner_id, filters)
        category_filter = None
        if load_filters.get("category") and load_filters.get("type") in (
            None,
            ItemType.EVENT.value,
            "event",
        ):
            category_filter = load_filters.pop("category")
        self._apply_filters(base_where, base_params, load_filters)
        if category_filter is not None:
            base_where.append(
                """(
                    TRIM(category) = ?
                    OR (
                        type = ?
                        AND event_collection_id IN (
                            SELECT id
                            FROM event_collections
                            WHERE owner_id = ? AND deleted = 0 AND TRIM(category) = ?
                        )
                    )
                )"""
            )
            base_params.extend([category_filter, ItemType.EVENT.value, owner_id, category_filter])

        items_by_id: dict[str, Item] = {}
        for offset in range(0, len(item_ids), _SQLITE_ID_BATCH_SIZE):
            batch = item_ids[offset : offset + _SQLITE_ID_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            where = [f"id IN ({placeholders})", *base_where]
            rows = conn.execute(
                f"SELECT * FROM items WHERE {' AND '.join(where)}",
                [*batch, *base_params],
            ).fetchall()
            for row in rows:
                item = self._row_to_item(row)
                if item:
                    items_by_id[item.id] = item
        return items_by_id

    def search_items(
        self,
        owner_id: str,
        query: str,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
    ) -> list[Item]:
        """全文搜索，返回 Item dataclass 列表。"""
        page_size = max(0, int(limit))
        merged_ids = self._search_item_ids(owner_id, query, filters=filters)
        if not merged_ids or page_size <= 0:
            return []

        items_by_id = self._load_items_for_search(owner_id, merged_ids[:page_size], filters=filters)
        return [items_by_id[item_id] for item_id in merged_ids[:limit] if item_id in items_by_id]

    def search_items_page(
        self,
        owner_id: str,
        query: str,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[Item], int]:
        """全文搜索，返回当前页条目与总匹配数。"""
        page_size = max(0, int(limit))
        start = max(0, int(offset))
        merged_ids = self._search_item_ids(owner_id, query, filters=filters)
        total = len(merged_ids)
        if not total or page_size <= 0:
            return [], total

        page_ids = merged_ids[start : start + page_size]
        if not page_ids:
            return [], total

        items_by_id = self._load_items_for_search(owner_id, page_ids, filters=filters)
        return [items_by_id[item_id] for item_id in page_ids if item_id in items_by_id], total

    _UNDO_DELETE_ACTIONS = ("delete_event_collection", "delete_task", "delete_note", "delete")

    def _parse_operation_details(self, raw_details: Any) -> dict[str, Any]:
        if not raw_details:
            return {}
        if isinstance(raw_details, dict):
            return raw_details
        try:
            details = json.loads(raw_details)
        except (TypeError, ValueError):
            return {}
        return details if isinstance(details, dict) else {}

    def _restore_deleted_item_ids(
        self,
        cursor: sqlite3.Cursor,
        conn: sqlite3.Connection,
        owner_id: str,
        item_ids: list[str],
    ) -> tuple[int, list[str]]:
        requested_ids = list(dict.fromkeys(str(item_id) for item_id in item_ids if item_id))
        if not requested_ids:
            return 0, []
        restorable_ids = self._select_owner_item_ids(cursor, owner_id, requested_ids, deleted=True)
        if not restorable_ids:
            return 0, []
        now = utc_now_iso()
        affected = 0
        restored_ids: list[str] = []
        for offset in range(0, len(restorable_ids), _SQLITE_ID_BATCH_SIZE):
            batch = restorable_ids[offset : offset + _SQLITE_ID_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            cursor.execute(
                f"""
                UPDATE items
                SET deleted = 0, deleted_at = NULL, updated_at = ?, version = version + 1
                WHERE owner_id = ? AND deleted = 1 AND id IN ({placeholders})
                """,
                [now, owner_id, *batch],
            )
            affected += cursor.rowcount
            if cursor.rowcount:
                restored_ids.extend(batch)
        for item_id in restored_ids:
            self._refresh_fts(item_id, conn)
        return affected, restored_ids

    def _load_item_for_undo(
        self, cursor: sqlite3.Cursor, owner_id: str, item_id: str | None
    ) -> Item | None:
        if not item_id:
            return None
        row = cursor.execute(
            "SELECT * FROM items WHERE id = ? AND owner_id = ?",
            (item_id, owner_id),
        ).fetchone()
        return self._row_to_item(row) if row else None

    def _latest_delete_log_row(
        self, cursor: sqlite3.Cursor, owner_id: str, threshold: str
    ) -> sqlite3.Row | None:
        placeholders = ",".join("?" for _ in self._UNDO_DELETE_ACTIONS)
        return cast(
            sqlite3.Row | None,
            cursor.execute(
                f"""
                SELECT id, item_id, action, item_type, details, created_at
                FROM operation_logs
                WHERE user_id = ? AND action IN ({placeholders})
                    AND undone_at IS NULL AND created_at >= ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                [owner_id] + list(self._UNDO_DELETE_ACTIONS) + [threshold],
            ).fetchone(),
        )

    def _latest_deleted_item_row(
        self, cursor: sqlite3.Cursor, owner_id: str, threshold: str
    ) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            cursor.execute(
                """
                SELECT * FROM items
                WHERE owner_id = ? AND deleted = 1 AND deleted_at >= ?
                ORDER BY deleted_at DESC
                LIMIT 1
                """,
                (owner_id, threshold),
            ).fetchone(),
        )

    def _mark_operation_logs_undone(
        self,
        cursor: sqlite3.Cursor,
        *,
        owner_id: str,
        log_ids: list[int],
        source_action: str,
        item_type: str | None,
        item_id: str | None,
        restored_item_ids: list[str],
    ) -> int:
        """Append an undo event and link every consumed source log to it."""

        unique_ids = list(dict.fromkeys(int(log_id) for log_id in log_ids if log_id))
        if not unique_ids:
            raise ValueError("undo audit requires at least one source log")
        timestamp = utc_now_iso()
        undo_log_id = self._log_operation_with_cursor(
            cursor,
            user_id=owner_id,
            action=f"undo_{source_action}",
            item_type=item_type,
            item_id=item_id,
            details={
                "source_log_ids": unique_ids,
                "source_action": source_action,
                "restored_item_ids": list(dict.fromkeys(restored_item_ids)),
            },
            created_at=timestamp,
        )
        marked = 0
        for offset in range(0, len(unique_ids), _SQLITE_ID_BATCH_SIZE):
            batch = unique_ids[offset : offset + _SQLITE_ID_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            cursor.execute(
                f"""
                UPDATE operation_logs
                SET undone_at = ?, undo_log_id = ?
                WHERE user_id = ? AND undone_at IS NULL AND id IN ({placeholders})
                """,
                [timestamp, undo_log_id, owner_id, *batch],
            )
            marked += cursor.rowcount
        if marked != len(unique_ids):
            raise RuntimeError("undo source log state changed during restore")
        return undo_log_id

    def _undo_delete_from_log(
        self,
        conn: sqlite3.Connection,
        cursor: sqlite3.Cursor,
        owner_id: str,
        log_row: sqlite3.Row,
    ) -> dict[str, Any]:
        action = str(log_row["action"] or "")
        item_id = str(log_row["item_id"] or "")
        details = self._parse_operation_details(log_row["details"])

        if action == "delete_event_collection":
            child_ids = [str(child_id) for child_id in (details.get("child_ids") or []) if child_id]
            if not child_ids and item_id:
                child_ids = [
                    str(row["id"])
                    for row in cursor.execute(
                        """
                        SELECT id FROM items
                        WHERE owner_id = ? AND event_collection_id = ? AND deleted = 1
                        ORDER BY COALESCE(event_index, 999999), start_time, id
                        """,
                        (owner_id, item_id),
                    ).fetchall()
                ]

            now = utc_now_iso()
            with conn:
                collection_affected = 0
                if item_id:
                    cursor.execute(
                        """
                        UPDATE event_collections
                        SET deleted = 0, deleted_at = NULL, updated_at = ?
                        WHERE id = ? AND owner_id = ? AND deleted = 1
                        """,
                        (now, item_id, owner_id),
                    )
                    collection_affected = cursor.rowcount
                item_affected, restored_ids = self._restore_deleted_item_ids(
                    cursor, conn, owner_id, child_ids
                )
                self._mark_operation_logs_undone(
                    cursor,
                    owner_id=owner_id,
                    log_ids=[int(log_row["id"])],
                    source_action=action,
                    item_type=log_row["item_type"],
                    item_id=item_id,
                    restored_item_ids=restored_ids,
                )

            self.cache_clear()
            if collection_affected or item_affected:
                return {
                    "status": "success",
                    "message": f"已恢复日程集合和 {item_affected} 个日程",
                    "item": None,
                    "affected": collection_affected + item_affected,
                    "item_ids": restored_ids,
                    "collection_id": item_id,
                }
            return {"status": "error", "message": "该删除操作没有可恢复的数据"}

        if action in {"delete_task", "delete_note"}:
            params: list[Any] = [owner_id, action, log_row["created_at"]]
            item_type_clause = ""
            if log_row["item_type"] is not None:
                item_type_clause = " AND item_type = ?"
                params.append(log_row["item_type"])
            batch_rows = cursor.execute(
                f"""
                SELECT id, item_id FROM operation_logs
                WHERE user_id = ? AND action = ? AND created_at = ?
                    AND undone_at IS NULL{item_type_clause}
                ORDER BY id
                """,
                params,
            ).fetchall()
            log_ids = [int(row["id"]) for row in batch_rows]
            item_ids = [str(row["item_id"]) for row in batch_rows if row["item_id"]]
            with conn:
                affected, restored_ids = self._restore_deleted_item_ids(
                    cursor, conn, owner_id, item_ids
                )
                self._mark_operation_logs_undone(
                    cursor,
                    owner_id=owner_id,
                    log_ids=log_ids,
                    source_action=action,
                    item_type=log_row["item_type"],
                    item_id=item_id,
                    restored_item_ids=restored_ids,
                )

            self.cache_clear()
            if affected:
                type_name = "待办" if action == "delete_task" else "笔记"
                return {
                    "status": "success",
                    "message": f"已恢复 {affected} 个{type_name}",
                    "item": None,
                    "affected": affected,
                    "item_ids": restored_ids,
                }
            return {"status": "error", "message": "该删除操作没有可恢复的数据"}

        with conn:
            affected, restored_ids = self._restore_deleted_item_ids(
                cursor, conn, owner_id, [item_id]
            )
            self._mark_operation_logs_undone(
                cursor,
                owner_id=owner_id,
                log_ids=[int(log_row["id"])],
                source_action=action,
                item_type=log_row["item_type"],
                item_id=item_id,
                restored_item_ids=restored_ids,
            )
            item = self._load_item_for_undo(
                cursor, owner_id, restored_ids[0] if restored_ids else None
            )

        self.cache_clear()
        if affected and item:
            return {"status": "success", "message": "已恢复", "item": item, "affected": affected}
        return {"status": "error", "message": "该删除操作没有可恢复的数据"}

    def _undo_delete_from_item_row(
        self,
        conn: sqlite3.Connection,
        cursor: sqlite3.Cursor,
        owner_id: str,
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        item = self._row_to_item(row)
        if not item:
            return {"status": "error", "message": "数据转换失败"}

        with conn:
            affected, _ = self._restore_deleted_item_ids(cursor, conn, owner_id, [item.id])
            restored_item = self._load_item_for_undo(cursor, owner_id, item.id)
            if affected:
                self._log_operation_with_cursor(
                    cursor,
                    user_id=owner_id,
                    action="undo_delete",
                    item_type=item.type,
                    item_id=item.id,
                    details={
                        "source_log_ids": [],
                        "source_action": "unlogged_delete",
                        "restored_item_ids": [item.id],
                    },
                )

        self.cache_clear()
        if affected and restored_item:
            return {
                "status": "success",
                "message": "已恢复",
                "item": restored_item,
                "affected": affected,
            }
        return {"status": "error", "message": "该删除操作没有可恢复的数据"}

    @staticmethod
    def _undo_threshold(minutes: int) -> str:
        if type(minutes) is not int or not 1 <= minutes <= PendoConfig.UNDO_WINDOW_MINUTES:
            raise ValueError(
                f"undo window must be from 1 to {PendoConfig.UNDO_WINDOW_MINUTES} minutes"
            )
        return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()

    def undo_delete(
        self,
        owner_id: str,
        minutes: int = PendoConfig.UNDO_WINDOW_MINUTES,
    ) -> dict[str, Any]:
        """撤销删除，按最近一次删除操作恢复单条、批量条目或日程集合。"""
        conn = self.get_connection()
        cursor = conn.cursor()

        threshold = self._undo_threshold(minutes)
        log_row = self._latest_delete_log_row(cursor, owner_id, threshold)
        item_row = self._latest_deleted_item_row(cursor, owner_id, threshold)

        if not log_row and not item_row:
            return {"status": "error", "message": f"未找到{minutes}分钟内删除的条目"}

        if log_row and (
            not item_row or str(log_row["created_at"] or "") >= str(item_row["deleted_at"] or "")
        ):
            return self._undo_delete_from_log(conn, cursor, owner_id, log_row)

        if item_row is None:
            return {"status": "error", "message": "该删除操作没有可恢复的数据"}
        return self._undo_delete_from_item_row(conn, cursor, owner_id, item_row)

    def undo_edit(
        self,
        owner_id: str,
        minutes: int = PendoConfig.UNDO_WINDOW_MINUTES,
    ) -> dict[str, Any]:
        """撤销编辑操作

        从 operation_logs 中查找最近的 edit_* 操作，
        读取其 old_values 快照并写回数据库。
        """
        conn = self.get_connection()
        cursor = conn.cursor()

        threshold = self._undo_threshold(minutes)
        cursor.execute(
            """
            SELECT id, item_id, action, item_type, details, created_at
            FROM operation_logs
            WHERE user_id = ? AND action LIKE 'edit_%'
                AND undone_at IS NULL AND created_at >= ?
            ORDER BY created_at DESC, id DESC LIMIT 1
        """,
            (owner_id, threshold),
        )
        row = cursor.fetchone()
        if not row:
            return {"status": "error", "message": f"未找到{minutes}分钟内的编辑操作"}

        log_id = row[0]
        item_id = row[1]
        action = row[2]
        details = self._parse_operation_details(row[4])

        old_values = details.get("old_values")
        if not isinstance(old_values, dict) or not old_values:
            return {"status": "error", "message": "该编辑操作没有保存旧值快照，无法撤销"}

        instance_ids = details.get("instance_ids", [item_id])
        if not isinstance(instance_ids, list):
            instance_ids = [item_id]
        requested_ids = list(
            dict.fromkeys(str(instance_id) for instance_id in instance_ids if instance_id)
        )
        restorable_ids = self._select_owner_item_ids(cursor, owner_id, requested_ids, deleted=False)
        if not restorable_ids:
            return {"status": "error", "message": "该编辑操作对应的条目已不存在"}

        affected = self._restore_edit_snapshot(
            conn,
            cursor,
            owner_id,
            restorable_ids,
            old_values,
            int(log_id),
            str(action),
            row[3],
        )

        return {
            "status": "success",
            "item_id": item_id,
            "action": action,
            "affected": affected,
            "instance_count": len(restorable_ids),
        }

    def _restore_edit_snapshot(
        self,
        conn: sqlite3.Connection,
        cursor: sqlite3.Cursor,
        owner_id: str,
        item_ids: list[str],
        old_values: dict[str, Any],
        log_id: int,
        source_action: str,
        item_type: str | None,
    ) -> int:
        """在一个事务内恢复编辑快照并把来源日志标记为已撤销。"""
        restore_values = dict(old_values)
        for field in self._IMMUTABLE_UPDATE_FIELDS:
            restore_values.pop(field, None)
        restore_values["updated_at"] = utc_now_iso()
        current_row = cursor.execute(
            "SELECT type, timezone FROM items WHERE id = ? AND owner_id = ? AND deleted = 0",
            (item_ids[0], owner_id),
        ).fetchone()
        if current_row is None:
            raise ItemNotFoundException(item_ids[0])
        user_timezone = TimezoneHelper.get_user_timezone(owner_id, self)
        normalization_context = {
            "type": str(current_row["type"] or item_type or ""),
            "timezone": str(current_row["timezone"] or user_timezone.key),
            **restore_values,
        }
        normalized_context = normalize_item_datetimes_for_storage(
            normalization_context,
            user_timezone,
        )
        restore_values = {key: normalized_context[key] for key in restore_values}
        restore_data = self._prepare_data(restore_values)
        set_clause = ", ".join(f"{self._quote_col(key)} = ?" for key in restore_data)
        affected = 0

        with conn:
            for offset in range(0, len(item_ids), _SQLITE_ID_BATCH_SIZE):
                batch = item_ids[offset : offset + _SQLITE_ID_BATCH_SIZE]
                placeholders = ",".join("?" for _ in batch)
                cursor.execute(
                    f"UPDATE items SET {set_clause}, version = version + 1 "
                    f"WHERE id IN ({placeholders}) AND owner_id = ? AND deleted = 0",
                    [*restore_data.values(), *batch, owner_id],
                )
                affected += cursor.rowcount
                if "remind_times" in restore_values:
                    for restored_id in batch:
                        self._sync_reminder_logs(
                            cursor,
                            restored_id,
                            cast(list[str] | None, restore_values.get("remind_times")),
                        )
            if self._FTS_FIELDS & restore_data.keys():
                for restored_id in item_ids:
                    self._refresh_fts(restored_id, conn)
            self._mark_operation_logs_undone(
                cursor,
                owner_id=owner_id,
                log_ids=[log_id],
                source_action=source_action,
                item_type=item_type,
                item_id=item_ids[0] if len(item_ids) == 1 else None,
                restored_item_ids=item_ids,
            )

        for restored_id in item_ids:
            self.cache_invalidate(restored_id)
        self.cache_invalidate(f"items|{owner_id}")
        return affected

    def get_latest_undoable_operation(
        self,
        owner_id: str,
        minutes: int = PendoConfig.UNDO_WINDOW_MINUTES,
    ) -> dict[str, Any]:
        """查找最近可撤销的操作（删除 或 编辑）

        比较 deleted_at（删除操作）和 operation_logs.created_at（编辑操作），
        返回最近的那个操作类型和时间。

        Returns:
            {'type': 'delete'|'edit'|None, 'time': ISO时间}
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        threshold = self._undo_threshold(minutes)

        delete_log = self._latest_delete_log_row(cursor, owner_id, threshold)
        delete_log_time = delete_log["created_at"] if delete_log else None

        delete_row = self._latest_deleted_item_row(cursor, owner_id, threshold)
        deleted_item_time = delete_row["deleted_at"] if delete_row else None
        delete_time = max(
            [time_value for time_value in (delete_log_time, deleted_item_time) if time_value],
            default=None,
        )

        # 查找最近的编辑
        cursor.execute(
            """
            SELECT id, created_at FROM operation_logs
            WHERE user_id = ? AND action LIKE 'edit_%'
                AND undone_at IS NULL AND created_at >= ?
            ORDER BY created_at DESC, id DESC LIMIT 1
        """,
            (owner_id, threshold),
        )
        edit_row = cursor.fetchone()
        edit_id = int(edit_row["id"]) if edit_row else None
        edit_time = edit_row["created_at"] if edit_row else None

        if not delete_time and not edit_time:
            return {"type": None}
        if not edit_time:
            return {"type": "delete", "time": delete_time}
        if not delete_time:
            return {"type": "edit", "time": edit_time}
        if edit_time > delete_time:
            return {"type": "edit", "time": edit_time}
        if edit_time < delete_time:
            return {"type": "delete", "time": delete_time}

        # 持久化时间按秒规范化；同一秒内必须用自增日志 ID 恢复真实顺序。
        # 没有对应日志的旧版软删除无法提供序号，此时优先选择可精确排序的编辑日志。
        delete_log_id = (
            int(delete_log["id"])
            if delete_log is not None and delete_log_time == delete_time
            else None
        )
        if delete_log_id is None or (edit_id is not None and edit_id > delete_log_id):
            return {"type": "edit", "time": edit_time}
        return {"type": "delete", "time": delete_time}

    def get_events_for_range(self, user_id: str, start_date: str, end_date: str) -> list[EventItem]:
        """按用户时区获取与日期范围重叠的可调度日程 leaf。"""
        user_timezone = TimezoneHelper.get_user_timezone(user_id, self)
        range_start = TimezoneHelper.parse(start_date, user_timezone)
        range_end = TimezoneHelper.parse(end_date, user_timezone)
        if range_end < range_start:
            raise ValueError("event range end must not precede start")

        # ISO 文本可能混合无偏移和带偏移值，字典序不等于绝对时间。
        # 全球合法偏移最多可相差 26 小时，因此 SQL 用前后各两天的日期前缀
        # 缩小候选集；最终重叠判定仍在 Python 完成。
        lower_date = (range_start - timedelta(days=2)).date().isoformat()
        upper_date = (range_end + timedelta(days=2)).date().isoformat()
        conn = self.get_connection()
        rows = conn.execute(
            f"""
            SELECT * FROM items WHERE owner_id = ? AND type = '{ItemType.EVENT.value}' AND deleted = 0
            AND (event_role IS NULL OR event_role IN ('single', 'multi_node_child', 'recurring_occurrence'))
            AND start_time IS NOT NULL AND substr(start_time, 1, 10) <= ?
            AND (
                (end_time IS NOT NULL AND end_time != '' AND substr(end_time, 1, 10) >= ?)
                OR ((end_time IS NULL OR end_time = '') AND substr(start_time, 1, 10) >= ?)
            )
            """,
            (user_id, upper_date, lower_date, lower_date),
        ).fetchall()

        matches: list[tuple[datetime, EventItem]] = []
        for row in rows:
            item = self._row_to_item(row)
            if not isinstance(item, EventItem) or not item.start_time:
                continue
            try:
                event_start = TimezoneHelper.parse(item.start_time, user_timezone)
                event_end = (
                    TimezoneHelper.parse(item.end_time, user_timezone)
                    if item.end_time
                    else event_start
                )
            except (TypeError, ValueError):
                continue
            if event_start <= range_end and event_end >= range_start:
                matches.append((event_start, item))
        matches.sort(key=lambda pair: pair[0])
        return [item for _start, item in matches]

    def get_briefing_items(
        self, user_id: str, today_iso: str, tomorrow_iso: str
    ) -> tuple[list[EventItem], list[TaskItem], list[TaskItem]]:
        """获取每日简报条目

        Args:
            user_id: 用户ID
            today_iso: 今日开始时间ISO格式
            tomorrow_iso: 明日开始时间ISO格式

        Returns:
            (events, tasks, overdue_tasks) 元组
        """
        user_timezone = TimezoneHelper.get_user_timezone(user_id, self)
        today_start = TimezoneHelper.parse(today_iso, user_timezone)
        tomorrow_start = TimezoneHelper.parse(tomorrow_iso, user_timezone)
        if tomorrow_start <= today_start:
            raise ValueError("briefing end must be after start")

        # 与 get_events_for_range 共用“日期前缀粗筛 + Python 真实时刻判定”。
        events = self.get_events_for_range(
            user_id,
            today_start.isoformat(),
            (tomorrow_start - timedelta(microseconds=1)).isoformat(),
        )

        today_date = today_start.date().isoformat()
        lower_date = (today_start - timedelta(days=2)).date().isoformat()
        upper_date = (tomorrow_start + timedelta(days=2)).date().isoformat()
        cursor = self.get_connection().cursor()
        rows = cursor.execute(
            f"""
            SELECT * FROM items
            WHERE owner_id = ? AND type = '{ItemType.TASK.value}' AND deleted = 0
              AND status = 'open'
              AND (
                plan_date = ?
                OR (
                  deadline_at IS NOT NULL AND deadline_at != ''
                  AND substr(deadline_at, 1, 10) BETWEEN ? AND ?
                )
              )
            """,
            (user_id, today_date, lower_date, upper_date),
        ).fetchall()

        task_matches: list[tuple[int, datetime, str, TaskItem]] = []
        for row in rows:
            item = self._row_to_item(row)
            if not isinstance(item, TaskItem):
                continue
            deadline: datetime | None = None
            if item.deadline_at:
                try:
                    deadline = TimezoneHelper.parse(item.deadline_at, user_timezone)
                except (TypeError, ValueError):
                    pass
            if item.plan_date != today_date and not (
                deadline is not None and today_start <= deadline < tomorrow_start
            ):
                continue
            priority = item.priority if isinstance(item.priority, int) else 3
            sort_time = deadline or tomorrow_start
            task_matches.append((priority, sort_time, item.created_at, item))
        task_matches.sort(key=lambda match: match[:3])
        tasks = [match[3] for match in task_matches[:10]]

        # 截止时间也按真实时刻比较；前缀只负责排除肯定在未来的候选。
        overdue_rows = cursor.execute(
            f"""
            SELECT * FROM items
            WHERE owner_id = ? AND type = '{ItemType.TASK.value}' AND deleted = 0
              AND status = 'open' AND deadline_at IS NOT NULL AND deadline_at != ''
              AND substr(deadline_at, 1, 10) <= ?
            """,
            (user_id, upper_date),
        ).fetchall()
        overdue_matches: list[tuple[datetime, TaskItem]] = []
        for row in overdue_rows:
            item = self._row_to_item(row)
            if not isinstance(item, TaskItem) or not item.deadline_at:
                continue
            try:
                deadline = TimezoneHelper.parse(item.deadline_at, user_timezone)
            except (TypeError, ValueError):
                continue
            if deadline < today_start:
                overdue_matches.append((deadline, item))
        overdue_matches.sort(key=lambda match: match[0])
        overdue_tasks = [match[1] for match in overdue_matches[:10]]

        return events, tasks, overdue_tasks

    def has_diary_for_date(self, user_id: str, diary_date: str) -> bool:
        """检查指定日期是否已有日记

        Args:
            user_id: 用户ID
            diary_date: 日期字符串(YYYY-MM-DD)

        Returns:
            是否存在日记
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT COUNT(*) FROM items WHERE owner_id = ? AND type = '{ItemType.DIARY.value}'
            AND deleted = 0 AND diary_date = ?
        """,
            (user_id, diary_date),
        )
        row = cursor.fetchone()
        return row is not None and int(row[0]) > 0

    def query_items_by_date_range(
        self, user_id: str, item_type: str, date_field: str, start_date: str, end_date: str
    ) -> list[Item]:
        """按一个有界日期窗口直接查询，避免经分页器装载额外历史。"""
        if date_field not in self.ALLOWED_DATE_FIELDS:
            raise ValueError(f"Invalid date field: {date_field}")
        filters = self._resolve_item_filters(
            user_id,
            {
                "type": item_type,
                "date_field": date_field,
                "start_date": start_date,
                "end_date": end_date,
                "sort_field": date_field,
                "sort_order": "DESC",
            },
        )
        where, params = self._item_list_conditions(user_id, filters)
        sort_field, sort_order, sort_params = self._resolve_item_sort(filters)
        rows = self.get_connection().execute(
            f"SELECT * FROM items WHERE {' AND '.join(where)} ORDER BY {sort_field} {sort_order}",
            params + sort_params,
        )
        return [item for row in rows.fetchall() if (item := self._row_to_item(row)) is not None]

    # ==================== 设置操作 ====================

    def get_user_settings(self, user_id: str) -> dict[str, Any]:
        """获取用户设置"""
        cache_key = self._cache_key("settings", user_id)
        cached = self._cache_get_or_miss(cache_key)
        if cached is not _CACHE_MISS:
            return cast(dict[str, Any], cached)

        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()

        if row:
            settings = self._hydrate_user_settings_row(dict(row))
            self._cache_set(cache_key, settings)
            return settings

        return self._default_user_settings(user_id)

    def get_user_settings_batch(self, user_ids: list[str]) -> dict[str, dict[str, Any]]:
        """分批读取用户设置，并为没有持久化记录的用户补齐默认值。"""
        unique_user_ids = list(dict.fromkeys(str(user_id) for user_id in user_ids if user_id))
        if not unique_user_ids:
            return {}

        results: dict[str, dict[str, Any]] = {}
        missing_user_ids: list[str] = []
        for user_id in unique_user_ids:
            cached = self._cache_get_or_miss(self._cache_key("settings", user_id))
            if cached is not _CACHE_MISS:
                results[user_id] = cast(dict[str, Any], cached)
            else:
                missing_user_ids.append(user_id)

        if missing_user_ids:
            conn = self.get_connection()
            for offset in range(0, len(missing_user_ids), _SQLITE_ID_BATCH_SIZE):
                batch = missing_user_ids[offset : offset + _SQLITE_ID_BATCH_SIZE]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT * FROM user_settings WHERE user_id IN ({placeholders})",
                    batch,
                ).fetchall()
                for row in rows:
                    settings = self._hydrate_user_settings_row(dict(row))
                    user_id = str(settings["user_id"])
                    self._cache_set(self._cache_key("settings", user_id), settings)
                    results[user_id] = settings

        for user_id in unique_user_ids:
            results.setdefault(user_id, self._default_user_settings(user_id))

        return results

    def _default_user_settings(self, user_id: str) -> dict[str, Any]:
        return {
            "user_id": user_id,
            "timezone": PendoConfig.DEFAULT_TIMEZONE,
            "quiet_hours_start": PendoConfig.DEFAULT_QUIET_HOURS_START,
            "quiet_hours_end": PendoConfig.DEFAULT_QUIET_HOURS_END,
            "daily_report_time": PendoConfig.DEFAULT_DAILY_REPORT_TIME,
            "diary_remind_time": PendoConfig.DEFAULT_DIARY_REMIND_TIME,
            "default_category": PendoConfig.DEFAULT_CATEGORY,
            "settings_json": normalize_settings_json({}),
        }

    def _hydrate_user_settings_row(self, settings: dict[str, Any]) -> dict[str, Any]:
        raw_settings = settings.get("settings_json")
        if isinstance(raw_settings, dict):
            decoded_settings = raw_settings
        elif raw_settings:
            try:
                decoded = json.loads(raw_settings)
            except (TypeError, json.JSONDecodeError, ValueError):
                decoded_settings = {}
            else:
                decoded_settings = decoded if isinstance(decoded, dict) else {}
        else:
            decoded_settings = {}
        settings["settings_json"] = normalize_settings_json(decoded_settings)
        return settings

    def update_user_settings(self, user_id: str, settings: dict[str, Any]) -> bool:
        # 进程内锁覆盖 SQLite 取得写锁前的短窗口；BEGIN IMMEDIATE 同时串行化
        # 其他进程的设置更新，防止两个 JSON patch 互相覆盖。
        with self._settings_lock, self.transaction(immediate=True) as conn:
            cursor = conn.cursor()
            row = cursor.execute(
                "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
            ).fetchone()
            current = (
                self._hydrate_user_settings_row(dict(row))
                if row
                else self._default_user_settings(user_id)
            )
            merged = {**current, **settings}
            timezone_changed = "timezone" in settings and merged.get("timezone") != current.get(
                "timezone"
            )
            custom_patch = normalize_settings_json(settings.get("settings_json", {}), partial=True)
            cursor.execute(
                """
                INSERT INTO user_settings
                (user_id, timezone, quiet_hours_start, quiet_hours_end, daily_report_time,
                 diary_remind_time, default_category, settings_json, updated_at, version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(user_id) DO UPDATE SET
                    timezone = excluded.timezone,
                    quiet_hours_start = excluded.quiet_hours_start,
                    quiet_hours_end = excluded.quiet_hours_end,
                    daily_report_time = excluded.daily_report_time,
                    diary_remind_time = excluded.diary_remind_time,
                    default_category = excluded.default_category,
                    settings_json = json_patch(COALESCE(user_settings.settings_json, '{}'), excluded.settings_json),
                    updated_at = excluded.updated_at,
                    version = user_settings.version + 1
                """,
                (
                    user_id,
                    merged.get("timezone", PendoConfig.DEFAULT_TIMEZONE),
                    merged.get("quiet_hours_start", PendoConfig.DEFAULT_QUIET_HOURS_START),
                    merged.get("quiet_hours_end", PendoConfig.DEFAULT_QUIET_HOURS_END),
                    merged.get("daily_report_time", PendoConfig.DEFAULT_DAILY_REPORT_TIME),
                    merged.get("diary_remind_time", PendoConfig.DEFAULT_DIARY_REMIND_TIME),
                    merged.get("default_category", PendoConfig.DEFAULT_CATEGORY),
                    json.dumps(custom_patch, ensure_ascii=False),
                    utc_now_iso(),
                ),
            )
            if timezone_changed:
                reminder_rows = cursor.execute(
                    """
                    SELECT id, remind_times
                    FROM items
                    WHERE owner_id = ? AND deleted = 0 AND type IN ('event', 'task')
                      AND remind_times IS NOT NULL AND remind_times != '[]'
                    """,
                    (user_id,),
                ).fetchall()
                timezone_name = str(merged.get("timezone") or PendoConfig.DEFAULT_TIMEZONE)
                for reminder_row in reminder_rows:
                    remind_times = self._decode_json_field(
                        "remind_times", reminder_row["remind_times"]
                    )
                    self._sync_reminder_logs(
                        cursor,
                        str(reminder_row["id"]),
                        remind_times,
                        timezone_name=timezone_name,
                    )

        self.cache_invalidate(self._cache_key("settings", user_id))
        return True

    # ==================== 日志操作 ====================

    def log_operation(
        self,
        user_id: str,
        action: str,
        item_type: str | None = None,
        item_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> bool:
        """记录操作日志"""
        conn = self.get_connection()
        cursor = conn.cursor()
        with conn:
            self._log_operation_with_cursor(
                cursor,
                user_id=user_id,
                action=action,
                item_type=item_type,
                item_id=item_id,
                details=details,
            )
        return True

    def prune_operation_logs(
        self,
        *,
        now: datetime | None = None,
        retention_days: int | None = None,
        undo_snapshot_minutes: int | None = None,
    ) -> dict[str, int]:
        """按保留期删除日志，并在撤销窗口结束后擦除正文类快照。"""

        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("operation-log prune time must be timezone-aware")
        retention_days = (
            retention_days
            if retention_days is not None
            else PendoConfig.LOG_OPERATION_RETENTION_DAYS
        )
        undo_snapshot_minutes = (
            undo_snapshot_minutes
            if undo_snapshot_minutes is not None
            else PendoConfig.UNDO_WINDOW_MINUTES
        )
        if retention_days < 0 or undo_snapshot_minutes < 0:
            raise ValueError("operation-log retention windows must be non-negative")
        delete_before = (
            (current - timedelta(days=retention_days)).astimezone(timezone.utc).isoformat()
        )
        redact_before = (
            (current - timedelta(minutes=undo_snapshot_minutes))
            .astimezone(timezone.utc)
            .isoformat()
        )
        redacted = 0
        with self.transaction(immediate=True) as conn:
            cursor = conn.cursor()
            rows = cursor.execute(
                "SELECT id, details FROM operation_logs WHERE created_at < ? AND details IS NOT NULL",
                (redact_before,),
            ).fetchall()
            for row in rows:
                try:
                    details = json.loads(row["details"] or "{}")
                except (TypeError, json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(details, dict):
                    continue
                removed = False
                for sensitive_key in ("item_data", "old_values", "updates"):
                    if sensitive_key in details:
                        details.pop(sensitive_key, None)
                        removed = True
                if removed:
                    details["snapshot_redacted"] = True
                    cursor.execute(
                        "UPDATE operation_logs SET details = ? WHERE id = ?",
                        (json.dumps(details, ensure_ascii=False), row["id"]),
                    )
                    redacted += 1
            cursor.execute("DELETE FROM operation_logs WHERE created_at < ?", (delete_before,))
            deleted = cursor.rowcount
        return {"deleted": deleted, "redacted": redacted}

    # ==================== 数据迁移审计 ====================

    def log_transfer(
        self,
        owner_id: str,
        action: str,
        bundle_id: str | None = None,
        filename: str | None = None,
        types: list[str] | None = None,
        record_count: int = 0,
        result_summary: dict[str, Any] | None = None,
    ) -> int:
        """记录数据迁移操作日志，返回日志 ID"""
        conn = self.get_connection()
        cursor = conn.cursor()
        with conn:
            return self._log_transfer_with_cursor(
                cursor,
                owner_id=owner_id,
                action=action,
                bundle_id=bundle_id,
                filename=filename,
                types=types,
                record_count=record_count,
                result_summary=result_summary,
            )

    def get_transfer_logs(
        self, owner_id: str, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        """查询迁移审计日志"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT *
            FROM transfer_logs
            WHERE owner_id = ?
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            (owner_id, limit, offset),
        )
        rows = cursor.fetchall()
        results = []
        for row in rows:
            entry = dict(row)
            for field in ("types", "result_summary"):
                if entry.get(field) and isinstance(entry[field], str):
                    try:
                        entry[field] = json.loads(entry[field])
                    except (json.JSONDecodeError, TypeError):
                        pass
            results.append(entry)
        return results

    def has_imported_bundle(self, owner_id: str, bundle_id: str) -> bool:
        """检查某个 bundle_id 是否已被成功导入过"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM imported_bundles WHERE owner_id = ? AND bundle_id = ?",
            (owner_id, bundle_id),
        )
        if cursor.fetchone() is not None:
            return True
        cursor.execute(
            "SELECT 1 FROM transfer_logs WHERE owner_id = ? AND bundle_id = ? AND action = 'import'",
            (owner_id, bundle_id),
        )
        return cursor.fetchone() is not None

    # ==================== 提醒相关 ====================

    @staticmethod
    def _as_utc(value: datetime, field_name: str) -> datetime:
        """要求显式时区并转换到 UTC，杜绝本机时区参与租约计算。"""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _scheduled_identity(
        task_name: str,
        owner_id: str,
        period_key: str,
    ) -> tuple[str, str, str]:
        """规范调度投递的三段身份，确保领取、完成和查询使用同一键。"""
        identity = tuple(str(value).strip() for value in (task_name, owner_id, period_key))
        if not all(identity):
            raise ValueError("scheduled delivery identity fields must be non-empty")
        return cast(tuple[str, str, str], identity)

    def log_reminder(self, item_id: str, remind_time: str, sent: bool = True) -> None:
        """记录提醒发送（UPSERT：首次 INSERT，重复发送 UPDATE repeat_count + last_sent_at）"""
        require_canonical_utc_timestamp(remind_time, "remind_time")
        conn = self.get_connection()
        cursor = conn.cursor()
        now = utc_now_iso() if sent else None
        with conn:
            cursor.execute(
                """
                INSERT INTO reminder_logs (item_id, remind_time, sent_at, last_sent_at, repeat_count, state)
                VALUES (?, ?, ?, ?, 1, CASE WHEN ? IS NULL THEN 'pending' ELSE 'sent' END)
                ON CONFLICT(item_id, remind_time) DO UPDATE SET
                    sent_at = COALESCE(reminder_logs.sent_at, excluded.sent_at),
                    repeat_count = repeat_count + 1,
                    last_sent_at = excluded.sent_at,
                    state = 'sent', claim_token = NULL, claim_expires_at = NULL
                WHERE excluded.sent_at IS NOT NULL
                """,
                (item_id, remind_time, now, now, now),
            )

    def claim_reminder(
        self,
        item_id: str,
        remind_time: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = PendoConfig.REMINDER_CLAIM_LEASE_SECONDS,
    ) -> str | None:
        """原子领取一个尚未发送的提醒。"""
        if lease_seconds <= 0:
            raise ValueError("reminder claim lease must be positive")
        require_canonical_utc_timestamp(remind_time, "remind_time")
        current = self._as_utc(now or datetime.now(timezone.utc), "reminder claim time")
        token = uuid.uuid4().hex
        now_text = current.isoformat(timespec="seconds")
        lease_text = (current + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        conn = self.get_connection()
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO reminder_logs
                    (item_id, remind_time, state, claim_token, claim_expires_at, repeat_count, failure_count)
                VALUES (?, ?, 'claimed', ?, ?, 0, 0)
                ON CONFLICT(item_id, remind_time) DO UPDATE SET
                    state = 'claimed', claim_token = excluded.claim_token,
                    claim_expires_at = excluded.claim_expires_at
                WHERE reminder_logs.confirmed_at IS NULL
                  AND reminder_logs.sent_at IS NULL
                  AND (reminder_logs.next_attempt_at IS NULL OR reminder_logs.next_attempt_at <= ?)
                  AND (reminder_logs.state != 'claimed'
                       OR reminder_logs.claim_expires_at IS NULL
                       OR reminder_logs.claim_expires_at <= ?)
                """,
                (item_id, remind_time, token, lease_text, now_text, now_text),
            )
        return token if cursor.rowcount > 0 else None

    @staticmethod
    def scheduled_delivery_key(task_name: str, owner_id: str, period_key: str) -> str:
        """为一次逻辑投递生成稳定且不暴露明文身份的幂等键。"""
        normalized = Database._scheduled_identity(task_name, owner_id, period_key)
        identity = "\0".join(normalized).encode("utf-8")
        return f"pendo-{hashlib.sha256(identity).hexdigest()}"

    def claim_scheduled_delivery(
        self,
        task_name: str,
        owner_id: str,
        period_key: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = PendoConfig.REMINDER_CLAIM_LEASE_SECONDS,
    ) -> dict[str, str] | None:
        """原子创建或领取一个持久化调度投递记录。"""
        if lease_seconds <= 0:
            raise ValueError("scheduled delivery lease must be positive")
        task, owner, period = self._scheduled_identity(task_name, owner_id, period_key)
        current = self._as_utc(now or datetime.now(timezone.utc), "scheduled delivery claim time")
        now_text = current.isoformat(timespec="seconds")
        token = uuid.uuid4().hex
        lease_text = (current + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        delivery_key = self.scheduled_delivery_key(task, owner, period)
        conn = self.get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO scheduled_delivery_outbox
                    (task_name, owner_id, period_key, delivery_key, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(task_name, owner_id, period_key) DO NOTHING
                """,
                (task, owner, period, delivery_key, now_text, now_text),
            )
            cursor = conn.execute(
                """
                UPDATE scheduled_delivery_outbox
                SET state = 'leased', claim_token = ?, claim_expires_at = ?, updated_at = ?
                WHERE task_name = ? AND owner_id = ? AND period_key = ?
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                  AND (
                    state IN ('pending', 'failed')
                    OR (state = 'leased' AND (claim_expires_at IS NULL OR claim_expires_at <= ?))
                  )
                """,
                (token, lease_text, now_text, task, owner, period, now_text, now_text),
            )
        if cursor.rowcount != 1:
            return None
        return {"claim_token": token, "delivery_key": delivery_key}

    def complete_scheduled_delivery(
        self,
        task_name: str,
        owner_id: str,
        period_key: str,
        claim_token: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """仅在调用方仍持有租约时把调度投递标为已发送。"""
        task, owner, period = self._scheduled_identity(task_name, owner_id, period_key)
        current = self._as_utc(
            now or datetime.now(timezone.utc), "scheduled delivery completion time"
        ).isoformat(timespec="seconds")
        conn = self.get_connection()
        with conn:
            cursor = conn.execute(
                """
                UPDATE scheduled_delivery_outbox
                SET state = 'sent', sent_at = COALESCE(sent_at, ?), updated_at = ?,
                    claim_token = NULL, claim_expires_at = NULL, next_attempt_at = NULL
                WHERE task_name = ? AND owner_id = ? AND period_key = ?
                  AND state = 'leased' AND claim_token = ?
                """,
                (current, current, task, owner, period, claim_token),
            )
        return cursor.rowcount == 1

    def release_scheduled_delivery(
        self,
        task_name: str,
        owner_id: str,
        period_key: str,
        claim_token: str,
        *,
        retry_at: datetime | None = None,
        now: datetime | None = None,
    ) -> bool:
        """释放失败租约，使其他 worker 可在指定时间后重试。"""
        task, owner, period = self._scheduled_identity(task_name, owner_id, period_key)
        current = self._as_utc(
            now or datetime.now(timezone.utc), "scheduled delivery release time"
        ).isoformat(timespec="seconds")
        retry_text = (
            self._as_utc(retry_at, "scheduled delivery retry time").isoformat(timespec="seconds")
            if retry_at
            else None
        )
        conn = self.get_connection()
        with conn:
            cursor = conn.execute(
                """
                UPDATE scheduled_delivery_outbox
                SET state = 'failed', claim_token = NULL, claim_expires_at = NULL,
                    next_attempt_at = ?, failure_count = failure_count + 1, updated_at = ?
                WHERE task_name = ? AND owner_id = ? AND period_key = ?
                  AND state = 'leased' AND claim_token = ?
                """,
                (retry_text, current, task, owner, period, claim_token),
            )
        return cursor.rowcount == 1

    def get_scheduled_delivery(
        self,
        task_name: str,
        owner_id: str,
        period_key: str,
    ) -> dict[str, Any] | None:
        task, owner, period = self._scheduled_identity(task_name, owner_id, period_key)
        row = (
            self.get_connection()
            .execute(
                """
            SELECT * FROM scheduled_delivery_outbox
            WHERE task_name = ? AND owner_id = ? AND period_key = ?
            """,
                (task, owner, period),
            )
            .fetchone()
        )
        return dict(row) if row is not None else None

    def claim_reminder_repeat(
        self,
        item_id: str,
        remind_time: str,
        expected_repeat_count: int,
        *,
        now: datetime | None = None,
        lease_seconds: int = PendoConfig.REMINDER_CLAIM_LEASE_SECONDS,
    ) -> str | None:
        """原子领取一个已发送提醒的下一次重复投递。"""
        if expected_repeat_count < 1:
            raise ValueError("expected repeat count must be positive")
        if lease_seconds <= 0:
            raise ValueError("reminder repeat lease must be positive")
        require_canonical_utc_timestamp(remind_time, "remind_time")
        current = self._as_utc(now or datetime.now(timezone.utc), "reminder repeat claim time")
        token = uuid.uuid4().hex
        now_text = current.isoformat(timespec="seconds")
        lease_text = (current + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        conn = self.get_connection()
        with conn:
            cursor = conn.execute(
                """
                UPDATE reminder_logs
                SET state = 'claimed', claim_token = ?, claim_expires_at = ?
                WHERE item_id = ? AND remind_time = ? AND confirmed_at IS NULL
                  AND sent_at IS NOT NULL AND repeat_count = ?
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                  AND (
                    state = 'sent'
                    OR (state = 'claimed' AND (claim_expires_at IS NULL OR claim_expires_at <= ?))
                  )
                """,
                (
                    token,
                    lease_text,
                    item_id,
                    remind_time,
                    expected_repeat_count,
                    now_text,
                    now_text,
                ),
            )
        return token if cursor.rowcount == 1 else None

    def complete_reminder_repeat(
        self,
        item_id: str,
        remind_time: str,
        claim_token: str,
        expected_repeat_count: int,
    ) -> bool:
        require_canonical_utc_timestamp(remind_time, "remind_time")
        now = utc_now_iso()
        conn = self.get_connection()
        with conn:
            cursor = conn.execute(
                """
                UPDATE reminder_logs
                SET state = 'sent', repeat_count = repeat_count + 1, last_sent_at = ?,
                    claim_token = NULL, claim_expires_at = NULL, next_attempt_at = NULL
                WHERE item_id = ? AND remind_time = ? AND state = 'claimed'
                  AND claim_token = ? AND repeat_count = ? AND confirmed_at IS NULL
                """,
                (now, item_id, remind_time, claim_token, expected_repeat_count),
            )
        return cursor.rowcount == 1

    def release_reminder_repeat(
        self,
        item_id: str,
        remind_time: str,
        claim_token: str,
        expected_repeat_count: int,
        *,
        retry_at: datetime | None = None,
    ) -> bool:
        require_canonical_utc_timestamp(remind_time, "remind_time")
        retry_text = (
            self._as_utc(retry_at, "reminder retry time").isoformat(timespec="seconds")
            if retry_at
            else None
        )
        conn = self.get_connection()
        with conn:
            cursor = conn.execute(
                """
                UPDATE reminder_logs
                SET state = 'sent', claim_token = NULL, claim_expires_at = NULL,
                    next_attempt_at = ?, failure_count = failure_count + 1
                WHERE item_id = ? AND remind_time = ? AND state = 'claimed'
                  AND claim_token = ? AND repeat_count = ? AND confirmed_at IS NULL
                """,
                (retry_text, item_id, remind_time, claim_token, expected_repeat_count),
            )
        return cursor.rowcount == 1

    def complete_reminder_claim(self, item_id: str, remind_time: str, claim_token: str) -> bool:
        """仅在当前 worker 仍持有租约时持久化首次投递。"""
        require_canonical_utc_timestamp(remind_time, "remind_time")
        now = utc_now_iso()
        conn = self.get_connection()
        with conn:
            cursor = conn.execute(
                """
                UPDATE reminder_logs
                SET state = 'sent', sent_at = COALESCE(sent_at, ?),
                    last_sent_at = ?, repeat_count = MAX(repeat_count, 1),
                    claim_token = NULL, claim_expires_at = NULL, next_attempt_at = NULL
                WHERE item_id = ? AND remind_time = ? AND state = 'claimed' AND claim_token = ?
                """,
                (now, now, item_id, remind_time, claim_token),
            )
        return cursor.rowcount == 1

    def release_reminder_claim(
        self,
        item_id: str,
        remind_time: str,
        claim_token: str,
        *,
        retry_at: datetime | None = None,
    ) -> bool:
        """在暂时失败或静默延迟后释放首次提醒租约。"""
        require_canonical_utc_timestamp(remind_time, "remind_time")
        retry_text = (
            self._as_utc(retry_at, "reminder retry time").isoformat(timespec="seconds")
            if retry_at
            else None
        )
        conn = self.get_connection()
        with conn:
            cursor = conn.execute(
                """
                UPDATE reminder_logs
                SET state = 'pending', claim_token = NULL, claim_expires_at = NULL,
                    next_attempt_at = ?, failure_count = failure_count + 1
                WHERE item_id = ? AND remind_time = ? AND state = 'claimed' AND claim_token = ?
                """,
                (retry_text, item_id, remind_time, claim_token),
            )
        return cursor.rowcount == 1

    def confirm_reminder(
        self,
        item_id: str,
        user_action: str = "confirmed",
        owner_id: str | None = None,
        remind_time: str | None = None,
        allow_future: bool = False,
    ) -> dict[str, Any]:
        """确认提醒——直接 UPDATE 该 item 所有未确认行"""
        if remind_time is not None:
            require_canonical_utc_timestamp(remind_time, "remind_time")
        conn = self.get_connection()
        cursor = conn.cursor()
        now = utc_now_iso()
        with conn:
            # 构建 UPDATE 条件
            where_clauses = ["rl.item_id = ?", "rl.confirmed_at IS NULL"]
            params: list[Any] = [item_id]
            if owner_id is not None:
                where_clauses.append("i.owner_id = ?")
                params.append(owner_id)
            if remind_time is not None:
                where_clauses.append("rl.remind_time = ?")
                params.append(remind_time)

            # 查找并确认
            cursor.execute(
                f"""
                SELECT rl.id FROM reminder_logs rl
                JOIN items i ON i.id = rl.item_id AND i.deleted = 0
                WHERE {" AND ".join(where_clauses)}
                """,
                params,
            )
            row_ids = [r["id"] for r in cursor.fetchall()]

            if row_ids:
                placeholders = ",".join(["?" for _ in row_ids])
                cursor.execute(
                    f"""UPDATE reminder_logs
                        SET confirmed_at = ?, user_action = ?, state = 'confirmed',
                            claim_token = NULL, claim_expires_at = NULL, next_attempt_at = NULL
                        WHERE id IN ({placeholders})""",
                    [now, user_action] + row_ids,
                )
            else:
                # 无已发送记录可确认（如静默时间未发出），补插一条已确认的记录
                self._insert_confirm_for_unsent(
                    cursor,
                    item_id,
                    owner_id,
                    remind_time,
                    now,
                    user_action,
                    allow_future=allow_future,
                )

        return {"status": "success", "message": f"已记录: {user_action}"}

    def _insert_confirm_for_unsent(
        self,
        cursor: sqlite3.Cursor,
        item_id: str,
        owner_id: str | None,
        remind_time: str | None,
        now: str,
        user_action: str,
        *,
        allow_future: bool = False,
    ) -> None:
        """为未发送但用户手动确认的提醒补插一条记录"""
        item_where = ["id = ?", "deleted = 0"]
        item_params: list[Any] = [item_id]
        if owner_id is not None:
            item_where.append("owner_id = ?")
            item_params.append(owner_id)
        cursor.execute(
            f"SELECT owner_id, type, timezone, remind_times "
            f"FROM items WHERE {' AND '.join(item_where)}",
            item_params,
        )
        row = cursor.fetchone()
        if not row or not row["remind_times"]:
            return
        try:
            remind_times = json.loads(row["remind_times"])
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        if not isinstance(remind_times, list):
            return

        user_id = str(row["owner_id"])
        user_timezone = TimezoneHelper.get_user_timezone(user_id, self)
        timezone_info = user_timezone
        if str(row["type"] or "") == ItemType.EVENT.value and row["timezone"]:
            try:
                timezone_info = ZoneInfo(str(row["timezone"]))
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError("Invalid event timezone") from exc
        now_dt = datetime.fromisoformat(now)
        target_time = self._latest_confirmable_time(
            remind_times,
            requested_time=remind_time,
            allow_future=allow_future,
            now=now_dt,
            timezone_info=timezone_info,
        )
        if target_time is None:
            return
        target_time = normalize_datetime_for_storage(
            target_time,
            "remind_time",
            timezone_info,
        )
        require_canonical_utc_timestamp(target_time, "remind_time")
        # 已有待发送行和没有日志的提醒统一转为 confirmed。
        cursor.execute(
            """
            INSERT INTO reminder_logs
                (item_id, remind_time, sent_at, confirmed_at, user_action, repeat_count,
                 last_sent_at, state)
            VALUES (?, ?, NULL, ?, ?, 0, NULL, 'confirmed')
            ON CONFLICT(item_id, remind_time) DO UPDATE SET
                confirmed_at = excluded.confirmed_at,
                user_action = excluded.user_action,
                state = 'confirmed',
                claim_token = NULL,
                claim_expires_at = NULL,
                next_attempt_at = NULL
            """,
            (item_id, target_time, now, user_action),
        )

    @staticmethod
    def _latest_confirmable_time(
        remind_times: list[Any],
        *,
        requested_time: str | None,
        allow_future: bool,
        now: datetime,
        timezone_info: ZoneInfo,
    ) -> str | None:
        """选择最近一个已到期提醒；显式授权时也可选择指定未来提醒。"""
        candidates: list[tuple[datetime, str]] = []
        for raw_time in remind_times:
            if not isinstance(raw_time, str):
                continue
            if requested_time is not None and raw_time != requested_time:
                continue
            try:
                parsed_time = TimezoneHelper.parse(raw_time, timezone_info)
            except (TypeError, ValueError):
                continue
            if parsed_time <= now or (allow_future and raw_time == requested_time):
                candidates.append((parsed_time, raw_time))
        if not candidates:
            return None
        return max(candidates, key=lambda candidate: candidate[0])[1]

    def get_reminder_logs(self, item_id: str) -> list[dict[str, Any]]:
        """获取某个条目的所有提醒日志"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT remind_time, sent_at, confirmed_at, user_action, repeat_count, last_sent_at,
                   failure_count, next_attempt_at
            FROM reminder_logs WHERE item_id = ?
            ORDER BY remind_time
            """,
            (item_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_reminder_logs_by_item_ids(
        self,
        owner_id: str,
        item_ids: list[str],
    ) -> dict[str, dict[str, dict[str, Any]]]:
        """按 ``item_id -> remind_time -> log`` 分批读取用户范围的提醒日志。"""
        unique_ids = list(dict.fromkeys(str(value) for value in item_ids if value))
        log_maps: dict[str, dict[str, dict[str, Any]]] = {item_id: {} for item_id in unique_ids}
        conn = self.get_connection()
        for offset in range(0, len(unique_ids), _SQLITE_ID_BATCH_SIZE):
            batch = unique_ids[offset : offset + _SQLITE_ID_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"""
                SELECT rl.item_id, rl.remind_time, rl.sent_at, rl.confirmed_at,
                       rl.user_action, rl.repeat_count, rl.last_sent_at,
                       rl.failure_count, rl.next_attempt_at
                FROM reminder_logs AS rl
                JOIN items AS i ON i.id = rl.item_id
                WHERE i.owner_id = ? AND i.deleted = 0
                  AND rl.item_id IN ({placeholders})
                ORDER BY rl.item_id, rl.remind_time
                """,
                [owner_id, *batch],
            ).fetchall()
            for row in rows:
                log = dict(row)
                item_id = str(log.pop("item_id"))
                log_maps[item_id][str(log["remind_time"])] = log
        return log_maps

    def get_due_reminder_items(self, *, now: datetime | None = None) -> list[Item]:
        """通过物化 UTC 队列读取当前到期且仍可投递的提醒条目。"""

        current = self._as_utc(now or datetime.now(timezone.utc), "reminder queue time")
        now_text = current.isoformat(timespec="seconds")
        initial_cutoff = (
            current - timedelta(seconds=PendoConfig.REMINDER_CHECK_WINDOW_SECONDS)
        ).isoformat(timespec="seconds")
        retry_cutoff = (
            current - timedelta(seconds=PendoConfig.REMINDER_STALE_AFTER_SECONDS)
        ).isoformat(timespec="seconds")
        conn = self.get_connection()
        with conn:
            conn.execute(
                """
                UPDATE reminder_logs
                SET confirmed_at = ?, user_action = 'expired', state = 'confirmed',
                    claim_token = NULL, claim_expires_at = NULL, next_attempt_at = NULL
                WHERE sent_at IS NULL AND confirmed_at IS NULL AND fire_at_utc IS NOT NULL
                  AND (
                    (failure_count = 0 AND fire_at_utc < ?)
                    OR (failure_count > 0 AND fire_at_utc < ?)
                  )
                """,
                (now_text, initial_cutoff, retry_cutoff),
            )
            rows = conn.execute(
                """
                SELECT i.id, i.type, i.title, i.tags, i.owner_id, i.context,
                       i.start_time, i.location, i.notes, i.plan_date, i.deadline_at,
                       i.priority, i.status, i.remind_times, i.event_collection_id,
                       i.event_collection_kind, MIN(rl.fire_at_utc) AS due_at_utc
                FROM reminder_logs AS rl
                JOIN items AS i ON i.id = rl.item_id
                WHERE rl.sent_at IS NULL AND rl.confirmed_at IS NULL
                  AND rl.fire_at_utc IS NOT NULL AND rl.fire_at_utc <= ?
                  AND (
                    (rl.failure_count = 0 AND rl.fire_at_utc >= ?)
                    OR (rl.failure_count > 0 AND rl.fire_at_utc >= ?)
                  )
                  AND (rl.next_attempt_at IS NULL OR rl.next_attempt_at <= ?)
                  AND (
                    rl.state != 'claimed'
                    OR rl.claim_expires_at IS NULL
                    OR rl.claim_expires_at <= ?
                  )
                  AND i.deleted = 0 AND i.type IN ('event', 'task')
                  AND (i.type != 'task' OR COALESCE(i.status, 'open') = 'open')
                  AND (
                    i.type != 'event' OR i.event_role IS NULL
                    OR i.event_role IN ('single', 'multi_node_child', 'recurring_occurrence')
                  )
                  AND EXISTS (
                    SELECT 1
                    FROM json_each(
                      CASE WHEN json_valid(i.remind_times) THEN i.remind_times ELSE '[]' END
                    ) AS active_reminder
                    WHERE CAST(active_reminder.value AS TEXT) = rl.remind_time
                  )
                GROUP BY i.id
                ORDER BY due_at_utc, i.id
                """,
                (now_text, initial_cutoff, retry_cutoff, now_text, now_text),
            ).fetchall()
        return [item for row in rows if (item := self._row_to_item(row)) is not None]

    def prune_reminder_logs(self, *, before: datetime) -> int:
        """删除保留期之前的已确认提醒历史。"""

        cutoff = self._as_utc(before, "reminder retention cutoff").isoformat()
        conn = self.get_connection()
        with conn:
            cursor = conn.execute(
                "DELETE FROM reminder_logs WHERE confirmed_at IS NOT NULL AND confirmed_at < ?",
                (cutoff,),
            )
        return cursor.rowcount

    def get_unconfirmed_sent_reminders(self) -> list[dict[str, Any]]:
        """获取已发送但未确认的提醒（用于重复发送）

        已从条目当前 remind_times 中移除的 sent history 仍会保留在 reminder_logs，
        但不应继续进入重复发送队列。
        """
        rows = (
            self.get_connection()
            .execute("""
            SELECT rl.id, rl.item_id, rl.remind_time,
                   rl.repeat_count, COALESCE(rl.last_sent_at, rl.sent_at) AS last_sent_at
            FROM reminder_logs rl
            JOIN items i ON rl.item_id = i.id
                AND i.deleted = 0
                AND (i.type != 'task' OR COALESCE(i.status, 'open') = 'open')
            WHERE rl.sent_at IS NOT NULL AND rl.confirmed_at IS NULL
              AND EXISTS (
                SELECT 1
                FROM json_each(
                  CASE WHEN json_valid(i.remind_times) THEN i.remind_times ELSE '[]' END
                ) AS active_reminder
                WHERE CAST(active_reminder.value AS TEXT) = rl.remind_time
              )
            ORDER BY rl.sent_at
        """)
            .fetchall()
        )
        return [dict(row) for row in rows]

    def get_all_events_with_reminders(self, owner_id: str) -> list[Item]:
        """获取指定用户有提醒的活动日程和待办。

        不按条目的开始时间截断：“提前数天”的提醒可能已到期，
        即使日程本身仍在较远的将来。
        """
        conn = self.get_connection()
        cursor = conn.cursor()

        conditions = [
            f"type IN ('{ItemType.EVENT.value}', '{ItemType.TASK.value}')",
            "deleted = 0",
            f"(type != '{ItemType.EVENT.value}' OR event_role IS NULL OR event_role IN ('single', 'multi_node_child', 'recurring_occurrence'))",
            f"(type != '{ItemType.TASK.value}' OR COALESCE(status, 'open') = 'open')",
            "remind_times IS NOT NULL",
            "remind_times != '[]'",
        ]

        conditions.append("owner_id = ?")

        query = f"SELECT * FROM items WHERE {' AND '.join(conditions)}"

        cursor.execute(query, (owner_id,))

        return [item for row in cursor.fetchall() if (item := self._row_to_item(row)) is not None]

    # ==================== 私有方法 ====================

    @staticmethod
    def _quote_col(name: str) -> str:
        """校验并引用列名，兼容 references 等保留字且拒绝动态 SQL 注入。"""
        if not _SQL_IDENTIFIER.fullmatch(name):
            raise ValueError(f"Invalid SQL identifier: {name}")
        return f'"{name}"'

    @staticmethod
    def tag_filter_pattern(tag: Any) -> str:
        """生成 JSON 标签列表的完整元素 LIKE 模式。"""
        value = str(tag or "").strip().replace("\\", "\\\\")
        value = value.replace("%", "\\%").replace("_", "\\_")
        return f'%"{value}"%'

    @staticmethod
    def _like_contains_pattern(value: Any) -> str:
        """生成按字面值包含搜索的 LIKE 模式，不放大用户输入的通配符。"""
        escaped = str(value).replace("\\", "\\\\")
        escaped = escaped.replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    def _prepare_data(self, data: dict[str, Any]) -> dict[str, Any]:
        """准备数据用于存储"""
        unknown = data.keys() - self._ITEM_FIELDS
        if unknown:
            raise ValueError(f"Unsupported item field: {', '.join(sorted(unknown))}")
        require_canonical_utc_storage(data)
        prepared: dict[str, Any] = {}
        for key, value in data.items():
            if value is None:
                prepared[key] = None
            elif key in self._JSON_FIELDS and isinstance(value, (list, dict)):
                prepared[key] = json.dumps(value, ensure_ascii=False)
            else:
                prepared[key] = value
        return prepared

    def _decode_json_field(self, field: str, raw_value: Any) -> list[Any] | dict[str, Any]:
        """解码一个 JSON 字段，并按模型声明的容器类型安全降级。"""
        default: list[Any] | dict[str, Any] = [] if field in self._JSON_LIST_FIELDS else {}
        if raw_value in (None, ""):
            return default
        try:
            decoded = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
        except (json.JSONDecodeError, TypeError, ValueError):
            return default
        if field in self._JSON_LIST_FIELDS and isinstance(decoded, list):
            return decoded
        if field in self._JSON_OBJECT_FIELDS and isinstance(decoded, dict):
            return decoded
        return default

    def _row_to_item(self, row: sqlite3.Row) -> Item | None:
        """把数据库行解码成对应的条目 dataclass；损坏行记录告警并跳过。"""
        data = dict(row)

        if isinstance(data.get("type"), str):
            try:
                data["type"] = ItemType(data["type"])
            except (TypeError, ValueError):
                logger.warning("Unknown item type while decoding Pendo row")
                return None

        for field in self._JSON_FIELDS:
            data[field] = self._decode_json_field(field, data.get(field))

        if data.get("notes") is None:
            data["notes"] = ""

        data["deleted"] = bool(data.get("deleted"))
        data["is_favorite"] = normalize_bool_flag(data.get("is_favorite", False))

        item_type = data.get("type")
        if not isinstance(item_type, ItemType):
            return None
        if item_type is ItemType.TASK:
            raw_status = data.get("status") or TaskStatus.OPEN.value
            try:
                data["status"] = TaskStatus(raw_status)
            except (TypeError, ValueError):
                logger.warning("Unknown task status while decoding Pendo row")
                return None

        item_class = ITEM_TYPE_CLASS_MAP.get(item_type, Item)
        valid_fields = {field.name for field in item_class.__dataclass_fields__.values()}
        filtered_data = {key: value for key, value in data.items() if key in valid_fields}
        try:
            return item_class(**filtered_data)
        except (TypeError, ValueError):
            logger.warning("Invalid Pendo row for item type=%s", item_type.value)
            return None

    def _refresh_fts(self, item_id: str, conn: sqlite3.Connection | None = None) -> None:
        """从数据库当前行刷新 FTS 索引（用于 update/undo 等场景）"""
        if conn is None:
            conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT title, content, tags, category FROM items WHERE id = ? AND deleted = 0",
            (item_id,),
        )
        row = cursor.fetchone()
        if row:
            self._update_fts(
                item_id,
                {
                    "title": row[0] or "",
                    "content": row[1] or "",
                    "tags": self._decode_json_field("tags", row[2]),
                    "category": row[3] or "",
                },
                conn,
            )

    def _update_fts(
        self,
        item_id: str,
        item_data: dict[str, Any],
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """更新全文搜索索引"""
        if conn is None:
            conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute("DELETE FROM items_fts WHERE id = ?", (item_id,))

        tags = item_data.get("tags", [])
        tags_str = " ".join(str(tag) for tag in tags) if isinstance(tags, list) else ""

        cursor.execute(
            """
            INSERT INTO items_fts (id, title, content, tags, category)
            VALUES (?, ?, ?, ?, ?)
        """,
            (
                item_id,
                item_data.get("title", ""),
                item_data.get("content", ""),
                tags_str,
                item_data.get("category", ""),
            ),
        )

    def rebuild_fts_index(self, owner_id: str | None = None) -> dict[str, Any]:
        """重建有效条目的 FTS 记录，并删除过期或已删除记录。"""
        conn = self.get_connection()
        cursor = conn.cursor()
        active_clause = "WHERE deleted = 0"
        active_params: list[Any] = []
        if owner_id:
            active_clause += " AND owner_id = ?"
            active_params.append(owner_id)

        with conn:
            if owner_id:
                cursor.execute(
                    "DELETE FROM items_fts WHERE id IN (SELECT id FROM items WHERE owner_id = ?)",
                    (owner_id,),
                )
            else:
                cursor.execute("DELETE FROM items_fts")

            rows = cursor.execute(
                f"""
                SELECT id, title, content, tags, category
                FROM items
                {active_clause}
                ORDER BY id
                """,
                active_params,
            ).fetchall()
            for row in rows:
                try:
                    tags = json.loads(row["tags"]) if row["tags"] else []
                except (TypeError, ValueError):
                    tags = []
                self._update_fts(
                    row["id"],
                    {
                        "title": row["title"] or "",
                        "content": row["content"] or "",
                        "tags": tags,
                        "category": row["category"] or "",
                    },
                    conn,
                )

        self.cache_clear()
        return {
            "owner_id": owner_id,
            "indexed": len(rows),
            "scope": "owner" if owner_id else "all",
        }
