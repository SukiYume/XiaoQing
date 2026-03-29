"""
精简版数据库模块
合并 connection_manager, cache_manager, item_repository, settings_repository, log_repository
"""

import sqlite3
import json
import logging
import uuid
import time
import threading
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Optional
from contextlib import contextmanager

from ..utils.settings_utils import normalize_settings_json
from ..utils.validators import validate_item_data, sanitize_search_keyword
from ..models.item import (
    ItemType,
    Item,
    EventItem,
    TaskItem,
    NoteItem,
    DiaryItem,
    LedgerItem,
    ITEM_TYPE_CLASS_MAP,
)

logger = logging.getLogger(__name__)


class Database:
    """统一数据库服务类

    包含连接管理、缓存、数据访问层功能
    """

    CACHE_TTL = 30
    CACHE_MAX_SIZE = 1024
    ALLOWED_DATE_FIELDS = {"start_time", "end_time", "due_time", "diary_date", "created_at", "ledger_date"}

    # JSON 序列化字段：写入时 dumps，读取时 loads
    _JSON_FIELDS = [
        "tags",
        "context",
        "attachments",
        "ai_meta",
        "participants",
        "remind_times",
        "subtasks",
        "dependencies",
        "references",
        "related_items",
        "milestones",
    ]

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        self._all_connections: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

        # 使用 OrderedDict 实现 LRU 缓存
        self._cache: OrderedDict[str, tuple[float, Any]] = (
            OrderedDict()
        )  # key -> (timestamp, value)
        self._cache_lock = threading.Lock()

        # Repository引用（向后兼容）
        self.items = self
        self.settings = self
        self.logs = self
        self.conn_manager = self

        logger.debug("Initializing database at %s", db_path)
        self._init_database()

    # ==================== 连接管理 ====================

    def get_connection(self) -> sqlite3.Connection:
        """获取当前线程的数据库连接"""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
            with self._lock:
                self._all_connections.append(conn)
        return self._local.conn

    def close_all_connections(self):
        """关闭所有连接"""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
        with self._lock:
            for conn in list(self._all_connections):
                try:
                    conn.close()
                except Exception:
                    pass
            self._all_connections.clear()

    def cleanup(self):
        """清理资源"""
        self.close_all_connections()

    @contextmanager
    def transaction(self):
        """事务上下文管理器"""
        conn = self.get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ==================== 缓存管理 ====================

    def _cache_key(self, *parts) -> str:
        """生成缓存键"""

        def normalize(v):
            if isinstance(v, (dict, list)):
                return json.dumps(v, sort_keys=True, default=str)
            return str(v)

        return "|".join(normalize(p) for p in parts)

    def _cache_get(self, key: str) -> Any:
        """获取缓存"""
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry:
                ts, val = entry
                if time.time() - ts <= self.CACHE_TTL:
                    self._cache.move_to_end(key)  # 维护真正的 LRU 顺序
                    return val
                del self._cache[key]
        return None

    def _cache_set(self, key: str, value: Any):
        """设置缓存（使用 LRU 淘汰策略）"""
        with self._cache_lock:
            # 如果键已存在，先删除（为了更新位置）
            if key in self._cache:
                del self._cache[key]
            # 如果超过容量，删除最旧的项（OrderedDict 的第一个）
            elif len(self._cache) >= self.CACHE_MAX_SIZE:
                self._cache.popitem(last=False)
            # 添加新项（自动放到最后）
            self._cache[key] = (time.time(), value)

    def cache_clear(self):
        """清空缓存"""
        with self._cache_lock:
            self._cache.clear()

    def cache_invalidate(self, pattern: str | None = None):
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

    # 兼容旧接口
    def clear(self):
        """清空缓存（兼容CacheManager）"""
        self.cache_clear()

    # ==================== 数据库初始化 ====================

    def _init_database(self):
        """初始化数据库表结构"""
        with self.transaction() as conn:
            cursor = conn.cursor()

            # 主表
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
                rrule TEXT,
                remind_policy_id TEXT,
                remind_times TEXT,
                parent_id TEXT,
                due_time TEXT,
                priority INTEGER,
                status TEXT,
                estimate INTEGER,
                subtasks TEXT,
                dependencies TEXT,
                progress INTEGER DEFAULT 0,
                completed_at TEXT,
                "references" TEXT,
                last_viewed TEXT,
                related_items TEXT,
                mood TEXT,
                mood_score INTEGER,
                weather TEXT,
                template_id TEXT,
                diary_date TEXT,
                milestones TEXT,
                notes TEXT
            )
            """)

            # Schema 迁移：新增字段（幂等，已存在则忽略）
            migrations = [
                "ALTER TABLE items ADD COLUMN milestones TEXT",
                "ALTER TABLE items ADD COLUMN notes TEXT",
                "ALTER TABLE items ADD COLUMN amount REAL",
                "ALTER TABLE items ADD COLUMN direction TEXT",
                "ALTER TABLE items ADD COLUMN ledger_category TEXT",
                "ALTER TABLE items ADD COLUMN payment_method TEXT",
                "ALTER TABLE items ADD COLUMN ledger_date TEXT",
                "ALTER TABLE items ADD COLUMN remark TEXT",
                # reminder_logs 重构：一行一个 (item_id, remind_time)，用 repeat_count 替代多行
                "ALTER TABLE reminder_logs ADD COLUMN repeat_count INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE reminder_logs ADD COLUMN last_sent_at TEXT",
            ]
            for sql in migrations:
                try:
                    cursor.execute(sql)
                except sqlite3.OperationalError:
                    pass  # 列已存在，忽略

            # 索引
            for idx in [
                "CREATE INDEX IF NOT EXISTS idx_owner_type ON items(owner_id, type, deleted)",
                f"CREATE INDEX IF NOT EXISTS idx_start_time ON items(start_time) WHERE type='{ItemType.EVENT.value}'",
                f"CREATE INDEX IF NOT EXISTS idx_due_time ON items(due_time) WHERE type='{ItemType.TASK.value}'",
                f"CREATE INDEX IF NOT EXISTS idx_diary_date ON items(diary_date) WHERE type='{ItemType.DIARY.value}'",
                "CREATE INDEX IF NOT EXISTS idx_parent_id ON items(parent_id) WHERE parent_id IS NOT NULL",
            ]:
                cursor.execute(idx)

            # 全文搜索表
            cursor.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
                    id UNINDEXED, title, content, tags, category
                )
            """)

            # 提醒记录表：每个 (item_id, remind_time) 一行
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS reminder_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT NOT NULL,
                    remind_time TEXT NOT NULL,
                    sent_at TEXT,
                    confirmed_at TEXT,
                    user_action TEXT,
                    repeat_count INTEGER NOT NULL DEFAULT 1,
                    last_sent_at TEXT
                )
            """)

            # 数据迁移：合并旧的多行 reminder_logs 为每 (item_id, remind_time) 一行
            # 仅在存在重复行且 last_sent_at 尚未填充时执行
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

            # 操作日志表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS operation_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    item_type TEXT,
                    item_id TEXT,
                    details TEXT,
                    created_at TEXT NOT NULL
                )
            """)

            # 用户设置表
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
                    updated_at TEXT
                )
            """)

            # 数据迁移审计日志表
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

    # ==================== Item操作 ====================

    def insert_item(self, item_data: dict[str, Any] | Item, custom_id: str | None = None) -> str:
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
            item_dict["id"] = uuid.uuid4().hex[:8]

        item_dict.setdefault("created_at", datetime.now().isoformat())
        item_dict.setdefault("updated_at", datetime.now().isoformat())

        data = self._prepare_data(item_dict)
        columns = ", ".join(self._quote_col(k) for k in data.keys())
        placeholders = ", ".join(["?" for _ in data])

        try:
            # S-1修复：使用 with conn: 代替手动 BEGIN/COMMIT/ROLLBACK，
            # 避免与 Python sqlite3 隐式事务冲突
            with conn:
                cursor.execute(
                    f"INSERT INTO items ({columns}) VALUES ({placeholders})", list(data.values())
                )
                self._update_fts(item_dict["id"], item_dict, conn)
            # 精确失效：只清除与该条目相关的缓存
            self.cache_invalidate(item_dict["id"])
            self.cache_invalidate(f"items|{item_dict.get('owner_id', '')}")
            return item_dict["id"]
        except Exception as e:
            logger.exception("Failed to insert item: %s", e)
            raise

    # FTS 相关字段，只有这些字段被修改时才需要更新全文索引
    _FTS_FIELDS = frozenset({"title", "content", "tags", "category"})

    def update_item(
        self, item_id: str, updates: dict[str, Any] | Item, owner_id: str | None = None
    ) -> bool:
        """更新条目，支持dict或Item dataclass实例"""
        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            # 如果是Item dataclass实例，转换为字典
            if isinstance(updates, Item):
                update_dict = updates.to_dict()
                # 更新时不应包含id字段
                update_dict.pop("id", None)
            else:
                update_dict = updates

            update_dict["updated_at"] = datetime.now().isoformat()
            data = self._prepare_data(update_dict)
            set_clause = ", ".join([f"{self._quote_col(k)} = ?" for k in data.keys()])

            # S-1修复：使用 with conn: 代替手动 BEGIN/COMMIT/ROLLBACK
            affected = 0
            with conn:
                if owner_id:
                    cursor.execute(
                        f"UPDATE items SET {set_clause} WHERE id = ? AND owner_id = ?",
                        list(data.values()) + [item_id, owner_id],
                    )
                else:
                    cursor.execute(
                        f"UPDATE items SET {set_clause} WHERE id = ?",
                        list(data.values()) + [item_id],
                    )
                affected = cursor.rowcount

                if affected > 0 and "remind_times" in update_dict:
                    self._sync_reminder_logs(cursor, item_id, update_dict.get("remind_times"))

                # C-5修复：在同一事务内直接读取最新数据更新FTS，避免读到缓存中的旧值
                if affected > 0 and self._FTS_FIELDS & update_dict.keys():
                    self._refresh_fts(item_id, conn)

            # 精确失效：只清除与该条目相关的缓存
            self.cache_invalidate(item_id)
            # 即使调用方不传 owner_id，也需要失效该用户的列表缓存
            resolved_owner = owner_id
            if not resolved_owner and affected > 0:
                try:
                    oid_cursor = conn.cursor()
                    oid_cursor.execute("SELECT owner_id FROM items WHERE id = ?", (item_id,))
                    row = oid_cursor.fetchone()
                    if row:
                        resolved_owner = row[0]
                except Exception:
                    pass
            if resolved_owner:
                self.cache_invalidate(f"items|{resolved_owner}")
            return affected > 0
        except Exception as e:
            logger.exception("Failed to update item: %s", e)
            raise

    def find_instances(
        self, owner_id: str, parent_id: str, columns: str = "*",
    ) -> list[Any]:
        """查找 parent_id 对应的父事件及所有子实例（共用查询模式）"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT {columns} FROM items
                WHERE owner_id = ? AND type = '{ItemType.EVENT.value}' AND deleted = 0
                AND (id = ? OR parent_id = ? OR id LIKE ?)
                ORDER BY start_time""",
            (owner_id, parent_id, parent_id, f"{parent_id}_%"),
        )
        return cursor.fetchall()

    def batch_update_items(
        self, per_item_updates: dict[str, dict[str, Any]], owner_id: str,
    ) -> int:
        """单事务批量更新多个条目，含 FTS 维护和缓存失效"""
        if not per_item_updates:
            return 0
        conn = self.get_connection()
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        fts_fields = self._FTS_FIELDS
        needs_fts = False
        affected = 0

        with conn:
            for iid, updates in per_item_updates.items():
                updates["updated_at"] = now
                data = self._prepare_data(updates)
                set_clause = ", ".join([f'{self._quote_col(k)} = ?' for k in data.keys()])
                cursor.execute(
                    f"UPDATE items SET {set_clause} WHERE id = ? AND owner_id = ?",
                    list(data.values()) + [iid, owner_id],
                )
                affected += cursor.rowcount
                if cursor.rowcount > 0 and "remind_times" in updates:
                    self._sync_reminder_logs(cursor, iid, updates.get("remind_times"))
                needs_fts = needs_fts or bool(fts_fields & set(data.keys()))

            if needs_fts:
                for iid in per_item_updates:
                    self._refresh_fts(iid, conn)

        for iid in per_item_updates:
            self.cache_invalidate(iid)
        self.cache_invalidate(f"items|{owner_id}")
        return affected

    def batch_insert_or_update(
        self,
        operations: list[tuple[str, dict[str, Any]]],
        owner_id: str,
    ) -> list[tuple[str, str, Optional[str]]]:
        """单事务批量插入/更新，用于数据导入

        Args:
            operations: [(action, payload), ...] 其中 action 为 "insert" 或 "update"
            owner_id: 用户 ID

        Returns:
            [(action, item_id, error_or_none), ...] 每条操作的结果
        """
        if not operations:
            return []
        conn = self.get_connection()
        cursor = conn.cursor()
        results: list[tuple[str, str, Optional[str]]] = []

        with conn:
            for action, payload in operations:
                item_id = payload.get("id", "")
                try:
                    payload["owner_id"] = owner_id
                    validated = validate_item_data(payload)
                    validated.setdefault("created_at", datetime.now().isoformat())
                    validated.setdefault("updated_at", datetime.now().isoformat())
                    data = self._prepare_data(validated)

                    if action == "insert":
                        columns = ", ".join(self._quote_col(k) for k in data.keys())
                        placeholders = ", ".join(["?" for _ in data])
                        cursor.execute(
                            f"INSERT INTO items ({columns}) VALUES ({placeholders})",
                            list(data.values()),
                        )
                    elif action == "update":
                        update_data = {k: v for k, v in data.items() if k not in ("id", "type")}
                        set_clause = ", ".join(
                            [f"{self._quote_col(k)} = ?" for k in update_data.keys()]
                        )
                        cursor.execute(
                            f"UPDATE items SET {set_clause} WHERE id = ? AND owner_id = ?",
                            list(update_data.values()) + [item_id, owner_id],
                        )
                    results.append((action, item_id, None))
                except Exception as exc:
                    raise RuntimeError(f"导入记录 {item_id} 失败: {exc}") from exc

        # 事务成功后清缓存（包括单条目和列表缓存）
        for _action, payload in operations:
            iid = payload.get("id", "")
            if iid:
                self.cache_invalidate(iid)
        self.cache_invalidate(f"items|{owner_id}")
        return results

    def batch_soft_delete(self, item_ids: list[str], owner_id: str) -> int:
        """单事务批量软删除，含 FTS 清理和缓存失效"""
        if not item_ids:
            return 0
        conn = self.get_connection()
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        placeholders = ",".join(["?" for _ in item_ids])

        with conn:
            cursor.execute(
                f"UPDATE items SET deleted=1, deleted_at=?, updated_at=? "
                f"WHERE id IN ({placeholders}) AND owner_id=?",
                [now, now] + item_ids + [owner_id],
            )
            affected = cursor.rowcount
            cursor.execute(f"DELETE FROM items_fts WHERE id IN ({placeholders})", item_ids)

        for iid in item_ids:
            self.cache_invalidate(iid)
        self.cache_invalidate(f"items|{owner_id}")
        return affected

    def get_item(self, item_id: str, owner_id: str | None = None) -> Optional[Item]:
        """获取单个条目，返回Item dataclass实例"""
        cache_key = self._cache_key("item", item_id, owner_id or "*")
        cached = self._cache_get(cache_key)
        if cached:
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

    _ALLOWED_SORT_FIELDS = {"created_at", "updated_at", "ledger_date", "due_time", "start_time", "amount"}

    def get_items(
        self,
        owner_id: str,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Item]:
        """获取条目列表，返回Item dataclass实例列表

        Args:
            owner_id: 用户ID
            filters: 过滤条件，支持 type, category, ledger_category, status, tags, direction,
                     amount_min, amount_max, start_date, end_date, date_field,
                     sort_field, sort_order
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            Item dataclass实例列表
        """
        cache_key = self._cache_key("items", owner_id, filters or {}, limit, offset)
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        conn = self.get_connection()
        cursor = conn.cursor()

        where = ["owner_id = ?", "deleted = 0"]
        params = [owner_id]

        if filters:
            for key in ["type", "category", "ledger_category", "status"]:
                if key in filters:
                    where.append(f"{key} = ?")
                    params.append(filters[key])
            if "tags" in filters:
                where.append(f"tags LIKE ?")
                params.append(f"%{filters['tags']}%")
            if "direction" in filters:
                where.append("direction = ?")
                params.append(filters["direction"])
            if "amount_min" in filters:
                where.append("amount >= ?")
                params.append(filters["amount_min"])
            if "amount_max" in filters:
                where.append("amount <= ?")
                params.append(filters["amount_max"])

            # 支持日期范围过滤
            date_field = filters.get("date_field")
            if date_field:
                if date_field not in self.ALLOWED_DATE_FIELDS:
                    raise ValueError(f"Invalid date field: {date_field}")

                if "start_date" in filters:
                    where.append(f"{date_field} >= ?")
                    params.append(filters["start_date"])

                if "end_date" in filters:
                    where.append(f"{date_field} <= ?")
                    params.append(filters["end_date"])

        sort_field = "created_at"
        sort_order = "DESC"
        if filters:
            sf = filters.get("sort_field", "created_at")
            so = filters.get("sort_order", "DESC").upper()
            if sf in self._ALLOWED_SORT_FIELDS:
                sort_field = sf
            if so in ("ASC", "DESC"):
                sort_order = so

        sql = f"SELECT * FROM items WHERE {' AND '.join(where)} ORDER BY {sort_field} {sort_order} LIMIT ? OFFSET ?"
        cursor.execute(sql, params + [limit, offset])

        items = []
        for row in cursor.fetchall():
            item = self._row_to_item(row)
            if item:
                items.append(item)

        self._cache_set(cache_key, items)
        return items

    def get_active_user_ids(self) -> list[str]:
        """Return distinct active users with at least one non-deleted item."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT owner_id FROM items WHERE deleted = 0")
        return [str(row[0]) for row in cursor.fetchall()]

    def get_undone_tasks_for_date(self, user_id: str, date_str: str) -> list[TaskItem]:
        """Return unfinished tasks in the date-based category bucket."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT * FROM items WHERE owner_id = ? AND type = '{ItemType.TASK.value}' AND category = ? AND status = 'todo' AND deleted = 0",
            (user_id, date_str),
        )

        tasks: list[TaskItem] = []
        for row in cursor.fetchall():
            item = self._row_to_item(row)
            if isinstance(item, TaskItem):
                tasks.append(item)
        return tasks

    def get_last_unconfirmed_remind_time(self, item_id: str) -> Optional[str]:
        """Return the latest sent but unconfirmed remind time for an item."""
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

    def delete_item(self, item_id: str, soft: bool = True, owner_id: str | None = None) -> bool:
        """删除条目"""
        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            where = "id = ? AND owner_id = ?" if owner_id else "id = ?"
            params: list[Any] = [item_id, owner_id] if owner_id else [item_id]

            with conn:
                if soft:
                    now = datetime.now().isoformat()
                    cursor.execute(
                        f"UPDATE items SET deleted=1, deleted_at=?, updated_at=? WHERE {where}",
                        [now, now] + params,
                    )
                else:
                    cursor.execute(f"DELETE FROM items WHERE {where}", params)
                # S-2修复：在 FTS 操作前保存 rowcount，避免被后续语句覆盖
                affected = cursor.rowcount
                cursor.execute("DELETE FROM reminder_logs WHERE item_id = ?", (item_id,))
                cursor.execute("DELETE FROM items_fts WHERE id = ?", (item_id,))
            self.cache_clear()
            return affected > 0
        except Exception as e:
            logger.exception("Failed to delete item: %s", e)
            raise

    def _sync_reminder_logs(self, cursor, item_id: str, remind_times: list[str] | None):
        """删除当前条目已移除的提醒日志。"""
        active_times = sorted({
            str(remind_time)
            for remind_time in (remind_times or [])
            if remind_time
        })
        if not active_times:
            cursor.execute("DELETE FROM reminder_logs WHERE item_id = ?", (item_id,))
            return

        placeholders = ",".join("?" for _ in active_times)
        cursor.execute(
            f"DELETE FROM reminder_logs WHERE item_id = ? AND remind_time NOT IN ({placeholders})",
            [item_id] + active_times,
        )

    @staticmethod
    def _apply_filters(
        where: list[str], params: list[Any], filters: dict[str, Any] | None,
    ):
        """将 type/category/status/direction 过滤条件追加到 where / params"""
        if filters:
            for key in ("type", "category", "status", "direction"):
                if key in filters:
                    where.append(f"{key} = ?")
                    params.append(filters[key])

    def search_items(
        self,
        owner_id: str,
        query: str,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
    ) -> list[Item]:
        """全文搜索，返回Item dataclass列表"""
        conn = self.get_connection()
        cursor = conn.cursor()

        # 清洗搜索关键词
        query = sanitize_search_keyword(query)

        # FTS搜索
        fts_ids = []
        try:
            cursor.execute(
                "SELECT id FROM items_fts WHERE items_fts MATCH ? ORDER BY rank LIMIT ?",
                (query, limit),
            )
            fts_ids = [row[0] for row in cursor.fetchall()]
        except Exception as e:
            logger.warning("FTS search failed, falling back to LIKE: %s", e)

        # LIKE补充搜索（FTS的unicode61分词器对CJK子字符串匹配不完整，需要LIKE兜底）
        like = f"%{query}%"
        like_where: list[str] = [
            "owner_id = ?", "deleted = 0",
            """(
                title LIKE ? OR content LIKE ? OR tags LIKE ? OR category LIKE ? OR
                remark LIKE ? OR ledger_category LIKE ? OR location LIKE ? OR notes LIKE ? OR weather LIKE ?
            )""",
        ]
        like_params: list[Any] = [owner_id, like, like, like, like, like, like, like, like, like]
        self._apply_filters(like_where, like_params, filters)

        cursor.execute(
            f"SELECT id FROM items WHERE {' AND '.join(like_where)} LIMIT ?", like_params + [limit]
        )
        like_ids = [row[0] for row in cursor.fetchall()]

        # 合并去重：FTS结果优先（按rank排序），再补充LIKE独有的结果
        seen = set(fts_ids)
        merged_ids = list(fts_ids)
        for lid in like_ids:
            if lid not in seen:
                merged_ids.append(lid)
                seen.add(lid)

        if not merged_ids:
            return []

        # 查询完整条目
        placeholders = ",".join(["?" for _ in merged_ids])
        where: list[str] = [f"id IN ({placeholders})", "owner_id = ?", "deleted = 0"]
        params: list[Any] = merged_ids + [owner_id]
        self._apply_filters(where, params, filters)

        cursor.execute(f"SELECT * FROM items WHERE {' AND '.join(where)}", params)
        items_by_id: dict[str, Item] = {}
        for row in cursor.fetchall():
            item = self._row_to_item(row)
            if item:
                items_by_id[item.id] = item
        return [items_by_id[item_id] for item_id in merged_ids[:limit] if item_id in items_by_id]

    def undo_delete(self, owner_id: str, minutes: int = 5) -> dict[str, Any]:
        """撤销删除"""
        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            threshold = (datetime.now() - timedelta(minutes=minutes)).isoformat()
            cursor.execute(
                """
                SELECT * FROM items WHERE owner_id = ? AND deleted = 1 AND deleted_at >= ?
                ORDER BY deleted_at DESC LIMIT 1
            """,
                (owner_id, threshold),
            )
            row = cursor.fetchone()
            if not row:
                return {"status": "error", "message": f"未找到{minutes}分钟内删除的条目"}

            item = self._row_to_item(row)
            if not item:
                return {"status": "error", "message": "数据转换失败"}

            # S-1修复：使用 with conn: 代替手动 BEGIN/COMMIT/ROLLBACK
            with conn:
                cursor.execute(
                    "UPDATE items SET deleted = 0, deleted_at = NULL WHERE id = ?", (item.id,)
                )
                self._update_fts(item.id, item.to_dict(), conn)
            self.cache_clear()
            return {"status": "success", "message": "已恢复", "item": item}
        except Exception as e:
            logger.exception("Failed to undo delete: %s", e)
            return {"status": "error", "message": f"恢复失败: {e}"}

    def undo_edit(self, owner_id: str, minutes: int = 5) -> dict[str, Any]:
        """撤销编辑操作

        从 operation_logs 中查找最近的 edit_* 操作，
        读取其 old_values 快照并写回数据库。
        """
        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            threshold = (datetime.now() - timedelta(minutes=minutes)).isoformat()
            cursor.execute(
                """
                SELECT id, item_id, action, details, created_at FROM operation_logs
                WHERE user_id = ? AND action LIKE 'edit_%' AND created_at >= ?
                ORDER BY created_at DESC LIMIT 1
            """,
                (owner_id, threshold),
            )
            row = cursor.fetchone()
            if not row:
                return {"status": "error", "message": f"未找到{minutes}分钟内的编辑操作"}

            log_id = row[0]
            item_id = row[1]
            action = row[2]
            details = json.loads(row[3]) if row[3] else {}

            old_values = details.get("old_values")
            if not old_values:
                return {"status": "error", "message": "该编辑操作没有保存旧值快照，无法撤销"}

            # 确定需要恢复的 item IDs
            instance_ids = details.get("instance_ids", [item_id])
            if not isinstance(instance_ids, list):
                instance_ids = [item_id]

            # 准备恢复数据
            old_values["updated_at"] = datetime.now().isoformat()
            restore_data = self._prepare_data(old_values)
            set_clause = ", ".join([f"{self._quote_col(k)} = ?" for k in restore_data.keys()])
            placeholders = ",".join(["?" for _ in instance_ids])

            with conn:
                cursor.execute(
                    f"UPDATE items SET {set_clause} WHERE id IN ({placeholders}) AND owner_id = ?",
                    list(restore_data.values()) + instance_ids + [owner_id],
                )
                affected = cursor.rowcount

                # 更新 FTS 索引
                if self._FTS_FIELDS & set(restore_data.keys()):
                    for iid in instance_ids:
                        self._refresh_fts(iid, conn)

                # 删除该编辑日志，避免重复撤销
                cursor.execute("DELETE FROM operation_logs WHERE id = ?", (log_id,))

            self.cache_clear()

            return {
                "status": "success",
                "item_id": item_id,
                "action": action,
                "affected": affected,
                "instance_count": len(instance_ids),
            }
        except Exception as e:
            logger.exception("Failed to undo edit: %s", e)
            return {"status": "error", "message": f"撤销编辑失败: {e}"}

    def get_latest_undoable_operation(self, owner_id: str, minutes: int = 5) -> dict[str, Any]:
        """查找最近可撤销的操作（删除 或 编辑）

        比较 deleted_at（删除操作）和 operation_logs.created_at（编辑操作），
        返回最近的那个操作类型和时间。

        Returns:
            {'type': 'delete'|'edit'|None, 'time': ISO时间}
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        threshold = (datetime.now() - timedelta(minutes=minutes)).isoformat()

        # 查找最近的删除
        cursor.execute(
            """
            SELECT deleted_at FROM items WHERE owner_id = ? AND deleted = 1 AND deleted_at >= ?
            ORDER BY deleted_at DESC LIMIT 1
        """,
            (owner_id, threshold),
        )
        delete_row = cursor.fetchone()
        delete_time = delete_row[0] if delete_row else None

        # 查找最近的编辑
        cursor.execute(
            """
            SELECT created_at FROM operation_logs
            WHERE user_id = ? AND action LIKE 'edit_%' AND created_at >= ?
            ORDER BY created_at DESC LIMIT 1
        """,
            (owner_id, threshold),
        )
        edit_row = cursor.fetchone()
        edit_time = edit_row[0] if edit_row else None

        if not delete_time and not edit_time:
            return {"type": None}
        if not edit_time:
            return {"type": "delete", "time": delete_time}
        if not delete_time:
            return {"type": "edit", "time": edit_time}
        # 都有时取更近的
        return (
            {"type": "edit", "time": edit_time}
            if edit_time > delete_time
            else {"type": "delete", "time": delete_time}
        )

    def get_events_for_range(self, user_id: str, start_date: str, end_date: str):
        """获取日期范围内的日程

        普通事件与多节点事件：使用区间重叠判断
          start_time <= end_date AND (end_time IS NULL OR end_time >= start_date)
        重复事件：保持原来逻辑（start_time <= end_date），由上层按实例过滤
        """
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            f"""
            SELECT * FROM items WHERE owner_id = ? AND type = '{ItemType.EVENT.value}' AND deleted = 0
            AND (rrule IS NULL OR rrule = '')
            AND start_time <= ?
            AND (
                (end_time IS NOT NULL AND end_time != '' AND end_time >= ?)
                OR ((end_time IS NULL OR end_time = '') AND start_time >= ?)
            )
            ORDER BY start_time
        """,
            (user_id, end_date, start_date, start_date),
        )
        events = [item for row in cursor.fetchall() if (item := self._row_to_item(row)) is not None]

        cursor.execute(
            f"""
            SELECT * FROM items WHERE owner_id = ? AND type = '{ItemType.EVENT.value}' AND deleted = 0
            AND rrule IS NOT NULL AND rrule != '' AND start_time <= ?
            ORDER BY start_time
        """,
            (user_id, end_date),
        )
        repeat_events = [
            item for row in cursor.fetchall() if (item := self._row_to_item(row)) is not None
        ]

        return events, repeat_events

    def get_briefing_items(self, user_id: str, today_iso: str, tomorrow_iso: str):
        """获取每日简报条目

        Args:
            user_id: 用户ID
            today_iso: 今日开始时间ISO格式
            tomorrow_iso: 明日开始时间ISO格式

        Returns:
            (events, tasks, overdue_tasks) 元组
        """
        conn = self.get_connection()
        cursor = conn.cursor()

        # 今日日程
        cursor.execute(
            f"""
            SELECT * FROM items WHERE owner_id = ? AND type = '{ItemType.EVENT.value}' AND deleted = 0
            AND start_time >= ? AND start_time < ? ORDER BY start_time
        """,
            (user_id, today_iso, tomorrow_iso),
        )
        events = [item for row in cursor.fetchall() if (item := self._row_to_item(row)) is not None]

        # 今日待办（I-6修复：按 category 过滤今日分类，或 due_time 在今日范围内，避免历史积压全量纳入）
        today_date = today_iso[:10]  # YYYY-MM-DD
        cursor.execute(
            f"""
            SELECT * FROM items WHERE owner_id = ? AND type = '{ItemType.TASK.value}' AND deleted = 0
            AND status != 'done'
            AND (
                category = ?
                OR (due_time >= ? AND due_time < ?)
            )
            ORDER BY priority DESC, due_time ASC LIMIT 10
        """,
            (user_id, today_date, today_iso, tomorrow_iso),
        )
        tasks = [item for row in cursor.fetchall() if (item := self._row_to_item(row)) is not None]

        # 逾期待办
        cursor.execute(
            f"""
            SELECT * FROM items WHERE owner_id = ? AND type = '{ItemType.TASK.value}' AND deleted = 0
            AND status != 'done' AND due_time < ? ORDER BY due_time ASC LIMIT 10
        """,
            (user_id, today_iso),
        )
        overdue_tasks = [
            item for row in cursor.fetchall() if (item := self._row_to_item(row)) is not None
        ]

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
        return cursor.fetchone()[0] > 0

    def query_items_by_date_range(
        self, user_id: str, item_type: str, date_field: str, start_date: str, end_date: str
    ) -> list[Item]:
        """按日期范围查询，返回Item dataclass列表"""
        if date_field not in self.ALLOWED_DATE_FIELDS:
            raise ValueError(f"Invalid date field: {date_field}")

        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            f"""
            SELECT * FROM items WHERE owner_id = ? AND type = ? AND deleted = 0
            AND {date_field} >= ? AND {date_field} <= ? ORDER BY {date_field} DESC
        """,
            (user_id, item_type, start_date, end_date),
        )

        items = []
        for row in cursor.fetchall():
            item = self._row_to_item(row)
            if item:
                items.append(item)
        return items

    # ==================== 设置操作 ====================

    def get_user_settings(self, user_id: str) -> dict[str, Any]:
        """获取用户设置"""
        cache_key = self._cache_key("settings", user_id)
        cached = self._cache_get(cache_key)
        if cached:
            return cached

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
        """Batch-load user settings and fill missing users with defaults."""
        unique_user_ids = list(dict.fromkeys(user_ids))
        if not unique_user_ids:
            return {}

        results: dict[str, dict[str, Any]] = {}
        missing_user_ids: list[str] = []
        for user_id in unique_user_ids:
            cached = self._cache_get(self._cache_key("settings", user_id))
            if cached:
                results[user_id] = cached
            else:
                missing_user_ids.append(user_id)

        if missing_user_ids:
            conn = self.get_connection()
            cursor = conn.cursor()
            placeholders = ",".join("?" for _ in missing_user_ids)
            cursor.execute(
                f"SELECT * FROM user_settings WHERE user_id IN ({placeholders})",
                tuple(missing_user_ids),
            )
            for row in cursor.fetchall():
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
            "timezone": "Asia/Shanghai",
            "quiet_hours_start": "23:00",
            "quiet_hours_end": "07:00",
            "daily_report_time": "08:00",
            "diary_remind_time": "21:30",
            "default_category": "未分类",
            "settings_json": normalize_settings_json({}),
        }

    def _hydrate_user_settings_row(self, settings: dict[str, Any]) -> dict[str, Any]:
        raw_settings = settings.get("settings_json")
        if raw_settings:
            try:
                raw_settings = json.loads(raw_settings)
            except (TypeError, json.JSONDecodeError, ValueError):
                raw_settings = {}
        else:
            raw_settings = {}
        settings["settings_json"] = normalize_settings_json(raw_settings)
        return settings

    def update_user_settings(self, user_id: str, settings: dict[str, Any]) -> bool:
        """更新用户设置（C-1修复：先读当前设置再合并，避免部分更新覆盖其他字段）"""
        conn = self.get_connection()
        cursor = conn.cursor()

        # 读取当前设置作为 base（未登录用户返回默认值）
        current = self.get_user_settings(user_id)

        # 合并：新传入的字段覆盖现有字段，其余保留
        merged = {**current, **settings}
        merged_settings_json = {}
        if isinstance(current.get("settings_json"), dict):
            merged_settings_json.update(current["settings_json"])
        if isinstance(settings.get("settings_json"), dict):
            merged_settings_json.update(normalize_settings_json(settings["settings_json"], partial=True))
        merged["settings_json"] = normalize_settings_json(merged_settings_json)

        try:
            # S-1修复：使用 with conn: 代替手动 BEGIN/COMMIT/ROLLBACK
            with conn:
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO user_settings
                    (user_id, timezone, quiet_hours_start, quiet_hours_end, daily_report_time,
                     diary_remind_time, default_category, settings_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        user_id,
                        merged.get("timezone", "Asia/Shanghai"),
                        merged.get("quiet_hours_start", "23:00"),
                        merged.get("quiet_hours_end", "07:00"),
                        merged.get("daily_report_time", "08:00"),
                        merged.get("diary_remind_time", "21:30"),
                        merged.get("default_category", "未分类"),
                        json.dumps(merged.get("settings_json", {}), ensure_ascii=False),
                        datetime.now().isoformat(),
                    ),
                )
        except Exception as e:
            logger.exception("Failed to update user settings: %s", e)
            return False

        self.cache_clear()
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
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            # S-1修复：使用 with conn: 代替裸 conn.commit()
            with conn:
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
                        datetime.now().isoformat(),
                    ),
                )
            return True
        except Exception as e:
            logger.exception("Failed to log operation: %s", e)
            return False

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
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            return cursor.lastrowid

    def get_transfer_logs(self, owner_id: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
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
            "SELECT 1 FROM transfer_logs WHERE owner_id = ? AND bundle_id = ? AND action = 'import'",
            (owner_id, bundle_id),
        )
        return cursor.fetchone() is not None

    # ==================== 提醒相关 ====================

    def is_reminder_sent(self, item_id: str, remind_time: str) -> bool:
        """检查提醒是否已发送"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM reminder_logs WHERE item_id = ? AND remind_time = ? AND sent_at IS NOT NULL",
            (item_id, remind_time),
        )
        return cursor.fetchone() is not None

    def log_reminder(self, item_id: str, remind_time: str, sent: bool = True):
        """记录提醒发送（UPSERT：首次 INSERT，重复发送 UPDATE repeat_count + last_sent_at）"""
        conn = self.get_connection()
        cursor = conn.cursor()
        now = datetime.now().isoformat() if sent else None
        with conn:
            cursor.execute(
                """
                INSERT INTO reminder_logs (item_id, remind_time, sent_at, last_sent_at, repeat_count)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(item_id, remind_time) DO UPDATE SET
                    repeat_count = repeat_count + 1,
                    last_sent_at = excluded.sent_at
                WHERE excluded.sent_at IS NOT NULL
                """,
                (item_id, remind_time, now, now),
            )

    def confirm_reminder(
        self,
        item_id: str,
        user_action: str = "confirmed",
        owner_id: str | None = None,
        remind_time: str | None = None,
    ) -> dict[str, Any]:
        """确认提醒——直接 UPDATE 该 item 所有未确认行"""
        conn = self.get_connection()
        cursor = conn.cursor()
        now = datetime.now().isoformat()
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
                    f"UPDATE reminder_logs SET confirmed_at = ?, user_action = ? WHERE id IN ({placeholders})",
                    [now, user_action] + row_ids,
                )
            else:
                # 无已发送记录可确认（如静默时间未发出），补插一条已确认的记录
                self._insert_confirm_for_unsent(cursor, item_id, owner_id, remind_time, now, user_action)

        return {"status": "success", "message": f"已记录: {user_action}"}

    def _insert_confirm_for_unsent(
        self, cursor, item_id: str, owner_id: str | None,
        remind_time: str | None, now: str, user_action: str,
    ):
        """为未发送但用户手动确认的提醒补插一条记录"""
        item_where = ["id = ?", "deleted = 0"]
        item_params: list[Any] = [item_id]
        if owner_id is not None:
            item_where.append("owner_id = ?")
            item_params.append(owner_id)
        cursor.execute(
            f"SELECT remind_times FROM items WHERE {' AND '.join(item_where)}",
            item_params,
        )
        row = cursor.fetchone()
        if not row or not row[0]:
            return
        try:
            remind_times = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return
        past_times = [t for t in remind_times if isinstance(t, str) and t <= now]
        if remind_time is not None:
            past_times = [t for t in past_times if t == remind_time]
        if not past_times:
            return
        target_time = max(past_times)
        # UPSERT：如果已有行则更新确认状态，否则插入
        cursor.execute(
            """
            INSERT INTO reminder_logs (item_id, remind_time, sent_at, confirmed_at, user_action, repeat_count, last_sent_at)
            VALUES (?, ?, NULL, ?, ?, 0, NULL)
            ON CONFLICT(item_id, remind_time) DO UPDATE SET
                confirmed_at = excluded.confirmed_at,
                user_action = excluded.user_action
            """,
            (item_id, target_time, now, user_action),
        )

    def get_reminder_logs(self, item_id: str) -> list[dict[str, Any]]:
        """获取某个条目的所有提醒日志"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT remind_time, sent_at, confirmed_at, user_action, repeat_count, last_sent_at
            FROM reminder_logs WHERE item_id = ?
            ORDER BY remind_time
            """,
            (item_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_unconfirmed_sent_reminders(self) -> list[dict[str, Any]]:
        """获取已发送但未确认的提醒（用于重复发送）

        每个 (item_id, remind_time) 只有一行，直接返回含 repeat_count 和 last_sent_at 的记录。
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT rl.id, rl.item_id, rl.remind_time,
                   rl.repeat_count, COALESCE(rl.last_sent_at, rl.sent_at) AS last_sent_at
            FROM reminder_logs rl
            JOIN items i ON rl.item_id = i.id AND i.deleted = 0
            WHERE rl.sent_at IS NOT NULL AND rl.confirmed_at IS NULL
            ORDER BY rl.sent_at
        """)
        return [dict(row) for row in cursor.fetchall()]

    def get_all_events_with_reminders(
        self, owner_id: str | None = None, future_hours: int = 24
    ) -> list[Item]:
        """获取有提醒的日程

        Args:
            owner_id: 用户ID，如果为None则返回所有用户的日程
            future_hours: 只返回未来N小时内的日程，用于性能优化

        Returns:
            有提醒的日程列表
        """
        conn = self.get_connection()
        cursor = conn.cursor()

        # 构建查询条件
        conditions = [
            f"type = '{ItemType.EVENT.value}'",
            "deleted = 0",
            "remind_times IS NOT NULL",
            "remind_times != '[]'",
        ]

        params = []

        if owner_id:
            conditions.append("owner_id = ?")
            params.append(owner_id)

        # 添加时间范围过滤以优化性能
        # 里程碑事件的 start_time 是第一个里程碑，可能远在未来，但 remind_times 可能即将到期
        # 因此对多时间节点事件（milestones 非空）跳过 start_time 过滤
        if future_hours:
            future_time = (datetime.now() + timedelta(hours=future_hours)).isoformat()
            conditions.append(
                "(start_time <= ? OR (milestones IS NOT NULL AND milestones != '[]'))"
            )
            params.append(future_time)

        query = f"SELECT * FROM items WHERE {' AND '.join(conditions)}"

        cursor.execute(query, params)

        return [item for row in cursor.fetchall() if (item := self._row_to_item(row)) is not None]

    # ==================== 私有方法 ====================

    @staticmethod
    def _quote_col(name: str) -> str:
        """用双引号包裹列名，避免 SQL 保留字（如 references）导致语法错误"""
        return f'"{name}"'

    def _prepare_data(self, data: dict[str, Any]) -> dict[str, Any]:
        """准备数据用于存储"""
        prepared = {}
        for key, value in data.items():
            if value is None:
                prepared[key] = None
            elif key in self._JSON_FIELDS and isinstance(value, (list, dict)):
                prepared[key] = json.dumps(value, ensure_ascii=False)
            else:
                prepared[key] = value
        return prepared

    def _row_to_item(self, row: sqlite3.Row) -> Optional[Item]:
        """行转Item dataclass

        将数据库行转换为对应的Item子类实例
        根据type字段自动选择EventItem/TaskItem/NoteItem/DiaryItem
        """
        data = dict(row)

        # 将type字符串转换为ItemType枚举
        if "type" in data and isinstance(data["type"], str):
            try:
                data["type"] = ItemType(data["type"])
            except ValueError:
                logger.warning("Unknown item type: %s", data["type"])
                return None

        # 解析JSON字段
        for field in self._JSON_FIELDS:
            if field in data and data[field]:
                try:
                    data[field] = json.loads(data[field])
                except (json.JSONDecodeError, TypeError, ValueError):
                    data[field] = []
            elif field not in data or data[field] is None:
                # 为None的JSON字段设置为默认值
                if field in [
                    "tags",
                    "participants",
                    "remind_times",
                    "subtasks",
                    "dependencies",
                    "references",
                    "related_items",
                    "milestones",
                ]:
                    data[field] = []
                elif field in ["context", "attachments", "ai_meta"]:
                    data[field] = {}

        # 字符串字段 NULL 降级为空字符串
        for str_field in ["notes"]:
            if str_field in data and data[str_field] is None:
                data[str_field] = ""

        # 转换deleted字段
        if "deleted" in data:
            data["deleted"] = bool(data["deleted"])

        # 根据type选择对应的dataclass
        item_type = data.get("type")
        if item_type and isinstance(item_type, ItemType):
            item_class = ITEM_TYPE_CLASS_MAP.get(item_type, Item)
            try:
                # 过滤掉不存在的字段
                valid_fields = {f.name for f in item_class.__dataclass_fields__.values()}
                filtered_data = {k: v for k, v in data.items() if k in valid_fields}
                return item_class(**filtered_data)
            except Exception as e:
                logger.error("Failed to create item from row: %s", e)
                return None

        return None

    def _refresh_fts(self, item_id: str, conn=None):
        """从数据库当前行刷新 FTS 索引（用于 update/undo 等场景）"""
        if not conn:
            conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT title, content, tags, category FROM items WHERE id = ?", (item_id,)
        )
        row = cursor.fetchone()
        if row:
            self._update_fts(
                item_id,
                {
                    "title": row[0] or "",
                    "content": row[1] or "",
                    "tags": json.loads(row[2]) if row[2] else [],
                    "category": row[3] or "",
                },
                conn,
            )

    def _update_fts(self, item_id: str, item_data: dict[str, Any], conn=None):
        """更新全文搜索索引"""
        if not conn:
            conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute("DELETE FROM items_fts WHERE id = ?", (item_id,))

        tags = item_data.get("tags", [])
        tags_str = " ".join(tags) if isinstance(tags, list) else ""

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
