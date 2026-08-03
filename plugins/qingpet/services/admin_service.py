"""群级配置、用户管理和管理员审计服务。"""

from ..models import GroupConfig, OperationLog
from .database import Database

_MULTIPLIER_KEYS = frozenset({"economy_multiplier", "decay_multiplier"})
_BOOLEAN_KEYS = frozenset({"trade_enabled", "natural_trigger_enabled", "activity_enabled"})
_TRUE_VALUES = frozenset({"true", "1", "yes"})
_FALSE_VALUES = frozenset({"false", "0", "no"})


class AdminService:
    """封装管理员可执行的配置与原子数据操作。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def enable_plugin(self, group_id: int) -> bool:
        """启用指定群的宠物系统。"""
        config = self.db.get_group_config(group_id)
        config.enabled = True
        return self.db.update_group_config(config)

    def disable_plugin(self, group_id: int) -> bool:
        """停用指定群的宠物系统。"""
        config = self.db.get_group_config(group_id)
        config.enabled = False
        return self.db.update_group_config(config)

    def set_config(self, group_id: int, key: str, value: str) -> bool:
        """校验并更新一个允许公开配置的群级字段。"""
        config = self.db.get_group_config(group_id)

        if key in _MULTIPLIER_KEYS:
            try:
                multiplier = float(value)
            except ValueError:
                return False
            if not 0.1 <= multiplier <= 10.0:
                return False
            setattr(config, key, multiplier)
        elif key in _BOOLEAN_KEYS:
            normalized_value = value.strip().casefold()
            if normalized_value in _TRUE_VALUES:
                enabled = True
            elif normalized_value in _FALSE_VALUES:
                enabled = False
            else:
                return False
            setattr(config, key, enabled)
        else:
            return False

        return self.db.update_group_config(config)

    def get_config(self, group_id: int) -> GroupConfig:
        """返回指定群的当前配置。"""
        return self.db.get_group_config(group_id)

    def reset_user_pet(
        self,
        user_id: str,
        group_id: int,
        operator_user_id: str = "ADMIN",
    ) -> bool:
        """原子重置用户宠物及相关冷却，并写入管理员审计日志。"""
        return self.db.admin_reset_user_pet_atomic(user_id, group_id, operator_user_id)

    def ban_user(
        self, user_id: str, group_id: int, days: int, operator_user_id: str = "ADMIN"
    ) -> bool:
        """按天封禁用户，并在同一事务写入审计日志。"""
        return self.db.admin_set_ban_atomic(
            user_id,
            group_id,
            operator_user_id,
            days=days,
        )

    def unban_user(self, user_id: str, group_id: int, operator_user_id: str = "ADMIN") -> bool:
        """解除用户封禁，并在同一事务写入审计日志。"""
        return self.db.admin_set_ban_atomic(
            user_id,
            group_id,
            operator_user_id,
            days=None,
        )

    def delete_user_pet(
        self,
        user_id: str,
        group_id: int,
        operator_user_id: str = "ADMIN",
    ) -> bool:
        """原子删除用户宠物及其关联数据。"""
        return self.db.admin_delete_pet_atomic(user_id, group_id, operator_user_id)

    def get_logs(self, group_id: int, limit: int = 50) -> list[OperationLog]:
        """按时间倒序读取指定群的管理员操作日志。"""
        return self.db.get_operation_logs(group_id, limit)

    def log_admin_operation(
        self,
        group_id: int,
        user_id: str,
        operation_type: str,
        params: str = "",
        target_user_id: str | None = None,
    ) -> bool:
        """记录一条非事务型管理操作日志。"""
        log = OperationLog(
            id=0,
            group_id=group_id,
            user_id=user_id,
            target_user_id=target_user_id,
            operation_type=operation_type,
            params=params,
            result="success",
        )
        return self.db.log_operation(log)
