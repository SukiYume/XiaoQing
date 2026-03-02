from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, List


@dataclass
class User:
    user_id: str
    group_id: int
    
    coins: int = 100
    friendship_points: int = 0
    
    # 每日计数
    today_coins_earned: int = 0
    today_feed_count: int = 0
    today_clean_count: int = 0
    today_play_count: int = 0
    today_train_count: int = 0
    today_explore_count: int = 0
    today_visit_count: int = 0
    today_gift_count: int = 0
    today_free_feed_count: int = 0
    today_message_count: int = 0
    
    # 累计计数（用于称号系统）
    total_feed_count: int = 0
    total_clean_count: int = 0
    total_play_count: int = 0
    total_train_count: int = 0
    total_explore_count: int = 0
    total_visit_count: int = 0
    total_gift_count: int = 0
    total_free_feed_count: int = 0
    total_message_count: int = 0
    
    # 称号列表
    titles: List[str] = field(default_factory=list)
    
    last_visit_time: Optional[datetime] = None
    last_gift_time: Optional[datetime] = None
    
    trustee_until: Optional[datetime] = None
    
    is_banned: bool = False
    ban_until: Optional[datetime] = None
    
    created_at: datetime = field(default_factory=datetime.now)
    last_active: datetime = field(default_factory=datetime.now)
    
    def can_earn_coins(self, amount: int, daily_limit: int) -> bool:
        return self.today_coins_earned + amount <= daily_limit
    
    def can_do_action(self, action: str, count: int, daily_limit: int) -> bool:
        current_count = getattr(self, f"today_{action}_count", 0)
        return current_count + count <= daily_limit
    
    def increment_action(self, action: str, count: int = 1) -> None:
        # 增加每日计数
        today_attr = f"today_{action}_count"
        current = getattr(self, today_attr, 0)
        setattr(self, today_attr, current + count)
        # 增加累计计数
        total_attr = f"total_{action}_count"
        total_current = getattr(self, total_attr, 0)
        setattr(self, total_attr, total_current + count)
    
    def is_trustee_active(self) -> bool:
        if self.trustee_until is None:
            return False
        return datetime.now() < self.trustee_until
    
    def is_banned_active(self) -> bool:
        if not self.is_banned:
            return False
        if self.ban_until is None:
            return True
        if datetime.now() >= self.ban_until:
            # 自动解除过期封禁
            self.is_banned = False
            self.ban_until = None
            return False
        return True
    
    def reset_daily(self) -> None:
        self.today_coins_earned = 0
        self.today_feed_count = 0
        self.today_clean_count = 0
        self.today_play_count = 0
        self.today_train_count = 0
        self.today_explore_count = 0
        self.today_visit_count = 0
        self.today_gift_count = 0
        self.today_free_feed_count = 0
        self.today_message_count = 0
    
    def has_title(self, title: str) -> bool:
        return title in self.titles
    
    def add_title(self, title: str) -> bool:
        """添加称号，返回是否为新获得"""
        if title not in self.titles:
            self.titles.append(title)
            return True
        return False