"""Qingpet 用户、宠物、背包和群配置的 SQLite 仓储。"""

import json
import math
import sqlite3
from datetime import datetime

from ..models import GroupConfig, GroupConfigReadError, Inventory, Pet, User
from ..utils.constants import PetPersonality, PetStage, PetStatus
from . import database_clock
from .database_repository_base import DatabaseRepositorySupport
from .database_support import _log_database_failure


class IdentityRepositoryMixin(DatabaseRepositorySupport):
    """集中管理身份、宠物状态、背包和群配置的读写。"""

    # ──────────────────── 数据库行到领域对象的映射 ────────────────────

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

    @classmethod
    def _row_to_pet(cls, row: sqlite3.Row) -> Pet:
        keys = row.keys()
        pet  = Pet(
            id                 = row["id"],
            user_id            = row["user_id"],
            group_id           = row["group_id"],
            name               = row["name"],
            stage              = PetStage(row["stage"]),
            form               = row["form"],
            hunger             = row["hunger"],
            mood               = row["mood"],
            clean              = row["clean"],
            energy             = row["energy"],
            health             = row["health"],
            age                = row["age"],
            experience         = row["experience"],
            intimacy           = row["intimacy"],
            personality        = cls._parse_personality(row["personality"]),
            favorite_food      = row["favorite_food"],
            status             = cls._parse_status(row["status"]),
            status_expire_time = datetime.fromisoformat(row["status_expire_time"])
            if row["status_expire_time"]
            else None,
            dress_hat        = row["dress_hat"] if "dress_hat" in keys else None,
            dress_clothes    = row["dress_clothes"] if "dress_clothes" in keys else None,
            dress_accessory  = row["dress_accessory"] if "dress_accessory" in keys else None,
            dress_background = row["dress_background"] if "dress_background" in keys else None,
            last_update      = datetime.fromisoformat(row["last_update"])
            if row["last_update"]
            else database_clock.now(),
            decay_remainders=json.loads(row["decay_remainders"])
            if "decay_remainders" in keys
            else {},
            last_feed    = datetime.fromisoformat(row["last_feed"]) if row["last_feed"] else None,
            last_clean   = datetime.fromisoformat(row["last_clean"]) if row["last_clean"] else None,
            last_play    = datetime.fromisoformat(row["last_play"]) if row["last_play"] else None,
            last_train   = datetime.fromisoformat(row["last_train"]) if row["last_train"] else None,
            last_explore = datetime.fromisoformat(row["last_explore"])
            if row["last_explore"]
            else None,
            likes      = row["likes"] if "likes" in keys else 0,
            version    = int(row["version"]) if "version" in keys else 0,
            created_at = datetime.fromisoformat(row["created_at"])
            if row["created_at"]
            else database_clock.now(),
        )
        pet.mark_persisted()
        return pet

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> User:
        """从兼容新旧字段集合的数据库行构造用户对象。"""
        keys = row.keys()
        user = User(
            user_id               = row["user_id"],
            group_id              = row["group_id"],
            coins                 = row["coins"],
            friendship_points     = row["friendship_points"],
            today_coins_earned    = row["today_coins_earned"],
            today_feed_count      = row["today_feed_count"],
            today_clean_count     = row["today_clean_count"],
            today_play_count      = row["today_play_count"],
            today_train_count     = row["today_train_count"],
            today_explore_count   = row["today_explore_count"],
            today_visit_count     = row["today_visit_count"],
            today_gift_count      = row["today_gift_count"],
            today_free_feed_count = row["today_free_feed_count"]
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
            is_banned  = bool(row["is_banned"]),
            ban_until  = datetime.fromisoformat(row["ban_until"]) if row["ban_until"] else None,
            created_at = datetime.fromisoformat(row["created_at"])
            if row["created_at"]
            else database_clock.now(),
            last_active=datetime.fromisoformat(row["last_active"])
            if row["last_active"]
            else database_clock.now(),
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
               last_train = ?, last_explore = ?, likes = ?, decay_remainders = ?, version = version + 1
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
                json.dumps(pet.decay_remainders, separators=(",", ":")),
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
                database_clock.now().isoformat(),
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
                    user_id      = user.user_id,
                    group_id     = user.group_id,
                    asset_type   = "coins",
                    delta        = int(user.coins),
                    reason       = "account_opening",
                    reference_id = f"account-opening:{user.group_id}:{user.user_id}",
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
            conn   = self._get_connection()
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
                user_id    = merged.user_id,
                group_id   = merged.group_id,
                asset_type = "coins",
                delta      = stored_coins - int(row["coins"]),
                reason     = "user_update",
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
            conn   = self._get_connection()
            cursor = conn.execute(
                """
                INSERT INTO pets (
                    user_id, group_id, name, stage, form,
                    hunger, mood, clean, energy, health,
                    age, experience, intimacy, personality, favorite_food,
                    status, status_expire_time,
                    dress_hat, dress_clothes, dress_accessory, dress_background,
                    last_update, likes, created_at, decay_remainders
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    json.dumps(pet.decay_remainders, separators=(",", ":")),
                ),
            )
            pet_id = cursor.lastrowid
            if pet_id is None:
                raise RuntimeError("宠物写入后未返回主键")
            conn.commit()
            pet.id      = pet_id
            pet.version = 0
            pet.mark_persisted()
            return True
        except Exception as exc:
            conn.rollback()
            _log_database_failure("create_pet", exc)
            return False

    def get_pet(self, user_id: str, group_id: int) -> Pet | None:
        try:
            conn   = self._get_connection()
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
            conn   = self._get_connection()
            cursor = conn.execute("SELECT * FROM pets")
            return [self._row_to_pet(row) for row in cursor.fetchall()]
        except Exception as exc:
            _log_database_failure("get_all_pets", exc)
            return []

    def get_enabled_group_decay_map(self) -> dict[int, float]:
        try:
            conn   = self._get_connection()
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
            conn   = self._get_connection()
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
            conn   = self._get_connection()
            cursor = conn.execute(
                "SELECT items, version FROM inventories WHERE user_id = ? AND group_id = ?",
                (user_id, group_id),
            )
            row = cursor.fetchone()
            if row:
                items     = json.loads(row["items"]) if row["items"] else {}
                inventory = Inventory(
                    user_id  = user_id,
                    group_id = group_id,
                    items    = items,
                    version  = int(row["version"]),
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

    # ──────────────────── 群级配置 ────────────────────

    def get_group_config(self, group_id: int) -> GroupConfig:
        conn: sqlite3.Connection | None = None
        try:
            conn   = self._get_connection()
            cursor = conn.execute("SELECT * FROM group_configs WHERE group_id = ?", (group_id,))
            row    = cursor.fetchone()
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
        decay_multiplier   = float(row["decay_multiplier"])
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
            group_id                = expected_group_id,
            enabled                 = boolean_values["enabled"],
            economy_multiplier      = economy_multiplier,
            decay_multiplier        = decay_multiplier,
            trade_enabled           = boolean_values["trade_enabled"],
            natural_trigger_enabled = boolean_values["natural_trigger_enabled"],
            activity_enabled        = boolean_values["activity_enabled"],
            sensitive_words         = sensitive_words,
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
