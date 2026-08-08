"""供 Pendo 各 Handler 复用的异步数据库操作与审计日志组合。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

from core.plugin_base import run_sync

from ..core.exceptions import (
    ItemAlreadyDeletedException,
    ItemNotFoundException,
    ItemVersionConflictException,
)
from ..models.item import Item, ItemType, get_item_type_value

if TYPE_CHECKING:
    from ..services.db import Database

logger = logging.getLogger(__name__)

# 插件进程只维护一个数据库实例；运行时上下文和 Handler 共用该所有权入口。
_db_singleton: Database | None = None


def set_database_singleton(db: Database | None) -> None:
    """设置全局共享数据库实例。"""
    global _db_singleton
    _db_singleton = db


def claim_database_singleton(db: Database) -> None:
    """Publish one lifecycle-owned database without overwriting another generation."""

    global _db_singleton
    if _db_singleton is not None and _db_singleton is not db:
        raise RuntimeError("Pendo database singleton is already owned")
    _db_singleton = db


def detach_database_singleton() -> Database | None:
    """Remove and return the published database without closing it."""

    global _db_singleton
    database, _db_singleton = _db_singleton, None
    return database


def resolve_database_path(context: Any) -> Path:
    """Resolve Pendo's only database path from Core's writable data boundary."""

    from ..config import PendoConfig

    raw_data_dir = getattr(context, "data_dir", None)
    if raw_data_dir is None:
        raise RuntimeError("Pendo requires context.data_dir")
    try:
        data_dir = Path(raw_data_dir)
    except TypeError as exc:
        raise RuntimeError("Pendo context.data_dir must be path-like") from exc
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return data_dir / PendoConfig.DB_FILENAME


def get_database(context: Any) -> Database:
    """返回进程内唯一数据库实例，首次访问时按插件数据目录创建。"""
    global _db_singleton
    from ..services.db import Database

    if _db_singleton is None:
        db_path = resolve_database_path(context)
        _db_singleton = Database(str(db_path))
    return _db_singleton


async def get_user_settings_bundle_map(
    user_ids: list[str], db: Database
) -> dict[str, dict[str, Any]]:
    """批量读取定时任务需要的原始设置和自定义设置。"""
    from .settings_utils import parse_custom_settings

    if not user_ids:
        return {}

    settings_map = await run_sync(db.get_user_settings_batch, user_ids)
    return {
        user_id: {
            "settings": settings,
            "custom_settings": parse_custom_settings(settings),
        }
        for user_id, settings in settings_map.items()
    }


class DbOpsMixin:
    """依赖 ``self.db``，统一桥接同步数据库调用、异常和审计日志。"""

    db: Database
    _ITEM_TYPE_LABELS: ClassVar[dict[str, str]] = {
        ItemType.EVENT.value: "日程",
        ItemType.TASK.value: "待办",
        ItemType.NOTE.value: "笔记",
        ItemType.DIARY.value: "日记",
        ItemType.LEDGER.value: "账目",
    }

    @staticmethod
    def _single_token_error(value: str, message: str) -> dict[str, str] | None:
        """命令参数混入额外令牌时返回统一错误。"""
        if value.strip() and len(value.split()) != 1:
            return {"status": "error", "message": message}
        return None

    @staticmethod
    def _snapshot_item_values(item: Item, update_keys: Iterable[str]) -> dict[str, Any]:
        """Snapshot old field values for undo support.

        Args:
            item: 当前条目对象
            update_keys: 需要快照的字段名集合

        Returns:
            {field: old_value} 字典，值已序列化为 JSON 安全类型
        """
        old_values: dict[str, Any] = {}
        for key in update_keys:
            if key in ("type", "updated_at"):
                continue
            old_val = getattr(item, key, None)
            if old_val is not None:
                old_values[key] = (
                    old_val
                    if isinstance(old_val, (str, int, float, bool, list, dict))
                    else str(old_val)
                )
            else:
                old_values[key] = None
        return old_values

    async def _db_get_item(self, item_id: str, owner_id: str | None = None) -> Item | None:
        """获取单个条目"""
        return await run_sync(self.db.get_item, item_id, owner_id)

    async def _db_update_item(
        self,
        item_id: str,
        updates: dict[str, Any] | Item,
        owner_id: str | None = None,
        *,
        expected_version: int,
        operation_log: dict[str, Any] | None = None,
        touch: bool = True,
    ) -> None:
        """更新条目"""
        updated = await run_sync(
            self.db.update_item,
            item_id,
            updates,
            owner_id,
            expected_version=expected_version,
            operation_log=operation_log,
            touch=touch,
        )
        if not updated:
            raise ItemVersionConflictException(item_id)

    async def _db_insert_item(
        self,
        item_data: dict[str, Any] | Item,
        custom_id: str | None = None,
        *,
        operation_log: dict[str, Any] | None = None,
    ) -> str:
        """插入条目"""
        return cast(
            str,
            await run_sync(
                self.db.insert_item,
                item_data,
                custom_id,
                operation_log=operation_log,
            ),
        )

    async def _db_delete_item(
        self,
        item_id: str,
        soft: bool = True,
        *,
        owner_id: str,
        operation_log: dict[str, Any] | None = None,
    ) -> Any:
        """删除条目"""
        return await run_sync(
            self.db.delete_item,
            item_id,
            soft,
            owner_id=owner_id,
            operation_log=operation_log,
        )

    async def _db_get_and_check(self, item_id: str, owner_id: str) -> Item:
        """获取并检查所有权和状态

        统一的错误处理：
        - 不存在：抛出 ItemNotFoundException
        - 已删除：抛出 ItemAlreadyDeletedException

        Args:
            item_id: 条目ID
            owner_id: 所有者ID

        Returns:
            Item dataclass实例

        Raises:
            ItemNotFoundException: 条目不存在
            ItemAlreadyDeletedException: 条目已被删除
        """
        item = await self._db_get_item(item_id, owner_id)

        if not item:
            raise ItemNotFoundException(item_id)

        if item.deleted:
            raise ItemAlreadyDeletedException(item_id)

        return item

    @classmethod
    def _item_type_label(cls, item_type: str) -> str:
        return cls._ITEM_TYPE_LABELS.get(item_type, "其他条目")

    @staticmethod
    def _build_view_hint_for_item(item: Any) -> str:
        item_id = getattr(item, "id", "")
        item_type = get_item_type_value(getattr(item, "type", None), default="item")

        if item_type == ItemType.DIARY.value:
            diary_date = getattr(item, "diary_date", None)
            if diary_date:
                return f"`/pendo diary view {item_id}` 或 `/pendo diary view {diary_date}`"
            return f"`/pendo diary view {item_id}`"

        commands = {
            ItemType.EVENT.value: f"`/pendo event view {item_id}`",
            ItemType.TASK.value: f"`/pendo todo view {item_id}`",
            ItemType.NOTE.value: f"`/pendo note view {item_id}`",
            ItemType.LEDGER.value: f"`/pendo ledger view {item_id}`",
        }
        return commands.get(item_type, f"`/pendo search {item_id}`")

    @classmethod
    def _build_wrong_type_message(
        cls, query_id: str, expected_label: str, item: Any
    ) -> dict[str, Any]:
        item_type = get_item_type_value(getattr(item, "type", None), default="item")
        type_label = cls._item_type_label(item_type)
        command = cls._build_view_hint_for_item(item)
        return {
            "status": "success",
            "message": f"💡 `{query_id}` 不是{expected_label}ID，它属于{type_label}\n\n请使用 {command}",
        }

    async def _db_get_typed_item_or_message(
        self,
        item_id: str,
        owner_id: str,
        expected_type: str,
        expected_label: str,
    ) -> tuple[Any | None, dict[str, Any] | None]:
        item = await self._db_get_and_check(item_id, owner_id)
        item_type = get_item_type_value(getattr(item, "type", None), default="item")
        if item_type != expected_type:
            return None, self._build_wrong_type_message(item_id, expected_label, item)
        return item, None

    async def _db_update_with_log(
        self,
        item_id: str,
        updates: dict[str, Any] | Item,
        owner_id: str,
        action: str,
        expected_version: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        """更新并记录日志（含编辑前快照，支持撤销）

        Args:
            item_id: 条目ID
            updates: 更新内容 (dict 或 Item对象)
            owner_id: 所有者ID
            action: 操作类型（如 'update_event', 'complete_task'）
            details: 额外的日志详情
        """
        # 处理 updates 可能是 Item 对象的情况
        if isinstance(updates, Item):
            log_updates = updates.to_dict()
        else:
            log_updates = dict(updates)

        # 编辑操作：保存修改前的旧值快照，用于 undo
        old_values = {}
        if action.startswith("edit_"):
            current_item = await self._db_get_item(item_id, owner_id)
            if current_item:
                old_values = self._snapshot_item_values(current_item, log_updates)

        log_details = dict(details or {})
        log_details["updates"] = log_updates
        if old_values:
            log_details["old_values"] = old_values

        item_type = get_item_type_value(log_updates.get("type"), default="unknown")
        await self._db_update_item(
            item_id,
            updates,
            owner_id,
            expected_version=expected_version,
            operation_log={
                "user_id": owner_id,
                "action": action,
                "item_type": item_type,
                "item_id": item_id,
                "details": log_details,
            },
        )

        logger.info(
            "Updated item %s with action %s",
            item_id,
            action,
            extra={"user_id": owner_id, "item_id": item_id, "action": action},
        )

    async def _db_soft_delete_with_log(
        self, item_id: str, owner_id: str, item_type: str = "unknown"
    ) -> None:
        """软删除并记录日志

        Args:
            item_id: 条目ID
            owner_id: 所有者ID
            item_type: 条目类型（用于日志）
        """
        await self._db_delete_item(
            item_id,
            soft=True,
            owner_id=owner_id,
            operation_log={
                "user_id": owner_id,
                "action": "delete",
                "item_type": item_type,
                "item_id": item_id,
                "details": {"soft_delete": True},
            },
        )

        logger.info(
            "Soft deleted item %s",
            item_id,
            extra={"user_id": owner_id, "item_id": item_id, "item_type": item_type},
        )

    async def _db_batch_soft_delete_with_log(
        self,
        item_ids: list[str],
        owner_id: str,
        item_type: str,
        action: str,
        details_factory: Callable[[str], dict[str, Any]] | None = None,
    ) -> int:
        """由数据库事务原子完成批量软删除、附属清理和逐条审计。"""
        return cast(
            int,
            await run_sync(
                self.db.batch_soft_delete,
                item_ids,
                owner_id,
                item_type=item_type,
                operation_action=action,
                details_factory=details_factory,
            ),
        )

    async def _db_create_with_log(
        self,
        item_data: dict[str, Any] | Item,
        owner_id: str,
        action: str = "create",
        custom_id: str | None = None,
    ) -> str:
        """创建条目并记录日志

        Args:
            item_data: 条目数据 (dict 或 Item对象)
            owner_id: 所有者ID
            action: 操作类型（默认 'create'）
            custom_id: 自定义ID（可选）

        Returns:
            创建的条目ID
        """
        # 处理 item_data 可能是 Item 对象的情况
        if isinstance(item_data, Item):
            log_data = item_data.to_dict()
        else:
            log_data = dict(item_data)

        item_type = get_item_type_value(log_data.get("type"), default="unknown")
        item_id = await self._db_insert_item(
            item_data,
            custom_id,
            operation_log={
                "user_id": owner_id,
                "action": action,
                "item_type": item_type,
                "details": {"item_data": log_data},
            },
        )

        logger.info(
            "Created item %s",
            item_id,
            extra={"user_id": owner_id, "item_id": item_id, "item_type": item_type},
        )

        return item_id
