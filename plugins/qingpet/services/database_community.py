"""Qingpet 管理、任务、活动、社交和交易市场的 SQLite 仓储。"""

import math
import sqlite3
import time
from datetime import datetime, timedelta

from ..models import OperationLog
from ..utils.constants import GROUP_TASK_TEMPLATES, PetStage, PetStatus
from . import database_clock
from .database_repository_base import DatabaseRepositorySupport
from .database_support import (
    _DAILY_COIN_LIMIT,
    _DAILY_TASK_TEMPLATES,
    _log_database_failure,
)
from .database_types import LeaveMessageAtomicResult


class CommunityRepositoryMixin(DatabaseRepositorySupport):
    """集中管理管理员操作、任务、活动、留言、交易和展示会。"""

    # ──────────────────── 管理操作日志 ────────────────────

    @staticmethod
    def _insert_operation_log_in_transaction(
        conn: sqlite3.Connection,
        *,
        group_id: int,
        operator_user_id: str,
        operation_type: str,
        params: str                = "",
        target_user_id: str | None = None,
        result: str                = "success",
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
                database_clock.now().isoformat(),
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
            conn   = self._get_connection()
            cursor = conn.execute(
                "SELECT * FROM operation_logs WHERE group_id = ? ORDER BY created_at DESC LIMIT ?",
                (group_id, limit),
            )
            return [
                OperationLog(
                    id             = row["id"],
                    group_id       = row["group_id"],
                    user_id        = row["user_id"],
                    target_user_id = row["target_user_id"],
                    operation_type = row["operation_type"],
                    params         = row["params"],
                    result         = row["result"],
                    created_at     = datetime.fromisoformat(row["created_at"])
                    if row["created_at"]
                    else database_clock.now(),
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
                    database_clock.now().isoformat(),
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
                group_id         = group_id,
                operator_user_id = operator_user_id,
                operation_type   = "RESET",
                params           = f"reset user {user_id}",
                target_user_id   = user_id,
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
            ban_until = (
                (database_clock.now() + timedelta(days=days)).isoformat()
                if days is not None
                else None
            )
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
            params         = f"ban {days} days" if days is not None else "unban"
            self._insert_operation_log_in_transaction(
                conn,
                group_id         = group_id,
                operator_user_id = operator_user_id,
                operation_type   = operation_type,
                params           = params,
                target_user_id   = user_id,
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
                group_id         = group_id,
                operator_user_id = operator_user_id,
                operation_type   = "DELETE",
                params           = f"delete pet for user {user_id}",
                target_user_id   = user_id,
            )
            conn.commit()
            return True
        except Exception as exc:
            conn.rollback()
            _log_database_failure("admin_delete_pet_atomic", exc)
            return False

    # ──────────────────── 每日任务（使用日期范围查询）──────────────────

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
            now   = database_clock.now()
            today = database_clock.business_date(now)
            self._ensure_daily_task_templates(
                conn,
                user_id,
                group_id,
                today   = today,
                now_iso = now.isoformat(),
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
            today = database_clock.business_date(database_clock.now())
            row   = conn.execute(
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
                reason       = "daily_task",
                reference_id = f"daily-task:{today}:{group_id}:{task_type}:{user_id}",
                daily_limit  = _DAILY_COIN_LIMIT,
                now_iso      = database_clock.now().isoformat(),
                record_zero  = True,
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
            conn   = self._get_connection()
            cursor = conn.execute(
                """SELECT * FROM activities
                   WHERE group_id = ? AND is_active = 1
                     AND (end_time IS NULL OR end_time > ?)
                   ORDER BY id""",
                (group_id, database_clock.now().isoformat()),
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
        title         = title.strip()
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
            conn   = self._get_connection()
            now    = database_clock.now()
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
            (increment, group_id, activity_type, database_clock.now().isoformat()),
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
                (activity_id, user_id, database_clock.now().isoformat()),
            )
            if claim.rowcount != 1:
                conn.rollback()
                return None
            reward = self._credit_coins_in_transaction(
                conn,
                user_id,
                group_id,
                int(row["reward_coins"] or 0),
                reason       = "activity",
                reference_id = f"activity:{activity_id}:{user_id}",
                daily_limit  = _DAILY_COIN_LIMIT,
                now_iso      = database_clock.now().isoformat(),
                record_zero  = True,
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
        conn  = self._get_connection()
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

            now_iso = database_clock.now().isoformat()
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
            conn   = self._get_connection()
            cursor = conn.execute(
                """SELECT * FROM message_board
                WHERE to_user_id = ? AND group_id = ? ORDER BY created_at DESC LIMIT ?""",
                (to_user_id, group_id, limit),
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception as exc:
            _log_database_failure("get_messages", exc)
            return []

    # ──────────────────── 反脚本频率记录 ────────────────────

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
            conn      = self._get_connection()
            threshold = time.time() - window_seconds
            cursor    = conn.execute(
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
            conn      = self._get_connection()
            threshold = time.time() - window_seconds
            cursor    = conn.execute(
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
            conn      = self._get_connection()
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
            conn   = self._get_connection()
            now    = database_clock.now().isoformat()
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
            now  = database_clock.now().isoformat()
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
            now    = database_clock.now().isoformat()
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
            now    = database_clock.now().isoformat()
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
            tax        = int(total_cost * tax_rate)
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
                user_id      = buyer_id,
                group_id     = group_id,
                asset_type   = "coins",
                delta        = -total_cost,
                reason       = "trade_purchase",
                reference_id = f"trade-purchase:{listing_id}:buyer",
            )
            self._record_asset_delta(
                conn,
                user_id      = str(listing["seller_user_id"]),
                group_id     = group_id,
                asset_type   = "coins",
                delta        = max(0, total_cost - tax),
                reason       = "trade_purchase",
                reference_id = f"trade-purchase:{listing_id}:seller",
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
            now = database_clock.now()
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
            conn   = self._get_connection()
            now    = database_clock.now().isoformat()
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
            conn   = self._get_connection()
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
            conn   = self._get_connection()
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
            today = database_clock.business_date(database_clock.now())
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
            today = database_clock.business_date(database_clock.now())
            task  = conn.execute(
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
                (group_id, task_type, today, user_id, database_clock.now().isoformat()),
            )
            if claim.rowcount != 1:
                conn.rollback()
                return None
            reward = self._credit_coins_in_transaction(
                conn,
                user_id,
                group_id,
                int(task["reward_coins"] or 0),
                reason       = "group_task",
                reference_id = f"group-task:{today}:{group_id}:{task_type}:{user_id}",
                daily_limit  = daily_coin_limit,
                now_iso      = database_clock.now().isoformat(),
                record_zero  = True,
            )
            conn.commit()
            return reward
        except Exception as exc:
            conn.rollback()
            _log_database_failure("claim_group_task_reward", exc)
            return None
