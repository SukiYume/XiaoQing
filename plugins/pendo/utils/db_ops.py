"""
数据库通用操作封装
用于复用各 Handler 的基础 CRUD 操作
"""
import os
import logging
from typing import Any, Optional, TYPE_CHECKING

from core.plugin_base import run_sync
from ..core.exceptions import ItemNotFoundException, ItemAlreadyDeletedException

if TYPE_CHECKING:
    from ..services.db import Database

logger = logging.getLogger(__name__)

# 模块级单例缓存，用于 context 为 None 时的 fallback
_db_singleton: 'Database' = None

def get_database(context) -> 'Database':
    """获取数据库实例（带缓存）
    
    统一的数据库获取入口，避免在多处重复代码
    
    Args:
        context: 上下文对象
        
    Returns:
        Database实例
    """
    global _db_singleton
    from ..services.db import Database
    from ..config import PendoConfig
    
    # 如果context中有缓存，优先使用
    if hasattr(context, 'pendo_db'):
        return context.pendo_db
    
    # context 为 None 时使用单例
    if context is None:
        if _db_singleton is None:
            db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', PendoConfig.DB_FILENAME)
            _db_singleton = Database(db_path)
        return _db_singleton
    
    # 创建新的数据库实例并缓存到context
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', PendoConfig.DB_FILENAME)
    db = Database(db_path)
    
    # 尝试缓存到context（如果可能）
    try:
        if not hasattr(context, 'pendo_db'):
            context.pendo_db = db
    except Exception:
        pass  # 忽略缓存失败
    
    return db

def cleanup_db_singleton():
    """清理数据库单例"""
    global _db_singleton
    if _db_singleton:
        _db_singleton.cleanup()
        _db_singleton = None

class DbOpsMixin:
    """数据库操作混入类

    依赖: self.db
    提供统一的数据库操作接口，包括错误处理和日志记录
    """

    # ============================================================
    # 基础操作 - 直接封装数据库调用
    # ============================================================

    async def _db_get_item(self, item_id: str, owner_id: str = None) -> Optional[dict[str, Any]]:
        """获取单个条目"""
        return await run_sync(self.db.items.get_item, item_id, owner_id)

    async def _db_update_item(self, item_id: str, updates: dict[str, Any], owner_id: str = None):
        """更新条目"""
        return await run_sync(self.db.items.update_item, item_id, updates, owner_id)

    async def _db_insert_item(self, item_data: dict[str, Any], custom_id: str = None) -> str:
        """插入条目"""
        if custom_id:
            return await run_sync(self.db.items.insert_item, item_data, custom_id)
        return await run_sync(self.db.items.insert_item, item_data)

    async def _db_delete_item(self, item_id: str, soft: bool = True, owner_id: str = None):
        """删除条目"""
        return await run_sync(self.db.items.delete_item, item_id, soft, owner_id)

    async def _db_log_operation(self, **kwargs):
        """记录操作日志"""
        return await run_sync(self.db.logs.log_operation, **kwargs)

    # ============================================================
    # 组合操作 - 常用操作的封装
    # ============================================================

    async def _db_get_and_check(self, item_id: str, owner_id: str) -> Any:
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

    async def _db_update_with_log(self, item_id: str, updates: dict[str, Any] | Any, 
                                   owner_id: str, action: str, details: Optional[dict] = None):
        """更新并记录日志（含编辑前快照，支持撤销）
        
        Args:
            item_id: 条目ID
            updates: 更新内容 (dict 或 Item对象)
            owner_id: 所有者ID
            action: 操作类型（如 'update_event', 'complete_task'）
            details: 额外的日志详情
        """
        # 处理 updates 可能是 Item 对象的情况
        if hasattr(updates, 'to_dict'):
            log_updates = updates.to_dict()
        else:
            log_updates = updates

        # 编辑操作：保存修改前的旧值快照，用于 undo
        old_values = {}
        if action.startswith('edit_'):
            current_item = await self._db_get_item(item_id, owner_id)
            if current_item:
                for key in log_updates:
                    if key in ('type', 'updated_at'):
                        continue
                    old_val = getattr(current_item, key, None)
                    # 将旧值序列化为可 JSON 存储的格式
                    if old_val is not None:
                        old_values[key] = old_val if isinstance(old_val, (str, int, float, bool, list, dict)) else str(old_val)
                    else:
                        old_values[key] = None

        # 更新条目
        await self._db_update_item(item_id, updates, owner_id)
        
        # 记录日志
        log_details = details or {}
        log_details['updates'] = log_updates
        if old_values:
            log_details['old_values'] = old_values
        
        item_type = log_updates.get('type', 'unknown')

        await self._db_log_operation(
            user_id=owner_id,
            action=action,
            item_type=item_type,
            item_id=item_id,
            details=log_details
        )

        logger.info("Updated item %s with action %s", item_id, action, extra={
            'user_id': owner_id,
            'item_id': item_id,
            'action': action
        })

    async def _db_soft_delete_with_log(self, item_id: str, owner_id: str, 
                                        item_type: str = 'unknown'):
        """软删除并记录日志
        
        Args:
            item_id: 条目ID
            owner_id: 所有者ID
            item_type: 条目类型（用于日志）
        """
        # 执行软删除
        await self._db_delete_item(item_id, soft=True, owner_id=owner_id)
        
        # 记录日志
        await self._db_log_operation(
            user_id=owner_id,
            action='delete',
            item_type=item_type,
            item_id=item_id,
            details={'soft_delete': True}
        )

        logger.info("Soft deleted item %s", item_id, extra={
            'user_id': owner_id,
            'item_id': item_id,
            'item_type': item_type
        })

    async def _db_create_with_log(self, item_data: dict[str, Any] | Any, 
                                   owner_id: str, action: str = 'create',
                                   custom_id: str = None) -> str:
        """创建条目并记录日志
        
        Args:
            item_data: 条目数据 (dict 或 Item对象)
            owner_id: 所有者ID
            action: 操作类型（默认 'create'）
            custom_id: 自定义ID（可选）
            
        Returns:
            创建的条目ID
        """
        # 插入条目
        item_id = await self._db_insert_item(item_data, custom_id)
        
        # 处理 item_data 可能是 Item 对象的情况
        if hasattr(item_data, 'to_dict'):
            log_data = item_data.to_dict()
        else:
            log_data = item_data
        
        # 记录日志
        item_type = log_data.get('type', 'unknown')

        await self._db_log_operation(
            user_id=owner_id,
            action=action,
            item_type=item_type,
            item_id=item_id,
            details={'item_data': log_data}
        )

        logger.info("Created item %s", item_id, extra={
            'user_id': owner_id,
            'item_id': item_id,
            'item_type': item_type
        })
        
        return item_id

