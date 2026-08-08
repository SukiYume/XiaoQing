"""Qingpet 资产、社交和宠物动作的原子 SQLite 结算。"""

import json
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from ..models import Inventory, Pet, User
from ..utils.constants import DEFAULT_ITEMS, PET_SHOW_CONFIG, TITLES, ItemType, PetStatus
from . import database_clock
from .database_repository_base import DatabaseRepositorySupport
from .database_support import (
    _DAILY_COIN_LIMIT,
    _PET_ACTION_COUNTERS,
    _log_database_failure,
)
from .database_types import (
    MinigameAtomicResult,
    MinigameOutcome,
    PetActionAtomicResult,
    PetShowSettlementResult,
    PetShowWinner,
    TreatPetAtomicResult,
    VisitPetAtomicResult,
)


class AtomicActionRepositoryMixin(DatabaseRepositorySupport):
    """所有配额、库存、金币与宠物状态变化都在调用方连接中原子提交。"""

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
            current = database_clock.now()
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
        current = now or database_clock.now()
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

    def _increment_task_in_transaction(
        self,
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
        self._ensure_daily_task_templates(
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

            now = database_clock.now()
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

            now = database_clock.now()
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

            updated_at = database_clock.now().isoformat()
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
            # 先写宠物、再写背包是有意安排：背包持久化一旦失败，仍需由同一事务
            # 回滚前面的经验变化，不能留下“获得经验但未消耗道具”的半完成状态。
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
                elapsed = (database_clock.now() - datetime.fromisoformat(last_gift)).total_seconds()
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
            now = database_clock.now().isoformat()
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
            now = database_clock.now()
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
            now = database_clock.now()
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
            now = database_clock.now()
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
            today = database_clock.now().strftime("%Y-%m-%d")
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
                    database_clock.now().isoformat(),
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
        return bool(
            self._commit_pet_and_user(
                pet,
                user,
                inventory=inventory,
                task_type=task_type,
                group_task_type=group_task_type,
            ).success
        )

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
                current_user.last_active = database_clock.now()
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
            now = database_clock.now()
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
