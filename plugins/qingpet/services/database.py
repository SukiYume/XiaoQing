import sqlite3
import json
import logging
import threading
import time
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional, List, Dict, Tuple
from pathlib import Path

from ..models import Pet, User, Item, Inventory, GroupConfig, PluginConfig, OperationLog
from ..utils.constants import (
    DEFAULT_ITEMS, PetStage, PetPersonality, PetStatus
)
from ..utils.time import utc_now

logger = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_OPERATION_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SAFE_ERROR_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,95}\Z")


def _log_database_failure(operation: str, exc: BaseException) -> None:
    """Record a stable failure marker without database data or exception text."""

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
    """Committed result of one idempotent pet visit."""

    success: bool
    reason: str = ""
    pet_name: str = ""
    visitor_grant: int = 0
    target_grant: int = 0
    intimacy_grant: int = 0
    duplicate: bool = False


@dataclass(frozen=True)
class MinigameOutcome:
    """Requested effects and replayable presentation data for one game outcome."""

    requested_coins: int = 0
    experience: int = 0
    energy_cost: int = 0
    payload: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class MinigameAtomicResult:
    """Committed, idempotently replayable result of one minigame."""

    success: bool
    reason: str = ""
    pet_name: str = ""
    opponent_pet_name: str = ""
    coin_grant: int = 0
    experience_grant: int = 0
    energy_cost: int = 0
    payload: Optional[Dict[str, Any]] = None
    duplicate: bool = False


@dataclass(frozen=True)
class LeaveMessageAtomicResult:
    """Committed result of one quota-checked pet-board message."""

    success: bool
    reason: str = ""
    pet_name: str = ""

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
    },
    "pets": {
        "likes": "INTEGER DEFAULT 0",
        "dress_hat": "TEXT",
        "dress_clothes": "TEXT",
        "dress_accessory": "TEXT",
        "dress_background": "TEXT",
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
}


class Database:
    """
    数据库服务层。
    CR修复: 添加索引、持久化visit/gift时间、personality兼容、事务支持、
    date范围查询替代LIKE、delete_pet、increment_all_pet_ages、交易/展示会/装扮表。
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        path = Path(db_path)
        if path.exists() and path.stat().st_size > 0:
            backup = path.with_suffix(path.suffix + ".pre-migration.bak")
            if not backup.exists():
                shutil.copy2(path, backup)
        self._init_database()

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def cleanup(self):
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    # ──────────────────── 初始化 ────────────────────

    def _init_database(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
            )""")

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
                    UNIQUE (user_id, group_id)
                )
            """)

            cursor.execute("""CREATE TABLE IF NOT EXISTS inventories (
                user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
                items TEXT, PRIMARY KEY (user_id, group_id))""")

            cursor.execute("""CREATE TABLE IF NOT EXISTS group_configs (
                group_id INTEGER PRIMARY KEY, enabled BOOLEAN DEFAULT 1,
                economy_multiplier REAL DEFAULT 1.0, decay_multiplier REAL DEFAULT 1.0,
                trade_enabled BOOLEAN DEFAULT 0, natural_trigger_enabled BOOLEAN DEFAULT 0,
                activity_enabled BOOLEAN DEFAULT 1, sensitive_words TEXT DEFAULT '[]')""")

            cursor.execute("""CREATE TABLE IF NOT EXISTS plugin_configs (
                key TEXT PRIMARY KEY, value TEXT)""")

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
                PRIMARY KEY (job_name, period_key)
            )""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS asset_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
                asset_type TEXT NOT NULL, delta INTEGER NOT NULL,
                reason TEXT NOT NULL, reference_id TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (asset_type, reference_id)
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
            cursor.execute("""CREATE TRIGGER IF NOT EXISTS trg_users_daily_coin_cap
                AFTER UPDATE OF today_coins_earned ON users
                WHEN NEW.today_coins_earned > 500
                BEGIN
                    UPDATE users
                    SET coins = MAX(0, NEW.coins - (NEW.today_coins_earned - 500)),
                        today_coins_earned = 500
                    WHERE user_id = NEW.user_id AND group_id = NEW.group_id;
                END""")
            cursor.execute("""CREATE TRIGGER IF NOT EXISTS trg_users_nonnegative_insert
                BEFORE INSERT ON users WHEN NEW.coins < 0 OR NEW.friendship_points < 0
                BEGIN SELECT RAISE(ABORT, 'negative asset balance'); END""")
            cursor.execute("""CREATE TRIGGER IF NOT EXISTS trg_users_nonnegative_update
                BEFORE UPDATE OF coins, friendship_points ON users
                WHEN NEW.coins < 0 OR NEW.friendship_points < 0
                BEGIN SELECT RAISE(ABORT, 'negative asset balance'); END""")

            # 交易市场表 (新增)
            cursor.execute("""CREATE TABLE IF NOT EXISTS trade_listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
                item_id TEXT NOT NULL, amount INTEGER DEFAULT 1,
                price INTEGER NOT NULL, created_at TEXT, expires_at TEXT,
                is_active BOOLEAN DEFAULT 1, status TEXT DEFAULT 'active')""")

            # 群累计任务表 (新增)
            cursor.execute("""CREATE TABLE IF NOT EXISTS group_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL, task_type TEXT NOT NULL,
                target_value INTEGER NOT NULL, current_value INTEGER DEFAULT 0,
                reward_coins INTEGER DEFAULT 0, description TEXT DEFAULT '',
                created_date TEXT, is_completed BOOLEAN DEFAULT 0)""")

            # 装扮拥有表 (新增)
            cursor.execute("""CREATE TABLE IF NOT EXISTS dress_inventory (
                user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
                dress_item_id TEXT NOT NULL,
                PRIMARY KEY (user_id, group_id, dress_item_id))""")

            # CR Fix #13: 添加数据库索引
            self._create_indexes(cursor)

            # 兼容旧数据库迁移
            self._safe_add_column(cursor, "users", "total_feed_count", "INTEGER DEFAULT 0")
            self._safe_add_column(cursor, "users", "total_clean_count", "INTEGER DEFAULT 0")
            self._safe_add_column(cursor, "users", "total_play_count", "INTEGER DEFAULT 0")
            self._safe_add_column(cursor, "users", "total_train_count", "INTEGER DEFAULT 0")
            self._safe_add_column(cursor, "users", "total_explore_count", "INTEGER DEFAULT 0")
            self._safe_add_column(cursor, "users", "total_visit_count", "INTEGER DEFAULT 0")
            self._safe_add_column(cursor, "users", "total_gift_count", "INTEGER DEFAULT 0")
            self._safe_add_column(cursor, "users", "titles", "TEXT DEFAULT '[]'")
            self._safe_add_column(cursor, "pets", "likes", "INTEGER DEFAULT 0")
            self._safe_add_column(cursor, "pets", "dress_hat", "TEXT")
            self._safe_add_column(cursor, "pets", "dress_clothes", "TEXT")
            self._safe_add_column(cursor, "pets", "dress_accessory", "TEXT")
            self._safe_add_column(cursor, "pets", "dress_background", "TEXT")
            self._safe_add_column(cursor, "group_configs", "sensitive_words", "TEXT DEFAULT '[]'")
            self._safe_add_column(cursor, "tasks", "created_date", "TEXT")
            self._safe_add_column(cursor, "users", "today_free_feed_count", "INTEGER DEFAULT 0")
            self._safe_add_column(cursor, "users", "today_message_count", "INTEGER DEFAULT 0")
            self._safe_add_column(cursor, "users", "total_free_feed_count", "INTEGER DEFAULT 0")
            self._safe_add_column(cursor, "users", "total_message_count", "INTEGER DEFAULT 0")
            self._safe_add_column(cursor, "trade_listings", "status", "TEXT DEFAULT 'active'")

            # CR Review: 点赞记录表（用于频率限制）
            cursor.execute("""CREATE TABLE IF NOT EXISTS daily_likes (
                user_id TEXT NOT NULL, target_user_id TEXT NOT NULL,
                group_id INTEGER NOT NULL, like_date TEXT NOT NULL,
                like_count INTEGER DEFAULT 1,
                PRIMARY KEY (user_id, target_user_id, group_id, like_date))""")

            # CR Review: 称号过期表（用于时效性称号）
            cursor.execute("""CREATE TABLE IF NOT EXISTS title_expiry (
                user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
                title TEXT NOT NULL, expires_at TEXT NOT NULL,
                PRIMARY KEY (user_id, group_id, title))""")

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_daily_likes ON daily_likes(user_id, target_user_id, group_id, like_date)")
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
                "CREATE INDEX IF NOT EXISTS idx_trade_expiry ON trade_listings(is_active, expires_at, group_id)"
            )
            cursor.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, ?)",
                (utc_now().isoformat(),),
            )
            cursor.execute("PRAGMA user_version=1")

            conn.commit()

    def _create_indexes(self, cursor):
        """CR Fix #13: 创建常用查询索引"""
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
            try:
                cursor.execute(idx_sql)
            except sqlite3.OperationalError:
                pass

    @staticmethod
    def _migrate_pet_show_votes_table(cursor) -> None:
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
    def _safe_add_column(cursor, table: str, column: str, col_type: str):
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

        try:
            sql = "ALTER TABLE " + table + " ADD COLUMN " + column + " " + col_type
            cursor.execute(sql)
        except sqlite3.OperationalError:
            pass

    # ──────────────────── Row → Object 映射 ────────────────────

    @staticmethod
    def _parse_personality(raw: str) -> PetPersonality:
        """CR Fix #6: 兼容中文value和英文name两种存储格式"""
        try:
            return PetPersonality(raw)
        except ValueError:
            pass
        try:
            return PetPersonality[raw]
        except KeyError:
            pass
        return PetPersonality.LIVELY

    @staticmethod
    def _parse_status(raw: str) -> PetStatus:
        """兼容中文value和英文name两种存储格式"""
        try:
            return PetStatus(raw)
        except ValueError:
            pass
        try:
            return PetStatus[raw]
        except KeyError:
            pass
        return PetStatus.NORMAL

    @staticmethod
    def _row_to_pet(row) -> Pet:
        keys = row.keys()
        return Pet(
            id=row['id'], user_id=row['user_id'], group_id=row['group_id'],
            name=row['name'], stage=PetStage(row['stage']), form=row['form'],
            hunger=row['hunger'], mood=row['mood'], clean=row['clean'],
            energy=row['energy'], health=row['health'],
            age=row['age'], experience=row['experience'], intimacy=row['intimacy'],
            personality=Database._parse_personality(row['personality']),
            favorite_food=row['favorite_food'],
            status=Database._parse_status(row['status']),
            status_expire_time=datetime.fromisoformat(row['status_expire_time']) if row['status_expire_time'] else None,
            dress_hat=row['dress_hat'] if 'dress_hat' in keys else None,
            dress_clothes=row['dress_clothes'] if 'dress_clothes' in keys else None,
            dress_accessory=row['dress_accessory'] if 'dress_accessory' in keys else None,
            dress_background=row['dress_background'] if 'dress_background' in keys else None,
            last_update=datetime.fromisoformat(row['last_update']) if row['last_update'] else utc_now(),
            last_feed=datetime.fromisoformat(row['last_feed']) if row['last_feed'] else None,
            last_clean=datetime.fromisoformat(row['last_clean']) if row['last_clean'] else None,
            last_play=datetime.fromisoformat(row['last_play']) if row['last_play'] else None,
            last_train=datetime.fromisoformat(row['last_train']) if row['last_train'] else None,
            last_explore=datetime.fromisoformat(row['last_explore']) if row['last_explore'] else None,
            likes=row['likes'] if 'likes' in keys else 0,
            created_at=datetime.fromisoformat(row['created_at']) if row['created_at'] else utc_now()
        )

    @staticmethod
    def _row_to_user(row) -> User:
        """CR Fix #11: 读取 last_visit_time / last_gift_time"""
        keys = row.keys()
        return User(
            user_id=row['user_id'], group_id=row['group_id'],
            coins=row['coins'], friendship_points=row['friendship_points'],
            today_coins_earned=row['today_coins_earned'],
            today_feed_count=row['today_feed_count'], today_clean_count=row['today_clean_count'],
            today_play_count=row['today_play_count'], today_train_count=row['today_train_count'],
            today_explore_count=row['today_explore_count'],
            today_visit_count=row['today_visit_count'], today_gift_count=row['today_gift_count'],
            today_free_feed_count=row['today_free_feed_count'] if 'today_free_feed_count' in keys else 0,
            today_message_count=row['today_message_count'] if 'today_message_count' in keys else 0,
            total_feed_count=row['total_feed_count'] if 'total_feed_count' in keys else 0,
            total_clean_count=row['total_clean_count'] if 'total_clean_count' in keys else 0,
            total_play_count=row['total_play_count'] if 'total_play_count' in keys else 0,
            total_train_count=row['total_train_count'] if 'total_train_count' in keys else 0,
            total_explore_count=row['total_explore_count'] if 'total_explore_count' in keys else 0,
            total_visit_count=row['total_visit_count'] if 'total_visit_count' in keys else 0,
            total_gift_count=row['total_gift_count'] if 'total_gift_count' in keys else 0,
            total_free_feed_count=row['total_free_feed_count'] if 'total_free_feed_count' in keys else 0,
            total_message_count=row['total_message_count'] if 'total_message_count' in keys else 0,
            titles=json.loads(row['titles']) if 'titles' in keys and row['titles'] else [],
            last_visit_time=datetime.fromisoformat(row['last_visit_time']) if row['last_visit_time'] else None,
            last_gift_time=datetime.fromisoformat(row['last_gift_time']) if row['last_gift_time'] else None,
            trustee_until=datetime.fromisoformat(row['trustee_until']) if row['trustee_until'] else None,
            is_banned=bool(row['is_banned']),
            ban_until=datetime.fromisoformat(row['ban_until']) if row['ban_until'] else None,
            created_at=datetime.fromisoformat(row['created_at']) if row['created_at'] else utc_now(),
            last_active=datetime.fromisoformat(row['last_active']) if row['last_active'] else utc_now()
        )

    # ──────────────────── User CRUD ────────────────────

    def create_user(self, user: User) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("""
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
                """, (
                    user.user_id, user.group_id, user.coins, user.friendship_points,
                    user.today_coins_earned, user.today_feed_count, user.today_clean_count,
                    user.today_play_count, user.today_train_count, user.today_explore_count,
                    user.today_visit_count, user.today_gift_count,
                    user.total_feed_count, user.total_clean_count, user.total_play_count,
                    user.total_train_count, user.total_explore_count, user.total_visit_count,
                    user.total_gift_count, json.dumps(user.titles),
                    user.last_visit_time.isoformat() if user.last_visit_time else None,
                    user.last_gift_time.isoformat() if user.last_gift_time else None,
                    int(user.is_banned),
                    user.created_at.isoformat(), user.last_active.isoformat()
                ))
                conn.commit()
                return cursor.rowcount == 1
            except Exception as exc:
                _log_database_failure("create_user", exc)
                return False

    def get_user(self, user_id: str, group_id: int) -> Optional[User]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute(
                    "SELECT * FROM users WHERE user_id = ? AND group_id = ?",
                    (user_id, group_id))
                row = cursor.fetchone()
                return self._row_to_user(row) if row else None
            except Exception as exc:
                _log_database_failure("get_user", exc)
                return None

    def update_user(self, user: User) -> bool:
        """CR Fix #11: 写入 last_visit_time / last_gift_time"""
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("""
                    UPDATE users SET
                        coins = ?, friendship_points = ?,
                        today_coins_earned = ?, today_feed_count = ?, today_clean_count = ?,
                        today_play_count = ?, today_train_count = ?, today_explore_count = ?,
                        today_visit_count = ?, today_gift_count = ?, today_free_feed_count = ?, today_message_count = ?,
                        total_feed_count = ?, total_clean_count = ?, total_play_count = ?,
                        total_train_count = ?, total_explore_count = ?, total_visit_count = ?,
                        total_gift_count = ?, total_free_feed_count = ?, total_message_count = ?, titles = ?,
                        last_visit_time = ?, last_gift_time = ?,
                        trustee_until = ?, is_banned = ?, ban_until = ?, last_active = ?
                    WHERE user_id = ? AND group_id = ?
                """, (
                    user.coins, user.friendship_points,
                    user.today_coins_earned, user.today_feed_count, user.today_clean_count,
                    user.today_play_count, user.today_train_count, user.today_explore_count,
                    user.today_visit_count, user.today_gift_count, user.today_free_feed_count, user.today_message_count,
                    user.total_feed_count, user.total_clean_count, user.total_play_count,
                    user.total_train_count, user.total_explore_count, user.total_visit_count,
                    user.total_gift_count, user.total_free_feed_count, user.total_message_count, json.dumps(user.titles),
                    user.last_visit_time.isoformat() if user.last_visit_time else None,
                    user.last_gift_time.isoformat() if user.last_gift_time else None,
                    user.trustee_until.isoformat() if user.trustee_until else None,
                    int(user.is_banned),
                    user.ban_until.isoformat() if user.ban_until else None,
                    user.last_active.isoformat(),
                    user.user_id, user.group_id
                ))
                conn.commit()
                return cursor.rowcount == 1
            except Exception as exc:
                _log_database_failure("update_user", exc)
                return False

    # ──────────────────── Pet CRUD ────────────────────

    def create_pet(self, pet: Pet) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("""
                    INSERT INTO pets (
                        user_id, group_id, name, stage, form,
                        hunger, mood, clean, energy, health,
                        age, experience, intimacy, personality, favorite_food,
                        status, status_expire_time,
                        dress_hat, dress_clothes, dress_accessory, dress_background,
                        last_update, likes, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    pet.user_id, pet.group_id, pet.name, pet.stage.value, pet.form,
                    pet.hunger, pet.mood, pet.clean, pet.energy, pet.health,
                    pet.age, pet.experience, pet.intimacy, pet.personality.value,
                    pet.favorite_food, pet.status.value,
                    pet.status_expire_time.isoformat() if pet.status_expire_time else None,
                    pet.dress_hat, pet.dress_clothes, pet.dress_accessory, pet.dress_background,
                    pet.last_update.isoformat(), 0, pet.created_at.isoformat()
                ))
                conn.commit()
                return True
            except Exception as exc:
                _log_database_failure("create_pet", exc)
                return False

    def get_pet(self, user_id: str, group_id: int) -> Optional[Pet]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute(
                    "SELECT * FROM pets WHERE user_id = ? AND group_id = ?",
                    (user_id, group_id))
                row = cursor.fetchone()
                return self._row_to_pet(row) if row else None
            except Exception as exc:
                _log_database_failure("get_pet", exc)
                return None

    def update_pet(self, pet: Pet) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("""
                    UPDATE pets SET
                        name = ?, stage = ?, form = ?,
                        hunger = ?, mood = ?, clean = ?, energy = ?, health = ?,
                        age = ?, experience = ?, intimacy = ?, personality = ?, favorite_food = ?,
                        status = ?, status_expire_time = ?,
                        dress_hat = ?, dress_clothes = ?, dress_accessory = ?, dress_background = ?,
                        last_update = ?,
                        last_feed = ?, last_clean = ?, last_play = ?, last_train = ?, last_explore = ?
                    WHERE id = ?
                """, (
                    pet.name, pet.stage.value, pet.form,
                    pet.hunger, pet.mood, pet.clean, pet.energy, pet.health,
                    pet.age, pet.experience, pet.intimacy, pet.personality.value, pet.favorite_food,
                    pet.status.value,
                    pet.status_expire_time.isoformat() if pet.status_expire_time else None,
                    pet.dress_hat, pet.dress_clothes, pet.dress_accessory, pet.dress_background,
                    pet.last_update.isoformat(),
                    pet.last_feed.isoformat() if pet.last_feed else None,
                    pet.last_clean.isoformat() if pet.last_clean else None,
                    pet.last_play.isoformat() if pet.last_play else None,
                    pet.last_train.isoformat() if pet.last_train else None,
                    pet.last_explore.isoformat() if pet.last_explore else None,
                    pet.id
                ))
                conn.commit()
                return True
            except Exception as exc:
                _log_database_failure("update_pet", exc)
                return False

    def delete_pet(self, user_id: str, group_id: int) -> bool:
        """CR Fix #16: 添加缺失的 delete_pet 方法"""
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute(
                    "DELETE FROM pets WHERE user_id = ? AND group_id = ?", (user_id, group_id)
                )
                conn.commit()
                return cursor.rowcount == 1
            except Exception as exc:
                _log_database_failure("delete_pet", exc)
                return False

    def get_all_pets_in_group(self, group_id: int) -> List[Pet]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("SELECT * FROM pets WHERE group_id = ?", (group_id,))
                return [self._row_to_pet(row) for row in cursor.fetchall()]
            except Exception as exc:
                _log_database_failure("get_pets_by_group", exc)
                return []

    def get_all_pets(self) -> List[Pet]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("SELECT * FROM pets")
                return [self._row_to_pet(row) for row in cursor.fetchall()]
            except Exception as exc:
                _log_database_failure("get_all_pets", exc)
                return []

    def get_enabled_group_decay_map(self) -> Dict[int, float]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute(
                    "SELECT group_id, decay_multiplier FROM group_configs WHERE enabled = 1"
                )
                return {int(row["group_id"]): float(row["decay_multiplier"]) for row in cursor.fetchall()}
            except Exception as exc:
                _log_database_failure("get_enabled_group_decay_map", exc)
                return {}

    def get_pets_by_user(self, user_id: str) -> List[Pet]:
        with self._lock:
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

    def increment_all_pet_ages(self) -> int:
        """CR Fix #8: 宠物年龄每日递增"""
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("UPDATE pets SET age = age + 1")
                conn.commit()
                return cursor.rowcount
            except Exception as exc:
                _log_database_failure("increment_pet_ages", exc)
                return 0

    def increment_pet_ages_for_group(self, group_id: int) -> int:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute(
                    "UPDATE pets SET age = age + 1 WHERE group_id = ?", (group_id,)
                )
                conn.commit()
                return cursor.rowcount
            except Exception as exc:
                _log_database_failure("increment_group_pet_ages", exc)
                return 0

    def like_pet(self, user_id: str, group_id: int) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute(
                    "UPDATE pets SET likes = likes + 1 WHERE user_id = ? AND group_id = ?",
                    (user_id, group_id),
                )
                conn.commit()
                return cursor.rowcount == 1
            except Exception as exc:
                _log_database_failure("like_pet", exc)
                return False

    # ──────────────────── Inventory CRUD ────────────────────

    def get_or_create_inventory(self, user_id: str, group_id: int) -> Inventory:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute(
                    "SELECT items FROM inventories WHERE user_id = ? AND group_id = ?",
                    (user_id, group_id))
                row = cursor.fetchone()
                if row:
                    items = json.loads(row['items']) if row['items'] else {}
                    return Inventory(user_id=user_id, group_id=group_id, items=items)
                conn.execute("INSERT INTO inventories (user_id, group_id, items) VALUES (?, ?, ?)",
                             (user_id, group_id, json.dumps({})))
                conn.commit()
                return Inventory(user_id=user_id, group_id=group_id, items={})
            except Exception as exc:
                _log_database_failure("get_or_create_inventory", exc)
                return Inventory(user_id=user_id, group_id=group_id, items={})

    def update_inventory(self, inventory: Inventory) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute(
                    "UPDATE inventories SET items = ? WHERE user_id = ? AND group_id = ?",
                    (json.dumps(inventory.items), inventory.user_id, inventory.group_id),
                )
                conn.commit()
                return cursor.rowcount == 1
            except Exception as exc:
                _log_database_failure("update_inventory", exc)
                return False

    # ──────────────────── Group Config ────────────────────

    def get_group_config(self, group_id: int) -> GroupConfig:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("SELECT * FROM group_configs WHERE group_id = ?", (group_id,))
                row = cursor.fetchone()
                if row:
                    keys = row.keys()
                    return GroupConfig(
                        group_id=row['group_id'], enabled=bool(row['enabled']),
                        economy_multiplier=row['economy_multiplier'],
                        decay_multiplier=row['decay_multiplier'],
                        trade_enabled=bool(row['trade_enabled']),
                        natural_trigger_enabled=bool(row['natural_trigger_enabled']),
                        activity_enabled=bool(row['activity_enabled']),
                        sensitive_words=json.loads(row['sensitive_words']) if 'sensitive_words' in keys and row['sensitive_words'] else [])
                default = GroupConfig.default(group_id)
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
                _log_database_failure("get_group_config", exc)
                return GroupConfig.default(group_id)

    def update_group_config(self, config: GroupConfig) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("""
                    INSERT OR REPLACE INTO group_configs
                    (group_id, enabled, economy_multiplier, decay_multiplier,
                     trade_enabled, natural_trigger_enabled, activity_enabled, sensitive_words)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (config.group_id, int(config.enabled), config.economy_multiplier,
                      config.decay_multiplier, int(config.trade_enabled),
                      int(config.natural_trigger_enabled), int(config.activity_enabled),
                      json.dumps(config.sensitive_words)))
                conn.commit()
                return True
            except Exception as exc:
                _log_database_failure("update_group_config", exc)
                return False

    # ──────────────────── Plugin Config (CR Fix: 全局配置接入) ────────

    def get_plugin_config(self, key: str) -> Optional[str]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("SELECT value FROM plugin_configs WHERE key = ?", (key,))
                row = cursor.fetchone()
                return row['value'] if row else None
            except Exception as exc:
                _log_database_failure("get_plugin_config", exc)
                return None

    def set_plugin_config(self, key: str, value: str) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("INSERT OR REPLACE INTO plugin_configs (key, value) VALUES (?, ?)", (key, value))
                conn.commit()
                return True
            except Exception as exc:
                _log_database_failure("set_plugin_config", exc)
                return False

    # ──────────────────── Operation Logs ────────────────────

    def log_operation(self, log: OperationLog) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("""INSERT INTO operation_logs
                    (group_id, user_id, target_user_id, operation_type, params, result, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (log.group_id, log.user_id, log.target_user_id,
                     log.operation_type, log.params, log.result, log.created_at.isoformat()))
                conn.commit()
                return True
            except Exception as exc:
                _log_database_failure("log_operation", exc)
                return False

    def get_operation_logs(self, group_id: int, limit: int = 50) -> List[OperationLog]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute(
                    "SELECT * FROM operation_logs WHERE group_id = ? ORDER BY created_at DESC LIMIT ?",
                    (group_id, limit))
                return [OperationLog(
                    id=row['id'], group_id=row['group_id'], user_id=row['user_id'],
                    target_user_id=row['target_user_id'], operation_type=row['operation_type'],
                    params=row['params'], result=row['result'],
                    created_at=datetime.fromisoformat(row['created_at']) if row['created_at'] else utc_now()
                ) for row in cursor.fetchall()]
            except Exception as exc:
                _log_database_failure("get_operation_logs", exc)
                return []

    # ──────────────────── Tasks (CR Fix #10: date范围查询替代LIKE) ────

    def get_or_create_daily_tasks(self, user_id: str, group_id: int) -> List[Dict]:
        with self._lock:
            try:
                conn = self._get_connection()
                today = utc_now().strftime("%Y-%m-%d")
                cursor = conn.execute(
                    "SELECT * FROM tasks WHERE user_id = ? AND group_id = ? AND created_date = ?",
                    (user_id, group_id, today))
                rows = cursor.fetchall()
                if rows:
                    return [dict(row) for row in rows]
                task_templates = [("feed", 3, 30), ("clean", 2, 20), ("play", 3, 25), ("visit", 2, 20)]
                now_str = utc_now().isoformat()
                for task_type, target, reward in task_templates:
                    conn.execute("""INSERT OR REPLACE INTO tasks
                        (user_id, group_id, task_type, target_value, current_value,
                         reward_coins, claimed, created_date, created_at)
                        VALUES (?, ?, ?, ?, 0, ?, 0, ?, ?)""",
                        (user_id, group_id, task_type, target, reward, today, now_str))
                conn.commit()
                cursor = conn.execute(
                    "SELECT * FROM tasks WHERE user_id = ? AND group_id = ? AND created_date = ?",
                    (user_id, group_id, today))
                return [dict(row) for row in cursor.fetchall()]
            except Exception as exc:
                _log_database_failure("get_or_create_daily_tasks", exc)
                return []

    def update_task_progress(self, user_id: str, group_id: int, task_type: str, increment: int = 1) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                today = utc_now().strftime("%Y-%m-%d")
                now_str = utc_now().isoformat()

                task_templates = [("feed", 3, 30), ("clean", 2, 20), ("play", 3, 25), ("visit", 2, 20)]
                for template_task_type, target, reward in task_templates:
                    conn.execute("""INSERT OR IGNORE INTO tasks
                        (user_id, group_id, task_type, target_value, current_value,
                         reward_coins, claimed, created_date, created_at)
                        VALUES (?, ?, ?, ?, 0, ?, 0, ?, ?)""",
                        (user_id, group_id, template_task_type, target, reward, today, now_str))

                conn.execute("""UPDATE tasks SET current_value = MIN(current_value + ?, target_value)
                    WHERE user_id = ? AND group_id = ? AND task_type = ? AND created_date = ?""",
                    (increment, user_id, group_id, task_type, today))
                conn.commit()
                return True
            except Exception as exc:
                _log_database_failure("update_task_progress", exc)
                return False

    def claim_task_reward(self, user_id: str, group_id: int, task_type: str) -> Optional[int]:
        with self._lock:
            try:
                conn = self._get_connection()
                today = utc_now().strftime("%Y-%m-%d")
                cursor = conn.execute("""SELECT * FROM tasks
                    WHERE user_id = ? AND group_id = ? AND task_type = ?
                    AND created_date = ? AND claimed = 0 AND current_value >= target_value""",
                    (user_id, group_id, task_type, today))
                row = cursor.fetchone()
                if not row:
                    return None
                reward = row['reward_coins']
                conn.execute("""UPDATE tasks SET claimed = 1
                    WHERE user_id = ? AND group_id = ? AND task_type = ? AND created_date = ?""",
                    (user_id, group_id, task_type, today))
                conn.execute("""UPDATE users SET coins = coins + ?, today_coins_earned = today_coins_earned + ?
                    WHERE user_id = ? AND group_id = ?""",
                    (reward, reward, user_id, group_id))
                conn.commit()
                return reward
            except Exception as exc:
                _log_database_failure("claim_task_reward", exc)
                return None

    # ──────────────────── Activities ────────────────────

    def get_active_activities(self, group_id: int) -> List[Dict]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("SELECT * FROM activities WHERE group_id = ? AND is_active = 1", (group_id,))
                return [dict(row) for row in cursor.fetchall()]
            except Exception as exc:
                _log_database_failure("get_active_activities", exc)
                return []

    def update_activity_progress(self, activity_id: int, increment: int = 1) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("UPDATE activities SET current_value = current_value + ? WHERE id = ?",
                             (increment, activity_id))
                conn.commit()
                return True
            except Exception as exc:
                _log_database_failure("update_activity_progress", exc)
                return False

    # ──────────────────── Message Board ────────────────────

    def add_message(self, group_id: int, from_user_id: str, to_user_id: str, message: str) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("""INSERT INTO message_board (group_id, from_user_id, to_user_id, message, created_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (group_id, from_user_id, to_user_id, message, utc_now().isoformat()))
                conn.commit()
                return True
            except Exception as exc:
                _log_database_failure("add_message", exc)
                return False

    def leave_message_atomic(
        self,
        from_user_id: str,
        to_user_id: str,
        group_id: int,
        message: str,
        daily_limit: int,
    ) -> LeaveMessageAtomicResult:
        """Insert a board message and consume its daily quota atomically."""
        if from_user_id == to_user_id:
            return LeaveMessageAtomicResult(False, "不能给自己留言")
        limit = max(0, int(daily_limit))
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                sender = conn.execute(
                    "SELECT today_message_count FROM users "
                    "WHERE user_id = ? AND group_id = ?",
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
                           last_active = ?
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

    def get_messages(self, to_user_id: str, group_id: int, limit: int = 10) -> List[Dict]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("""SELECT * FROM message_board
                    WHERE to_user_id = ? AND group_id = ? ORDER BY created_at DESC LIMIT ?""",
                    (to_user_id, group_id, limit))
                return [dict(row) for row in cursor.fetchall()]
            except Exception as exc:
                _log_database_failure("get_messages", exc)
                return []

    # ──────────────────── Anti-Spam ────────────────────

    def record_command_timestamp(self, user_id: str, group_id: int) -> None:
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("INSERT INTO command_timestamps (user_id, group_id, timestamp) VALUES (?, ?, ?)",
                             (user_id, group_id, time.time()))
                conn.commit()
            except Exception as exc:
                _log_database_failure("record_command_timestamp", exc)

    def get_recent_command_count(self, user_id: str, group_id: int, window_seconds: int) -> int:
        with self._lock:
            try:
                conn = self._get_connection()
                threshold = time.time() - window_seconds
                cursor = conn.execute(
                    "SELECT COUNT(*) as cnt FROM command_timestamps WHERE user_id = ? AND group_id = ? AND timestamp > ?",
                    (user_id, group_id, threshold))
                row = cursor.fetchone()
                return row['cnt'] if row else 0
            except Exception as exc:
                _log_database_failure("get_recent_command_count", exc)
                return 0

    def get_group_recent_command_count(self, group_id: int, window_seconds: int) -> int:
        with self._lock:
            try:
                conn = self._get_connection()
                threshold = time.time() - window_seconds
                cursor = conn.execute(
                    "SELECT COUNT(*) as cnt FROM command_timestamps WHERE group_id = ? AND timestamp > ?",
                    (group_id, threshold))
                row = cursor.fetchone()
                return row['cnt'] if row else 0
            except Exception as exc:
                _log_database_failure("get_group_recent_command_count", exc)
                return 0

    def cleanup_old_timestamps(self, max_age_seconds: int = 3600) -> None:
        with self._lock:
            try:
                conn = self._get_connection()
                threshold = time.time() - max_age_seconds
                conn.execute("DELETE FROM command_timestamps WHERE timestamp < ?", (threshold,))
                conn.commit()
            except Exception as exc:
                _log_database_failure("cleanup_old_timestamps", exc)

    def check_and_consume_minigame_cooldown(
        self, user_id: str, group_id: int, game_type: str, cooldown_seconds: int
    ) -> int:
        """Returns remaining seconds if still cooling down, otherwise records a new cooldown and returns 0."""
        with self._lock:
            try:
                conn = self._get_connection()
                now_ts = time.time()
                cursor = conn.execute(
                    """
                    SELECT available_at FROM minigame_cooldowns
                    WHERE user_id = ? AND group_id = ? AND game_type = ?
                    """,
                    (user_id, group_id, game_type),
                )
                row = cursor.fetchone()
                if row is not None and float(row["available_at"]) > now_ts:
                    return max(1, int(float(row["available_at"]) - now_ts))
                conn.execute(
                    """
                    INSERT INTO minigame_cooldowns (user_id, group_id, game_type, available_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id, group_id, game_type)
                    DO UPDATE SET available_at = excluded.available_at
                    """,
                    (user_id, group_id, game_type, now_ts + max(0, cooldown_seconds)),
                )
                conn.commit()
                return 0
            except Exception as exc:
                _log_database_failure("check_minigame_cooldown", exc)
                return 0

    # ──────────────────── Trade Market (新增) ────────────────────

    def create_trade_listing(self, seller_id: str, group_id: int, item_id: str,
                             amount: int, price: int, expire_hours: int = 72) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                now = utc_now()
                expires = now + timedelta(hours=expire_hours)
                conn.execute("""INSERT INTO trade_listings
                    (seller_user_id, group_id, item_id, amount, price, created_at, expires_at, is_active)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
                    (seller_id, group_id, item_id, amount, price, now.isoformat(), expires.isoformat()))
                conn.commit()
                return True
            except Exception as exc:
                _log_database_failure("create_trade_listing", exc)
                return False

    def get_active_listings(self, group_id: int) -> List[Dict]:
        self.settle_expired_trade_listings(group_id)
        with self._lock:
            try:
                conn = self._get_connection()
                now = utc_now().isoformat()
                cursor = conn.execute("""SELECT * FROM trade_listings
                    WHERE group_id = ? AND is_active = 1 AND expires_at > ?
                    ORDER BY created_at DESC""", (group_id, now))
                return [dict(row) for row in cursor.fetchall()]
            except Exception as exc:
                _log_database_failure("get_active_trade_listings", exc)
                return []

    def get_user_listing_count(self, user_id: str, group_id: int) -> int:
        with self._lock:
            try:
                conn = self._get_connection()
                now = utc_now().isoformat()
                cursor = conn.execute("""SELECT COUNT(*) as cnt FROM trade_listings
                    WHERE seller_user_id = ? AND group_id = ? AND is_active = 1 AND expires_at > ?""",
                    (user_id, group_id, now))
                row = cursor.fetchone()
                return row['cnt'] if row else 0
            except Exception as exc:
                _log_database_failure("get_user_listing_count", exc)
                return 0

    def get_listing_by_id(self, listing_id: int, group_id: Optional[int] = None) -> Optional[Dict]:
        with self._lock:
            try:
                conn = self._get_connection()
                if group_id is None:
                    cursor = conn.execute(
                        "SELECT * FROM trade_listings WHERE id = ? AND is_active = 1", (listing_id,)
                    )
                else:
                    cursor = conn.execute(
                        "SELECT * FROM trade_listings WHERE id = ? AND group_id = ? AND is_active = 1",
                        (listing_id, group_id),
                    )
                row = cursor.fetchone()
                return dict(row) if row else None
            except Exception as exc:
                _log_database_failure("get_trade_listing", exc)
                return None

    def deactivate_listing(self, listing_id: int) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("UPDATE trade_listings SET is_active = 0 WHERE id = ?", (listing_id,))
                conn.commit()
                return True
            except Exception as exc:
                _log_database_failure("deactivate_trade_listing", exc)
                return False

    def cancel_trade_listing(self, listing_id: int, seller_id: str, group_id: int) -> bool:
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "SELECT * FROM trade_listings WHERE id = ? AND group_id = ? AND is_active = 1",
                    (listing_id, group_id),
                )
                row = cursor.fetchone()
                if not row or str(row["seller_user_id"]) != str(seller_id):
                    return False

                listing = dict(row)
                claim = conn.execute(
                    """UPDATE trade_listings SET is_active = 0, status = 'cancelled'
                       WHERE id = ? AND group_id = ? AND is_active = 1""",
                    (listing_id, group_id),
                )
                if claim.rowcount != 1:
                    conn.rollback()
                    return False
                inv_cursor = conn.execute(
                    "SELECT items FROM inventories WHERE user_id = ? AND group_id = ?",
                    (seller_id, int(listing["group_id"])),
                )
                inv_row = inv_cursor.fetchone()
                inventory_items = json.loads(inv_row["items"]) if inv_row and inv_row["items"] else {}
                inventory_items[listing["item_id"]] = (
                    int(inventory_items.get(listing["item_id"], 0)) + int(listing["amount"])
                )

                if inv_row:
                    conn.execute(
                        "UPDATE inventories SET items = ? WHERE user_id = ? AND group_id = ?",
                        (json.dumps(inventory_items), seller_id, int(listing["group_id"])),
                    )
                else:
                    conn.execute(
                        "INSERT INTO inventories (user_id, group_id, items) VALUES (?, ?, ?)",
                        (seller_id, int(listing["group_id"]), json.dumps(inventory_items)),
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
    ) -> tuple[bool, Dict | str]:
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "SELECT * FROM trade_listings WHERE id = ? AND is_active = 1",
                    (listing_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return False, "订单不存在或已过期"

                listing = dict(row)
                if str(listing["seller_user_id"]) == str(buyer_id):
                    return False, "不能购买自己的挂单"
                if int(listing["group_id"]) != int(group_id):
                    return False, "该订单不属于本群"

                buyer_row = conn.execute(
                    "SELECT * FROM users WHERE user_id = ? AND group_id = ?",
                    (buyer_id, group_id),
                ).fetchone()
                if not buyer_row:
                    return False, "用户不存在"

                total_cost = int(listing["price"])
                tax = int(total_cost * tax_rate)
                if int(buyer_row["coins"]) < total_cost:
                    return False, f"金币不足，需要{total_cost}金币"

                conn.execute(
                    "UPDATE users SET coins = coins - ? WHERE user_id = ? AND group_id = ?",
                    (total_cost, buyer_id, group_id),
                )
                conn.execute(
                    "UPDATE users SET coins = coins + ? WHERE user_id = ? AND group_id = ?",
                    (max(0, total_cost - tax), str(listing["seller_user_id"]), group_id),
                )

                inv_cursor = conn.execute(
                    "SELECT items FROM inventories WHERE user_id = ? AND group_id = ?",
                    (buyer_id, group_id),
                )
                inv_row = inv_cursor.fetchone()
                inventory_items = json.loads(inv_row["items"]) if inv_row and inv_row["items"] else {}
                inventory_items[listing["item_id"]] = (
                    int(inventory_items.get(listing["item_id"], 0)) + int(listing["amount"])
                )
                if inv_row:
                    conn.execute(
                        "UPDATE inventories SET items = ? WHERE user_id = ? AND group_id = ?",
                        (json.dumps(inventory_items), buyer_id, group_id),
                    )
                else:
                    conn.execute(
                        "INSERT INTO inventories (user_id, group_id, items) VALUES (?, ?, ?)",
                        (buyer_id, group_id, json.dumps(inventory_items)),
                    )

                update_cursor = conn.execute(
                    "UPDATE trade_listings SET is_active = 0 WHERE id = ? AND is_active = 1",
                    (listing_id,),
                )
                if update_cursor.rowcount <= 0:
                    conn.rollback()
                    return False, "订单不存在或已过期"

                conn.commit()
                listing["tax"] = tax
                return True, listing
            except Exception as exc:
                conn.rollback()
                _log_database_failure("purchase_trade_listing", exc)
                return False, "购买失败，请稍后重试"

    # ──────────────────── Pet Show (新增完整实现) ────────────────────

    def create_pet_show(self, group_id: int, title: str, duration_hours: int) -> Optional[int]:
        with self._lock:
            try:
                conn = self._get_connection()
                now = utc_now()
                end = now + timedelta(hours=duration_hours)
                cursor = conn.execute("""INSERT INTO pet_shows
                    (group_id, title, start_time, end_time, is_active)
                    VALUES (?, ?, ?, ?, 1)""",
                    (group_id, title, now.isoformat(), end.isoformat()))
                conn.commit()
                return cursor.lastrowid
            except Exception as exc:
                _log_database_failure("create_pet_show", exc)
                return None

    def get_active_pet_show(self, group_id: int) -> Optional[Dict]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("""SELECT * FROM pet_shows
                    WHERE group_id = ? AND is_active = 1 ORDER BY id DESC LIMIT 1""", (group_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
            except Exception as exc:
                _log_database_failure("get_active_pet_show", exc)
                return None

    def vote_pet_show(self, show_id: int, voter_id: str, pet_user_id: str) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("""INSERT INTO pet_show_votes
                    (show_id, voter_user_id, pet_user_id, created_at)
                    VALUES (?, ?, ?, ?)""",
                    (show_id, voter_id, pet_user_id, utc_now().isoformat()))
                conn.commit()
                return True
            except Exception as exc:
                _log_database_failure("vote_pet_show", exc)
                return False

    def get_pet_show_votes(self, show_id: int) -> Dict[str, int]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("""SELECT pet_user_id, COUNT(*) as votes
                    FROM pet_show_votes WHERE show_id = ?
                    GROUP BY pet_user_id ORDER BY votes DESC""", (show_id,))
                return {row['pet_user_id']: row['votes'] for row in cursor.fetchall()}
            except Exception as exc:
                _log_database_failure("get_pet_show_votes", exc)
                return {}

    def get_user_vote_count(self, show_id: int, voter_id: str) -> int:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("""SELECT COUNT(*) as cnt FROM pet_show_votes
                    WHERE show_id = ? AND voter_user_id = ?""", (show_id, voter_id))
                row = cursor.fetchone()
                return row['cnt'] if row else 0
            except Exception as exc:
                _log_database_failure("get_user_vote_count", exc)
                return 0

    def end_pet_show(self, show_id: int) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("UPDATE pet_shows SET is_active = 0 WHERE id = ?", (show_id,))
                conn.commit()
                return True
            except Exception as exc:
                _log_database_failure("end_pet_show", exc)
                return False

    # ──────────────────── Dress Inventory (新增) ────────────────────

    def get_dress_inventory(self, user_id: str, group_id: int) -> List[str]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("""SELECT dress_item_id FROM dress_inventory
                    WHERE user_id = ? AND group_id = ?""", (user_id, group_id))
                return [row['dress_item_id'] for row in cursor.fetchall()]
            except Exception as exc:
                _log_database_failure("get_dress_inventory", exc)
                return []

    def add_dress_item(self, user_id: str, group_id: int, dress_item_id: str) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("""INSERT OR IGNORE INTO dress_inventory
                    (user_id, group_id, dress_item_id) VALUES (?, ?, ?)""",
                    (user_id, group_id, dress_item_id))
                conn.commit()
                return True
            except Exception as exc:
                _log_database_failure("add_dress_item", exc)
                return False

    # ──────────────────── Group Tasks (新增: 群累计任务) ────────────────

    def get_or_create_group_tasks(self, group_id: int) -> List[Dict]:
        with self._lock:
            try:
                conn = self._get_connection()
                today = utc_now().strftime("%Y-%m-%d")
                cursor = conn.execute(
                    "SELECT * FROM group_tasks WHERE group_id = ? AND created_date = ?",
                    (group_id, today))
                rows = cursor.fetchall()
                if rows:
                    return [dict(row) for row in rows]
                from ..utils.constants import GROUP_TASK_TEMPLATES
                for tmpl in GROUP_TASK_TEMPLATES:
                    conn.execute("""INSERT INTO group_tasks
                        (group_id, task_type, target_value, current_value, reward_coins,
                         description, created_date, is_completed)
                        VALUES (?, ?, ?, 0, ?, ?, ?, 0)""",
                        (group_id, tmpl["type"], tmpl["target"], tmpl["reward_coins"],
                         tmpl["description"], today))
                conn.commit()
                cursor = conn.execute(
                    "SELECT * FROM group_tasks WHERE group_id = ? AND created_date = ?",
                    (group_id, today))
                return [dict(row) for row in cursor.fetchall()]
            except Exception as exc:
                _log_database_failure("get_or_create_group_tasks", exc)
                return []

    def update_group_task_progress(self, group_id: int, task_type: str, increment: int = 1) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                today = utc_now().strftime("%Y-%m-%d")
                cursor = conn.execute(
                    "SELECT 1 FROM group_tasks WHERE group_id = ? AND created_date = ? LIMIT 1",
                    (group_id, today),
                )
                if cursor.fetchone() is None:
                    from ..utils.constants import GROUP_TASK_TEMPLATES

                    for tmpl in GROUP_TASK_TEMPLATES:
                        conn.execute("""INSERT INTO group_tasks
                            (group_id, task_type, target_value, current_value, reward_coins,
                             description, created_date, is_completed)
                            VALUES (?, ?, ?, 0, ?, ?, ?, 0)""",
                            (group_id, tmpl["type"], tmpl["target"], tmpl["reward_coins"],
                             tmpl["description"], today))
                conn.execute("""UPDATE group_tasks SET
                    current_value = MIN(current_value + ?, target_value),
                    is_completed = CASE WHEN current_value + ? >= target_value THEN 1 ELSE is_completed END
                    WHERE group_id = ? AND task_type = ? AND created_date = ? AND is_completed = 0""",
                    (increment, increment, group_id, task_type, today))
                conn.commit()
                return True
            except Exception as exc:
                _log_database_failure("update_group_task", exc)
                return False

    # ──────────────────── Batch Operations ────────────────────

    def batch_daily_reset(self, group_id: int) -> int:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("""UPDATE users SET
                    today_coins_earned = 0, today_feed_count = 0, today_clean_count = 0,
                    today_play_count = 0, today_train_count = 0,
                    today_explore_count = 0, today_visit_count = 0, today_gift_count = 0,
                    today_free_feed_count = 0, today_message_count = 0
                    WHERE group_id = ?""", (group_id,))
                conn.commit()
                return cursor.rowcount
            except Exception as exc:
                _log_database_failure("batch_daily_reset", exc)
                return 0

    def batch_daily_reset_all(self) -> int:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("""UPDATE users SET
                    today_coins_earned = 0, today_feed_count = 0, today_clean_count = 0,
                    today_play_count = 0, today_train_count = 0,
                    today_explore_count = 0, today_visit_count = 0, today_gift_count = 0,
                    today_free_feed_count = 0, today_message_count = 0""")
                conn.commit()
                return cursor.rowcount
            except Exception as exc:
                _log_database_failure("batch_daily_reset_all", exc)
                return 0

    # ──────────────────── Transaction helpers (CR Fix #5 & #9) ────────

    def execute_in_transaction(self, operations: list) -> bool:
        """在一个事务中执行多个操作 operations: [(sql, params), ...]"""
        with self._lock:
            conn = self._get_connection()
            try:
                for sql, params in operations:
                    conn.execute(sql, params)
                conn.commit()
                return True
            except Exception as exc:
                conn.rollback()
                _log_database_failure("transaction", exc)
                return False

    def create_activity(
        self,
        group_id: int,
        activity_type: str,
        title: str,
        target_value: int,
        reward_coins: int,
        duration_hours: int = 24,
    ) -> Optional[int]:
        with self._lock:
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
                conn.commit()
                return int(cursor.lastrowid)
            except Exception as exc:
                _log_database_failure("create_activity", exc)
                return None

    def trigger_activities(self, group_id: int, activity_type: str, increment: int = 1) -> int:
        with self._lock:
            try:
                conn = self._get_connection()
                config = conn.execute(
                    "SELECT enabled, activity_enabled, natural_trigger_enabled FROM group_configs WHERE group_id = ?",
                    (group_id,),
                ).fetchone()
                if not config or not all(
                    bool(config[key]) for key in ("enabled", "activity_enabled", "natural_trigger_enabled")
                ):
                    return 0
                cursor = conn.execute(
                    """UPDATE activities SET current_value = MIN(current_value + ?, target_value)
                       WHERE group_id = ? AND activity_type = ? AND is_active = 1
                       AND (end_time IS NULL OR end_time > ?)""",
                    (increment, group_id, activity_type, utc_now().isoformat()),
                )
                conn.commit()
                return cursor.rowcount
            except Exception as exc:
                _log_database_failure("trigger_activities", exc)
                return 0

    def claim_activity_reward(self, activity_id: int, user_id: str, group_id: int) -> Optional[int]:
        with self._lock:
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
                user = conn.execute(
                    "SELECT today_coins_earned FROM users WHERE user_id = ? AND group_id = ?",
                    (user_id, group_id),
                ).fetchone()
                if user is None:
                    conn.rollback()
                    return None
                reward = min(int(row["reward_coins"] or 0), max(0, 500 - int(user["today_coins_earned"] or 0)))
                conn.execute(
                    "UPDATE users SET coins = coins + ?, today_coins_earned = today_coins_earned + ? WHERE user_id = ? AND group_id = ?",
                    (reward, reward, user_id, group_id),
                )
                conn.execute(
                    """INSERT INTO asset_ledger
                       (user_id, group_id, asset_type, delta, reason, reference_id, created_at)
                       VALUES (?, ?, 'coins', ?, 'activity', ?, ?)""",
                    (
                        user_id,
                        group_id,
                        reward,
                        f"activity:{activity_id}:{user_id}",
                        utc_now().isoformat(),
                    ),
                )
                conn.commit()
                return reward
            except Exception as exc:
                conn.rollback()
                _log_database_failure("claim_activity_reward", exc)
                return None

    def claim_group_task_reward(
        self, user_id: str, group_id: int, task_type: str, daily_coin_limit: int = 500
    ) -> Optional[int]:
        with self._lock:
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
                user = conn.execute(
                    "SELECT today_coins_earned FROM users WHERE user_id = ? AND group_id = ?",
                    (user_id, group_id),
                ).fetchone()
                if user is None:
                    conn.rollback()
                    return None
                reward = min(
                    int(task["reward_coins"] or 0),
                    max(0, daily_coin_limit - int(user["today_coins_earned"] or 0)),
                )
                conn.execute(
                    """UPDATE users SET coins = coins + ?, today_coins_earned = today_coins_earned + ?
                       WHERE user_id = ? AND group_id = ?""",
                    (reward, reward, user_id, group_id),
                )
                conn.execute(
                    """INSERT INTO asset_ledger
                       (user_id, group_id, asset_type, delta, reason, reference_id, created_at)
                       VALUES (?, ?, 'coins', ?, 'group_task', ?, ?)""",
                    (
                        user_id,
                        group_id,
                        reward,
                        f"group-task:{today}:{group_id}:{task_type}:{user_id}",
                        utc_now().isoformat(),
                    ),
                )
                conn.commit()
                return reward
            except Exception as exc:
                conn.rollback()
                _log_database_failure("claim_group_task_reward", exc)
                return None

    @staticmethod
    def _load_inventory_items(conn: sqlite3.Connection, user_id: str, group_id: int) -> dict:
        row = conn.execute(
            "SELECT items FROM inventories WHERE user_id = ? AND group_id = ?",
            (user_id, group_id),
        ).fetchone()
        return json.loads(row["items"] or "{}") if row else {}

    @staticmethod
    def _save_inventory_items(
        conn: sqlite3.Connection, user_id: str, group_id: int, items: dict
    ) -> None:
        conn.execute(
            """INSERT INTO inventories (user_id, group_id, items) VALUES (?, ?, ?)
               ON CONFLICT(user_id, group_id) DO UPDATE SET items = excluded.items""",
            (user_id, group_id, json.dumps(items)),
        )

    def purchase_item_atomic(
        self, user_id: str, group_id: int, item_id: str, amount: int, total_cost: int
    ) -> tuple[bool, int]:
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    """UPDATE users SET coins = coins - ?
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
                conn.commit()
                return True, 0
            except Exception as exc:
                conn.rollback()
                _log_database_failure("purchase_item_atomic", exc)
                return False, -1

    def claim_action_quota(
        self,
        user_id: str,
        group_id: int,
        action: str,
        daily_limit: int,
        cooldown_seconds: int = 0,
    ) -> tuple[bool, int]:
        """Persisted cross-worker quota/cooldown claim for valuable actions."""
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                claimed, remaining = self._claim_action_quota_in_transaction(
                    conn,
                    user_id,
                    group_id,
                    action,
                    daily_limit,
                    cooldown_seconds,
                )
                if not claimed:
                    conn.rollback()
                    return False, remaining
                conn.commit()
                return True, 0
            except Exception as exc:
                conn.rollback()
                _log_database_failure("claim_action_quota", exc)
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
        now=None,
        now_ts: Optional[float] = None,
    ) -> tuple[bool, int]:
        """Claim a persisted quota using the caller's open transaction."""
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
        """Apply a capped coin credit and ledger row in an existing transaction."""
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
                   today_coins_earned = today_coins_earned + ?
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
    def _increment_task_in_transaction(
        conn: sqlite3.Connection,
        user_id: str,
        group_id: int,
        task_type: str,
        *,
        now_iso: str,
        today: str,
    ) -> None:
        templates = [("feed", 3, 30), ("clean", 2, 20), ("play", 3, 25), ("visit", 2, 20)]
        for template_type, target, reward in templates:
            conn.execute(
                """INSERT OR IGNORE INTO tasks
                   (user_id, group_id, task_type, target_value, current_value,
                    reward_coins, claimed, created_date, created_at)
                   VALUES (?, ?, ?, ?, 0, ?, 0, ?, ?)""",
                (user_id, group_id, template_type, target, reward, today, now_iso),
            )
        updated = conn.execute(
            """UPDATE tasks SET current_value = MIN(current_value + 1, target_value)
               WHERE user_id = ? AND group_id = ? AND task_type = ? AND created_date = ?""",
            (user_id, group_id, task_type, today),
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
        outcome_factory: Callable[[Pet, Optional[Pet]], MinigameOutcome],
        opponent_user_id: Optional[str] = None,
        minimum_energy: int = 0,
    ) -> MinigameAtomicResult:
        """Validate and settle a minigame in one immediate transaction.

        The outcome payload is stored with the settlement so a delivery retry
        replays the original rolls/choices instead of presenting fresh random
        data alongside an earlier asset grant.
        """
        if not str(reference_id).strip():
            return MinigameAtomicResult(False, "小游戏请求标识不能为空")
        normalized_opponent = str(opponent_user_id or "")
        if normalized_opponent and normalized_opponent == user_id:
            return MinigameAtomicResult(False, "不能和自己的宠物进行对战")
        with self._lock:
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

                opponent_pet: Optional[Pet] = None
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
                       SET experience = experience + ?, energy = energy - ?
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
        """Settle an entire visit exactly once in one ``BEGIN IMMEDIATE``."""
        if visitor_user_id == target_user_id:
            return VisitPetAtomicResult(False, "不能访问自己的宠物")
        if not reference_id:
            return VisitPetAtomicResult(False, "访问请求缺少唯一标识")

        with self._lock:
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
                        f"访问冷却中，请等待{remaining}秒"
                        if remaining > 0
                        else "今日访问次数已达上限"
                    )
                    return VisitPetAtomicResult(False, reason)

                now_iso = now.isoformat()
                visitor_update = conn.execute(
                    """UPDATE users SET today_visit_count = today_visit_count + 1,
                       total_visit_count = total_visit_count + 1,
                       last_visit_time = ?, last_active = ?
                       WHERE user_id = ? AND group_id = ?""",
                    (now_iso, now_iso, visitor_user_id, group_id),
                )
                if visitor_update.rowcount != 1:
                    raise RuntimeError("visitor changed during settlement")
                pet_update = conn.execute(
                    "UPDATE pets SET intimacy = intimacy + 1 WHERE id = ?",
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

    def credit_coins_atomic(
        self,
        user_id: str,
        group_id: int,
        amount: int,
        *,
        reason: str,
        daily_limit: int = 500,
        exempt: bool = False,
        reference_id: Optional[str] = None,
    ) -> int:
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT today_coins_earned FROM users WHERE user_id = ? AND group_id = ?",
                    (user_id, group_id),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return 0
                grant = max(0, int(amount))
                if not exempt:
                    grant = min(grant, max(0, int(daily_limit) - int(row["today_coins_earned"] or 0)))
                if grant <= 0:
                    conn.rollback()
                    return 0
                if reference_id:
                    duplicate = conn.execute(
                        "SELECT 1 FROM asset_ledger WHERE asset_type = 'coins' AND reference_id = ?",
                        (reference_id,),
                    ).fetchone()
                    if duplicate:
                        conn.rollback()
                        return 0
                conn.execute(
                    """UPDATE users SET coins = coins + ?,
                       today_coins_earned = today_coins_earned + ?
                       WHERE user_id = ? AND group_id = ?""",
                    (grant, 0 if exempt else grant, user_id, group_id),
                )
                conn.execute(
                    """INSERT INTO asset_ledger
                       (user_id, group_id, asset_type, delta, reason, reference_id, created_at)
                       VALUES (?, ?, 'coins', ?, ?, ?, ?)""",
                    (user_id, group_id, grant, reason, reference_id, utc_now().isoformat()),
                )
                conn.commit()
                return grant
            except sqlite3.IntegrityError:
                conn.rollback()
                return 0
            except Exception as exc:
                conn.rollback()
                _log_database_failure("credit_coins_atomic", exc)
                return 0

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
        with self._lock:
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
                       total_gift_count = total_gift_count + 1, last_gift_time = ?
                       WHERE user_id = ? AND group_id = ?""",
                    (friendship_gain, now, from_user_id, group_id),
                )
                conn.execute(
                    "UPDATE users SET friendship_points = friendship_points + ? WHERE user_id = ? AND group_id = ?",
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
        with self._lock:
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
        column = "friendship_points" if currency == "friendship" else "coins"
        with self._lock:
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
                    f"UPDATE users SET {column} = {column} - ? WHERE user_id = ? AND group_id = ? AND {column} >= ?",
                    (price, user_id, group_id, price),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    return False, "余额不足"
                conn.execute(
                    "INSERT INTO dress_inventory (user_id, group_id, dress_item_id) VALUES (?, ?, ?)",
                    (user_id, group_id, dress_item_id),
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
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                count = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM pet_show_votes WHERE show_id = ? AND voter_user_id = ?",
                    (show_id, voter_id),
                ).fetchone()["cnt"]
                if int(count) >= max_votes:
                    conn.rollback()
                    return False
                conn.execute(
                    "INSERT INTO pet_show_votes (show_id, voter_user_id, pet_user_id, created_at) VALUES (?, ?, ?, ?)",
                    (show_id, voter_id, pet_user_id, utc_now().isoformat()),
                )
                conn.commit()
                return True
            except Exception as exc:
                conn.rollback()
                _log_database_failure("vote_pet_show_atomic", exc)
                return False

    def like_pet_atomic(
        self, user_id: str, target_user_id: str, group_id: int, daily_limit: int
    ) -> bool:
        with self._lock:
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
                    """UPDATE pets SET likes = likes + 1, intimacy = intimacy + 1
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

    def treat_pet_atomic(self, pet: Pet, inventory: Inventory) -> bool:
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    """UPDATE pets SET health = ?, clean = ?, status = ?,
                       status_expire_time = ?, last_update = ? WHERE id = ?""",
                    (
                        pet.health,
                        pet.clean,
                        pet.status.value,
                        pet.status_expire_time.isoformat() if pet.status_expire_time else None,
                        utc_now().isoformat(),
                        pet.id,
                    ),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    return False
                self._save_inventory_items(
                    conn, inventory.user_id, inventory.group_id, inventory.items
                )
                conn.commit()
                return True
            except Exception as exc:
                conn.rollback()
                _log_database_failure("treat_pet_atomic", exc)
                return False

    def atomic_update_pet_and_user(
        self,
        pet: Pet,
        user: User,
        *,
        inventory: Optional[Inventory] = None,
        task_type: Optional[str] = None,
        group_task_type: Optional[str] = None,
    ) -> bool:
        """CR Fix #5: 原子性更新宠物和用户"""
        with self._lock:
            conn = self._get_connection()
            try:
                pet_cursor = conn.execute("""UPDATE pets SET
                    name = ?, stage = ?, form = ?,
                    hunger = ?, mood = ?, clean = ?, energy = ?, health = ?,
                    age = ?, experience = ?, intimacy = ?, personality = ?, favorite_food = ?,
                    status = ?, status_expire_time = ?,
                    dress_hat = ?, dress_clothes = ?, dress_accessory = ?, dress_background = ?,
                    last_update = ?,
                    last_feed = ?, last_clean = ?, last_play = ?, last_train = ?, last_explore = ?
                    WHERE id = ?""", (
                    pet.name, pet.stage.value, pet.form,
                    pet.hunger, pet.mood, pet.clean, pet.energy, pet.health,
                    pet.age, pet.experience, pet.intimacy, pet.personality.value, pet.favorite_food,
                    pet.status.value,
                    pet.status_expire_time.isoformat() if pet.status_expire_time else None,
                    pet.dress_hat, pet.dress_clothes, pet.dress_accessory, pet.dress_background,
                    pet.last_update.isoformat(),
                    pet.last_feed.isoformat() if pet.last_feed else None,
                    pet.last_clean.isoformat() if pet.last_clean else None,
                    pet.last_play.isoformat() if pet.last_play else None,
                    pet.last_train.isoformat() if pet.last_train else None,
                    pet.last_explore.isoformat() if pet.last_explore else None,
                    pet.id))
                user_cursor = conn.execute("""UPDATE users SET
                    coins = ?, friendship_points = ?,
                    today_coins_earned = ?, today_feed_count = ?, today_clean_count = ?,
                    today_play_count = ?, today_train_count = ?, today_explore_count = ?,
                    today_visit_count = ?, today_gift_count = ?, today_free_feed_count = ?, today_message_count = ?,
                    total_feed_count = ?, total_clean_count = ?, total_play_count = ?,
                    total_train_count = ?, total_explore_count = ?, total_visit_count = ?,
                    total_gift_count = ?, total_free_feed_count = ?, total_message_count = ?, titles = ?,
                    last_visit_time = ?, last_gift_time = ?,
                    trustee_until = ?, is_banned = ?, ban_until = ?, last_active = ?
                    WHERE user_id = ? AND group_id = ?""", (
                    user.coins, user.friendship_points,
                    user.today_coins_earned, user.today_feed_count, user.today_clean_count,
                    user.today_play_count, user.today_train_count, user.today_explore_count,
                    user.today_visit_count, user.today_gift_count, user.today_free_feed_count, user.today_message_count,
                    user.total_feed_count, user.total_clean_count, user.total_play_count,
                    user.total_train_count, user.total_explore_count, user.total_visit_count,
                    user.total_gift_count, user.total_free_feed_count, user.total_message_count, json.dumps(user.titles),
                    user.last_visit_time.isoformat() if user.last_visit_time else None,
                    user.last_gift_time.isoformat() if user.last_gift_time else None,
                    user.trustee_until.isoformat() if user.trustee_until else None,
                    int(user.is_banned),
                    user.ban_until.isoformat() if user.ban_until else None,
                    user.last_active.isoformat(),
                    user.user_id, user.group_id))
                if pet_cursor.rowcount != 1 or user_cursor.rowcount != 1:
                    conn.rollback()
                    return False
                if inventory is not None:
                    self._save_inventory_items(
                        conn, inventory.user_id, inventory.group_id, inventory.items
                    )
                today = utc_now().strftime("%Y-%m-%d")
                now_str = utc_now().isoformat()
                if task_type:
                    for template_type, target, reward in [
                        ("feed", 3, 30), ("clean", 2, 20), ("play", 3, 25), ("visit", 2, 20)
                    ]:
                        conn.execute(
                            """INSERT OR IGNORE INTO tasks
                               (user_id, group_id, task_type, target_value, current_value,
                                reward_coins, claimed, created_date, created_at)
                               VALUES (?, ?, ?, ?, 0, ?, 0, ?, ?)""",
                            (user.user_id, user.group_id, template_type, target, reward, today, now_str),
                        )
                    conn.execute(
                        """UPDATE tasks SET current_value = MIN(current_value + 1, target_value)
                           WHERE user_id = ? AND group_id = ? AND task_type = ? AND created_date = ?""",
                        (user.user_id, user.group_id, task_type, today),
                    )
                if group_task_type:
                    from ..utils.constants import GROUP_TASK_TEMPLATES

                    for template in GROUP_TASK_TEMPLATES:
                        conn.execute(
                            """INSERT OR IGNORE INTO group_tasks
                               (group_id, task_type, target_value, current_value, reward_coins,
                                description, created_date, is_completed)
                               VALUES (?, ?, ?, 0, ?, ?, ?, 0)""",
                            (
                                user.group_id,
                                template["type"],
                                template["target"],
                                template["reward_coins"],
                                template["description"],
                                today,
                            ),
                        )
                    conn.execute(
                        """UPDATE group_tasks SET current_value = MIN(current_value + 1, target_value)
                           WHERE group_id = ? AND task_type = ? AND created_date = ?""",
                        (user.group_id, group_task_type, today),
                    )
                conn.commit()
                return True
            except Exception as exc:
                conn.rollback()
                _log_database_failure("atomic_update_pet_and_user", exc)
                return False

    def get_all_group_ids(self) -> List[int]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("SELECT DISTINCT group_id FROM users")
                return [row['group_id'] for row in cursor.fetchall()]
            except Exception as exc:
                _log_database_failure("get_all_group_ids", exc)
                return []
    def settle_expired_trade_listings(self, group_id: Optional[int] = None) -> int:
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                params: tuple = (utc_now().isoformat(),)
                group_clause = ""
                if group_id is not None:
                    group_clause = " AND group_id = ?"
                    params = (params[0], group_id)
                rows = conn.execute(
                    """SELECT * FROM trade_listings WHERE is_active = 1
                       AND expires_at <= ?""" + group_clause,
                    params,
                ).fetchall()
                settled = 0
                for row in rows:
                    claim = conn.execute(
                        """UPDATE trade_listings SET is_active = 0, status = 'expired'
                           WHERE id = ? AND is_active = 1""",
                        (row["id"],),
                    )
                    if claim.rowcount != 1:
                        continue
                    items = self._load_inventory_items(
                        conn, str(row["seller_user_id"]), int(row["group_id"])
                    )
                    item_id = str(row["item_id"])
                    amount = int(row["amount"])
                    items[item_id] = int(items.get(item_id, 0)) + amount
                    self._save_inventory_items(
                        conn, str(row["seller_user_id"]), int(row["group_id"]), items
                    )
                    conn.execute(
                        """INSERT OR IGNORE INTO asset_ledger
                           (user_id, group_id, asset_type, delta, reason, reference_id, created_at)
                           VALUES (?, ?, 'item', ?, 'trade_expiry_refund', ?, ?)""",
                        (
                            row["seller_user_id"],
                            row["group_id"],
                            amount,
                            f"trade-expire:{row['id']}",
                            utc_now().isoformat(),
                        ),
                    )
                    settled += 1
                conn.commit()
                return settled
            except Exception as exc:
                conn.rollback()
                _log_database_failure("settle_expired_trade_listings", exc)
                return 0

    def get_enabled_group_ids(self, *, require_activity: bool = False) -> List[int]:
        with self._lock:
            try:
                conn = self._get_connection()
                condition = "enabled = 1 AND activity_enabled = 1" if require_activity else "enabled = 1"
                rows = conn.execute(
                    f"SELECT group_id FROM group_configs WHERE {condition} ORDER BY group_id"
                ).fetchall()
                return [int(row["group_id"]) for row in rows]
            except Exception as exc:
                _log_database_failure("get_enabled_group_ids", exc)
                return []

    def claim_scheduler_run(self, job_name: str, period_key: str) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO scheduler_runs (job_name, period_key, claimed_at) VALUES (?, ?, ?)",
                    (job_name, period_key, utc_now().isoformat()),
                )
                conn.commit()
                return cursor.rowcount == 1
            except Exception as exc:
                _log_database_failure("claim_scheduler_run", exc)
                return False

    # ─────────────────── CR Review: 点赞频率限制 ──────────────────

    def record_daily_like(self, user_id: str, target_user_id: str, group_id: int) -> bool:
        """记录今日点赞"""
        with self._lock:
            try:
                conn = self._get_connection()
                today = utc_now().strftime("%Y-%m-%d")
                conn.execute("""INSERT INTO daily_likes (user_id, target_user_id, group_id, like_date, like_count)
                    VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(user_id, target_user_id, group_id, like_date)
                    DO UPDATE SET like_count = like_count + 1""",
                    (user_id, target_user_id, group_id, today))
                conn.commit()
                return True
            except Exception as exc:
                _log_database_failure("record_daily_like", exc)
                return False

    def get_daily_like_count(self, user_id: str, target_user_id: str, group_id: int) -> int:
        """获取今日对特定目标的点赞次数"""
        with self._lock:
            try:
                conn = self._get_connection()
                today = utc_now().strftime("%Y-%m-%d")
                cursor = conn.execute("""SELECT like_count FROM daily_likes
                    WHERE user_id = ? AND target_user_id = ? AND group_id = ? AND like_date = ?""",
                    (user_id, target_user_id, group_id, today))
                row = cursor.fetchone()
                return row['like_count'] if row else 0
            except Exception as exc:
                _log_database_failure("get_daily_like_count", exc)
                return 0

    # ─────────────────── CR Review: 称号过期清理 ──────────────────

    def add_title_with_expiry(self, user_id: str, group_id: int,
                              title: str, duration_days: int) -> bool:
        """添加有时效的称号"""
        with self._lock:
            try:
                conn = self._get_connection()
                expires_at = (utc_now() + timedelta(days=duration_days)).isoformat()
                conn.execute("""INSERT OR REPLACE INTO title_expiry
                    (user_id, group_id, title, expires_at)
                    VALUES (?, ?, ?, ?)""",
                    (user_id, group_id, title, expires_at))
                conn.commit()
                return True
            except Exception as exc:
                _log_database_failure("add_title_with_expiry", exc)
                return False

    def grant_temporary_title(self, user_id: str, group_id: int, title: str, duration_days: int) -> bool:
        """Add a temporary title to the user and persist the expiry in one transaction."""
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute(
                    "SELECT titles FROM users WHERE user_id = ? AND group_id = ?",
                    (user_id, group_id),
                )
                row = cursor.fetchone()
                if row is None:
                    return False
                titles = json.loads(row["titles"] or "[]")
                if title not in titles:
                    titles.append(title)
                    conn.execute(
                        "UPDATE users SET titles = ? WHERE user_id = ? AND group_id = ?",
                        (json.dumps(titles), user_id, group_id),
                    )
                expires_at = (utc_now() + timedelta(days=duration_days)).isoformat()
                conn.execute(
                    """
                    INSERT OR REPLACE INTO title_expiry (user_id, group_id, title, expires_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_id, group_id, title, expires_at),
                )
                conn.commit()
                return True
            except Exception as exc:
                _log_database_failure("grant_temporary_title", exc)
                return False

    def cleanup_expired_titles(self) -> int:
        """清理所有过期称号"""
        with self._lock:
            try:
                conn = self._get_connection()
                now = utc_now().isoformat()
                # 获取过期称号
                cursor = conn.execute("""SELECT user_id, group_id, title FROM title_expiry
                    WHERE expires_at < ?""", (now,))
                expired = cursor.fetchall()

                count = 0
                for row in expired:
                    user_id, group_id, title = row['user_id'], row['group_id'], row['title']
                    # 从用户称号列表中移除
                    user_cursor = conn.execute(
                        "SELECT titles FROM users WHERE user_id = ? AND group_id = ?",
                        (user_id, group_id))
                    user_row = user_cursor.fetchone()
                    if user_row and user_row['titles']:
                        titles = json.loads(user_row['titles'])
                        if title in titles:
                            titles.remove(title)
                            conn.execute("UPDATE users SET titles = ? WHERE user_id = ? AND group_id = ?",
                                         (json.dumps(titles), user_id, group_id))
                            count += 1

                # 删除过期记录
                conn.execute("DELETE FROM title_expiry WHERE expires_at < ?", (now,))
                conn.commit()
                return count
            except Exception as exc:
                _log_database_failure("cleanup_expired_titles", exc)
                return 0

    # ─────────────────── CR Review: 优化金币排行（解决 N+1 问题） ────────

    def get_coins_ranking(self, group_id: int, limit: int = 10) -> List[Dict]:
        """CR Review: 使用 JOIN 查询替代 N+1 循环"""
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("""
                    SELECT u.user_id, p.name as pet_name, u.coins
                    FROM users u
                    JOIN pets p ON u.user_id = p.user_id AND u.group_id = p.group_id
                    WHERE u.group_id = ?
                    ORDER BY u.coins DESC
                    LIMIT ?
                """, (group_id, limit))
                return [dict(row) for row in cursor.fetchall()]
            except Exception as exc:
                _log_database_failure("get_coins_ranking", exc)
                return []

    def get_pet_ranking(self, group_id: int, ranking_type: str, limit: int = 10) -> List[Dict]:
        expressions = {
            "care_score": "(hunger + mood + clean + energy + health) / 5.0",
            "intimacy": "intimacy",
            "experience": "experience",
        }
        expression = expressions.get(ranking_type)
        if expression is None:
            return []
        with self._lock:
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
        with self._lock:
            try:
                rows = self._get_connection().execute(
                    "SELECT user_id, group_id FROM users WHERE trustee_until > ?",
                    (utc_now().isoformat(),),
                ).fetchall()
                return {(str(row["user_id"]), int(row["group_id"])) for row in rows}
            except Exception as exc:
                _log_database_failure("get_active_trustee_keys", exc)
                return set()

    # ─────────────────── CR Review: 管理员清空留言 ──────────────────

    def clear_messages(self, group_id: int, target_user_id: Optional[str] = None) -> int:
        """清空留言（管理员功能）"""
        with self._lock:
            try:
                conn = self._get_connection()
                if target_user_id:
                    cursor = conn.execute(
                        "DELETE FROM message_board WHERE group_id = ? AND to_user_id = ?",
                        (group_id, target_user_id))
                else:
                    cursor = conn.execute(
                        "DELETE FROM message_board WHERE group_id = ?",
                        (group_id,))
                conn.commit()
                return cursor.rowcount
            except Exception as exc:
                _log_database_failure("clear_messages", exc)
                return 0

    # ─────────────────── CR Review: 交易记录查询 ──────────────────

    def get_trade_history(self, group_id: int, limit: int = 20) -> List[Dict]:
        """查询交易历史日志"""
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("""
                    SELECT * FROM operation_logs
                    WHERE group_id = ? AND operation_type IN ('TRADE_SELL', 'TRADE_BUY', 'TRADE_CANCEL')
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (group_id, limit))
                return [dict(row) for row in cursor.fetchall()]
            except Exception as exc:
                _log_database_failure("get_trade_history", exc)
                return []
