"""Pendo 提醒队列、租约、确认和周期调度 Outbox 的 SQLite 仓储。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import PendoConfig
from ..models.item import Item, ItemType
from ..utils.time_utils import (
    TimezoneHelper,
    normalize_datetime_for_storage,
    require_canonical_utc_timestamp,
    utc_now_iso,
)

# SQLite 默认变量上限留有余量，批量查询统一分片。
_SQLITE_ID_BATCH_SIZE = 500


class ReminderRepositoryMixin:
    """集中管理提醒状态机和周期任务投递租约。"""

    def get_connection(self) -> sqlite3.Connection:
        """由最终的 Database 提供线程本地连接。"""

        raise NotImplementedError

    def transaction(
        self,
        *,
        immediate: bool = False,
    ) -> AbstractContextManager[sqlite3.Connection]:
        """由最终的 Database 提供事务边界。"""

        raise NotImplementedError

    def _row_to_item(self, row: sqlite3.Row) -> Item | None:
        """由最终的 Database 提供条目反序列化。"""

        raise NotImplementedError

    def get_user_settings(self, user_id: str) -> dict[str, Any]:
        """由最终的 Database 提供用户时区等设置。"""

        raise NotImplementedError

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
        normalized = ReminderRepositoryMixin._scheduled_identity(task_name, owner_id, period_key)
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
        """确认指定条目的未确认提醒，并在需要时物化确认记录。"""
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

    def set_future_reminder_confirmation(
        self,
        item_id: str,
        remind_time: str,
        owner_id: str,
        *,
        confirmed: bool,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """提前确认或重新开启一条仍未到期的日程提醒。

        返回 ``None`` 表示当前用户没有这条日程提醒；``outcome=expired``
        表示提醒已经到期。检查与写入共用即时事务，避免提醒调度器在状态切换
        过程中同时领取同一行。
        """

        require_canonical_utc_timestamp(remind_time, "remind_time")
        normalized_owner = str(owner_id).strip()
        if not normalized_owner:
            raise ValueError("owner_id is required")

        with self.transaction(immediate=True) as conn:
            item_row = conn.execute(
                """
                SELECT remind_times
                FROM items
                WHERE id = ? AND owner_id = ? AND type = 'event' AND deleted = 0
                """,
                (item_id, normalized_owner),
            ).fetchone()
            if item_row is None:
                return None

            try:
                raw_times: object = json.loads(str(item_row["remind_times"] or "[]"))
            except (json.JSONDecodeError, TypeError, ValueError):
                return None
            if not isinstance(raw_times, list) or remind_time not in {
                value for value in raw_times if isinstance(value, str)
            }:
                return None

            current = self._as_utc(
                now or datetime.now(timezone.utc),
                "future reminder confirmation time",
            )
            target = datetime.fromisoformat(remind_time).astimezone(timezone.utc)
            if target <= current:
                return {"outcome": "expired", "time": remind_time}

            now_text = current.isoformat(timespec="seconds")
            # 正常写入流程会物化提醒行；导入或异常恢复留下的合法日程也通过
            # 同一条幂等写入进入提醒状态机，无需启动时扫描业务数据。
            conn.execute(
                """
                INSERT INTO reminder_logs
                    (item_id, remind_time, fire_at_utc, state, repeat_count, failure_count)
                VALUES (?, ?, ?, 'pending', 0, 0)
                ON CONFLICT(item_id, remind_time) DO UPDATE SET
                    fire_at_utc = COALESCE(reminder_logs.fire_at_utc, excluded.fire_at_utc)
                """,
                (item_id, remind_time, remind_time),
            )

            if confirmed:
                conn.execute(
                    """
                    UPDATE reminder_logs
                    SET confirmed_at = COALESCE(confirmed_at, ?),
                        user_action = CASE
                            WHEN confirmed_at IS NULL THEN 'preconfirmed'
                            ELSE user_action
                        END,
                        state = 'confirmed', claim_token = NULL,
                        claim_expires_at = NULL, next_attempt_at = NULL
                    WHERE item_id = ? AND remind_time = ?
                    """,
                    (now_text, item_id, remind_time),
                )
            else:
                # 未来提醒尚未发生，重新开启时恢复为一条全新的待发送行。
                conn.execute(
                    """
                    UPDATE reminder_logs
                    SET sent_at = NULL, confirmed_at = NULL, user_action = NULL,
                        repeat_count = 0, last_sent_at = NULL, state = 'pending',
                        claim_token = NULL, claim_expires_at = NULL,
                        next_attempt_at = NULL, failure_count = 0
                    WHERE item_id = ? AND remind_time = ?
                    """,
                    (item_id, remind_time),
                )

            updated = conn.execute(
                """
                SELECT remind_time, sent_at, confirmed_at, repeat_count
                FROM reminder_logs
                WHERE item_id = ? AND remind_time = ?
                """,
                (item_id, remind_time),
            ).fetchone()

        if updated is None:
            return None
        return {
            "outcome": "updated",
            "time": str(updated["remind_time"]),
            "status": (
                "confirmed"
                if updated["confirmed_at"]
                else "sent"
                if updated["sent_at"]
                else "pending"
            ),
            "sent_at": updated["sent_at"],
            "confirmed_at": updated["confirmed_at"],
            "repeat_count": int(updated["repeat_count"] or 0),
        }

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
