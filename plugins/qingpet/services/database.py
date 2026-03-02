import sqlite3
import json
import logging
import threading
import time
import re
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from pathlib import Path

from ..models import Pet, User, Item, Inventory, GroupConfig, PluginConfig, OperationLog
from ..utils.constants import (
    DEFAULT_ITEMS, PetStage, PetPersonality, PetStatus
)

logger = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

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
}


class Database:
    """
    数据库服务层。
    CR修复: 添加索引、持久化visit/gift时间、personality兼容、事务支持、
    date范围查询替代LIKE、delete_pet、increment_all_pet_ages、交易/展示会/装扮表。
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_database()

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
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
                show_id INTEGER NOT NULL, voter_user_id TEXT NOT NULL,
                pet_user_id TEXT NOT NULL, created_at TEXT,
                PRIMARY KEY (show_id, voter_user_id))""")

            cursor.execute("""CREATE TABLE IF NOT EXISTS command_timestamps (
                user_id TEXT NOT NULL, group_id INTEGER NOT NULL, timestamp REAL NOT NULL)""")

            # 交易市场表 (新增)
            cursor.execute("""CREATE TABLE IF NOT EXISTS trade_listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_user_id TEXT NOT NULL, group_id INTEGER NOT NULL,
                item_id TEXT NOT NULL, amount INTEGER DEFAULT 1,
                price INTEGER NOT NULL, created_at TEXT, expires_at TEXT,
                is_active BOOLEAN DEFAULT 1)""")

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
            last_update=datetime.fromisoformat(row['last_update']) if row['last_update'] else datetime.now(),
            last_feed=datetime.fromisoformat(row['last_feed']) if row['last_feed'] else None,
            last_clean=datetime.fromisoformat(row['last_clean']) if row['last_clean'] else None,
            last_play=datetime.fromisoformat(row['last_play']) if row['last_play'] else None,
            last_train=datetime.fromisoformat(row['last_train']) if row['last_train'] else None,
            last_explore=datetime.fromisoformat(row['last_explore']) if row['last_explore'] else None,
            likes=row['likes'] if 'likes' in keys else 0,
            created_at=datetime.fromisoformat(row['created_at']) if row['created_at'] else datetime.now()
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
            created_at=datetime.fromisoformat(row['created_at']) if row['created_at'] else datetime.now(),
            last_active=datetime.fromisoformat(row['last_active']) if row['last_active'] else datetime.now()
        )

    # ──────────────────── User CRUD ────────────────────

    def create_user(self, user: User) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("""
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
                return True
            except Exception as e:
                logger.error(f"Failed to create user: {e}")
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
            except Exception as e:
                logger.error(f"Failed to get user: {e}")
                return None

    def update_user(self, user: User) -> bool:
        """CR Fix #11: 写入 last_visit_time / last_gift_time"""
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("""
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
                return True
            except Exception as e:
                logger.error(f"Failed to update user: {e}")
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
            except Exception as e:
                logger.error(f"Failed to create pet: {e}")
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
            except Exception as e:
                logger.error(f"Failed to get pet: {e}")
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
            except Exception as e:
                logger.error(f"Failed to update pet: {e}")
                return False

    def delete_pet(self, user_id: str, group_id: int) -> bool:
        """CR Fix #16: 添加缺失的 delete_pet 方法"""
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("DELETE FROM pets WHERE user_id = ? AND group_id = ?",
                             (user_id, group_id))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Failed to delete pet: {e}")
                return False

    def get_all_pets_in_group(self, group_id: int) -> List[Pet]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("SELECT * FROM pets WHERE group_id = ?", (group_id,))
                return [self._row_to_pet(row) for row in cursor.fetchall()]
            except Exception as e:
                logger.error(f"Failed to get pets in group: {e}")
                return []

    def get_all_pets(self) -> List[Pet]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("SELECT * FROM pets")
                return [self._row_to_pet(row) for row in cursor.fetchall()]
            except Exception as e:
                logger.error(f"Failed to get all pets: {e}")
                return []

    def get_pets_by_user(self, user_id: str) -> List[Pet]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute(
                    "SELECT * FROM pets WHERE user_id = ? ORDER BY group_id",
                    (user_id,),
                )
                return [self._row_to_pet(row) for row in cursor.fetchall()]
            except Exception as e:
                logger.error(f"Failed to get pets by user: {e}")
                return []

    def increment_all_pet_ages(self) -> int:
        """CR Fix #8: 宠物年龄每日递增"""
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("UPDATE pets SET age = age + 1")
                conn.commit()
                return cursor.rowcount
            except Exception as e:
                logger.error(f"Failed to increment pet ages: {e}")
                return 0

    def like_pet(self, user_id: str, group_id: int) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("UPDATE pets SET likes = likes + 1 WHERE user_id = ? AND group_id = ?",
                             (user_id, group_id))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Failed to like pet: {e}")
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
            except Exception as e:
                logger.error(f"Failed to get/create inventory: {e}")
                return Inventory(user_id=user_id, group_id=group_id, items={})

    def update_inventory(self, inventory: Inventory) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("UPDATE inventories SET items = ? WHERE user_id = ? AND group_id = ?",
                             (json.dumps(inventory.items), inventory.user_id, inventory.group_id))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Failed to update inventory: {e}")
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
                return GroupConfig.default(group_id)
            except Exception as e:
                logger.error(f"Failed to get group config: {e}")
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
            except Exception as e:
                logger.error(f"Failed to update group config: {e}")
                return False

    # ──────────────────── Plugin Config (CR Fix: 全局配置接入) ────────

    def get_plugin_config(self, key: str) -> Optional[str]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("SELECT value FROM plugin_configs WHERE key = ?", (key,))
                row = cursor.fetchone()
                return row['value'] if row else None
            except Exception as e:
                logger.error(f"Failed to get plugin config: {e}")
                return None

    def set_plugin_config(self, key: str, value: str) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("INSERT OR REPLACE INTO plugin_configs (key, value) VALUES (?, ?)", (key, value))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Failed to set plugin config: {e}")
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
            except Exception as e:
                logger.error(f"Failed to log operation: {e}")
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
                    created_at=datetime.fromisoformat(row['created_at']) if row['created_at'] else datetime.now()
                ) for row in cursor.fetchall()]
            except Exception as e:
                logger.error(f"Failed to get operation logs: {e}")
                return []

    # ──────────────────── Tasks (CR Fix #10: date范围查询替代LIKE) ────

    def get_or_create_daily_tasks(self, user_id: str, group_id: int) -> List[Dict]:
        with self._lock:
            try:
                conn = self._get_connection()
                today = datetime.now().strftime("%Y-%m-%d")
                cursor = conn.execute(
                    "SELECT * FROM tasks WHERE user_id = ? AND group_id = ? AND created_date = ?",
                    (user_id, group_id, today))
                rows = cursor.fetchall()
                if rows:
                    return [dict(row) for row in rows]
                task_templates = [("feed", 3, 30), ("clean", 2, 20), ("play", 3, 25), ("visit", 2, 20)]
                now_str = datetime.now().isoformat()
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
            except Exception as e:
                logger.error(f"Failed to get/create daily tasks: {e}")
                return []

    def update_task_progress(self, user_id: str, group_id: int, task_type: str, increment: int = 1) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                today = datetime.now().strftime("%Y-%m-%d")
                now_str = datetime.now().isoformat()

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
            except Exception as e:
                logger.error(f"Failed to update task progress: {e}")
                return False

    def claim_task_reward(self, user_id: str, group_id: int, task_type: str) -> Optional[int]:
        with self._lock:
            try:
                conn = self._get_connection()
                today = datetime.now().strftime("%Y-%m-%d")
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
            except Exception as e:
                logger.error(f"Failed to claim task reward: {e}")
                return None

    # ──────────────────── Activities ────────────────────

    def get_active_activities(self, group_id: int) -> List[Dict]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("SELECT * FROM activities WHERE group_id = ? AND is_active = 1", (group_id,))
                return [dict(row) for row in cursor.fetchall()]
            except Exception as e:
                logger.error(f"Failed to get active activities: {e}")
                return []

    def update_activity_progress(self, activity_id: int, increment: int = 1) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("UPDATE activities SET current_value = current_value + ? WHERE id = ?",
                             (increment, activity_id))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Failed to update activity progress: {e}")
                return False

    # ──────────────────── Message Board ────────────────────

    def add_message(self, group_id: int, from_user_id: str, to_user_id: str, message: str) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("""INSERT INTO message_board (group_id, from_user_id, to_user_id, message, created_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (group_id, from_user_id, to_user_id, message, datetime.now().isoformat()))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Failed to add message: {e}")
                return False

    def get_messages(self, to_user_id: str, group_id: int, limit: int = 10) -> List[Dict]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("""SELECT * FROM message_board
                    WHERE to_user_id = ? AND group_id = ? ORDER BY created_at DESC LIMIT ?""",
                    (to_user_id, group_id, limit))
                return [dict(row) for row in cursor.fetchall()]
            except Exception as e:
                logger.error(f"Failed to get messages: {e}")
                return []

    # ──────────────────── Anti-Spam ────────────────────

    def record_command_timestamp(self, user_id: str, group_id: int) -> None:
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("INSERT INTO command_timestamps (user_id, group_id, timestamp) VALUES (?, ?, ?)",
                             (user_id, group_id, time.time()))
                conn.commit()
            except Exception as e:
                logger.error(f"Failed to record command timestamp: {e}")

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
            except Exception as e:
                logger.error(f"Failed to get recent command count: {e}")
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
            except Exception as e:
                logger.error(f"Failed to get group recent command count: {e}")
                return 0

    def cleanup_old_timestamps(self, max_age_seconds: int = 3600) -> None:
        with self._lock:
            try:
                conn = self._get_connection()
                threshold = time.time() - max_age_seconds
                conn.execute("DELETE FROM command_timestamps WHERE timestamp < ?", (threshold,))
                conn.commit()
            except Exception as e:
                logger.error(f"Failed to cleanup timestamps: {e}")

    # ──────────────────── Trade Market (新增) ────────────────────

    def create_trade_listing(self, seller_id: str, group_id: int, item_id: str,
                             amount: int, price: int, expire_hours: int = 72) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                now = datetime.now()
                expires = now + timedelta(hours=expire_hours)
                conn.execute("""INSERT INTO trade_listings
                    (seller_user_id, group_id, item_id, amount, price, created_at, expires_at, is_active)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
                    (seller_id, group_id, item_id, amount, price, now.isoformat(), expires.isoformat()))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Failed to create trade listing: {e}")
                return False

    def get_active_listings(self, group_id: int) -> List[Dict]:
        with self._lock:
            try:
                conn = self._get_connection()
                now = datetime.now().isoformat()
                cursor = conn.execute("""SELECT * FROM trade_listings
                    WHERE group_id = ? AND is_active = 1 AND expires_at > ?
                    ORDER BY created_at DESC""", (group_id, now))
                return [dict(row) for row in cursor.fetchall()]
            except Exception as e:
                logger.error(f"Failed to get active listings: {e}")
                return []

    def get_user_listing_count(self, user_id: str, group_id: int) -> int:
        with self._lock:
            try:
                conn = self._get_connection()
                now = datetime.now().isoformat()
                cursor = conn.execute("""SELECT COUNT(*) as cnt FROM trade_listings
                    WHERE seller_user_id = ? AND group_id = ? AND is_active = 1 AND expires_at > ?""",
                    (user_id, group_id, now))
                row = cursor.fetchone()
                return row['cnt'] if row else 0
            except Exception as e:
                logger.error(f"Failed to get user listing count: {e}")
                return 0

    def get_listing_by_id(self, listing_id: int) -> Optional[Dict]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("SELECT * FROM trade_listings WHERE id = ? AND is_active = 1", (listing_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
            except Exception as e:
                logger.error(f"Failed to get listing: {e}")
                return None

    def deactivate_listing(self, listing_id: int) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("UPDATE trade_listings SET is_active = 0 WHERE id = ?", (listing_id,))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Failed to deactivate listing: {e}")
                return False

    # ──────────────────── Pet Show (新增完整实现) ────────────────────

    def create_pet_show(self, group_id: int, title: str, duration_hours: int) -> Optional[int]:
        with self._lock:
            try:
                conn = self._get_connection()
                now = datetime.now()
                end = now + timedelta(hours=duration_hours)
                cursor = conn.execute("""INSERT INTO pet_shows
                    (group_id, title, start_time, end_time, is_active)
                    VALUES (?, ?, ?, ?, 1)""",
                    (group_id, title, now.isoformat(), end.isoformat()))
                conn.commit()
                return cursor.lastrowid
            except Exception as e:
                logger.error(f"Failed to create pet show: {e}")
                return None

    def get_active_pet_show(self, group_id: int) -> Optional[Dict]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("""SELECT * FROM pet_shows
                    WHERE group_id = ? AND is_active = 1 ORDER BY id DESC LIMIT 1""", (group_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
            except Exception as e:
                logger.error(f"Failed to get active pet show: {e}")
                return None

    def vote_pet_show(self, show_id: int, voter_id: str, pet_user_id: str) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("""INSERT OR IGNORE INTO pet_show_votes
                    (show_id, voter_user_id, pet_user_id, created_at)
                    VALUES (?, ?, ?, ?)""",
                    (show_id, voter_id, pet_user_id, datetime.now().isoformat()))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Failed to vote: {e}")
                return False

    def get_pet_show_votes(self, show_id: int) -> Dict[str, int]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("""SELECT pet_user_id, COUNT(*) as votes
                    FROM pet_show_votes WHERE show_id = ?
                    GROUP BY pet_user_id ORDER BY votes DESC""", (show_id,))
                return {row['pet_user_id']: row['votes'] for row in cursor.fetchall()}
            except Exception as e:
                logger.error(f"Failed to get votes: {e}")
                return {}

    def get_user_vote_count(self, show_id: int, voter_id: str) -> int:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("""SELECT COUNT(*) as cnt FROM pet_show_votes
                    WHERE show_id = ? AND voter_user_id = ?""", (show_id, voter_id))
                row = cursor.fetchone()
                return row['cnt'] if row else 0
            except Exception as e:
                return 0

    def end_pet_show(self, show_id: int) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute("UPDATE pet_shows SET is_active = 0 WHERE id = ?", (show_id,))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Failed to end pet show: {e}")
                return False

    # ──────────────────── Dress Inventory (新增) ────────────────────

    def get_dress_inventory(self, user_id: str, group_id: int) -> List[str]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("""SELECT dress_item_id FROM dress_inventory
                    WHERE user_id = ? AND group_id = ?""", (user_id, group_id))
                return [row['dress_item_id'] for row in cursor.fetchall()]
            except Exception as e:
                logger.error(f"Failed to get dress inventory: {e}")
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
            except Exception as e:
                logger.error(f"Failed to add dress item: {e}")
                return False

    # ──────────────────── Group Tasks (新增: 群累计任务) ────────────────

    def get_or_create_group_tasks(self, group_id: int) -> List[Dict]:
        with self._lock:
            try:
                conn = self._get_connection()
                today = datetime.now().strftime("%Y-%m-%d")
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
            except Exception as e:
                logger.error(f"Failed to get/create group tasks: {e}")
                return []

    def update_group_task_progress(self, group_id: int, task_type: str, increment: int = 1) -> bool:
        with self._lock:
            try:
                conn = self._get_connection()
                today = datetime.now().strftime("%Y-%m-%d")
                conn.execute("""UPDATE group_tasks SET current_value = MIN(current_value + ?, target_value)
                    WHERE group_id = ? AND task_type = ? AND created_date = ? AND is_completed = 0""",
                    (increment, group_id, task_type, today))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Failed to update group task: {e}")
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
            except Exception as e:
                logger.error(f"Failed to batch daily reset: {e}")
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
            except Exception as e:
                logger.error(f"Failed to batch daily reset all: {e}")
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
            except Exception as e:
                conn.rollback()
                logger.error(f"Transaction failed: {e}")
                return False

    def atomic_update_pet_and_user(self, pet: Pet, user: User) -> bool:
        """CR Fix #5: 原子性更新宠物和用户"""
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("""UPDATE pets SET
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
                conn.execute("""UPDATE users SET
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
                conn.commit()
                return True
            except Exception as e:
                conn.rollback()
                logger.error(f"Atomic update failed: {e}")
                return False

    # ──────────────────── Data Export (新增) ────────────────────

    def export_group_data(self, group_id: int) -> Dict:
        """导出群数据（用于备份）"""
        with self._lock:
            try:
                conn = self._get_connection()
                data = {"group_id": group_id, "exported_at": datetime.now().isoformat()}
                cursor = conn.execute("SELECT * FROM users WHERE group_id = ?", (group_id,))
                data["users"] = [dict(row) for row in cursor.fetchall()]
                cursor = conn.execute("SELECT * FROM pets WHERE group_id = ?", (group_id,))
                data["pets"] = [dict(row) for row in cursor.fetchall()]
                cursor = conn.execute("SELECT * FROM inventories WHERE group_id = ?", (group_id,))
                data["inventories"] = [dict(row) for row in cursor.fetchall()]
                return data
            except Exception as e:
                logger.error(f"Failed to export data: {e}")
                return {}

    def get_all_group_ids(self) -> List[int]:
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.execute("SELECT DISTINCT group_id FROM users")
                return [row['group_id'] for row in cursor.fetchall()]
            except Exception as e:
                logger.error(f"Failed to get group ids: {e}")
                return []

    # ─────────────────── CR Review: 点赞频率限制 ──────────────────

    def record_daily_like(self, user_id: str, target_user_id: str, group_id: int) -> bool:
        """记录今日点赞"""
        with self._lock:
            try:
                conn = self._get_connection()
                today = datetime.now().strftime("%Y-%m-%d")
                conn.execute("""INSERT INTO daily_likes (user_id, target_user_id, group_id, like_date, like_count)
                    VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(user_id, target_user_id, group_id, like_date)
                    DO UPDATE SET like_count = like_count + 1""",
                    (user_id, target_user_id, group_id, today))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Failed to record daily like: {e}")
                return False

    def get_daily_like_count(self, user_id: str, target_user_id: str, group_id: int) -> int:
        """获取今日对特定目标的点赞次数"""
        with self._lock:
            try:
                conn = self._get_connection()
                today = datetime.now().strftime("%Y-%m-%d")
                cursor = conn.execute("""SELECT like_count FROM daily_likes
                    WHERE user_id = ? AND target_user_id = ? AND group_id = ? AND like_date = ?""",
                    (user_id, target_user_id, group_id, today))
                row = cursor.fetchone()
                return row['like_count'] if row else 0
            except Exception as e:
                logger.error(f"Failed to get daily like count: {e}")
                return 0

    # ─────────────────── CR Review: 称号过期清理 ──────────────────

    def add_title_with_expiry(self, user_id: str, group_id: int,
                              title: str, duration_days: int) -> bool:
        """添加有时效的称号"""
        with self._lock:
            try:
                conn = self._get_connection()
                expires_at = (datetime.now() + timedelta(days=duration_days)).isoformat()
                conn.execute("""INSERT OR REPLACE INTO title_expiry
                    (user_id, group_id, title, expires_at)
                    VALUES (?, ?, ?, ?)""",
                    (user_id, group_id, title, expires_at))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Failed to add title with expiry: {e}")
                return False

    def cleanup_expired_titles(self) -> int:
        """清理所有过期称号"""
        with self._lock:
            try:
                conn = self._get_connection()
                now = datetime.now().isoformat()
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
            except Exception as e:
                logger.error(f"Failed to cleanup expired titles: {e}")
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
            except Exception as e:
                logger.error(f"Failed to get coins ranking: {e}")
                return []

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
            except Exception as e:
                logger.error(f"Failed to clear messages: {e}")
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
            except Exception as e:
                logger.error(f"Failed to get trade history: {e}")
                return []
