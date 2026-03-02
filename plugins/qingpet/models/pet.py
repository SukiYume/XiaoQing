from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict
from enum import Enum

from ..utils.constants import PetStage, PetPersonality, PetStatus, MAX_STAT_VALUE


@dataclass
class Pet:
    id: int
    user_id: str
    group_id: int
    name: str
    stage: PetStage
    form: str = "普通"

    hunger: int = 100
    mood: int = 100
    clean: int = 100
    energy: int = 100
    health: int = 100

    age: int = 0
    experience: int = 0
    intimacy: int = 0

    personality: PetPersonality = PetPersonality.LIVELY
    favorite_food: Optional[str] = None

    status: PetStatus = PetStatus.NORMAL
    status_expire_time: Optional[datetime] = None

    # 装扮系统 (新增功能)
    dress_hat: Optional[str] = None
    dress_clothes: Optional[str] = None
    dress_accessory: Optional[str] = None
    dress_background: Optional[str] = None

    last_update: datetime = field(default_factory=datetime.now)
    last_feed: Optional[datetime] = None
    last_clean: Optional[datetime] = None
    last_play: Optional[datetime] = None
    last_train: Optional[datetime] = None
    last_explore: Optional[datetime] = None

    likes: int = 0

    created_at: datetime = field(default_factory=datetime.now)

    @property
    def care_score(self) -> float:
        """照顾评分，0.0 ~ 1.0"""
        avg_stats = (self.hunger + self.mood + self.clean + self.energy + self.health) / 5
        return avg_stats / MAX_STAT_VALUE

    def update_stat(self, stat: str, delta: int, max_val: int = MAX_STAT_VALUE, min_val: int = 0) -> int:
        current = getattr(self, stat, 0)
        new_value = min(max_val, max(min_val, current + delta))
        setattr(self, stat, new_value)
        return new_value

    def is_alive(self) -> bool:
        return self.status != PetStatus.DEAD

    def can_interact(self) -> bool:
        return self.is_alive() and self.status == PetStatus.NORMAL

    def is_traveling(self) -> bool:
        return self.status == PetStatus.TRAVELING

    def get_dress_slots(self) -> Dict[str, Optional[str]]:
        """获取所有装扮槽位"""
        return {
            "帽子": self.dress_hat,
            "衣服": self.dress_clothes,
            "饰品": self.dress_accessory,
            "背景": self.dress_background,
        }

    def get_dress_mood_bonus(self) -> int:
        """计算装扮带来的心情加成"""
        from ..utils.constants import DEFAULT_DRESS_ITEMS
        bonus = 0
        for slot_attr in ["dress_hat", "dress_clothes", "dress_accessory", "dress_background"]:
            item_id = getattr(self, slot_attr, None)
            if item_id and item_id in DEFAULT_DRESS_ITEMS:
                bonus += DEFAULT_DRESS_ITEMS[item_id].get("mood_bonus", 0)
        return bonus