"""QingPet 的 SQLite 模式迁移、领域持久化和原子结算实现。"""

import json
import logging
import math
import re
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..models import GroupConfig, GroupConfigReadError, Inventory, OperationLog, Pet, User
from ..utils.constants import (
    DAILY_LIMITS,
    DEFAULT_ITEMS,
    GROUP_TASK_TEMPLATES,
    PET_SHOW_CONFIG,
    TITLES,
    ItemType,
    PetPersonality,
    PetStage,
    PetStatus,
)
from ..utils.time import utc_now

logger = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_OPERATION_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SAFE_ERROR_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,95}\Z")


def _log_database_failure(operation: str, exc: BaseException) -> None:
    """仅记录稳定的操作名和异常类型，避免数据库内容进入日志。"""

    safe_operation = operation if _SAFE_OPERATION_RE.fullmatch(operation) else "unknown"
    error_type = type(exc).__name__
    if not _SAFE_ERROR_TYPE_RE.fullmatch(error_type):
        error_type = "Exception"
    logger.error(
        "QingPet database operation failed operation=%s error_type=%s",
        safe_operation,
        error_type,
    )


@dataclass(frozen=True)
class VisitPetAtomicResult:
    """一次可幂等重放的宠物互访结算结果。"""

    success: bool
    reason: str = ""
    pet_name: str = ""
    visitor_grant: int = 0
    target_grant: int = 0
    intimacy_grant: int = 0
    duplicate: bool = False


@dataclass(frozen=True)
class PetShowWinner:
    user_id: str
    pet_name: str
    vote_count: int
    coins_granted: int


@dataclass(frozen=True)
class PetShowSettlementResult:
    show_id: int
    title: str
    winners: tuple[PetShowWinner, ...] = ()


@dataclass(frozen=True)
class MinigameOutcome:
    """小游戏随机结果请求的资产变化及可重放展示数据。"""

    requested_coins: int = 0
    experience: int = 0
    energy_cost: int = 0
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class MinigameAtomicResult:
    """一次已提交且可幂等重放的小游戏结算结果。"""

    success: bool
    reason: str = ""
    pet_name: str = ""
    opponent_pet_name: str = ""
    coin_grant: int = 0
    experience_grant: int = 0
    energy_cost: int = 0
    payload: dict[str, Any] | None = None
    duplicate: bool = False


@dataclass(frozen=True)
class LeaveMessageAtomicResult:
    """一次经过配额校验的留言提交结果。"""

    success: bool
    reason: str = ""
    pet_name: str = ""


@dataclass(frozen=True)
class PetActionAtomicResult:
    """一次受配额约束的宠物动作提交结果。"""

    success: bool
    reason: str = ""
    remaining: int = 0
    coins_granted: int = 0


@dataclass(frozen=True)
class TreatPetAtomicResult:
    """一次治疗事务的结果及成功写入后的宠物快照。"""

    success: bool
    reason: str = ""
    remaining: int = 0
    pet: Pet | None = None


@dataclass(frozen=True)
class DailyResetResult:
    users_reset: int
    pets_aged: int


@dataclass(frozen=True)
class WeeklyRankingWinner:
    user_id: str
    pet_name: str
    score: float
    coins_granted: int
    title_granted: bool = False


@dataclass(frozen=True)
class WeeklyActivitySettlementResult:
    winners: tuple[WeeklyRankingWinner, ...] = ()


@dataclass(frozen=True)
class GroupEconomySnapshot:
    """单次查询得到的群经济快照；余额以 ``users.coins`` 为准。"""

    total_pets: int = 0
    total_coins: int = 0
    total_experience: int = 0
    total_intimacy: int = 0
    average_care_score: float = 0.0
    active_today: int = 0


@dataclass(frozen=True)
class CoinLedgerReconciliation:
    """基于检查点比较权威余额与资产账本增量的结果。"""

    status: str
    current_balance: int
    expected_balance: int
    difference: int
    consistent: bool


_PET_ACTION_COUNTERS = {
    "feed": ("today_feed_count", "total_feed_count"),
    "clean": ("today_clean_count", "total_clean_count"),
    "play": ("today_play_count", "total_play_count"),
    "train": ("today_train_count", "total_train_count"),
    "explore": ("today_explore_count", "total_explore_count"),
}
_DAILY_TASK_TEMPLATES = (
    ("feed", 3, 30),
    ("clean", 2, 20),
    ("play", 3, 25),
    ("visit", 2, 20),
)
_WEEKLY_RANKING_REWARDS = (100, 50, 30)
_DAILY_COIN_LIMIT = int(DAILY_LIMITS["coins"])


_ALLOWED_SCHEMA_COLUMNS: dict[str, dict[str, str]] = {
    "users": {
        "total_feed_count": "INTEGER DEFAULT 0",
        "total_clean_count": "INTEGER DEFAULT 0",
        "total_play_count": "INTEGER DEFAULT 0",
        "total_train_count": "INTEGER DEFAULT 0",
        "total_explore_count": "INTEGER DEFAULT 0",
        "total_visit_count": "INTEGER DEFAULT 0",
        "total_gift_count": "INTEGER DEFAULT 0",
        "titles": "TEXT DEFAULT '[]'",
        "today_free_feed_count": "INTEGER DEFAULT 0",
        "today_message_count": "INTEGER DEFAULT 0",
        "total_free_feed_count": "INTEGER DEFAULT 0",
        "total_message_count": "INTEGER DEFAULT 0",
        "version": "INTEGER NOT NULL DEFAULT 0",
    },
    "pets": {
        "likes": "INTEGER DEFAULT 0",
        "dress_hat": "TEXT",
        "dress_clothes": "TEXT",
        "dress_accessory": "TEXT",
        "dress_background": "TEXT",
        "version": "INTEGER NOT NULL DEFAULT 0",
    },
    "group_configs": {
        "sensitive_words": "TEXT DEFAULT '[]'",
    },
    "tasks": {
        "created_date": "TEXT",
    },
    "trade_listings": {
        "status": "TEXT DEFAULT 'active'",
    },
    "pet_shows": {
        "status": "TEXT DEFAULT 'active'",
        "settled_at": "TEXT",
    },
    "inventories": {
        "version": "INTEGER NOT NULL DEFAULT 0",
    },
    "scheduler_runs": {
        "status": "TEXT NOT NULL DEFAULT 'completed'",
        "lease_until": "TEXT",
        "attempt_count": "INTEGER NOT NULL DEFAULT 1",
        "completed_at": "TEXT",
    },
}

_CURRENT_SCHEMA_VERSION = 5
_LEGACY_COLUMN_MIGRATIONS = (
    ("users", "total_feed_count", "INTEGER DEFAULT 0"),
    ("users", "total_clean_count", "INTEGER DEFAULT 0"),
    ("users", "total_play_count", "INTEGER DEFAULT 0"),
    ("users", "total_train_count", "INTEGER DEFAULT 0"),
    ("users", "total_explore_count", "INTEGER DEFAULT 0"),
    ("users", "total_visit_count", "INTEGER DEFAULT 0"),
    ("users", "total_gift_count", "INTEGER DEFAULT 0"),
    ("users", "titles", "TEXT DEFAULT '[]'"),
    ("pets", "likes", "INTEGER DEFAULT 0"),
    ("pets", "dress_hat", "TEXT"),
    ("pets", "dress_clothes", "TEXT"),
    ("pets", "dress_accessory", "TEXT"),
    ("pets", "dress_background", "TEXT"),
    ("group_configs", "sensitive_words", "TEXT DEFAULT '[]'"),
    ("tasks", "created_date", "TEXT"),
    ("users", "today_free_feed_count", "INTEGER DEFAULT 0"),
    ("users", "today_message_count", "INTEGER DEFAULT 0"),
    ("users", "total_free_feed_count", "INTEGER DEFAULT 0"),
    ("users", "total_message_count", "INTEGER DEFAULT 0"),
    ("users", "version", "INTEGER NOT NULL DEFAULT 0"),
    ("pets", "version", "INTEGER NOT NULL DEFAULT 0"),
    ("inventories", "version", "INTEGER NOT NULL DEFAULT 0"),
    ("trade_listings", "status", "TEXT DEFAULT 'active'"),
    ("pet_shows", "status", "TEXT DEFAULT 'active'"),
    ("pet_shows", "settled_at", "TEXT"),
    ("scheduler_runs", "status", "TEXT NOT NULL DEFAULT 'completed'"),
    ("scheduler_runs", "lease_until", "TEXT"),
    ("scheduler_runs", "attempt_count", "INTEGER NOT NULL DEFAULT 1"),
    ("scheduler_runs", "completed_at", "TEXT"),
)


class Database:
    """QingPet 数据库服务层。

    负责表结构迁移、索引、缓存和领域数据持久化；涉及配额、资产或多张表的状态变更由本类
    在同一 SQLite 事务中提交，避免服务层分步写入造成部分成功。
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._local = threading.local()
        self._connections_lock = threading.Lock()
        self._all_connections: dict[int, tuple[int, sqlite3.Connection]] = {}
        path = Path(db_path)
        if path.exists() and path.stat().st_size > 0:
            backup = path.with_suffix(path.suffix + ".pre-migration.bak")
            if not backup.exists():
                shutil.copy2(path, backup)
        self._init_database()

    def _get_connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            # 查询与事务不跨线程共享；关闭时由生命周期线程统一处理登记连接。
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
            with self._connections_lock:
                self._all_connections[id(conn)] = (threading.get_ident(), conn)
        return conn

    def cleanup(self) -> None:
        with self._connections_lock:
            connections = list(self._all_connections.values())
            self._all_connections.clear()

        failures: list[sqlite3.Error] = []
        for thread_id, conn in connections:
            try:
                conn.close()
            except sqlite3.Error as exc:
                logger.error(
                    "Failed to close QingPet SQLite connection thread=%s error_type=%s",
                    thread_id,
                    type(exc).__name__,
                )
                failures.append(exc)
        if hasattr(self._local, "conn"):
            self._local.conn = None
        if failures:
            raise RuntimeError(
                f"Failed to close {len(failures)} QingPet SQLite connection(s)"
            ) from failures[0]

    # ──────────────────── 初始化 ────────────────────

    @staticmethod
    def _create_schema_migration_table(cursor: sqlite3.Cursor) -> None:
        cursor.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
        )""")

    @staticmethod
    def _create_identity_schema(cursor: sqlite3.Cursor) -> None:
        """创建用户、宠物、背包和群配置等权威数据表。"""

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
                coins INTEGER DEFAULT 100, friendship_points INTEGER DEFAULT 0,
                today_coins_earned INTEGER DEFAULT 0,
                today_feed_count INTEGER DEFAULT 0, today_clean_count INTEGER DEFAULT 0,
                today_play_count INTEGER DEFAULT 0, today_train_count INTEGER DEFAULT 0,
                today_explore_count INTEGER DEFAULT 0,
                today_visit_count INTEGER DEFAULT 0, today_gift_count INTEGER DEFAULT 0,
                total_feed_count INTEGER DEFAULT 0, total_clean_count INTEGER DEFAULT 0,
                total_play_count INTEGER DEFAULT 0, total_train_count INTEGER DEFAULT 0,
                total_explore_count INTEGER DEFAULT 0,
                total_visit_count INTEGER DEFAULT 0, total_gift_count INTEGER DEFAULT 0,
                last_visit_time TEXT, last_gift_time TEXT,
                trustee_until TEXT, is_banned BOOLEAN DEFAULT 0, ban_until TEXT,
                titles TEXT DEFAULT '[]', created_at TEXT, last_active TEXT,
                version INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, group_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
                name TEXT NOT NULL, stage TEXT NOT NULL, form TEXT DEFAULT '普通',
                hunger INTEGER DEFAULT 100, mood INTEGER DEFAULT 100,
                clean INTEGER DEFAULT 100, energy INTEGER DEFAULT 100,
                health INTEGER DEFAULT 100, age INTEGER DEFAULT 0,
                experience INTEGER DEFAULT 0, intimacy INTEGER DEFAULT 0,
                personality TEXT DEFAULT '活泼', favorite_food TEXT,
                status TEXT DEFAULT '正常', status_expire_time TEXT,
                dress_hat TEXT, dress_clothes TEXT, dress_accessory TEXT,
                dress_background TEXT,
                last_update TEXT, last_feed TEXT, last_clean TEXT,
                last_play TEXT, last_train TEXT, last_explore TEXT,
                likes INTEGER DEFAULT 0, created_at TEXT,
                version INTEGER NOT NULL DEFAULT 0,
                UNIQUE (user_id, group_id)
            )
        """)
        cursor.execute("""CREATE TABLE IF NOT EXISTS inventories (
            user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
            items TEXT, version INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, group_id))""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS group_configs (
            group_id INTEGER PRIMARY KEY, enabled BOOLEAN DEFAULT 1,
            economy_multiplier REAL DEFAULT 1.0, decay_multiplier REAL DEFAULT 1.0,
            trade_enabled BOOLEAN DEFAULT 0, natural_trigger_enabled BOOLEAN DEFAULT 0,
            activity_enabled BOOLEAN DEFAULT 1, sensitive_words TEXT DEFAULT '[]')""")

    @staticmethod
    def _create_interaction_schema(cursor: sqlite3.Cursor) -> None:
        """创建活动、任务、留言、展示会、配额和调度状态表。"""

        cursor.execute("""CREATE TABLE IF NOT EXISTS operation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL,
            user_id TEXT NOT NULL, target_user_id TEXT,
            operation_type TEXT NOT NULL, params TEXT,
            result TEXT DEFAULT 'success', created_at TEXT)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL,
            activity_type TEXT NOT NULL, title TEXT DEFAULT '',
            description TEXT DEFAULT '', target_value INTEGER NOT NULL,
            current_value INTEGER DEFAULT 0, reward_coins INTEGER DEFAULT 0,
            reward_items TEXT DEFAULT '{}', start_time TEXT, end_time TEXT,
            is_active BOOLEAN DEFAULT 0)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS tasks (
            user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
            task_type TEXT NOT NULL, target_value INTEGER NOT NULL,
            current_value INTEGER DEFAULT 0, reward_coins INTEGER DEFAULT 0,
            claimed BOOLEAN DEFAULT 0, created_date TEXT,
            created_at TEXT, PRIMARY KEY (user_id, group_id, task_type, created_date))""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS message_board (
            id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL,
            from_user_id TEXT NOT NULL, to_user_id TEXT NOT NULL,
            message TEXT NOT NULL, created_at TEXT)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS pet_shows (
            id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL,
            title TEXT DEFAULT '宠物展示会',
            start_time TEXT, end_time TEXT, is_active BOOLEAN DEFAULT 0)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS pet_show_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            show_id INTEGER NOT NULL, voter_user_id TEXT NOT NULL,
            pet_user_id TEXT NOT NULL, created_at TEXT)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS command_timestamps (
            user_id TEXT NOT NULL, group_id INTEGER NOT NULL, timestamp REAL NOT NULL)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS minigame_cooldowns (
            user_id TEXT NOT NULL, group_id INTEGER NOT NULL, game_type TEXT NOT NULL,
            available_at REAL NOT NULL,
            PRIMARY KEY (user_id, group_id, game_type)
        )""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS action_quotas (
            user_id TEXT NOT NULL, group_id INTEGER NOT NULL, action TEXT NOT NULL,
            period_date TEXT NOT NULL, action_count INTEGER NOT NULL DEFAULT 0,
            available_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, group_id, action, period_date)
        )""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS scheduler_runs (
            job_name TEXT NOT NULL, period_key TEXT NOT NULL,
            claimed_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running', lease_until TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 1, completed_at TEXT,
            PRIMARY KEY (job_name, period_key)
        )""")

    @staticmethod
    def _sync_daily_coin_cap_trigger(cursor: sqlite3.Cursor) -> None:
        """重建每日金币触发器，使部署后的上限配置立即成为数据库约束。"""

        cursor.execute("DROP TRIGGER IF EXISTS trg_users_daily_coin_cap")
        cursor.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_users_daily_coin_cap
            AFTER UPDATE OF today_coins_earned ON users
            WHEN NEW.today_coins_earned > {_DAILY_COIN_LIMIT}
            BEGIN
                UPDATE users SET
                    coins = MAX(0, NEW.coins - (NEW.today_coins_earned - {_DAILY_COIN_LIMIT})),
                    today_coins_earned = {_DAILY_COIN_LIMIT}
                WHERE user_id = NEW.user_id AND group_id = NEW.group_id;
            END""")

    @staticmethod
    def _create_settlement_schema(cursor: sqlite3.Cursor) -> None:
        """创建幂等结算、资产账本、奖励领取及余额约束结构。"""

        cursor.execute("""CREATE TABLE IF NOT EXISTS asset_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
            asset_type TEXT NOT NULL, delta INTEGER NOT NULL,
            reason TEXT NOT NULL, reference_id TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (asset_type, reference_id)
        )""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS asset_reconciliation_checkpoints (
            group_id INTEGER NOT NULL, asset_type TEXT NOT NULL,
            balance INTEGER NOT NULL, ledger_total INTEGER NOT NULL,
            checked_at TEXT NOT NULL,
            PRIMARY KEY (group_id, asset_type)
        )""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS visit_settlements (
            reference_id TEXT PRIMARY KEY,
            visitor_user_id TEXT NOT NULL, target_user_id TEXT NOT NULL,
            group_id INTEGER NOT NULL, pet_name TEXT NOT NULL,
            visitor_grant INTEGER NOT NULL, target_grant INTEGER NOT NULL,
            intimacy_grant INTEGER NOT NULL, created_at TEXT NOT NULL
        )""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS minigame_settlements (
            reference_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
            game_type TEXT NOT NULL, opponent_user_id TEXT NOT NULL DEFAULT '',
            pet_name TEXT NOT NULL, opponent_pet_name TEXT NOT NULL DEFAULT '',
            coin_grant INTEGER NOT NULL, experience_grant INTEGER NOT NULL,
            energy_cost INTEGER NOT NULL, outcome_payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS group_task_claims (
            group_id INTEGER NOT NULL, task_type TEXT NOT NULL,
            created_date TEXT NOT NULL, user_id TEXT NOT NULL,
            claimed_at TEXT NOT NULL,
            PRIMARY KEY (group_id, task_type, created_date, user_id)
        )""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS activity_claims (
            activity_id INTEGER NOT NULL, user_id TEXT NOT NULL,
            claimed_at TEXT NOT NULL,
            PRIMARY KEY (activity_id, user_id)
        )""")
        Database._sync_daily_coin_cap_trigger(cursor)
        cursor.execute("""CREATE TRIGGER IF NOT EXISTS trg_users_nonnegative_insert
            BEFORE INSERT ON users WHEN NEW.coins < 0 OR NEW.friendship_points < 0
            BEGIN SELECT RAISE(ABORT, 'negative asset balance'); END""")
        cursor.execute("""CREATE TRIGGER IF NOT EXISTS trg_users_nonnegative_update
            BEFORE UPDATE OF coins, friendship_points ON users
            WHEN NEW.coins < 0 OR NEW.friendship_points < 0
            BEGIN SELECT RAISE(ABORT, 'negative asset balance'); END""")

    def _create_market_schema(self, cursor: sqlite3.Cursor) -> None:
        """创建交易、每日群任务和装扮所有权表。"""

        cursor.execute("""CREATE TABLE IF NOT EXISTS trade_listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
            item_id TEXT NOT NULL, amount INTEGER DEFAULT 1,
            price INTEGER NOT NULL, created_at TEXT, expires_at TEXT,
            is_active BOOLEAN DEFAULT 1, status TEXT DEFAULT 'active')""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS group_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL, task_type TEXT NOT NULL,
            target_value INTEGER NOT NULL, current_value INTEGER DEFAULT 0,
            reward_coins INTEGER DEFAULT 0, description TEXT DEFAULT '',
            created_date TEXT NOT NULL, is_completed BOOLEAN DEFAULT 0,
            UNIQUE (group_id, task_type, created_date))""")
        self._migrate_group_tasks_table(cursor)
        cursor.execute("""CREATE TABLE IF NOT EXISTS dress_inventory (
            user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
            dress_item_id TEXT NOT NULL,
            PRIMARY KEY (user_id, group_id, dress_item_id))""")

    def _apply_legacy_column_migrations(self, cursor: sqlite3.Cursor) -> None:
        """按既定顺序为历史数据库补齐缺失字段。"""

        for table, column, column_type in _LEGACY_COLUMN_MIGRATIONS:
            self._safe_add_column(cursor, table, column, column_type)

    @staticmethod
    def _create_auxiliary_schema(cursor: sqlite3.Cursor) -> None:
        cursor.execute("""CREATE TABLE IF NOT EXISTS daily_likes (
            user_id TEXT NOT NULL, target_user_id TEXT NOT NULL,
            group_id INTEGER NOT NULL, like_date TEXT NOT NULL,
            like_count INTEGER DEFAULT 1,
            PRIMARY KEY (user_id, target_user_id, group_id, like_date))""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS title_expiry (
            user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
            title TEXT NOT NULL, expires_at TEXT NOT NULL,
            PRIMARY KEY (user_id, group_id, title))""")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_likes "
            "ON daily_likes(user_id, target_user_id, group_id, like_date)"
        )

    def _repair_pet_show_schema_and_state(self, cursor: sqlite3.Cursor) -> None:
        """升级旧投票键，并保证每个群最多存在一个进行中的展示会。"""

        self._migrate_pet_show_votes_table(cursor)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_pet_show_votes_show_voter "
            "ON pet_show_votes(show_id, voter_user_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_pet_show_votes_show_pet "
            "ON pet_show_votes(show_id, pet_user_id)"
        )
        cursor.execute(
            """UPDATE pet_shows SET is_active = 0, status = 'superseded'
               WHERE is_active = 1 AND EXISTS (
                   SELECT 1 FROM pet_shows AS newer
                   WHERE newer.group_id = pet_shows.group_id
                     AND newer.is_active = 1 AND newer.id > pet_shows.id
               )"""
        )
        cursor.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS uq_pet_shows_one_active_group
               ON pet_shows(group_id) WHERE is_active = 1"""
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_trade_expiry "
            "ON trade_listings(is_active, expires_at, group_id)"
        )

    @staticmethod
    def _record_schema_version(cursor: sqlite3.Cursor) -> None:
        for version in range(1, _CURRENT_SCHEMA_VERSION + 1):
            cursor.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, utc_now().isoformat()),
            )
        cursor.execute(f"PRAGMA user_version={_CURRENT_SCHEMA_VERSION}")

    def _init_database(self) -> None:
        """初始化或升级全部模式子系统，最后统一提交。"""

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            self._create_schema_migration_table(cursor)
            self._create_identity_schema(cursor)
            self._create_interaction_schema(cursor)
            self._create_settlement_schema(cursor)
            self._create_market_schema(cursor)
            self._apply_legacy_column_migrations(cursor)
            self._create_indexes(cursor)
            self._create_auxiliary_schema(cursor)
            self._repair_pet_show_schema_and_state(cursor)
            self._record_schema_version(cursor)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _create_indexes(self, cursor: sqlite3.Cursor) -> None:
        """在所有兼容迁移完成后创建常用查询索引。"""
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_cmd_ts_user ON command_timestamps(user_id, group_id, timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_cmd_ts_group ON command_timestamps(group_id, timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_op_logs ON operation_logs(group_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_date ON tasks(user_id, group_id, created_date)",
            "CREATE INDEX IF NOT EXISTS idx_pets_group ON pets(group_id)",
            "CREATE INDEX IF NOT EXISTS idx_trade_group ON trade_listings(group_id, is_active)",
            "CREATE INDEX IF NOT EXISTS idx_shows_group ON pet_shows(group_id, is_active)",
            "CREATE INDEX IF NOT EXISTS idx_group_tasks ON group_tasks(group_id, created_date)",
        ]
        for idx_sql in indexes:
            cursor.execute(idx_sql)

    @staticmethod
    def _migrate_pet_show_votes_table(cursor: sqlite3.Cursor) -> None:
        row = cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'pet_show_votes'"
        ).fetchone()
        sql_text = str(row["sql"] or "") if row else ""
        if "PRIMARY KEY (show_id, voter_user_id)" not in sql_text:
            return

        cursor.execute("ALTER TABLE pet_show_votes RENAME TO pet_show_votes_old")
        cursor.execute("""CREATE TABLE pet_show_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            show_id INTEGER NOT NULL,
            voter_user_id TEXT NOT NULL,
            pet_user_id TEXT NOT NULL,
            created_at TEXT)""")
        cursor.execute(
            """INSERT INTO pet_show_votes (show_id, voter_user_id, pet_user_id, created_at)
               SELECT show_id, voter_user_id, pet_user_id, created_at
               FROM pet_show_votes_old"""
        )
        cursor.execute("DROP TABLE pet_show_votes_old")

    @staticmethod
    def _migrate_group_tasks_table(cursor: sqlite3.Cursor) -> None:
        """合并旧版重复群任务，并建立每日任务唯一键。"""
        table_info = cursor.execute("PRAGMA table_info(group_tasks)").fetchall()
        created_date_not_null = any(
            row["name"] == "created_date" and int(row["notnull"]) == 1 for row in table_info
        )
        has_unique_key = False
        for index in cursor.execute("PRAGMA index_list(group_tasks)").fetchall():
            if int(index["unique"]) != 1:
                continue
            index_name = str(index["name"]).replace('"', '""')
            columns = [
                str(row["name"])
                for row in cursor.execute(f'PRAGMA index_info("{index_name}")').fetchall()
            ]
            if columns == ["group_id", "task_type", "created_date"]:
                has_unique_key = True
                break
        if created_date_not_null and has_unique_key:
            return

        cursor.execute("DROP TABLE IF EXISTS group_tasks_v2")
        cursor.execute("""
            CREATE TABLE group_tasks_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                task_type TEXT NOT NULL,
                target_value INTEGER NOT NULL,
                current_value INTEGER DEFAULT 0,
                reward_coins INTEGER DEFAULT 0,
                description TEXT DEFAULT '',
                created_date TEXT NOT NULL,
                is_completed BOOLEAN DEFAULT 0,
                UNIQUE (group_id, task_type, created_date)
            )
        """)
        cursor.execute("""
            INSERT INTO group_tasks_v2
                (id, group_id, task_type, target_value, current_value,
                 reward_coins, description, created_date, is_completed)
            SELECT
                keep_id,
                group_id,
                task_type,
                target_value,
                MIN(current_value, target_value),
                reward_coins,
                description,
                created_date,
                CASE
                    WHEN was_completed = 1 OR current_value >= target_value THEN 1
                    ELSE 0
                END
            FROM (
                SELECT
                    MIN(id) AS keep_id,
                    group_id,
                    task_type,
                    MAX(COALESCE(target_value, 0)) AS target_value,
                    MAX(COALESCE(current_value, 0)) AS current_value,
                    MAX(COALESCE(reward_coins, 0)) AS reward_coins,
                    MAX(COALESCE(description, '')) AS description,
                    COALESCE(created_date, '1970-01-01') AS created_date,
                    MAX(COALESCE(is_completed, 0)) AS was_completed
                FROM group_tasks
                GROUP BY group_id, task_type, COALESCE(created_date, '1970-01-01')
            ) AS merged
        """)
        cursor.execute("DROP TABLE group_tasks")
        cursor.execute("ALTER TABLE group_tasks_v2 RENAME TO group_tasks")

    @staticmethod
    def _safe_add_column(
        cursor: sqlite3.Cursor,
        table: str,
        column: str,
        col_type: str,
    ) -> None:
        if not _IDENTIFIER_RE.match(table) or not _IDENTIFIER_RE.match(column):
            logger.warning("Skip adding column with invalid identifier: %s.%s", table, column)
            return

        expected_type = _ALLOWED_SCHEMA_COLUMNS.get(table, {}).get(column)
        if expected_type is None:
            logger.warning("Skip adding unsupported schema column: %s.%s", table, column)
            return

        if col_type != expected_type:
            logger.warning(
                "Skip adding column with mismatched type: %s.%s expected=%s actual=%s",
                table,
                column,
                expected_type,
                col_type,
            )
            return

        existing_columns = {
            str(row["name"]) for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column in existing_columns:
            return
        sql = "ALTER TABLE " + table + " ADD COLUMN " + column + " " + col_type
        cursor.execute(sql)

    # ──────────────────── Row → Object 映射 ────────────────────

    @staticmethod
    def _parse_personality(raw: str) -> PetPersonality:
        """兼容持久化数据中的中文枚举值与英文枚举名。"""
        try:
            return PetPersonality(raw)
        except ValueError:
            try:
                return PetPersonality[raw]
            except KeyError:
                return PetPersonality.LIVELY

    @staticmethod
    def _parse_status(raw: str) -> PetStatus:
        """兼容持久化数据中的中文枚举值与英文枚举名。"""
        try:
            return PetStatus(raw)
        except ValueError:
            try:
                return PetStatus[raw]
            except KeyError:
                return PetStatus.NORMAL

    @staticmethod
    def _row_to_pet(row: sqlite3.Row) -> Pet:
        keys = row.keys()
        pet = Pet(
            id=row["id"],
            user_id=row["user_id"],
            group_id=row["group_id"],
            name=row["name"],
            stage=PetStage(row["stage"]),
            form=row["form"],
            hunger=row["hunger"],
            mood=row["mood"],
            clean=row["clean"],
            energy=row["energy"],
            health=row["health"],
            age=row["age"],
            experience=row["experience"],
            intimacy=row["intimacy"],
            personality=Database._parse_personality(row["personality"]),
            favorite_food=row["favorite_food"],
            status=Database._parse_status(row["status"]),
            status_expire_time=datetime.fromisoformat(row["status_expire_time"])
            if row["status_expire_time"]
            else None,
            dress_hat=row["dress_hat"] if "dress_hat" in keys else None,
            dress_clothes=row["dress_clothes"] if "dress_clothes" in keys else None,
            dress_accessory=row["dress_accessory"] if "dress_accessory" in keys else None,
            dress_background=row["dress_background"] if "dress_background" in keys else None,
            last_update=datetime.fromisoformat(row["last_update"])
            if row["last_update"]
            else utc_now(),
            last_feed=datetime.fromisoformat(row["last_feed"]) if row["last_feed"] else None,
            last_clean=datetime.fromisoformat(row["last_clean"]) if row["last_clean"] else None,
            last_play=datetime.fromisoformat(row["last_play"]) if row["last_play"] else None,
            last_train=datetime.fromisoformat(row["last_train"]) if row["last_train"] else None,
            last_explore=datetime.fromisoformat(row["last_explore"])
            if row["last_explore"]
            else None,
            likes=row["likes"] if "likes" in keys else 0,
            version=int(row["version"]) if "version" in keys else 0,
            created_at=datetime.fromisoformat(row["created_at"])
            if row["created_at"]
            else utc_now(),
        )
        pet.mark_persisted()
        return pet

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> User:
        """从兼容新旧字段集合的数据库行构造用户对象。"""
        keys = row.keys()
        user = User(
            user_id=row["user_id"],
            group_id=row["group_id"],
            coins=row["coins"],
            friendship_points=row["friendship_points"],
            today_coins_earned=row["today_coins_earned"],
            today_feed_count=row["today_feed_count"],
            today_clean_count=row["today_clean_count"],
            today_play_count=row["today_play_count"],
            today_train_count=row["today_train_count"],
            today_explore_count=row["today_explore_count"],
            today_visit_count=row["today_visit_count"],
            today_gift_count=row["today_gift_count"],
            today_free_feed_count=row["today_free_feed_count"]
            if "today_free_feed_count" in keys
            else 0,
            today_message_count=row["today_message_count"] if "today_message_count" in keys else 0,
            total_feed_count=row["total_feed_count"] if "total_feed_count" in keys else 0,
            total_clean_count=row["total_clean_count"] if "total_clean_count" in keys else 0,
            total_play_count=row["total_play_count"] if "total_play_count" in keys else 0,
            total_train_count=row["total_train_count"] if "total_train_count" in keys else 0,
            total_explore_count=row["total_explore_count"] if "total_explore_count" in keys else 0,
            total_visit_count=row["total_visit_count"] if "total_visit_count" in keys else 0,
            total_gift_count=row["total_gift_count"] if "total_gift_count" in keys else 0,
            total_free_feed_count=row["total_free_feed_count"]
            if "total_free_feed_count" in keys
            else 0,
            total_message_count=row["total_message_count"] if "total_message_count" in keys else 0,
            titles=json.loads(row["titles"]) if "titles" in keys and row["titles"] else [],
            last_visit_time=datetime.fromisoformat(row["last_visit_time"])
            if row["last_visit_time"]
            else None,
            last_gift_time=datetime.fromisoformat(row["last_gift_time"])
            if row["last_gift_time"]
            else None,
            trustee_until=datetime.fromisoformat(row["trustee_until"])
            if row["trustee_until"]
            else None,
            is_banned=bool(row["is_banned"]),
            ban_until=datetime.fromisoformat(row["ban_until"]) if row["ban_until"] else None,
            created_at=datetime.fromisoformat(row["created_at"])
            if row["created_at"]
            else utc_now(),
            last_active=datetime.fromisoformat(row["last_active"])
            if row["last_active"]
            else utc_now(),
            version=int(row["version"]) if "version" in keys else 0,
        )
        user.mark_persisted()
        return user

    @staticmethod
    def _write_pet_in_transaction(conn: sqlite3.Connection, pet: Pet) -> bool:
        cursor = conn.execute(
            """UPDATE pets SET
               name = ?, stage = ?, form = ?,
               hunger = ?, mood = ?, clean = ?, energy = ?, health = ?,
               age = ?, experience = ?, intimacy = ?, personality = ?, favorite_food = ?,
               status = ?, status_expire_time = ?,
               dress_hat = ?, dress_clothes = ?, dress_accessory = ?, dress_background = ?,
               last_update = ?, last_feed = ?, last_clean = ?, last_play = ?,
               last_train = ?, last_explore = ?, likes = ?, version = version + 1
               WHERE id = ? AND version = ?""",
            (
                pet.name,
                pet.stage.value,
                pet.form,
                pet.hunger,
                pet.mood,
                pet.clean,
                pet.energy,
                pet.health,
                pet.age,
                pet.experience,
                pet.intimacy,
                pet.personality.value,
                pet.favorite_food,
                pet.status.value,
                pet.status_expire_time.isoformat() if pet.status_expire_time else None,
                pet.dress_hat,
                pet.dress_clothes,
                pet.dress_accessory,
                pet.dress_background,
                pet.last_update.isoformat(),
                pet.last_feed.isoformat() if pet.last_feed else None,
                pet.last_clean.isoformat() if pet.last_clean else None,
                pet.last_play.isoformat() if pet.last_play else None,
                pet.last_train.isoformat() if pet.last_train else None,
                pet.last_explore.isoformat() if pet.last_explore else None,
                pet.likes,
                pet.id,
                pet.version,
            ),
        )
        if cursor.rowcount == 1:
            pet.version += 1
            return True
        return False

    @staticmethod
    def _write_user_in_transaction(conn: sqlite3.Connection, user: User) -> bool:
        cursor = conn.execute(
            """UPDATE users SET
               coins = ?, friendship_points = ?,
               today_coins_earned = ?, today_feed_count = ?, today_clean_count = ?,
               today_play_count = ?, today_train_count = ?, today_explore_count = ?,
               today_visit_count = ?, today_gift_count = ?, today_free_feed_count = ?,
               today_message_count = ?, total_feed_count = ?, total_clean_count = ?,
               total_play_count = ?, total_train_count = ?, total_explore_count = ?,
               total_visit_count = ?, total_gift_count = ?, total_free_feed_count = ?,
               total_message_count = ?, titles = ?, last_visit_time = ?, last_gift_time = ?,
               trustee_until = ?, is_banned = ?, ban_until = ?, last_active = ?,
               version = version + 1
               WHERE user_id = ? AND group_id = ? AND version = ?""",
            (
                user.coins,
                user.friendship_points,
                user.today_coins_earned,
                user.today_feed_count,
                user.today_clean_count,
                user.today_play_count,
                user.today_train_count,
                user.today_explore_count,
                user.today_visit_count,
                user.today_gift_count,
                user.today_free_feed_count,
                user.today_message_count,
                user.total_feed_count,
                user.total_clean_count,
                user.total_play_count,
                user.total_train_count,
                user.total_explore_count,
                user.total_visit_count,
                user.total_gift_count,
                user.total_free_feed_count,
                user.total_message_count,
                json.dumps(user.titles),
                user.last_visit_time.isoformat() if user.last_visit_time else None,
                user.last_gift_time.isoformat() if user.last_gift_time else None,
                user.trustee_until.isoformat() if user.trustee_until else None,
                int(user.is_banned),
                user.ban_until.isoformat() if user.ban_until else None,
                user.last_active.isoformat(),
                user.user_id,
                user.group_id,
                user.version,
            ),
        )
        if cursor.rowcount == 1:
            user.version += 1
            return True
        return False

    @staticmethod
    def _record_asset_delta(
        conn: sqlite3.Connection,
        *,
        user_id: str,
        group_id: int,
        asset_type: str,
        delta: int,
        reason: str,
        reference_id: str | None = None,
    ) -> None:
        """在调用方事务中追加一条资产余额变更记录。"""
        if delta == 0:
            return
        conn.execute(
            """INSERT INTO asset_ledger
               (user_id, group_id, asset_type, delta, reason, reference_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                group_id,
                asset_type,
                delta,
                reason,
                reference_id,
                utc_now().isoformat(),
            ),
        )

    # ──────────────────── 用户数据 ────────────────────

    def create_user(self, user: User) -> bool:
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO users (
                    user_id, group_id, coins, friendship_points,
                    today_coins_earned, today_feed_count, today_clean_count,
                    today_play_count, today_train_count, today_explore_count,
                    today_visit_count, today_gift_count,
                    total_feed_count, total_clean_count, total_play_count,
                    total_train_count, total_explore_count, total_visit_count,
                    total_gift_count, titles,
                    last_visit_time, last_gift_time,
                    is_banned, created_at, last_active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    user.user_id,
                    user.group_id,
                    user.coins,
                    user.friendship_points,
                    user.today_coins_earned,
                    user.today_feed_count,
                    user.today_clean_count,
                    user.today_play_count,
                    user.today_train_count,
                    user.today_explore_count,
                    user.today_visit_count,
                    user.today_gift_count,
                    user.total_feed_count,
                    user.total_clean_count,
                    user.total_play_count,
                    user.total_train_count,
                    user.total_explore_count,
                    user.total_visit_count,
                    user.total_gift_count,
                    json.dumps(user.titles),
                    user.last_visit_time.isoformat() if user.last_visit_time else None,
                    user.last_gift_time.isoformat() if user.last_gift_time else None,
                    int(user.is_banned),
                    user.created_at.isoformat(),
                    user.last_active.isoformat(),
                ),
            )
            created = cursor.rowcount == 1
            if created:
                self._record_asset_delta(
                    conn,
                    user_id=user.user_id,
                    group_id=user.group_id,
                    asset_type="coins",
                    delta=int(user.coins),
                    reason="account_opening",
                    reference_id=f"account-opening:{user.group_id}:{user.user_id}",
                )
            conn.commit()
            if created:
                user.version = 0
                user.mark_persisted()
            return created
        except Exception as exc:
            conn.rollback()
            _log_database_failure("create_user", exc)
            return False

    def get_user(self, user_id: str, group_id: int) -> User | None:
        try:
            conn = self._get_connection()
            cursor = conn.execute(
                "SELECT * FROM users WHERE user_id = ? AND group_id = ?", (user_id, group_id)
            )
            row = cursor.fetchone()
            return self._row_to_user(row) if row else None
        except Exception as exc:
            _log_database_failure("get_user", exc)
            return None

    def update_user(self, user: User) -> bool:
        """把本地增量合并到最新用户行，并以版本号比较后提交。"""
        try:
            conn = self._get_connection()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ? AND group_id = ?",
                (user.user_id, user.group_id),
            ).fetchone()
            if row is None:
                conn.rollback()
                return False
            merged = user.merged_onto(self._row_to_user(row))
            cursor = conn.execute(
                """
                UPDATE users SET
                    coins = ?, friendship_points = ?,
                    today_coins_earned = ?, today_feed_count = ?, today_clean_count = ?,
                    today_play_count = ?, today_train_count = ?, today_explore_count = ?,
                    today_visit_count = ?, today_gift_count = ?, today_free_feed_count = ?, today_message_count = ?,
                    total_feed_count = ?, total_clean_count = ?, total_play_count = ?,
                    total_train_count = ?, total_explore_count = ?, total_visit_count = ?,
                    total_gift_count = ?, total_free_feed_count = ?, total_message_count = ?, titles = ?,
                    last_visit_time = ?, last_gift_time = ?,
                    trustee_until = ?, is_banned = ?, ban_until = ?, last_active = ?,
                    version = version + 1
                WHERE user_id = ? AND group_id = ? AND version = ?
            """,
                (
                    merged.coins,
                    merged.friendship_points,
                    merged.today_coins_earned,
                    merged.today_feed_count,
                    merged.today_clean_count,
                    merged.today_play_count,
                    merged.today_train_count,
                    merged.today_explore_count,
                    merged.today_visit_count,
                    merged.today_gift_count,
                    merged.today_free_feed_count,
                    merged.today_message_count,
                    merged.total_feed_count,
                    merged.total_clean_count,
                    merged.total_play_count,
                    merged.total_train_count,
                    merged.total_explore_count,
                    merged.total_visit_count,
                    merged.total_gift_count,
                    merged.total_free_feed_count,
                    merged.total_message_count,
                    json.dumps(merged.titles),
                    merged.last_visit_time.isoformat() if merged.last_visit_time else None,
                    merged.last_gift_time.isoformat() if merged.last_gift_time else None,
                    merged.trustee_until.isoformat() if merged.trustee_until else None,
                    int(merged.is_banned),
                    merged.ban_until.isoformat() if merged.ban_until else None,
                    merged.last_active.isoformat(),
                    merged.user_id,
                    merged.group_id,
                    merged.version,
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False
            stored_coins = int(
                conn.execute(
                    "SELECT coins FROM users WHERE user_id = ? AND group_id = ?",
                    (merged.user_id, merged.group_id),
                ).fetchone()["coins"]
            )
            self._record_asset_delta(
                conn,
                user_id=merged.user_id,
                group_id=merged.group_id,
                asset_type="coins",
                delta=stored_coins - int(row["coins"]),
                reason="user_update",
            )
            conn.commit()
            merged.coins = stored_coins
            merged.version += 1
            user.__dict__.update(merged.__dict__)
            user.mark_persisted()
            return True
        except Exception as exc:
            conn.rollback()
            _log_database_failure("update_user", exc)
            return False

    # ──────────────────── 宠物数据 ────────────────────

    def create_pet(self, pet: Pet) -> bool:
        try:
            conn = self._get_connection()
            cursor = conn.execute(
                """
                INSERT INTO pets (
                    user_id, group_id, name, stage, form,
                    hunger, mood, clean, energy, health,
                    age, experience, intimacy, personality, favorite_food,
                    status, status_expire_time,
                    dress_hat, dress_clothes, dress_accessory, dress_background,
                    last_update, likes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    pet.user_id,
                    pet.group_id,
                    pet.name,
                    pet.stage.value,
                    pet.form,
                    pet.hunger,
                    pet.mood,
                    pet.clean,
                    pet.energy,
                    pet.health,
                    pet.age,
                    pet.experience,
                    pet.intimacy,
                    pet.personality.value,
                    pet.favorite_food,
                    pet.status.value,
                    pet.status_expire_time.isoformat() if pet.status_expire_time else None,
                    pet.dress_hat,
                    pet.dress_clothes,
                    pet.dress_accessory,
                    pet.dress_background,
                    pet.last_update.isoformat(),
                    0,
                    pet.created_at.isoformat(),
                ),
            )
            pet_id = cursor.lastrowid
            if pet_id is None:
                raise RuntimeError("宠物写入后未返回主键")
            conn.commit()
            pet.id = pet_id
            pet.version = 0
            pet.mark_persisted()
            return True
        except Exception as exc:
            conn.rollback()
            _log_database_failure("create_pet", exc)
            return False

    def get_pet(self, user_id: str, group_id: int) -> Pet | None:
        try:
            conn = self._get_connection()
            cursor = conn.execute(
                "SELECT * FROM pets WHERE user_id = ? AND group_id = ?", (user_id, group_id)
            )
            row = cursor.fetchone()
            return self._row_to_pet(row) if row else None
        except Exception as exc:
            _log_database_failure("get_pet", exc)
            return None

    def update_pet(self, pet: Pet) -> bool:
        """把本地增量合并到最新宠物行，并以版本号比较后提交。"""
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM pets WHERE id = ?", (pet.id,)).fetchone()
            if row is None:
                conn.rollback()
                return False
            latest = self._row_to_pet(row)
            if latest.user_id != pet.user_id or latest.group_id != pet.group_id:
                conn.rollback()
                return False
            merged = pet.merged_onto(latest)
            if not self._write_pet_in_transaction(conn, merged):
                conn.rollback()
                return False
            conn.commit()
            pet.__dict__.update(merged.__dict__)
            pet.mark_persisted()
            return True
        except Exception as exc:
            conn.rollback()
            _log_database_failure("update_pet", exc)
            return False

    def get_all_pets(self) -> list[Pet]:
        try:
            conn = self._get_connection()
            cursor = conn.execute("SELECT * FROM pets")
            return [self._row_to_pet(row) for row in cursor.fetchall()]
        except Exception as exc:
            _log_database_failure("get_all_pets", exc)
            return []

    def get_enabled_group_decay_map(self) -> dict[int, float]:
        try:
            conn = self._get_connection()
            cursor = conn.execute(
                "SELECT group_id, decay_multiplier FROM group_configs WHERE enabled = 1"
            )
            return {
                int(row["group_id"]): float(row["decay_multiplier"]) for row in cursor.fetchall()
            }
        except Exception as exc:
            _log_database_failure("get_enabled_group_decay_map", exc)
            return {}

    def get_pets_by_user(self, user_id: str) -> list[Pet]:
        try:
            conn = self._get_connection()
            cursor = conn.execute(
                "SELECT * FROM pets WHERE user_id = ? ORDER BY group_id",
                (user_id,),
            )
            return [self._row_to_pet(row) for row in cursor.fetchall()]
        except Exception as exc:
            _log_database_failure("get_pets_by_user", exc)
            return []

    # ──────────────────── 背包数据 ────────────────────

    def get_or_create_inventory(self, user_id: str, group_id: int) -> Inventory:
        conn: sqlite3.Connection | None = None
        try:
            conn = self._get_connection()
            cursor = conn.execute(
                "SELECT items, version FROM inventories WHERE user_id = ? AND group_id = ?",
                (user_id, group_id),
            )
            row = cursor.fetchone()
            if row:
                items = json.loads(row["items"]) if row["items"] else {}
                inventory = Inventory(
                    user_id=user_id,
                    group_id=group_id,
                    items=items,
                    version=int(row["version"]),
                )
                inventory.mark_persisted()
                return inventory
            conn.execute(
                "INSERT INTO inventories (user_id, group_id, items, version) VALUES (?, ?, ?, 0)",
                (user_id, group_id, json.dumps({})),
            )
            conn.commit()
            inventory = Inventory(user_id=user_id, group_id=group_id, items={}, version=0)
            inventory.mark_persisted()
            return inventory
        except Exception as exc:
            if conn is not None:
                conn.rollback()
            _log_database_failure("get_or_create_inventory", exc)
            return Inventory(user_id=user_id, group_id=group_id, items={})

    @staticmethod
    def _load_inventory_items(
        conn: sqlite3.Connection,
        user_id: str,
        group_id: int,
    ) -> dict[str, int]:
        """在现有连接中读取背包字典；没有记录时返回空背包。"""
        row = conn.execute(
            "SELECT items FROM inventories WHERE user_id = ? AND group_id = ?",
            (user_id, group_id),
        ).fetchone()
        return json.loads(row["items"] or "{}") if row else {}

    @staticmethod
    def _save_inventory_items(
        conn: sqlite3.Connection,
        user_id: str,
        group_id: int,
        items: dict[str, int],
    ) -> None:
        """在调用方事务中写入背包，并推进乐观并发版本号。"""
        conn.execute(
            """INSERT INTO inventories (user_id, group_id, items, version) VALUES (?, ?, ?, 0)
               ON CONFLICT(user_id, group_id) DO UPDATE SET
                   items = excluded.items,
                   version = inventories.version + 1""",
            (user_id, group_id, json.dumps(items)),
        )

    # ──────────────────── Group Config ────────────────────

    def get_group_config(self, group_id: int) -> GroupConfig:
        conn: sqlite3.Connection | None = None
        try:
            conn = self._get_connection()
            cursor = conn.execute("SELECT * FROM group_configs WHERE group_id = ?", (group_id,))
            row = cursor.fetchone()
            if row:
                return self._parse_group_config_row(row, group_id)
            default = GroupConfig(group_id=group_id)
            conn.execute(
                """INSERT OR IGNORE INTO group_configs
                   (group_id, enabled, economy_multiplier, decay_multiplier,
                    trade_enabled, natural_trigger_enabled, activity_enabled, sensitive_words)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    group_id,
                    int(default.enabled),
                    default.economy_multiplier,
                    default.decay_multiplier,
                    int(default.trade_enabled),
                    int(default.natural_trigger_enabled),
                    int(default.activity_enabled),
                    json.dumps(default.sensitive_words),
                ),
            )
            conn.commit()
            return default
        except Exception as exc:
            if conn is not None:
                conn.rollback()
            _log_database_failure("get_group_config", exc)
            raise GroupConfigReadError(group_id) from None

    @staticmethod
    def _parse_group_config_row(row: sqlite3.Row, expected_group_id: int) -> GroupConfig:
        required_columns = {
            "group_id",
            "enabled",
            "economy_multiplier",
            "decay_multiplier",
            "trade_enabled",
            "natural_trigger_enabled",
            "activity_enabled",
            "sensitive_words",
        }
        if not required_columns.issubset(row.keys()):
            raise ValueError("group config row is missing required columns")
        if int(row["group_id"]) != int(expected_group_id):
            raise ValueError("group config row has a mismatched group id")

        boolean_values: dict[str, bool] = {}
        for column in (
            "enabled",
            "trade_enabled",
            "natural_trigger_enabled",
            "activity_enabled",
        ):
            value = row[column]
            if type(value) is not int or value not in (0, 1):
                raise ValueError(f"invalid boolean group config field: {column}")
            boolean_values[column] = bool(value)

        economy_multiplier = float(row["economy_multiplier"])
        decay_multiplier = float(row["decay_multiplier"])
        for name, value in (
            ("economy_multiplier", economy_multiplier),
            ("decay_multiplier", decay_multiplier),
        ):
            if not math.isfinite(value) or not 0.1 <= value <= 10.0:
                raise ValueError(f"invalid group config multiplier: {name}")

        sensitive_words = json.loads(row["sensitive_words"] or "[]")
        if (
            not isinstance(sensitive_words, list)
            or len(sensitive_words) > 100
            or any(
                not isinstance(word, str) or not word or len(word) > 64 for word in sensitive_words
            )
        ):
            raise ValueError("invalid group sensitive words")

        return GroupConfig(
            group_id=expected_group_id,
            enabled=boolean_values["enabled"],
            economy_multiplier=economy_multiplier,
            decay_multiplier=decay_multiplier,
            trade_enabled=boolean_values["trade_enabled"],
            natural_trigger_enabled=boolean_values["natural_trigger_enabled"],
            activity_enabled=boolean_values["activity_enabled"],
            sensitive_words=sensitive_words,
        )

    def update_group_config(self, config: GroupConfig) -> bool:
        conn: sqlite3.Connection | None = None
        try:
            conn = self._get_connection()
            conn.execute(
                """
                INSERT OR REPLACE INTO group_configs
                (group_id, enabled, economy_multiplier, decay_multiplier,
                 trade_enabled, natural_trigger_enabled, activity_enabled, sensitive_words)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    config.group_id,
                    int(config.enabled),
                    config.economy_multiplier,
                    config.decay_multiplier,
                    int(config.trade_enabled),
                    int(config.natural_trigger_enabled),
                    int(config.activity_enabled),
                    json.dumps(config.sensitive_words),
                ),
            )
            conn.commit()
            return True
        except Exception as exc:
            if conn is not None:
                conn.rollback()
            _log_database_failure("update_group_config", exc)
            return False

    # ──────────────────── Operation Logs ────────────────────

    @staticmethod
    def _insert_operation_log_in_transaction(
        conn: sqlite3.Connection,
        *,
        group_id: int,
        operator_user_id: str,
        operation_type: str,
        params: str = "",
        target_user_id: str | None = None,
        result: str = "success",
    ) -> None:
        conn.execute(
            """INSERT INTO operation_logs
               (group_id, user_id, target_user_id, operation_type, params, result, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                group_id,
                operator_user_id,
                target_user_id,
                operation_type,
                params,
                result,
                utc_now().isoformat(),
            ),
        )

    def log_operation(self, log: OperationLog) -> bool:
        conn: sqlite3.Connection | None = None
        try:
            conn = self._get_connection()
            conn.execute(
                """INSERT INTO operation_logs
                (group_id, user_id, target_user_id, operation_type, params, result, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    log.group_id,
                    log.user_id,
                    log.target_user_id,
                    log.operation_type,
                    log.params,
                    log.result,
                    log.created_at.isoformat(),
                ),
            )
            conn.commit()
            return True
        except Exception as exc:
            if conn is not None:
                conn.rollback()
            _log_database_failure("log_operation", exc)
            return False

    def get_operation_logs(self, group_id: int, limit: int = 50) -> list[OperationLog]:
        try:
            conn = self._get_connection()
            cursor = conn.execute(
                "SELECT * FROM operation_logs WHERE group_id = ? ORDER BY created_at DESC LIMIT ?",
                (group_id, limit),
            )
            return [
                OperationLog(
                    id=row["id"],
                    group_id=row["group_id"],
                    user_id=row["user_id"],
                    target_user_id=row["target_user_id"],
                    operation_type=row["operation_type"],
                    params=row["params"],
                    result=row["result"],
                    created_at=datetime.fromisoformat(row["created_at"])
                    if row["created_at"]
                    else utc_now(),
                )
                for row in cursor.fetchall()
            ]
        except Exception as exc:
            _log_database_failure("get_operation_logs", exc)
            return []

    def admin_reset_user_pet_atomic(
        self,
        user_id: str,
        group_id: int,
        operator_user_id: str,
    ) -> bool:
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            pet_cursor = conn.execute(
                """UPDATE pets SET
                       hunger = 100, mood = 100, clean = 100,
                       energy = 100, health = 100, stage = ?,
                       experience = 0, intimacy = 0, age = 0,
                       status = ?, status_expire_time = NULL,
                       last_update = ?, last_feed = NULL, last_clean = NULL,
                       last_play = NULL, last_train = NULL, last_explore = NULL,
                       version = version + 1
                   WHERE user_id = ? AND group_id = ?""",
                (
                    PetStage.EGG.value,
                    PetStatus.NORMAL.value,
                    utc_now().isoformat(),
                    user_id,
                    group_id,
                ),
            )
            user_cursor = conn.execute(
                """UPDATE users SET
                       last_visit_time = NULL, last_gift_time = NULL,
                       version = version + 1
                   WHERE user_id = ? AND group_id = ?""",
                (user_id, group_id),
            )
            if pet_cursor.rowcount != 1 or user_cursor.rowcount != 1:
                conn.rollback()
                return False
            self._insert_operation_log_in_transaction(
                conn,
                group_id=group_id,
                operator_user_id=operator_user_id,
                operation_type="RESET",
                params=f"reset user {user_id}",
                target_user_id=user_id,
            )
            conn.commit()
            return True
        except Exception as exc:
            conn.rollback()
            _log_database_failure("admin_reset_user_pet_atomic", exc)
            return False

    def admin_set_ban_atomic(
        self,
        user_id: str,
        group_id: int,
        operator_user_id: str,
        *,
        days: int | None,
    ) -> bool:
        if days is not None and days <= 0:
            return False
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            is_banned = days is not None
            ban_until = (utc_now() + timedelta(days=days)).isoformat() if days is not None else None
            cursor = conn.execute(
                """UPDATE users SET
                       is_banned = ?, ban_until = ?, version = version + 1
                   WHERE user_id = ? AND group_id = ?""",
                (int(is_banned), ban_until, user_id, group_id),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False
            operation_type = "BAN" if is_banned else "UNBAN"
            params = f"ban {days} days" if days is not None else "unban"
            self._insert_operation_log_in_transaction(
                conn,
                group_id=group_id,
                operator_user_id=operator_user_id,
                operation_type=operation_type,
                params=params,
                target_user_id=user_id,
            )
            conn.commit()
            return True
        except Exception as exc:
            conn.rollback()
            _log_database_failure("admin_set_ban_atomic", exc)
            return False

    def admin_delete_pet_atomic(
        self,
        user_id: str,
        group_id: int,
        operator_user_id: str,
    ) -> bool:
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "DELETE FROM pets WHERE user_id = ? AND group_id = ?",
                (user_id, group_id),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False
            self._insert_operation_log_in_transaction(
                conn,
                group_id=group_id,
                operator_user_id=operator_user_id,
                operation_type="DELETE",
                params=f"delete pet for user {user_id}",
                target_user_id=user_id,
            )
            conn.commit()
            return True
        except Exception as exc:
            conn.rollback()
            _log_database_failure("admin_delete_pet_atomic", exc)
            return False

    # ──────────────────── Tasks（使用日期范围查询）──────────────────

    @staticmethod
    def _ensure_daily_task_templates(
        conn: sqlite3.Connection,
        user_id: str,
        group_id: int,
        *,
        today: str,
        now_iso: str,
    ) -> None:
        """确保用户当天的四类个人任务均已建立。"""
        for task_type, target, reward in _DAILY_TASK_TEMPLATES:
            conn.execute(
                """INSERT OR IGNORE INTO tasks
                   (user_id, group_id, task_type, target_value, current_value,
                    reward_coins, claimed, created_date, created_at)
                   VALUES (?, ?, ?, ?, 0, ?, 0, ?, ?)""",
                (user_id, group_id, task_type, target, reward, today, now_iso),
            )

    def get_or_create_daily_tasks(self, user_id: str, group_id: int) -> list[dict]:
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now()
            today = now.strftime("%Y-%m-%d")
            self._ensure_daily_task_templates(
                conn,
                user_id,
                group_id,
                today=today,
                now_iso=now.isoformat(),
            )
            rows = conn.execute(
                "SELECT * FROM tasks WHERE user_id = ? AND group_id = ? AND created_date = ?",
                (user_id, group_id, today),
            ).fetchall()
            conn.commit()
            return [dict(row) for row in rows]
        except Exception as exc:
            conn.rollback()
            _log_database_failure("get_or_create_daily_tasks", exc)
            return []

    def claim_task_reward(self, user_id: str, group_id: int, task_type: str) -> int | None:
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            today = utc_now().strftime("%Y-%m-%d")
            row = conn.execute(
                """SELECT reward_coins FROM tasks
                WHERE user_id = ? AND group_id = ? AND task_type = ?
                AND created_date = ? AND claimed = 0 AND current_value >= target_value""",
                (user_id, group_id, task_type, today),
            ).fetchone()
            if not row:
                conn.rollback()
                return None
            claim = conn.execute(
                """UPDATE tasks SET claimed = 1
                WHERE user_id = ? AND group_id = ? AND task_type = ?
                  AND created_date = ? AND claimed = 0""",
                (user_id, group_id, task_type, today),
            )
            if claim.rowcount != 1:
                conn.rollback()
                return None
            reward = self._credit_coins_in_transaction(
                conn,
                user_id,
                group_id,
                int(row["reward_coins"] or 0),
                reason="daily_task",
                reference_id=f"daily-task:{today}:{group_id}:{task_type}:{user_id}",
                daily_limit=_DAILY_COIN_LIMIT,
                now_iso=utc_now().isoformat(),
                record_zero=True,
            )
            conn.commit()
            return reward
        except Exception as exc:
            conn.rollback()
            _log_database_failure("claim_task_reward", exc)
            return None

    # ──────────────────── 群活动 ────────────────────

    def get_active_activities(self, group_id: int) -> list[dict]:
        try:
            conn = self._get_connection()
            cursor = conn.execute(
                """SELECT * FROM activities
                   WHERE group_id = ? AND is_active = 1
                     AND (end_time IS NULL OR end_time > ?)
                   ORDER BY id""",
                (group_id, utc_now().isoformat()),
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception as exc:
            _log_database_failure("get_active_activities", exc)
            return []

    def create_activity(
        self,
        group_id: int,
        activity_type: str,
        title: str,
        target_value: int,
        reward_coins: int,
        duration_hours: int = 24,
    ) -> int | None:
        if not isinstance(activity_type, str) or not isinstance(title, str):
            return None
        activity_type = activity_type.strip()
        title = title.strip()
        if (
            not activity_type
            or not title
            or type(target_value) is not int
            or target_value <= 0
            or type(reward_coins) is not int
            or reward_coins < 0
            or type(duration_hours) is not int
            or duration_hours <= 0
        ):
            return None
        conn: sqlite3.Connection | None = None
        try:
            conn = self._get_connection()
            now = utc_now()
            cursor = conn.execute(
                """INSERT INTO activities
                   (group_id, activity_type, title, target_value, current_value,
                    reward_coins, start_time, end_time, is_active)
                   VALUES (?, ?, ?, ?, 0, ?, ?, ?, 1)""",
                (
                    group_id,
                    activity_type,
                    title,
                    target_value,
                    reward_coins,
                    now.isoformat(),
                    (now + timedelta(hours=duration_hours)).isoformat(),
                ),
            )
            activity_id = cursor.lastrowid
            if activity_id is None:
                raise RuntimeError("活动写入后未返回主键")
            conn.commit()
            return activity_id
        except Exception as exc:
            if conn is not None:
                conn.rollback()
            _log_database_failure("create_activity", exc)
            return None

    @staticmethod
    def _advance_activities_in_transaction(
        conn: sqlite3.Connection,
        group_id: int,
        activity_type: str,
        increment: int = 1,
    ) -> int:
        """推进显式宠物活动；自然聊天触发由独立配置控制。"""
        if increment <= 0:
            return 0
        config = conn.execute(
            "SELECT enabled, activity_enabled FROM group_configs WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        if not config or not bool(config["enabled"]) or not bool(config["activity_enabled"]):
            return 0
        cursor = conn.execute(
            """UPDATE activities SET current_value = MIN(current_value + ?, target_value)
               WHERE group_id = ? AND activity_type = ? AND is_active = 1
               AND (end_time IS NULL OR end_time > ?)""",
            (increment, group_id, activity_type, utc_now().isoformat()),
        )
        return cursor.rowcount

    def claim_activity_reward(self, activity_id: int, user_id: str, group_id: int) -> int | None:
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT reward_coins FROM activities WHERE id = ? AND group_id = ?
                   AND current_value >= target_value""",
                (activity_id, group_id),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            claim = conn.execute(
                "INSERT OR IGNORE INTO activity_claims (activity_id, user_id, claimed_at) VALUES (?, ?, ?)",
                (activity_id, user_id, utc_now().isoformat()),
            )
            if claim.rowcount != 1:
                conn.rollback()
                return None
            reward = self._credit_coins_in_transaction(
                conn,
                user_id,
                group_id,
                int(row["reward_coins"] or 0),
                reason="activity",
                reference_id=f"activity:{activity_id}:{user_id}",
                daily_limit=_DAILY_COIN_LIMIT,
                now_iso=utc_now().isoformat(),
                record_zero=True,
            )
            conn.commit()
            return reward
        except Exception as exc:
            conn.rollback()
            _log_database_failure("claim_activity_reward", exc)
            return None

    # ──────────────────── 宠物留言板 ────────────────────

    def leave_message_atomic(
        self,
        from_user_id: str,
        to_user_id: str,
        group_id: int,
        message: str,
        daily_limit: int,
    ) -> LeaveMessageAtomicResult:
        """原子写入留言并消耗发送者的每日留言配额。"""
        if from_user_id == to_user_id:
            return LeaveMessageAtomicResult(False, "不能给自己留言")
        limit = max(0, int(daily_limit))
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            sender = conn.execute(
                "SELECT today_message_count FROM users WHERE user_id = ? AND group_id = ?",
                (from_user_id, group_id),
            ).fetchone()
            target = conn.execute(
                "SELECT 1 FROM users WHERE user_id = ? AND group_id = ?",
                (to_user_id, group_id),
            ).fetchone()
            pet = conn.execute(
                "SELECT name FROM pets WHERE user_id = ? AND group_id = ?",
                (to_user_id, group_id),
            ).fetchone()
            if sender is None:
                conn.rollback()
                return LeaveMessageAtomicResult(False, "留言用户不存在")
            if target is None:
                conn.rollback()
                return LeaveMessageAtomicResult(False, "目标用户不存在")
            if pet is None:
                conn.rollback()
                return LeaveMessageAtomicResult(False, "该用户没有宠物")
            if int(sender["today_message_count"] or 0) >= limit:
                conn.rollback()
                return LeaveMessageAtomicResult(
                    False,
                    f"今日留言次数已达上限({limit}次)",
                )

            now_iso = utc_now().isoformat()
            conn.execute(
                """INSERT INTO message_board
                   (group_id, from_user_id, to_user_id, message, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (group_id, from_user_id, to_user_id, message, now_iso),
            )
            updated = conn.execute(
                """UPDATE users
                   SET today_message_count = today_message_count + 1,
                       total_message_count = total_message_count + 1,
                       last_active = ?, version = version + 1
                   WHERE user_id = ? AND group_id = ?
                     AND today_message_count < ?""",
                (now_iso, from_user_id, group_id, limit),
            )
            if updated.rowcount != 1:
                raise RuntimeError("message quota changed during settlement")
            conn.commit()
            return LeaveMessageAtomicResult(True, pet_name=str(pet["name"]))
        except Exception as exc:
            conn.rollback()
            _log_database_failure("leave_message_atomic", exc)
            return LeaveMessageAtomicResult(False, "留言失败")

    def get_messages(self, to_user_id: str, group_id: int, limit: int = 10) -> list[dict]:
        try:
            conn = self._get_connection()
            cursor = conn.execute(
                """SELECT * FROM message_board
                WHERE to_user_id = ? AND group_id = ? ORDER BY created_at DESC LIMIT ?""",
                (to_user_id, group_id, limit),
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception as exc:
            _log_database_failure("get_messages", exc)
            return []

    # ──────────────────── Anti-Spam ────────────────────

    def record_command_timestamp(self, user_id: str, group_id: int) -> None:
        conn: sqlite3.Connection | None = None
        try:
            conn = self._get_connection()
            conn.execute(
                "INSERT INTO command_timestamps (user_id, group_id, timestamp) VALUES (?, ?, ?)",
                (user_id, group_id, time.time()),
            )
            conn.commit()
        except Exception as exc:
            if conn is not None:
                conn.rollback()
            _log_database_failure("record_command_timestamp", exc)

    def get_recent_command_count(self, user_id: str, group_id: int, window_seconds: int) -> int:
        try:
            conn = self._get_connection()
            threshold = time.time() - window_seconds
            cursor = conn.execute(
                "SELECT COUNT(*) as cnt FROM command_timestamps WHERE user_id = ? AND group_id = ? AND timestamp > ?",
                (user_id, group_id, threshold),
            )
            row = cursor.fetchone()
            return row["cnt"] if row else 0
        except Exception as exc:
            _log_database_failure("get_recent_command_count", exc)
            return 0

    def get_group_recent_command_count(self, group_id: int, window_seconds: int) -> int:
        try:
            conn = self._get_connection()
            threshold = time.time() - window_seconds
            cursor = conn.execute(
                "SELECT COUNT(*) as cnt FROM command_timestamps WHERE group_id = ? AND timestamp > ?",
                (group_id, threshold),
            )
            row = cursor.fetchone()
            return row["cnt"] if row else 0
        except Exception as exc:
            _log_database_failure("get_group_recent_command_count", exc)
            return 0

    def cleanup_old_timestamps(self, max_age_seconds: int = 3600) -> None:
        conn: sqlite3.Connection | None = None
        try:
            conn = self._get_connection()
            threshold = time.time() - max_age_seconds
            conn.execute("DELETE FROM command_timestamps WHERE timestamp < ?", (threshold,))
            conn.commit()
        except Exception as exc:
            if conn is not None:
                conn.rollback()
            _log_database_failure("cleanup_old_timestamps", exc)

    # ──────────────────── 交易市场 ────────────────────

    def get_active_listings(self, group_id: int) -> list[dict]:
        self.settle_expired_trade_listings(group_id)
        try:
            conn = self._get_connection()
            now = utc_now().isoformat()
            cursor = conn.execute(
                """SELECT * FROM trade_listings
                WHERE group_id = ? AND is_active = 1 AND expires_at > ?
                ORDER BY created_at DESC""",
                (group_id, now),
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception as exc:
            _log_database_failure("get_active_trade_listings", exc)
            return []

    def get_listing_by_id(self, listing_id: int, group_id: int | None = None) -> dict | None:
        self.settle_expired_trade_listings(group_id)
        try:
            conn = self._get_connection()
            now = utc_now().isoformat()
            if group_id is None:
                cursor = conn.execute(
                    """SELECT * FROM trade_listings
                       WHERE id = ? AND is_active = 1 AND expires_at > ?""",
                    (listing_id, now),
                )
            else:
                cursor = conn.execute(
                    """SELECT * FROM trade_listings
                       WHERE id = ? AND group_id = ? AND is_active = 1 AND expires_at > ?""",
                    (listing_id, group_id, now),
                )
            row = cursor.fetchone()
            return dict(row) if row else None
        except Exception as exc:
            _log_database_failure("get_trade_listing", exc)
            return None

    def cancel_trade_listing(self, listing_id: int, seller_id: str, group_id: int) -> bool:
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now().isoformat()
            cursor = conn.execute(
                "SELECT * FROM trade_listings WHERE id = ? AND group_id = ? AND is_active = 1",
                (listing_id, group_id),
            )
            row = cursor.fetchone()
            if not row or str(row["seller_user_id"]) != str(seller_id):
                conn.rollback()
                return False

            listing = dict(row)
            if str(listing["expires_at"]) <= now:
                expired = self._expire_trade_listing_in_transaction(conn, row, now)
                if expired:
                    conn.commit()
                else:
                    conn.rollback()
                return False

            claim = conn.execute(
                """UPDATE trade_listings SET is_active = 0, status = 'cancelled'
                   WHERE id = ? AND group_id = ? AND is_active = 1 AND expires_at > ?""",
                (listing_id, group_id, now),
            )
            if claim.rowcount != 1:
                conn.rollback()
                return False
            inventory_items = self._load_inventory_items(
                conn,
                seller_id,
                int(listing["group_id"]),
            )
            inventory_items[listing["item_id"]] = int(
                inventory_items.get(listing["item_id"], 0)
            ) + int(listing["amount"])

            self._save_inventory_items(
                conn,
                seller_id,
                int(listing["group_id"]),
                inventory_items,
            )

            conn.commit()
            return True
        except Exception as exc:
            conn.rollback()
            _log_database_failure("cancel_trade_listing", exc)
            return False

    def purchase_trade_listing(
        self,
        listing_id: int,
        buyer_id: str,
        group_id: int,
        tax_rate: float,
    ) -> tuple[bool, dict | str]:
        if not math.isfinite(tax_rate) or not 0.0 <= tax_rate <= 1.0:
            return False, "交易税率无效"
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now().isoformat()
            cursor = conn.execute(
                "SELECT * FROM trade_listings WHERE id = ? AND is_active = 1",
                (listing_id,),
            )
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return False, "订单不存在或已过期"

            listing = dict(row)
            if int(listing["group_id"]) != int(group_id):
                conn.rollback()
                return False, "该订单不属于本群"
            if str(listing["expires_at"]) <= now:
                expired = self._expire_trade_listing_in_transaction(conn, row, now)
                if expired:
                    conn.commit()
                else:
                    conn.rollback()
                return False, "订单已过期，道具已退还"
            if str(listing["seller_user_id"]) == str(buyer_id):
                conn.rollback()
                return False, "不能购买自己的挂单"

            buyer_row = conn.execute(
                "SELECT * FROM users WHERE user_id = ? AND group_id = ?",
                (buyer_id, group_id),
            ).fetchone()
            if not buyer_row:
                conn.rollback()
                return False, "用户不存在"

            total_cost = int(listing["price"])
            tax = int(total_cost * tax_rate)
            if int(buyer_row["coins"]) < total_cost:
                conn.rollback()
                return False, f"金币不足，需要{total_cost}金币"

            claim = conn.execute(
                """UPDATE trade_listings SET is_active = 0, status = 'purchased'
                   WHERE id = ? AND group_id = ? AND is_active = 1 AND expires_at > ?""",
                (listing_id, group_id, now),
            )
            if claim.rowcount != 1:
                conn.rollback()
                return False, "订单不存在或已过期"

            buyer_update = conn.execute(
                """UPDATE users SET coins = coins - ?, version = version + 1
                   WHERE user_id = ? AND group_id = ?""",
                (total_cost, buyer_id, group_id),
            )
            seller_update = conn.execute(
                """UPDATE users SET coins = coins + ?, version = version + 1
                   WHERE user_id = ? AND group_id = ?""",
                (max(0, total_cost - tax), str(listing["seller_user_id"]), group_id),
            )
            if buyer_update.rowcount != 1 or seller_update.rowcount != 1:
                raise RuntimeError("trade participant changed during settlement")
            self._record_asset_delta(
                conn,
                user_id=buyer_id,
                group_id=group_id,
                asset_type="coins",
                delta=-total_cost,
                reason="trade_purchase",
                reference_id=f"trade-purchase:{listing_id}:buyer",
            )
            self._record_asset_delta(
                conn,
                user_id=str(listing["seller_user_id"]),
                group_id=group_id,
                asset_type="coins",
                delta=max(0, total_cost - tax),
                reason="trade_purchase",
                reference_id=f"trade-purchase:{listing_id}:seller",
            )

            inventory_items = self._load_inventory_items(conn, buyer_id, group_id)
            inventory_items[listing["item_id"]] = int(
                inventory_items.get(listing["item_id"], 0)
            ) + int(listing["amount"])
            self._save_inventory_items(conn, buyer_id, group_id, inventory_items)

            conn.commit()
            listing["tax"] = tax
            return True, listing
        except Exception as exc:
            conn.rollback()
            _log_database_failure("purchase_trade_listing", exc)
            return False, "购买失败，请稍后重试"

    # ──────────────────── 宠物展示会 ────────────────────

    def create_pet_show(self, group_id: int, title: str, duration_hours: int) -> int | None:
        if duration_hours <= 0:
            return None
        # 到期记录必须先完整结算，不能仅为新活动让路而丢失奖励。
        self.settle_pet_show_atomic(group_id, force=False)
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now()
            end = now + timedelta(hours=duration_hours)
            existing = conn.execute(
                "SELECT 1 FROM pet_shows WHERE group_id = ? AND is_active = 1",
                (group_id,),
            ).fetchone()
            if existing:
                conn.rollback()
                return None
            cursor = conn.execute(
                """INSERT INTO pet_shows
                (group_id, title, start_time, end_time, is_active, status)
                VALUES (?, ?, ?, ?, 1, 'active')""",
                (group_id, title, now.isoformat(), end.isoformat()),
            )
            conn.commit()
            return cursor.lastrowid
        except Exception as exc:
            conn.rollback()
            _log_database_failure("create_pet_show", exc)
            return None

    def get_active_pet_show(self, group_id: int) -> dict | None:
        try:
            conn = self._get_connection()
            now = utc_now().isoformat()
            cursor = conn.execute(
                """SELECT * FROM pet_shows
                WHERE group_id = ? AND is_active = 1
                  AND start_time <= ? AND end_time > ?
                ORDER BY id DESC LIMIT 1""",
                (group_id, now, now),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        except Exception as exc:
            _log_database_failure("get_active_pet_show", exc)
            return None

    def get_pet_show_votes(self, show_id: int) -> dict[str, int]:
        try:
            conn = self._get_connection()
            cursor = conn.execute(
                """SELECT pet_user_id, COUNT(*) as votes
                FROM pet_show_votes WHERE show_id = ?
                GROUP BY pet_user_id ORDER BY votes DESC""",
                (show_id,),
            )
            return {row["pet_user_id"]: row["votes"] for row in cursor.fetchall()}
        except Exception as exc:
            _log_database_failure("get_pet_show_votes", exc)
            return {}

    # ──────────────────── 装扮库存 ────────────────────

    def get_dress_inventory(self, user_id: str, group_id: int) -> list[str]:
        try:
            conn = self._get_connection()
            cursor = conn.execute(
                """SELECT dress_item_id FROM dress_inventory
                WHERE user_id = ? AND group_id = ?""",
                (user_id, group_id),
            )
            return [row["dress_item_id"] for row in cursor.fetchall()]
        except Exception as exc:
            _log_database_failure("get_dress_inventory", exc)
            return []

    # ──────────────────── 群累计任务 ────────────────────

    @staticmethod
    def _ensure_group_task_templates(
        conn: sqlite3.Connection,
        group_id: int,
        created_date: str,
    ) -> None:
        for template in GROUP_TASK_TEMPLATES:
            conn.execute(
                """
                INSERT INTO group_tasks
                    (group_id, task_type, target_value, current_value, reward_coins,
                     description, created_date, is_completed)
                VALUES (?, ?, ?, 0, ?, ?, ?, 0)
                ON CONFLICT(group_id, task_type, created_date) DO NOTHING
                """,
                (
                    group_id,
                    template["type"],
                    template["target"],
                    template["reward_coins"],
                    template["description"],
                    created_date,
                ),
            )

    def get_or_create_group_tasks(self, group_id: int) -> list[dict]:
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            today = utc_now().strftime("%Y-%m-%d")
            self._ensure_group_task_templates(conn, group_id, today)
            rows = conn.execute(
                """SELECT * FROM group_tasks
                   WHERE group_id = ? AND created_date = ?
                   ORDER BY task_type""",
                (group_id, today),
            ).fetchall()
            conn.commit()
            return [dict(row) for row in rows]
        except Exception as exc:
            conn.rollback()
            _log_database_failure("get_or_create_group_tasks", exc)
            return []

    def claim_group_task_reward(
        self,
        user_id: str,
        group_id: int,
        task_type: str,
        daily_coin_limit: int = _DAILY_COIN_LIMIT,
    ) -> int | None:
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            today = utc_now().strftime("%Y-%m-%d")
            task = conn.execute(
                """SELECT reward_coins FROM group_tasks
                   WHERE group_id = ? AND task_type = ? AND created_date = ?
                   AND (is_completed = 1 OR current_value >= target_value)""",
                (group_id, task_type, today),
            ).fetchone()
            if task is None:
                conn.rollback()
                return None
            claim = conn.execute(
                """INSERT OR IGNORE INTO group_task_claims
                   (group_id, task_type, created_date, user_id, claimed_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (group_id, task_type, today, user_id, utc_now().isoformat()),
            )
            if claim.rowcount != 1:
                conn.rollback()
                return None
            reward = self._credit_coins_in_transaction(
                conn,
                user_id,
                group_id,
                int(task["reward_coins"] or 0),
                reason="group_task",
                reference_id=f"group-task:{today}:{group_id}:{task_type}:{user_id}",
                daily_limit=daily_coin_limit,
                now_iso=utc_now().isoformat(),
                record_zero=True,
            )
            conn.commit()
            return reward
        except Exception as exc:
            conn.rollback()
            _log_database_failure("claim_group_task_reward", exc)
            return None

    def purchase_item_atomic(
        self, user_id: str, group_id: int, item_id: str, amount: int, total_cost: int
    ) -> tuple[bool, int]:
        if type(amount) is not int or amount <= 0 or item_id not in DEFAULT_ITEMS:
            return False, -1
        if type(total_cost) is not int or total_cost < 0:
            return False, -1
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """UPDATE users SET coins = coins - ?, version = version + 1
                   WHERE user_id = ? AND group_id = ? AND coins >= ?""",
                (total_cost, user_id, group_id, total_cost),
            )
            if cursor.rowcount != 1:
                row = conn.execute(
                    "SELECT coins FROM users WHERE user_id = ? AND group_id = ?",
                    (user_id, group_id),
                ).fetchone()
                conn.rollback()
                return False, int(row["coins"]) if row else -1
            items = self._load_inventory_items(conn, user_id, group_id)
            items[item_id] = int(items.get(item_id, 0)) + int(amount)
            self._save_inventory_items(conn, user_id, group_id, items)
            self._record_asset_delta(
                conn,
                user_id=user_id,
                group_id=group_id,
                asset_type="coins",
                delta=-total_cost,
                reason="item_purchase",
            )
            conn.commit()
            return True, 0
        except Exception as exc:
            conn.rollback()
            _log_database_failure("purchase_item_atomic", exc)
            return False, -1

    def check_action_quota(
        self,
        user_id: str,
        group_id: int,
        action: str,
        daily_limit: int,
    ) -> tuple[bool, int]:
        """只读检查持久化配额和冷却，不消耗次数。"""
        try:
            current = utc_now()
            timestamp = time.time()
            row = (
                self._get_connection()
                .execute(
                    """SELECT action_count, available_at FROM action_quotas
                   WHERE user_id = ? AND group_id = ? AND action = ? AND period_date = ?""",
                    (user_id, group_id, action, current.strftime("%Y-%m-%d")),
                )
                .fetchone()
            )
            count = int(row["action_count"] or 0) if row else 0
            available_at = float(row["available_at"] or 0) if row else 0.0
            if count >= max(0, int(daily_limit)):
                return False, -1
            if available_at > timestamp:
                return False, max(1, int(available_at - timestamp))
            return True, 0
        except Exception as exc:
            _log_database_failure("check_action_quota", exc)
            return False, -1

    @staticmethod
    def _claim_action_quota_in_transaction(
        conn: sqlite3.Connection,
        user_id: str,
        group_id: int,
        action: str,
        daily_limit: int,
        cooldown_seconds: int = 0,
        *,
        now: datetime | None = None,
        now_ts: float | None = None,
    ) -> tuple[bool, int]:
        """在调用方已开启的事务中领取一次持久化配额。"""
        current = now or utc_now()
        timestamp = time.time() if now_ts is None else now_ts
        today = current.strftime("%Y-%m-%d")
        row = conn.execute(
            """SELECT action_count, available_at FROM action_quotas
               WHERE user_id = ? AND group_id = ? AND action = ? AND period_date = ?""",
            (user_id, group_id, action, today),
        ).fetchone()
        count = int(row["action_count"] or 0) if row else 0
        available_at = float(row["available_at"] or 0) if row else 0.0
        if count >= max(0, int(daily_limit)):
            return False, -1
        if available_at > timestamp:
            return False, max(1, int(available_at - timestamp))
        conn.execute(
            """INSERT INTO action_quotas
               (user_id, group_id, action, period_date, action_count, available_at)
               VALUES (?, ?, ?, ?, 1, ?)
               ON CONFLICT(user_id, group_id, action, period_date)
               DO UPDATE SET action_count = action_count + 1,
                             available_at = excluded.available_at""",
            (
                user_id,
                group_id,
                action,
                today,
                timestamp + max(0, int(cooldown_seconds)),
            ),
        )
        return True, 0

    @staticmethod
    def _credit_coins_in_transaction(
        conn: sqlite3.Connection,
        user_id: str,
        group_id: int,
        amount: int,
        *,
        reason: str,
        reference_id: str,
        daily_limit: int,
        now_iso: str,
        record_zero: bool = False,
    ) -> int:
        """在现有事务中应用每日上限并写入金币账本。"""
        row = conn.execute(
            "SELECT today_coins_earned FROM users WHERE user_id = ? AND group_id = ?",
            (user_id, group_id),
        ).fetchone()
        if row is None:
            raise LookupError(f"user missing during coin credit: {user_id}")
        grant = min(
            max(0, int(amount)),
            max(0, int(daily_limit) - int(row["today_coins_earned"] or 0)),
        )
        if grant:
            updated = conn.execute(
                """UPDATE users SET coins = coins + ?,
                   today_coins_earned = today_coins_earned + ?,
                   version = version + 1
                   WHERE user_id = ? AND group_id = ?""",
                (grant, grant, user_id, group_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError("coin recipient changed during settlement")
        if grant or record_zero:
            conn.execute(
                """INSERT INTO asset_ledger
                   (user_id, group_id, asset_type, delta, reason, reference_id, created_at)
                   VALUES (?, ?, 'coins', ?, ?, ?, ?)""",
                (user_id, group_id, grant, reason, reference_id, now_iso),
            )
        return grant

    @staticmethod
    def _grant_temporary_title_in_transaction(
        conn: sqlite3.Connection,
        user_id: str,
        group_id: int,
        title: str,
        *,
        now: datetime,
    ) -> bool:
        """在调用方事务中更新用户称号列表及对应过期时间。"""
        title_config = TITLES.get(title)
        if title_config is None:
            return False
        duration_days = title_config["duration_days"]
        if duration_days is None:
            return False
        row = conn.execute(
            "SELECT titles FROM users WHERE user_id = ? AND group_id = ?",
            (user_id, group_id),
        ).fetchone()
        if row is None:
            return False
        titles = json.loads(row["titles"] or "[]")
        if title not in titles:
            titles.append(title)
            updated = conn.execute(
                """UPDATE users SET titles = ?, version = version + 1
                   WHERE user_id = ? AND group_id = ?""",
                (json.dumps(titles), user_id, group_id),
            )
            if updated.rowcount != 1:
                return False
        expires_at = now + timedelta(days=duration_days)
        conn.execute(
            """INSERT OR REPLACE INTO title_expiry
               (user_id, group_id, title, expires_at) VALUES (?, ?, ?, ?)""",
            (user_id, group_id, title, expires_at.isoformat()),
        )
        return True

    @staticmethod
    def _increment_task_in_transaction(
        conn: sqlite3.Connection,
        user_id: str,
        group_id: int,
        task_type: str,
        *,
        now_iso: str,
        today: str,
        increment: int = 1,
    ) -> None:
        if increment <= 0:
            raise ValueError("daily task increment must be positive")
        Database._ensure_daily_task_templates(
            conn,
            user_id,
            group_id,
            today=today,
            now_iso=now_iso,
        )
        updated = conn.execute(
            """UPDATE tasks SET current_value = MIN(current_value + ?, target_value)
               WHERE user_id = ? AND group_id = ? AND task_type = ? AND created_date = ?""",
            (increment, user_id, group_id, task_type, today),
        )
        if updated.rowcount != 1:
            raise RuntimeError("daily task changed during settlement")

    def settle_minigame_atomic(
        self,
        user_id: str,
        group_id: int,
        game_type: str,
        *,
        reference_id: str,
        daily_coin_limit: int,
        cooldown_seconds: int,
        outcome_factory: Callable[[Pet, Pet | None], MinigameOutcome],
        opponent_user_id: str | None = None,
        minimum_energy: int = 0,
    ) -> MinigameAtomicResult:
        """在一个立即事务中校验并结算小游戏。

        结算会保存随机结果；消息投递重试时复用原结果，避免重复随机展示与既有资产奖励不一致。
        """
        if not str(reference_id).strip():
            return MinigameAtomicResult(False, "小游戏请求标识不能为空")
        normalized_opponent = str(opponent_user_id or "")
        if normalized_opponent and normalized_opponent == user_id:
            return MinigameAtomicResult(False, "不能和自己的宠物进行对战")
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                "SELECT * FROM minigame_settlements WHERE reference_id = ?",
                (reference_id,),
            ).fetchone()
            if prior is not None:
                same_request = (
                    str(prior["user_id"]) == user_id
                    and int(prior["group_id"]) == int(group_id)
                    and str(prior["game_type"]) == game_type
                    and str(prior["opponent_user_id"] or "") == normalized_opponent
                )
                conn.rollback()
                if not same_request:
                    return MinigameAtomicResult(False, "小游戏请求标识冲突")
                payload = json.loads(str(prior["outcome_payload"]))
                return MinigameAtomicResult(
                    True,
                    pet_name=str(prior["pet_name"]),
                    opponent_pet_name=str(prior["opponent_pet_name"] or ""),
                    coin_grant=int(prior["coin_grant"]),
                    experience_grant=int(prior["experience_grant"]),
                    energy_cost=int(prior["energy_cost"]),
                    payload=payload if isinstance(payload, dict) else {},
                    duplicate=True,
                )

            user_row = conn.execute(
                "SELECT * FROM users WHERE user_id = ? AND group_id = ?",
                (user_id, group_id),
            ).fetchone()
            pet_row = conn.execute(
                "SELECT * FROM pets WHERE user_id = ? AND group_id = ?",
                (user_id, group_id),
            ).fetchone()
            if user_row is None:
                conn.rollback()
                return MinigameAtomicResult(False, "用户不存在")
            if pet_row is None:
                conn.rollback()
                return MinigameAtomicResult(False, "你还没有宠物")
            pet = self._row_to_pet(pet_row)
            if not pet.can_interact():
                conn.rollback()
                return MinigameAtomicResult(False, "宠物现在无法互动")

            opponent_pet: Pet | None = None
            opponent_pet_name = ""
            if normalized_opponent:
                opponent_user = conn.execute(
                    "SELECT 1 FROM users WHERE user_id = ? AND group_id = ?",
                    (normalized_opponent, group_id),
                ).fetchone()
                opponent_row = conn.execute(
                    "SELECT * FROM pets WHERE user_id = ? AND group_id = ?",
                    (normalized_opponent, group_id),
                ).fetchone()
                if opponent_user is None or opponent_row is None:
                    conn.rollback()
                    return MinigameAtomicResult(False, "对方的宠物无法参赛")
                opponent_pet = self._row_to_pet(opponent_row)
                if not opponent_pet.can_interact():
                    conn.rollback()
                    return MinigameAtomicResult(False, "对方的宠物无法参赛")
                opponent_pet_name = opponent_pet.name

            minimum_energy = max(0, int(minimum_energy))
            if pet.energy < minimum_energy:
                conn.rollback()
                return MinigameAtomicResult(False, "你的宠物精力不足")

            now = utc_now()
            now_ts = time.time()
            cooldown_row = conn.execute(
                """SELECT available_at FROM minigame_cooldowns
                   WHERE user_id = ? AND group_id = ? AND game_type = ?""",
                (user_id, group_id, game_type),
            ).fetchone()
            if cooldown_row is not None and float(cooldown_row["available_at"]) > now_ts:
                remaining = max(1, int(float(cooldown_row["available_at"]) - now_ts))
                conn.rollback()
                return MinigameAtomicResult(False, f"小游戏冷却中，请等待{remaining}秒")

            outcome = outcome_factory(pet, opponent_pet)
            requested_coins = max(0, int(outcome.requested_coins))
            experience_grant = max(0, int(outcome.experience))
            energy_cost = max(0, int(outcome.energy_cost))
            if energy_cost > int(pet.energy):
                conn.rollback()
                return MinigameAtomicResult(False, "你的宠物精力不足")
            payload = dict(outcome.payload or {})
            payload_json = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )

            conn.execute(
                """INSERT INTO minigame_cooldowns
                   (user_id, group_id, game_type, available_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id, group_id, game_type)
                   DO UPDATE SET available_at = excluded.available_at""",
                (
                    user_id,
                    group_id,
                    game_type,
                    now_ts + max(0, int(cooldown_seconds)),
                ),
            )
            now_iso = now.isoformat()
            coin_grant = self._credit_coins_in_transaction(
                conn,
                user_id,
                group_id,
                requested_coins,
                reason=f"minigame_{game_type}",
                reference_id=f"{reference_id}:coins",
                daily_limit=daily_coin_limit,
                now_iso=now_iso,
                record_zero=True,
            )
            pet_update = conn.execute(
                """UPDATE pets
                   SET experience = experience + ?, energy = energy - ?,
                       version = version + 1
                   WHERE id = ? AND energy >= ?""",
                (experience_grant, energy_cost, int(pet_row["id"]), energy_cost),
            )
            if pet_update.rowcount != 1:
                raise RuntimeError("minigame pet changed during settlement")
            conn.execute(
                """INSERT INTO minigame_settlements
                   (reference_id, user_id, group_id, game_type, opponent_user_id,
                    pet_name, opponent_pet_name, coin_grant, experience_grant,
                    energy_cost, outcome_payload, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    reference_id,
                    user_id,
                    group_id,
                    game_type,
                    normalized_opponent,
                    pet.name,
                    opponent_pet_name,
                    coin_grant,
                    experience_grant,
                    energy_cost,
                    payload_json,
                    now_iso,
                ),
            )
            conn.commit()
            return MinigameAtomicResult(
                True,
                pet_name=pet.name,
                opponent_pet_name=opponent_pet_name,
                coin_grant=coin_grant,
                experience_grant=experience_grant,
                energy_cost=energy_cost,
                payload=payload,
            )
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            _log_database_failure("settle_minigame_conflict", exc)
            return MinigameAtomicResult(False, "小游戏请求冲突，请稍后重试")
        except Exception as exc:
            conn.rollback()
            _log_database_failure("settle_minigame_atomic", exc)
            return MinigameAtomicResult(False, "小游戏结算失败")

    def visit_pet_atomic(
        self,
        visitor_user_id: str,
        target_user_id: str,
        group_id: int,
        *,
        coin_reward: int,
        daily_visit_limit: int,
        daily_coin_limit: int,
        cooldown_seconds: int,
        reference_id: str,
    ) -> VisitPetAtomicResult:
        """在单个 ``BEGIN IMMEDIATE`` 事务中仅结算一次完整互访。"""
        if visitor_user_id == target_user_id:
            return VisitPetAtomicResult(False, "不能访问自己的宠物")
        if not reference_id:
            return VisitPetAtomicResult(False, "访问请求缺少唯一标识")

        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                "SELECT * FROM visit_settlements WHERE reference_id = ?",
                (reference_id,),
            ).fetchone()
            if prior is not None:
                same_request = (
                    str(prior["visitor_user_id"]) == visitor_user_id
                    and str(prior["target_user_id"]) == target_user_id
                    and int(prior["group_id"]) == int(group_id)
                )
                conn.rollback()
                if not same_request:
                    return VisitPetAtomicResult(False, "访问请求标识冲突")
                return VisitPetAtomicResult(
                    True,
                    pet_name=str(prior["pet_name"]),
                    visitor_grant=int(prior["visitor_grant"]),
                    target_grant=int(prior["target_grant"]),
                    intimacy_grant=int(prior["intimacy_grant"]),
                    duplicate=True,
                )

            visitor = conn.execute(
                "SELECT * FROM users WHERE user_id = ? AND group_id = ?",
                (visitor_user_id, group_id),
            ).fetchone()
            target = conn.execute(
                "SELECT * FROM users WHERE user_id = ? AND group_id = ?",
                (target_user_id, group_id),
            ).fetchone()
            pet_row = conn.execute(
                "SELECT * FROM pets WHERE user_id = ? AND group_id = ?",
                (target_user_id, group_id),
            ).fetchone()
            if visitor is None:
                conn.rollback()
                return VisitPetAtomicResult(False, "访客用户不存在")
            if target is None:
                conn.rollback()
                return VisitPetAtomicResult(False, "目标用户不存在")
            if pet_row is None:
                conn.rollback()
                return VisitPetAtomicResult(False, "目标宠物不存在")
            if not self._row_to_pet(pet_row).can_interact():
                conn.rollback()
                return VisitPetAtomicResult(False, "该宠物现在无法互动")

            now = utc_now()
            if int(visitor["today_visit_count"] or 0) >= int(daily_visit_limit):
                conn.rollback()
                return VisitPetAtomicResult(False, "今日访问次数已达上限")
            last_visit = visitor["last_visit_time"]
            if last_visit:
                elapsed = (now - datetime.fromisoformat(last_visit)).total_seconds()
                if elapsed < cooldown_seconds:
                    conn.rollback()
                    return VisitPetAtomicResult(
                        False,
                        f"访问冷却中，请等待{max(1, int(cooldown_seconds - elapsed))}秒",
                    )
            claimed, remaining = self._claim_action_quota_in_transaction(
                conn,
                visitor_user_id,
                group_id,
                "visit",
                daily_visit_limit,
                cooldown_seconds,
                now=now,
            )
            if not claimed:
                conn.rollback()
                reason = (
                    f"访问冷却中，请等待{remaining}秒" if remaining > 0 else "今日访问次数已达上限"
                )
                return VisitPetAtomicResult(False, reason)

            now_iso = now.isoformat()
            visitor_update = conn.execute(
                """UPDATE users SET today_visit_count = today_visit_count + 1,
                   total_visit_count = total_visit_count + 1,
                   last_visit_time = ?, last_active = ?, version = version + 1
                   WHERE user_id = ? AND group_id = ?""",
                (now_iso, now_iso, visitor_user_id, group_id),
            )
            if visitor_update.rowcount != 1:
                raise RuntimeError("visitor changed during settlement")
            pet_update = conn.execute(
                "UPDATE pets SET intimacy = intimacy + 1, version = version + 1 WHERE id = ?",
                (int(pet_row["id"]),),
            )
            if pet_update.rowcount != 1:
                raise RuntimeError("target pet changed during settlement")

            visitor_grant = self._credit_coins_in_transaction(
                conn,
                visitor_user_id,
                group_id,
                coin_reward,
                reason="pet_visit_visitor",
                reference_id=f"{reference_id}:visitor",
                daily_limit=daily_coin_limit,
                now_iso=now_iso,
                record_zero=True,
            )
            target_grant = self._credit_coins_in_transaction(
                conn,
                target_user_id,
                group_id,
                coin_reward,
                reason="pet_visit_target",
                reference_id=f"{reference_id}:target",
                daily_limit=daily_coin_limit,
                now_iso=now_iso,
                record_zero=True,
            )
            self._increment_task_in_transaction(
                conn,
                visitor_user_id,
                group_id,
                "visit",
                now_iso=now_iso,
                today=now.strftime("%Y-%m-%d"),
            )
            pet_name = str(pet_row["name"])
            conn.execute(
                """INSERT INTO visit_settlements
                   (reference_id, visitor_user_id, target_user_id, group_id, pet_name,
                    visitor_grant, target_grant, intimacy_grant, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (
                    reference_id,
                    visitor_user_id,
                    target_user_id,
                    group_id,
                    pet_name,
                    visitor_grant,
                    target_grant,
                    now_iso,
                ),
            )
            conn.commit()
            return VisitPetAtomicResult(
                True,
                pet_name=pet_name,
                visitor_grant=visitor_grant,
                target_grant=target_grant,
                intimacy_grant=1,
            )
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            _log_database_failure("visit_pet_conflict", exc)
            return VisitPetAtomicResult(False, "访问请求冲突，请稍后重试")
        except Exception as exc:
            conn.rollback()
            _log_database_failure("visit_pet_atomic", exc)
            return VisitPetAtomicResult(False, "访问失败")

    def use_acceleration_card_atomic(
        self,
        user_id: str,
        group_id: int,
        pet_id: int,
        exp_gain: int,
    ) -> Pet | None:
        """在一个事务中扣除一张加速卡并增加宠物经验。"""
        if exp_gain <= 0:
            return None
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            pet_row = conn.execute(
                """SELECT * FROM pets
                   WHERE id = ? AND user_id = ? AND group_id = ?""",
                (pet_id, user_id, group_id),
            ).fetchone()
            inventory_row = conn.execute(
                "SELECT items FROM inventories WHERE user_id = ? AND group_id = ?",
                (user_id, group_id),
            ).fetchone()
            if pet_row is None or inventory_row is None:
                conn.rollback()
                return None
            items = json.loads(inventory_row["items"] or "{}")
            available = int(items.get("acceleration_card", 0))
            if available <= 0:
                conn.rollback()
                return None
            if available == 1:
                items.pop("acceleration_card", None)
            else:
                items["acceleration_card"] = available - 1

            updated_at = utc_now().isoformat()
            cursor = conn.execute(
                """UPDATE pets SET
                       experience = experience + ?,
                       last_update = ?,
                       version = version + 1
                   WHERE id = ? AND user_id = ? AND group_id = ? AND version = ?""",
                (
                    exp_gain,
                    updated_at,
                    pet_id,
                    user_id,
                    group_id,
                    int(pet_row["version"]),
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return None
            # Deliberately persist the inventory after the pet update: any
            # inventory failure must still roll the experience change back.
            self._save_inventory_items(conn, user_id, group_id, items)
            updated_row = conn.execute("SELECT * FROM pets WHERE id = ?", (pet_id,)).fetchone()
            conn.commit()
            return self._row_to_pet(updated_row) if updated_row else None
        except Exception as exc:
            conn.rollback()
            _log_database_failure("use_acceleration_card_atomic", exc)
            return None

    def gift_item_atomic(
        self,
        from_user_id: str,
        to_user_id: str,
        group_id: int,
        item_id: str,
        amount: int,
        friendship_gain: int,
        daily_limit: int,
        cooldown_seconds: int,
    ) -> tuple[bool, str]:
        if from_user_id == to_user_id:
            return False, "不能给自己送礼物"
        if type(amount) is not int or amount <= 0 or amount > 99:
            return False, "礼物数量必须是1到99的整数"
        if item_id not in DEFAULT_ITEMS:
            return False, "道具不存在或不可赠送"
        if friendship_gain < 0 or daily_limit <= 0 or cooldown_seconds < 0:
            return False, "送礼参数无效"

        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            sender = conn.execute(
                "SELECT * FROM users WHERE user_id = ? AND group_id = ?",
                (from_user_id, group_id),
            ).fetchone()
            receiver = conn.execute(
                "SELECT 1 FROM users WHERE user_id = ? AND group_id = ?",
                (to_user_id, group_id),
            ).fetchone()
            if sender is None or receiver is None:
                conn.rollback()
                return False, "用户不存在"
            if int(sender["today_gift_count"] or 0) >= daily_limit:
                conn.rollback()
                return False, "今日送礼次数已达上限"
            last_gift = sender["last_gift_time"]
            if last_gift:
                elapsed = (utc_now() - datetime.fromisoformat(last_gift)).total_seconds()
                if elapsed < cooldown_seconds:
                    conn.rollback()
                    return False, f"送礼冷却中，请等待{int(cooldown_seconds - elapsed)}秒"
            sender_items = self._load_inventory_items(conn, from_user_id, group_id)
            if int(sender_items.get(item_id, 0)) < amount:
                conn.rollback()
                return False, "背包中没有足够的道具"
            sender_items[item_id] = int(sender_items[item_id]) - amount
            if sender_items[item_id] <= 0:
                sender_items.pop(item_id, None)
            receiver_items = self._load_inventory_items(conn, to_user_id, group_id)
            receiver_items[item_id] = int(receiver_items.get(item_id, 0)) + amount
            self._save_inventory_items(conn, from_user_id, group_id, sender_items)
            self._save_inventory_items(conn, to_user_id, group_id, receiver_items)
            now = utc_now().isoformat()
            conn.execute(
                """UPDATE users SET friendship_points = friendship_points + ?,
                   today_gift_count = today_gift_count + 1,
                   total_gift_count = total_gift_count + 1, last_gift_time = ?,
                   version = version + 1
                   WHERE user_id = ? AND group_id = ?""",
                (friendship_gain, now, from_user_id, group_id),
            )
            conn.execute(
                """UPDATE users SET friendship_points = friendship_points + ?,
                   version = version + 1 WHERE user_id = ? AND group_id = ?""",
                (friendship_gain, to_user_id, group_id),
            )
            conn.commit()
            return True, ""
        except Exception as exc:
            conn.rollback()
            _log_database_failure("gift_item_atomic", exc)
            return False, "送礼失败"

    def create_trade_listing_atomic(
        self,
        seller_id: str,
        group_id: int,
        item_id: str,
        amount: int,
        price: int,
        expire_hours: int,
        max_listings: int,
    ) -> bool:
        values = (amount, price, expire_hours, max_listings)
        if any(type(value) is not int or value <= 0 for value in values):
            return False
        if item_id not in DEFAULT_ITEMS:
            return False
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now()
            count = conn.execute(
                """SELECT COUNT(*) AS cnt FROM trade_listings WHERE seller_user_id = ?
                   AND group_id = ? AND is_active = 1 AND expires_at > ?""",
                (seller_id, group_id, now.isoformat()),
            ).fetchone()["cnt"]
            if int(count) >= max_listings:
                conn.rollback()
                return False
            items = self._load_inventory_items(conn, seller_id, group_id)
            if int(items.get(item_id, 0)) < amount:
                conn.rollback()
                return False
            items[item_id] = int(items[item_id]) - amount
            if items[item_id] <= 0:
                items.pop(item_id, None)
            self._save_inventory_items(conn, seller_id, group_id, items)
            conn.execute(
                """INSERT INTO trade_listings
                   (seller_user_id, group_id, item_id, amount, price, created_at, expires_at, is_active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
                (
                    seller_id,
                    group_id,
                    item_id,
                    amount,
                    price,
                    now.isoformat(),
                    (now + timedelta(hours=expire_hours)).isoformat(),
                ),
            )
            conn.commit()
            return True
        except Exception as exc:
            conn.rollback()
            _log_database_failure("create_trade_listing_atomic", exc)
            return False

    def purchase_dress_atomic(
        self, user_id: str, group_id: int, dress_item_id: str, currency: str, price: int
    ) -> tuple[bool, str]:
        if currency not in {"coins", "friendship"} or not dress_item_id:
            return False, "装扮参数无效"
        if type(price) is not int or price < 0:
            return False, "装扮价格无效"
        column = "friendship_points" if currency == "friendship" else "coins"
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            owned = conn.execute(
                "SELECT 1 FROM dress_inventory WHERE user_id = ? AND group_id = ? AND dress_item_id = ?",
                (user_id, group_id, dress_item_id),
            ).fetchone()
            if owned:
                conn.rollback()
                return False, "你已经拥有该装扮"
            cursor = conn.execute(
                f"""UPDATE users SET {column} = {column} - ?, version = version + 1
                    WHERE user_id = ? AND group_id = ? AND {column} >= ?""",
                (price, user_id, group_id, price),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False, "余额不足"
            conn.execute(
                "INSERT INTO dress_inventory (user_id, group_id, dress_item_id) VALUES (?, ?, ?)",
                (user_id, group_id, dress_item_id),
            )
            self._record_asset_delta(
                conn,
                user_id=user_id,
                group_id=group_id,
                asset_type=column,
                delta=-price,
                reason="dress_purchase",
            )
            conn.commit()
            return True, ""
        except sqlite3.IntegrityError:
            conn.rollback()
            return False, "你已经拥有该装扮"
        except Exception as exc:
            conn.rollback()
            _log_database_failure("purchase_dress_atomic", exc)
            return False, "购买失败"

    def vote_pet_show_atomic(
        self, show_id: int, voter_id: str, pet_user_id: str, max_votes: int
    ) -> bool:
        if max_votes <= 0 or str(voter_id) == str(pet_user_id):
            return False
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now()
            show = conn.execute(
                """SELECT id, group_id FROM pet_shows
                   WHERE id = ? AND is_active = 1
                     AND start_time <= ? AND end_time > ?""",
                (show_id, now.isoformat(), now.isoformat()),
            ).fetchone()
            if show is None:
                conn.rollback()
                return False
            group_id = int(show["group_id"])
            voter = conn.execute(
                "SELECT 1 FROM users WHERE user_id = ? AND group_id = ?",
                (voter_id, group_id),
            ).fetchone()
            target_pet = conn.execute(
                "SELECT 1 FROM pets WHERE user_id = ? AND group_id = ?",
                (pet_user_id, group_id),
            ).fetchone()
            if voter is None or target_pet is None:
                conn.rollback()
                return False
            count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM pet_show_votes WHERE show_id = ? AND voter_user_id = ?",
                (show_id, voter_id),
            ).fetchone()["cnt"]
            if int(count) >= max_votes:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO pet_show_votes (show_id, voter_user_id, pet_user_id, created_at) VALUES (?, ?, ?, ?)",
                (show_id, voter_id, pet_user_id, now.isoformat()),
            )
            conn.commit()
            return True
        except Exception as exc:
            conn.rollback()
            _log_database_failure("vote_pet_show_atomic", exc)
            return False

    def settle_pet_show_atomic(
        self,
        group_id: int,
        *,
        force: bool = False,
    ) -> PetShowSettlementResult | None:
        """一次性关闭展示会并提交有效票数、奖励和冠军称号。"""
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now()
            now_iso = now.isoformat()
            due_clause = "" if force else " AND end_time <= ?"
            params: tuple[Any, ...] = (group_id,) if force else (group_id, now_iso)
            show = conn.execute(
                """SELECT * FROM pet_shows
                   WHERE group_id = ? AND is_active = 1"""
                + due_clause
                + " ORDER BY id DESC LIMIT 1",
                params,
            ).fetchone()
            if show is None:
                conn.rollback()
                return None

            claim_sql = "UPDATE pet_shows SET status = 'settling' WHERE id = ? AND is_active = 1"
            claim_params: tuple[Any, ...] = (show["id"],)
            if not force:
                claim_sql += " AND end_time <= ?"
                claim_params = (show["id"], now_iso)
            claim = conn.execute(claim_sql, claim_params)
            if claim.rowcount != 1:
                conn.rollback()
                return None

            vote_rows = conn.execute(
                """SELECT votes.pet_user_id, pets.name AS pet_name,
                          COUNT(*) AS vote_count
                   FROM pet_show_votes AS votes
                   JOIN pets ON pets.user_id = votes.pet_user_id
                            AND pets.group_id = ?
                   JOIN users ON users.user_id = votes.pet_user_id
                             AND users.group_id = ?
                   WHERE votes.show_id = ?
                   GROUP BY votes.pet_user_id, pets.name
                   ORDER BY vote_count DESC, votes.pet_user_id ASC
                   LIMIT 3""",
                (group_id, group_id, show["id"]),
            ).fetchall()
            rewards = (
                int(PET_SHOW_CONFIG["reward_first"]),
                int(PET_SHOW_CONFIG["reward_second"]),
                int(PET_SHOW_CONFIG["reward_third"]),
            )
            winners: list[PetShowWinner] = []
            for index, vote_row in enumerate(vote_rows):
                user_id = str(vote_row["pet_user_id"])
                granted = self._credit_coins_in_transaction(
                    conn,
                    user_id,
                    group_id,
                    rewards[index],
                    reason="pet_show",
                    reference_id=f"show:{show['id']}:{index}:{user_id}",
                    daily_limit=_DAILY_COIN_LIMIT,
                    now_iso=now_iso,
                )
                if index == 0:
                    self._grant_temporary_title_in_transaction(
                        conn,
                        user_id,
                        group_id,
                        "展示会冠军",
                        now=now,
                    )

                winners.append(
                    PetShowWinner(
                        user_id=user_id,
                        pet_name=str(vote_row["pet_name"]),
                        vote_count=int(vote_row["vote_count"]),
                        coins_granted=granted,
                    )
                )

            conn.execute(
                """UPDATE pet_shows
                   SET is_active = 0, status = 'settled', settled_at = ?
                   WHERE id = ? AND is_active = 1 AND status = 'settling'""",
                (now_iso, show["id"]),
            )
            conn.commit()
            return PetShowSettlementResult(
                show_id=int(show["id"]),
                title=str(show["title"] or "展示会"),
                winners=tuple(winners),
            )
        except Exception as exc:
            conn.rollback()
            _log_database_failure("settle_pet_show_atomic", exc)
            return None

    def like_pet_atomic(
        self, user_id: str, target_user_id: str, group_id: int, daily_limit: int
    ) -> bool:
        if user_id == target_user_id or daily_limit <= 0:
            return False
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            today = utc_now().strftime("%Y-%m-%d")
            row = conn.execute(
                """SELECT like_count FROM daily_likes WHERE user_id = ?
                   AND target_user_id = ? AND group_id = ? AND like_date = ?""",
                (user_id, target_user_id, group_id, today),
            ).fetchone()
            if row and int(row["like_count"]) >= daily_limit:
                conn.rollback()
                return False
            pet_cursor = conn.execute(
                """UPDATE pets SET likes = likes + 1, intimacy = intimacy + 1,
                   version = version + 1
                   WHERE user_id = ? AND group_id = ?""",
                (target_user_id, group_id),
            )
            if pet_cursor.rowcount != 1:
                conn.rollback()
                return False
            conn.execute(
                """INSERT INTO daily_likes (user_id, target_user_id, group_id, like_date, like_count)
                   VALUES (?, ?, ?, ?, 1)
                   ON CONFLICT(user_id, target_user_id, group_id, like_date)
                   DO UPDATE SET like_count = like_count + 1""",
                (user_id, target_user_id, group_id, today),
            )
            conn.commit()
            return True
        except Exception as exc:
            conn.rollback()
            _log_database_failure("like_pet_atomic", exc)
            return False

    def treat_pet_atomic(
        self,
        user_id: str,
        group_id: int,
        pet_id: int,
        item_id: str,
        *,
        health_gain: int,
        clean_gain: int,
        daily_limit: int,
        cooldown_seconds: int,
    ) -> TreatPetAtomicResult:
        """在同一事务中领取治疗配额、扣除药品并更新宠物状态。"""
        item = DEFAULT_ITEMS.get(item_id)
        if item is None or item.get("type") != ItemType.MEDICINE:
            return TreatPetAtomicResult(False, "该道具不是药品")
        if health_gain < 0 or clean_gain < 0 or daily_limit <= 0 or cooldown_seconds < 0:
            return TreatPetAtomicResult(False, "治疗参数无效")
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            pet_row = conn.execute(
                """SELECT * FROM pets
                   WHERE id = ? AND user_id = ? AND group_id = ?""",
                (pet_id, user_id, group_id),
            ).fetchone()
            if pet_row is None:
                conn.rollback()
                return TreatPetAtomicResult(False, "宠物不存在")

            items = self._load_inventory_items(conn, user_id, group_id)
            available = int(items.get(item_id, 0))
            if available <= 0:
                conn.rollback()
                return TreatPetAtomicResult(False, "背包中没有该药品")

            claimed, remaining = self._claim_action_quota_in_transaction(
                conn,
                user_id,
                group_id,
                "treat",
                daily_limit,
                cooldown_seconds,
            )
            if not claimed:
                conn.rollback()
                reason = "治疗冷却中" if remaining > 0 else "今日治疗次数已达上限"
                return TreatPetAtomicResult(False, reason, max(0, remaining))

            if available == 1:
                items.pop(item_id, None)
            else:
                items[item_id] = available - 1
            health = min(100, int(pet_row["health"]) + int(health_gain))
            clean = min(100, int(pet_row["clean"]) + int(clean_gain))
            status = PetStatus.NORMAL.value if health >= 50 else str(pet_row["status"])
            status_expire_time = None if health >= 50 else pet_row["status_expire_time"]
            updated = conn.execute(
                """UPDATE pets SET health = ?, clean = ?, status = ?,
                   status_expire_time = ?, last_update = ?, version = version + 1
                   WHERE id = ? AND version = ?""",
                (
                    health,
                    clean,
                    status,
                    status_expire_time,
                    utc_now().isoformat(),
                    pet_id,
                    int(pet_row["version"]),
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("pet changed during treatment")
            self._save_inventory_items(conn, user_id, group_id, items)
            updated_row = conn.execute("SELECT * FROM pets WHERE id = ?", (pet_id,)).fetchone()
            if updated_row is None:
                raise RuntimeError("treated pet disappeared before commit")
            conn.commit()
            return TreatPetAtomicResult(True, pet=self._row_to_pet(updated_row))
        except Exception as exc:
            conn.rollback()
            _log_database_failure("treat_pet_atomic", exc)
            return TreatPetAtomicResult(False, "治疗失败")

    def atomic_update_pet_and_user(
        self,
        pet: Pet,
        user: User,
        *,
        inventory: Inventory | None = None,
        task_type: str | None = None,
        group_task_type: str | None = None,
    ) -> bool:
        """在同一事务中原子更新宠物、用户与关联动作状态。"""
        return self._commit_pet_and_user(
            pet,
            user,
            inventory=inventory,
            task_type=task_type,
            group_task_type=group_task_type,
        ).success

    def commit_pet_action(
        self,
        pet: Pet,
        user: User,
        *,
        action: str,
        daily_limit: int,
        cooldown_seconds: int,
        requested_coins: int = 0,
        consume_item_id: str | None = None,
        inventory_grants: dict[str, int] | None = None,
        free_feed_increment: int = 0,
        free_feed_limit: int = 0,
        task_type: str | None = None,
        group_task_type: str | None = None,
    ) -> PetActionAtomicResult:
        """统一提交动作配额、资源、状态、计数器和任务进度。"""
        if action not in _PET_ACTION_COUNTERS:
            return PetActionAtomicResult(False, reason="invalid_action")
        return self._commit_pet_and_user(
            pet,
            user,
            task_type=task_type,
            group_task_type=group_task_type,
            quota_action=action,
            quota_daily_limit=daily_limit,
            quota_cooldown_seconds=cooldown_seconds,
            requested_coins=requested_coins,
            consume_item_id=consume_item_id,
            inventory_grants=inventory_grants,
            free_feed_increment=free_feed_increment,
            free_feed_limit=free_feed_limit,
        )

    def _commit_pet_and_user(
        self,
        pet: Pet,
        user: User,
        *,
        inventory: Inventory | None = None,
        task_type: str | None = None,
        group_task_type: str | None = None,
        quota_action: str | None = None,
        quota_daily_limit: int = 0,
        quota_cooldown_seconds: int = 0,
        requested_coins: int = 0,
        consume_item_id: str | None = None,
        inventory_grants: dict[str, int] | None = None,
        free_feed_increment: int = 0,
        free_feed_limit: int = 0,
    ) -> PetActionAtomicResult:
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            pet_row = conn.execute("SELECT * FROM pets WHERE id = ?", (pet.id,)).fetchone()
            if pet_row is None:
                conn.rollback()
                return PetActionAtomicResult(False, reason="missing_pet")
            latest_pet = self._row_to_pet(pet_row)
            if latest_pet.user_id != pet.user_id or latest_pet.group_id != pet.group_id:
                conn.rollback()
                return PetActionAtomicResult(False, reason="missing_pet")
            pet_to_persist = pet.merged_onto(latest_pet)

            user_row = conn.execute(
                "SELECT * FROM users WHERE user_id = ? AND group_id = ?",
                (user.user_id, user.group_id),
            ).fetchone()
            if user_row is None:
                conn.rollback()
                return PetActionAtomicResult(False, reason="missing_user")
            current_user = self._row_to_user(user_row)
            user_to_persist = user.merged_onto(current_user)
            granted_coins = 0
            inventory_items: dict[str, int] | None = None
            merged_inventory_items: dict[str, int] | None = None
            merged_inventory_version: int | None = None
            if quota_action is not None:
                today_counter, _total_counter = _PET_ACTION_COUNTERS[quota_action]
                if getattr(current_user, today_counter) >= max(0, int(quota_daily_limit)):
                    conn.rollback()
                    return PetActionAtomicResult(False, reason="daily_limit")
                if free_feed_increment and current_user.today_free_feed_count >= max(
                    0, int(free_feed_limit)
                ):
                    conn.rollback()
                    return PetActionAtomicResult(False, reason="free_feed_limit")

                if consume_item_id or inventory_grants:
                    inventory_items = self._load_inventory_items(
                        conn,
                        user.user_id,
                        user.group_id,
                    )
                if consume_item_id:
                    assert inventory_items is not None
                    available = int(inventory_items.get(consume_item_id, 0))
                    if available <= 0:
                        conn.rollback()
                        return PetActionAtomicResult(False, reason="inventory")
                    if available == 1:
                        inventory_items.pop(consume_item_id, None)
                    else:
                        inventory_items[consume_item_id] = available - 1
                if inventory_grants:
                    assert inventory_items is not None
                    for item_id, amount in inventory_grants.items():
                        if amount > 0:
                            inventory_items[item_id] = int(inventory_items.get(item_id, 0)) + int(
                                amount
                            )

                claimed, remaining = self._claim_action_quota_in_transaction(
                    conn,
                    user.user_id,
                    user.group_id,
                    quota_action,
                    quota_daily_limit,
                    quota_cooldown_seconds,
                )
                if not claimed:
                    conn.rollback()
                    return PetActionAtomicResult(
                        False,
                        reason="cooldown" if remaining > 0 else "daily_limit",
                        remaining=max(0, remaining),
                    )

                granted_coins = min(
                    max(0, int(requested_coins)),
                    max(0, _DAILY_COIN_LIMIT - current_user.today_coins_earned),
                )
                current_user.coins += granted_coins
                current_user.today_coins_earned += granted_coins
                current_user.increment_action(quota_action)
                if free_feed_increment:
                    current_user.increment_action("free_feed", free_feed_increment)
                current_user.last_active = utc_now()
                user_to_persist = current_user

            if inventory is not None and quota_action is None:
                inventory_row = conn.execute(
                    "SELECT items, version FROM inventories WHERE user_id = ? AND group_id = ?",
                    (inventory.user_id, inventory.group_id),
                ).fetchone()
                latest_items = json.loads(inventory_row["items"] or "{}") if inventory_row else {}
                merged_inventory_items = inventory.merged_onto(latest_items)
                merged_inventory_version = int(inventory_row["version"]) + 1 if inventory_row else 0

            pet_written = self._write_pet_in_transaction(conn, pet_to_persist)
            user_written = self._write_user_in_transaction(conn, user_to_persist)
            if not pet_written or not user_written:
                conn.rollback()
                return PetActionAtomicResult(False, reason="persistence")
            self._record_asset_delta(
                conn,
                user_id=user_to_persist.user_id,
                group_id=user_to_persist.group_id,
                asset_type="coins",
                delta=int(user_to_persist.coins) - int(user_row["coins"]),
                reason=(
                    f"pet_action_{quota_action}" if quota_action is not None else "pet_state_update"
                ),
            )
            if inventory_items is not None:
                self._save_inventory_items(
                    conn,
                    user.user_id,
                    user.group_id,
                    inventory_items,
                )
            if merged_inventory_items is not None and inventory is not None:
                self._save_inventory_items(
                    conn,
                    inventory.user_id,
                    inventory.group_id,
                    merged_inventory_items,
                )
            now = utc_now()
            today = now.strftime("%Y-%m-%d")
            now_str = now.isoformat()
            if task_type:
                self._increment_task_in_transaction(
                    conn,
                    user.user_id,
                    user.group_id,
                    task_type,
                    now_iso=now_str,
                    today=today,
                )
            if group_task_type:
                self._ensure_group_task_templates(conn, user.group_id, today)
                conn.execute(
                    """UPDATE group_tasks SET
                           current_value = MIN(current_value + 1, target_value),
                           is_completed = CASE
                               WHEN current_value + 1 >= target_value THEN 1
                               ELSE is_completed
                           END
                       WHERE group_id = ? AND task_type = ? AND created_date = ?
                         AND is_completed = 0""",
                    (user.group_id, group_task_type, today),
                )
            if quota_action is not None:
                self._advance_activities_in_transaction(
                    conn,
                    user.group_id,
                    quota_action,
                )
            conn.commit()
            pet.__dict__.update(pet_to_persist.__dict__)
            pet.mark_persisted()
            user.__dict__.update(user_to_persist.__dict__)
            user.mark_persisted()
            if (
                inventory is not None
                and merged_inventory_items is not None
                and merged_inventory_version is not None
            ):
                inventory.items = merged_inventory_items
                inventory.version = merged_inventory_version
                inventory.mark_persisted()
            return PetActionAtomicResult(True, coins_granted=granted_coins)
        except Exception as exc:
            conn.rollback()
            _log_database_failure("atomic_update_pet_and_user", exc)
            return PetActionAtomicResult(False, reason="persistence")

    def get_all_group_ids(self) -> list[int]:
        try:
            conn = self._get_connection()
            cursor = conn.execute("SELECT DISTINCT group_id FROM users")
            return [row["group_id"] for row in cursor.fetchall()]
        except Exception as exc:
            _log_database_failure("get_all_group_ids", exc)
            return []

    def settle_expired_trade_listings(self, group_id: int | None = None) -> int:
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now().isoformat()
            params: tuple = (now,)
            group_clause = ""
            if group_id is not None:
                group_clause = " AND group_id = ?"
                params = (params[0], group_id)
            rows = conn.execute(
                """SELECT * FROM trade_listings WHERE is_active = 1
                   AND expires_at <= ?"""
                + group_clause,
                params,
            ).fetchall()
            settled = 0
            for row in rows:
                settled += int(self._expire_trade_listing_in_transaction(conn, row, now))
            conn.commit()
            return settled
        except Exception as exc:
            conn.rollback()
            _log_database_failure("settle_expired_trade_listings", exc)
            return 0

    def _expire_trade_listing_in_transaction(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        now: str,
    ) -> bool:
        """在调用方事务中认领一个到期托管单并退还道具。"""
        claim = conn.execute(
            """UPDATE trade_listings SET is_active = 0, status = 'expired'
               WHERE id = ? AND is_active = 1 AND expires_at <= ?""",
            (row["id"], now),
        )
        if claim.rowcount != 1:
            return False

        seller_id = str(row["seller_user_id"])
        group_id = int(row["group_id"])
        item_id = str(row["item_id"])
        amount = int(row["amount"])
        items = self._load_inventory_items(conn, seller_id, group_id)
        items[item_id] = int(items.get(item_id, 0)) + amount
        self._save_inventory_items(conn, seller_id, group_id, items)
        conn.execute(
            """INSERT INTO asset_ledger
               (user_id, group_id, asset_type, delta, reason, reference_id, created_at)
               VALUES (?, ?, 'item', ?, 'trade_expiry_refund', ?, ?)""",
            (seller_id, group_id, amount, f"trade-expire:{row['id']}", now),
        )
        return True

    def get_enabled_group_ids(self, *, require_activity: bool = False) -> list[int]:
        try:
            conn = self._get_connection()
            condition = (
                "enabled = 1 AND activity_enabled = 1" if require_activity else "enabled = 1"
            )
            rows = conn.execute(
                f"SELECT group_id FROM group_configs WHERE {condition} ORDER BY group_id"
            ).fetchall()
            return [int(row["group_id"]) for row in rows]
        except Exception as exc:
            _log_database_failure("get_enabled_group_ids", exc)
            return []

    @staticmethod
    def _claim_scheduler_run_in_transaction(
        conn: sqlite3.Connection,
        job_name: str,
        period_key: str,
        lease_seconds: int,
    ) -> bool:
        now = utc_now()
        now_iso = now.isoformat()
        lease_until = (now + timedelta(seconds=max(1, lease_seconds))).isoformat()
        row = conn.execute(
            """SELECT status, lease_until FROM scheduler_runs
               WHERE job_name = ? AND period_key = ?""",
            (job_name, period_key),
        ).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO scheduler_runs
                   (job_name, period_key, claimed_at, status, lease_until,
                    attempt_count, completed_at)
                   VALUES (?, ?, ?, 'running', ?, 1, NULL)""",
                (job_name, period_key, now_iso, lease_until),
            )
            return True
        if str(row["status"]) == "completed":
            return False
        if (
            str(row["status"]) == "running"
            and row["lease_until"]
            and str(row["lease_until"]) > now_iso
        ):
            return False
        cursor = conn.execute(
            """UPDATE scheduler_runs SET
                   claimed_at = ?, status = 'running', lease_until = ?,
                   attempt_count = attempt_count + 1, completed_at = NULL
               WHERE job_name = ? AND period_key = ? AND status != 'completed'""",
            (now_iso, lease_until, job_name, period_key),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _complete_scheduler_run_in_transaction(
        conn: sqlite3.Connection,
        job_name: str,
        period_key: str,
    ) -> bool:
        cursor = conn.execute(
            """UPDATE scheduler_runs SET
                   status = 'completed', lease_until = NULL, completed_at = ?
               WHERE job_name = ? AND period_key = ? AND status = 'running'""",
            (utc_now().isoformat(), job_name, period_key),
        )
        return cursor.rowcount == 1

    def run_daily_reset_atomic(self, period_key: str, group_id: int) -> DailyResetResult | None:
        """原子领取每日任务、重置计数、增加宠物年龄并完成调度。"""
        job_name = "qingpet_daily_reset"
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not self._claim_scheduler_run_in_transaction(
                conn,
                job_name,
                period_key,
                300,
            ):
                conn.rollback()
                return None
            user_cursor = conn.execute(
                """UPDATE users SET
                       today_coins_earned = 0, today_feed_count = 0,
                       today_clean_count = 0, today_play_count = 0,
                       today_train_count = 0, today_explore_count = 0,
                       today_visit_count = 0, today_gift_count = 0,
                       today_free_feed_count = 0, today_message_count = 0,
                       version = version + 1
                   WHERE group_id = ?""",
                (group_id,),
            )
            pet_cursor = conn.execute(
                """UPDATE pets SET age = age + 1, version = version + 1
                   WHERE group_id = ?""",
                (group_id,),
            )
            if not self._complete_scheduler_run_in_transaction(conn, job_name, period_key):
                raise sqlite3.DatabaseError("scheduler completion claim lost")
            conn.commit()
            return DailyResetResult(user_cursor.rowcount, pet_cursor.rowcount)
        except Exception as exc:
            conn.rollback()
            _log_database_failure("run_daily_reset_atomic", exc)
            return None

    def settle_weekly_activity_atomic(
        self,
        period_key: str,
        group_id: int,
    ) -> WeeklyActivitySettlementResult | None:
        """仅一次领取并结算周排行奖励、称号、账本和调度完成状态。"""
        job_name = "qingpet_weekly_activity"
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not self._claim_scheduler_run_in_transaction(
                conn,
                job_name,
                period_key,
                300,
            ):
                conn.rollback()
                return None
            ranking = conn.execute(
                """SELECT user_id, name AS pet_name,
                          (hunger + mood + clean + energy + health) / 5.0 AS score
                   FROM pets WHERE group_id = ?
                   ORDER BY score DESC, user_id ASC LIMIT 3""",
                (group_id,),
            ).fetchall()
            now = utc_now()
            winners: list[WeeklyRankingWinner] = []
            for index, rank_row in enumerate(ranking):
                user_id = str(rank_row["user_id"])
                grant = self._credit_coins_in_transaction(
                    conn,
                    user_id,
                    group_id,
                    _WEEKLY_RANKING_REWARDS[index],
                    reason="weekly_ranking",
                    reference_id=f"weekly:{period_key}:{index}:{user_id}",
                    daily_limit=_DAILY_COIN_LIMIT,
                    now_iso=now.isoformat(),
                )
                title_granted = index == 0 and self._grant_temporary_title_in_transaction(
                    conn,
                    user_id,
                    group_id,
                    "本周之星",
                    now=now,
                )
                winners.append(
                    WeeklyRankingWinner(
                        user_id=user_id,
                        pet_name=str(rank_row["pet_name"]),
                        score=round(float(rank_row["score"]), 1),
                        coins_granted=grant,
                        title_granted=title_granted,
                    )
                )
            if not self._complete_scheduler_run_in_transaction(conn, job_name, period_key):
                raise sqlite3.DatabaseError("scheduler completion claim lost")
            conn.commit()
            return WeeklyActivitySettlementResult(tuple(winners))
        except Exception as exc:
            conn.rollback()
            _log_database_failure("settle_weekly_activity_atomic", exc)
            return None

    # ─────────────────── 称号过期清理 ─────────────────────────────

    def cleanup_expired_titles(self) -> int:
        """在一个事务内移除所有到期称号及其过期记录。"""
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now().isoformat()
            expired = conn.execute(
                """SELECT user_id, group_id, title FROM title_expiry
                WHERE expires_at <= ?""",
                (now,),
            ).fetchall()

            count = 0
            for row in expired:
                user_id, group_id, title = row["user_id"], row["group_id"], row["title"]
                user_row = conn.execute(
                    "SELECT titles FROM users WHERE user_id = ? AND group_id = ?",
                    (user_id, group_id),
                ).fetchone()
                if user_row and user_row["titles"]:
                    titles = json.loads(user_row["titles"])
                    if title in titles:
                        titles.remove(title)
                        conn.execute(
                            """UPDATE users SET titles = ?, version = version + 1
                               WHERE user_id = ? AND group_id = ?""",
                            (json.dumps(titles), user_id, group_id),
                        )
                        count += 1

            conn.execute("DELETE FROM title_expiry WHERE expires_at <= ?", (now,))
            conn.commit()
            return count
        except Exception as exc:
            conn.rollback()
            _log_database_failure("cleanup_expired_titles", exc)
            return 0

    # ─────────────────── 金币排行（单次 JOIN 聚合）────────────────

    def get_group_economy_snapshot(
        self,
        group_id: int,
    ) -> tuple[GroupEconomySnapshot, CoinLedgerReconciliation]:
        """聚合宠物主余额，并核对检查点之后的金币账本增量。

        运行时余额以 ``users.coins`` 为权威。首次调用把既有历史作为基线；后续每次净余额变化都必须
        能由金币账本增量之和解释。
        """

        empty_snapshot = GroupEconomySnapshot()
        unavailable = CoinLedgerReconciliation(
            status="unavailable",
            current_balance=0,
            expected_balance=0,
            difference=0,
            consistent=False,
        )
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                WITH pet_stats AS (
                    SELECT
                        COUNT(*) AS total_pets,
                        COALESCE(SUM(experience), 0) AS total_experience,
                        COALESCE(SUM(intimacy), 0) AS total_intimacy,
                        COALESCE(AVG(
                            (hunger + mood + clean + energy + health) / 5.0
                        ), 0.0) AS average_care_score
                    FROM pets
                    WHERE group_id = ?
                ),
                user_stats AS (
                    SELECT
                        COALESCE(SUM(u.coins), 0) AS total_coins,
                        COALESCE(SUM(CASE WHEN
                            u.today_feed_count + u.today_clean_count
                            + u.today_play_count + u.today_train_count
                            + u.today_explore_count > 0
                            THEN 1 ELSE 0 END), 0) AS active_today
                    FROM users AS u
                    WHERE u.group_id = ?
                      AND EXISTS (
                          SELECT 1 FROM pets AS p
                          WHERE p.user_id = u.user_id AND p.group_id = u.group_id
                      )
                )
                SELECT pet_stats.*, user_stats.total_coins, user_stats.active_today
                FROM pet_stats CROSS JOIN user_stats
                """,
                (group_id, group_id),
            ).fetchone()
            snapshot = GroupEconomySnapshot(
                total_pets=int(row["total_pets"] or 0),
                total_coins=int(row["total_coins"] or 0),
                total_experience=int(row["total_experience"] or 0),
                total_intimacy=int(row["total_intimacy"] or 0),
                average_care_score=float(row["average_care_score"] or 0.0),
                active_today=int(row["active_today"] or 0),
            )
            ledger_row = conn.execute(
                """
                SELECT COALESCE(SUM(l.delta), 0) AS ledger_total
                FROM asset_ledger AS l
                WHERE l.group_id = ? AND l.asset_type = 'coins'
                  AND EXISTS (
                      SELECT 1 FROM pets AS p
                      WHERE p.user_id = l.user_id AND p.group_id = l.group_id
                  )
                """,
                (group_id,),
            ).fetchone()
            ledger_total = int(ledger_row["ledger_total"] or 0)
            checkpoint = conn.execute(
                """SELECT balance, ledger_total
                   FROM asset_reconciliation_checkpoints
                   WHERE group_id = ? AND asset_type = 'coins'""",
                (group_id,),
            ).fetchone()
            checked_at = utc_now().isoformat()
            if checkpoint is None:
                conn.execute(
                    """INSERT INTO asset_reconciliation_checkpoints
                       (group_id, asset_type, balance, ledger_total, checked_at)
                       VALUES (?, 'coins', ?, ?, ?)""",
                    (group_id, snapshot.total_coins, ledger_total, checked_at),
                )
                reconciliation = CoinLedgerReconciliation(
                    status="baseline_created",
                    current_balance=snapshot.total_coins,
                    expected_balance=snapshot.total_coins,
                    difference=0,
                    consistent=True,
                )
            else:
                expected_balance = int(checkpoint["balance"]) + (
                    ledger_total - int(checkpoint["ledger_total"])
                )
                difference = snapshot.total_coins - expected_balance
                consistent = difference == 0
                reconciliation = CoinLedgerReconciliation(
                    status="consistent" if consistent else "mismatch",
                    current_balance=snapshot.total_coins,
                    expected_balance=expected_balance,
                    difference=difference,
                    consistent=consistent,
                )
                if consistent:
                    conn.execute(
                        """UPDATE asset_reconciliation_checkpoints
                           SET balance = ?, ledger_total = ?, checked_at = ?
                           WHERE group_id = ? AND asset_type = 'coins'""",
                        (snapshot.total_coins, ledger_total, checked_at, group_id),
                    )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            _log_database_failure("get_group_economy_snapshot", exc)
            return empty_snapshot, unavailable
        if not reconciliation.consistent:
            logger.warning(
                "QingPet coin ledger reconciliation mismatch difference=%d",
                reconciliation.difference,
            )
        return snapshot, reconciliation

    def get_coins_ranking(self, group_id: int, limit: int = 10) -> list[dict]:
        """通过单次 JOIN 查询返回金币排行，避免逐用户读取宠物。"""
        try:
            conn = self._get_connection()
            cursor = conn.execute(
                """
                SELECT u.user_id, p.name as pet_name, u.coins
                FROM users u
                JOIN pets p ON u.user_id = p.user_id AND u.group_id = p.group_id
                WHERE u.group_id = ?
                ORDER BY u.coins DESC
                LIMIT ?
            """,
                (group_id, max(0, int(limit))),
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception as exc:
            _log_database_failure("get_coins_ranking", exc)
            return []

    def get_pet_ranking(self, group_id: int, ranking_type: str, limit: int = 10) -> list[dict]:
        expressions = {
            "care_score": "(hunger + mood + clean + energy + health) / 5.0",
            "intimacy": "intimacy",
            "experience": "experience",
        }
        expression = expressions.get(ranking_type)
        if expression is None:
            return []
        try:
            conn = self._get_connection()
            rows = conn.execute(
                f"""SELECT user_id, name AS pet_name, {expression} AS score
                    FROM pets WHERE group_id = ? ORDER BY score DESC LIMIT ?""",
                (group_id, max(0, int(limit))),
            ).fetchall()
            return [dict(row) for row in rows]
        except Exception as exc:
            _log_database_failure("get_pet_ranking", exc)
            return []

    def get_active_trustee_keys(self) -> set[tuple[str, int]]:
        try:
            rows = (
                self._get_connection()
                .execute(
                    "SELECT user_id, group_id FROM users WHERE trustee_until > ?",
                    (utc_now().isoformat(),),
                )
                .fetchall()
            )
            return {(str(row["user_id"]), int(row["group_id"])) for row in rows}
        except Exception as exc:
            _log_database_failure("get_active_trustee_keys", exc)
            return set()
