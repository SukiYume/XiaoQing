"""Qingpet 定时结算、到期清理、排行和经济快照仓储。"""

import json
import logging
import sqlite3
from datetime import timedelta

from . import database_clock
from .database_repository_base import DatabaseRepositorySupport
from .database_support import (
    _DAILY_COIN_LIMIT,
    _WEEKLY_RANKING_REWARDS,
    _log_database_failure,
)
from .database_types import (
    CoinLedgerReconciliation,
    DailyResetResult,
    GroupEconomySnapshot,
    WeeklyActivitySettlementResult,
    WeeklyRankingWinner,
)

logger = logging.getLogger(__name__)


class SchedulerRepositoryMixin(DatabaseRepositorySupport):
    """集中管理可幂等重跑的周期结算、清理、排行与统计查询。"""

    def get_all_group_ids(self) -> list[int]:
        try:
            conn   = self._get_connection()
            cursor = conn.execute("SELECT DISTINCT group_id FROM users")
            return [row["group_id"] for row in cursor.fetchall()]
        except Exception as exc:
            _log_database_failure("get_all_group_ids", exc)
            return []

    def settle_expired_trade_listings(self, group_id: int | None = None) -> int:
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now           = database_clock.now().isoformat()
            params: tuple = (now,)
            group_clause  = ""
            if group_id is not None:
                group_clause = " AND group_id = ?"
                params       = (params[0], group_id)
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

        seller_id      = str(row["seller_user_id"])
        group_id       = int(row["group_id"])
        item_id        = str(row["item_id"])
        amount         = int(row["amount"])
        items          = self._load_inventory_items(conn, seller_id, group_id)
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
            conn      = self._get_connection()
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
        now     = database_clock.now()
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
            (database_clock.now().isoformat(), job_name, period_key),
        )
        return cursor.rowcount == 1

    def run_daily_reset_atomic(self, period_key: str, group_id: int) -> DailyResetResult | None:
        """原子领取每日任务、重置计数、增加宠物年龄并完成调度。"""
        job_name = "qingpet_daily_reset"
        conn     = self._get_connection()
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
        conn     = self._get_connection()
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
            now                                = database_clock.now()
            winners: list[WeeklyRankingWinner] = []
            for index, rank_row in enumerate(ranking):
                user_id = str(rank_row["user_id"])
                grant   = self._credit_coins_in_transaction(
                    conn,
                    user_id,
                    group_id,
                    _WEEKLY_RANKING_REWARDS[index],
                    reason       = "weekly_ranking",
                    reference_id = f"weekly:{period_key}:{index}:{user_id}",
                    daily_limit  = _DAILY_COIN_LIMIT,
                    now_iso      = now.isoformat(),
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
                        user_id       = user_id,
                        pet_name      = str(rank_row["pet_name"]),
                        score         = round(float(rank_row["score"]), 1),
                        coins_granted = grant,
                        title_granted = title_granted,
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
            now     = database_clock.now().isoformat()
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
        unavailable    = CoinLedgerReconciliation(
            status           = "unavailable",
            current_balance  = 0,
            expected_balance = 0,
            difference       = 0,
            consistent       = False,
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
                total_pets         = int(row["total_pets"] or 0),
                total_coins        = int(row["total_coins"] or 0),
                total_experience   = int(row["total_experience"] or 0),
                total_intimacy     = int(row["total_intimacy"] or 0),
                average_care_score = float(row["average_care_score"] or 0.0),
                active_today       = int(row["active_today"] or 0),
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
            checkpoint   = conn.execute(
                """SELECT balance, ledger_total
                   FROM asset_reconciliation_checkpoints
                   WHERE group_id = ? AND asset_type = 'coins'""",
                (group_id,),
            ).fetchone()
            checked_at = database_clock.now().isoformat()
            if checkpoint is None:
                conn.execute(
                    """INSERT INTO asset_reconciliation_checkpoints
                       (group_id, asset_type, balance, ledger_total, checked_at)
                       VALUES (?, 'coins', ?, ?, ?)""",
                    (group_id, snapshot.total_coins, ledger_total, checked_at),
                )
                reconciliation = CoinLedgerReconciliation(
                    status           = "baseline_created",
                    current_balance  = snapshot.total_coins,
                    expected_balance = snapshot.total_coins,
                    difference       = 0,
                    consistent       = True,
                )
            else:
                expected_balance = int(checkpoint["balance"]) + (
                    ledger_total - int(checkpoint["ledger_total"])
                )
                difference     = snapshot.total_coins - expected_balance
                consistent     = difference == 0
                reconciliation = CoinLedgerReconciliation(
                    status           = "consistent" if consistent else "mismatch",
                    current_balance  = snapshot.total_coins,
                    expected_balance = expected_balance,
                    difference       = difference,
                    consistent       = consistent,
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
            conn   = self._get_connection()
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
                    (database_clock.now().isoformat(),),
                )
                .fetchall()
            )
            return {(str(row["user_id"]), int(row["group_id"])) for row in rows}
        except Exception as exc:
            _log_database_failure("get_active_trustee_keys", exc)
            return set()
