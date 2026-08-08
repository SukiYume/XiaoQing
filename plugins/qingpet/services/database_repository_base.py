"""Qingpet 数据库仓储 mixin 共用的最小连接契约。"""

import sqlite3
from datetime import datetime

from ..models import Pet, User
from .database_types import PetShowSettlementResult


class DatabaseRepositorySupport:
    """声明最终 Database 必须提供的跨仓储能力。

    这些方法均由排在本类之前的具体 mixin 实现；此处只为静态类型检查描述依赖，
    不参与正常运行时分发。若组合顺序错误，显式异常会在首次调用时暴露问题。
    """

    # ──────────────────── 连接与资产账本 ────────────────────

    def _get_connection(self) -> sqlite3.Connection:
        raise NotImplementedError

    def _record_asset_delta(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: str,
        group_id: int,
        asset_type: str,
        delta: int,
        reason: str,
        reference_id: str | None = None,
    ) -> None:
        raise NotImplementedError

    def _load_inventory_items(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        group_id: int,
    ) -> dict[str, int]:
        raise NotImplementedError

    def _save_inventory_items(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        group_id: int,
        items: dict[str, int],
    ) -> None:
        raise NotImplementedError

    def _credit_coins_in_transaction(
        self,
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
        raise NotImplementedError

    # ──────────────────── 交易与展示会结算 ────────────────────

    def settle_expired_trade_listings(self, group_id: int | None = None) -> int:
        raise NotImplementedError

    def _expire_trade_listing_in_transaction(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        now: str,
    ) -> bool:
        raise NotImplementedError

    def settle_pet_show_atomic(
        self,
        group_id: int,
        *,
        force: bool = False,
    ) -> PetShowSettlementResult | None:
        raise NotImplementedError

    # ──────────────────── 领域对象持久化 ────────────────────

    def _row_to_pet(self, row: sqlite3.Row) -> Pet:
        raise NotImplementedError

    def _row_to_user(self, row: sqlite3.Row) -> User:
        raise NotImplementedError

    def _write_pet_in_transaction(self, conn: sqlite3.Connection, pet: Pet) -> bool:
        raise NotImplementedError

    def _write_user_in_transaction(self, conn: sqlite3.Connection, user: User) -> bool:
        raise NotImplementedError

    # ──────────────────── 任务、活动与称号 ────────────────────

    def _ensure_group_task_templates(
        self,
        conn: sqlite3.Connection,
        group_id: int,
        created_date: str,
    ) -> None:
        raise NotImplementedError

    def _ensure_daily_task_templates(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        group_id: int,
        *,
        today: str,
        now_iso: str,
    ) -> None:
        raise NotImplementedError

    def _advance_activities_in_transaction(
        self,
        conn: sqlite3.Connection,
        group_id: int,
        activity_type: str,
        increment: int = 1,
    ) -> int:
        raise NotImplementedError

    def _grant_temporary_title_in_transaction(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        group_id: int,
        title: str,
        *,
        now: datetime,
    ) -> bool:
        raise NotImplementedError
