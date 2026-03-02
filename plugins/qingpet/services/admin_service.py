import logging
from datetime import datetime, timedelta
from typing import Optional, List

from ..models import Pet, User, GroupConfig, OperationLog
from ..utils.constants import PetStage
from .database import Database

logger = logging.getLogger(__name__)


class AdminService:
    def __init__(self, db: Database):
        self.db = db

    def enable_plugin(self, group_id: int) -> bool:
        config = self.db.get_group_config(group_id)
        config.enabled = True
        return self.db.update_group_config(config)

    def disable_plugin(self, group_id: int) -> bool:
        config = self.db.get_group_config(group_id)
        config.enabled = False
        return self.db.update_group_config(config)

    def set_config(self, group_id: int, key: str, value: str) -> bool:
        config = self.db.get_group_config(group_id)

        try:
            if key == "economy_multiplier":
                val = float(value)
                if val < 0.1 or val > 10.0:
                    return False
                config.economy_multiplier = val
            elif key == "decay_multiplier":
                val = float(value)
                if val < 0.1 or val > 10.0:
                    return False
                config.decay_multiplier = val
            elif key == "trade_enabled":
                config.trade_enabled = value.lower() in ("true", "1", "yes")
            elif key == "natural_trigger_enabled":
                config.natural_trigger_enabled = value.lower() in ("true", "1", "yes")
            elif key == "activity_enabled":
                config.activity_enabled = value.lower() in ("true", "1", "yes")
            else:
                return False

            return self.db.update_group_config(config)
        except ValueError:
            return False

    def get_config(self, group_id: int) -> GroupConfig:
        return self.db.get_group_config(group_id)

    def reset_user_pet(self, user_id: str, group_id: int) -> bool:
        pet = self.db.get_pet(user_id, group_id)
        if not pet:
            return False

        pet.hunger = 100
        pet.mood = 100
        pet.clean = 100
        pet.energy = 100
        pet.health = 100
        pet.stage = PetStage.EGG
        pet.experience = 0
        pet.intimacy = 0
        pet.age = 0

        from ..utils.constants import PetStatus
        pet.status = PetStatus.NORMAL

        return self.db.update_pet(pet)

    def ban_user(self, user_id: str, group_id: int, days: int) -> bool:
        user = self.db.get_user(user_id, group_id)
        if not user:
            return False

        user.is_banned = True
        user.ban_until = datetime.now() + timedelta(days=days)

        self.log_admin_operation(group_id, "ADMIN", "BAN", f"封禁{days}天", user_id)

        return self.db.update_user(user)

    def unban_user(self, user_id: str, group_id: int) -> bool:
        user = self.db.get_user(user_id, group_id)
        if not user:
            return False

        user.is_banned = False
        user.ban_until = None

        self.log_admin_operation(group_id, "ADMIN", "UNBAN", "解封", user_id)

        return self.db.update_user(user)

    def get_logs(self, group_id: int, limit: int = 50) -> List[OperationLog]:
        return self.db.get_operation_logs(group_id, limit)

    def log_admin_operation(self, group_id: int, user_id: str, operation_type: str,
                            params: str = "", target_user_id: str = None) -> bool:
        """记录管理操作日志"""
        log = OperationLog(
            id=0,
            group_id=group_id,
            user_id=user_id,
            target_user_id=target_user_id,
            operation_type=operation_type,
            params=params,
            result="success"
        )
        return self.db.log_operation(log)