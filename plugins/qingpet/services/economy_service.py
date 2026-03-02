import logging
from typing import Dict, List

from ..models import Pet, User, GroupConfig
from .database import Database

logger = logging.getLogger(__name__)


class EconomyService:
    def __init__(self, db: Database):
        self.db = db

    def get_group_stats(self, group_id: int) -> Dict:
        """获取群统计数据（实现 Issue #55: 数据统计 日活/留存/通胀指标）"""
        pets = self.db.get_all_pets_in_group(group_id)

        total_coins = 0
        total_experience = 0
        total_intimacy = 0
        avg_care_score = 0
        active_today = 0

        if pets:
            for pet in pets:
                user = self.db.get_user(pet.user_id, group_id)
                if user:
                    total_coins += user.coins
                    # 今日有操作的视为活跃
                    if (user.today_feed_count + user.today_clean_count +
                        user.today_play_count + user.today_train_count +
                            user.today_explore_count) > 0:
                        active_today += 1
                total_experience += pet.experience
                total_intimacy += pet.intimacy
                avg_care_score += pet.care_score

            avg_care_score /= len(pets)

        return {
            "total_pets": len(pets),
            "total_coins": total_coins,
            "total_experience": total_experience,
            "total_intimacy": total_intimacy,
            "avg_care_score": round(avg_care_score * 100, 1),
            "active_today": active_today,
            "coins_per_pet": round(total_coins / len(pets), 1) if pets else 0,
        }

    def apply_economy_multiplier(self, amount: int, multiplier: float) -> int:
        return int(amount * multiplier)

    def grant_daily_reward(self, user: User, group_config: GroupConfig) -> int:
        base_reward = 20
        reward = self.apply_economy_multiplier(base_reward, group_config.economy_multiplier)

        if user.can_earn_coins(reward, 500):
            user.coins += reward
            user.today_coins_earned += reward
            return reward
        return 0

    def format_stats(self, group_id: int) -> str:
        """格式化群统计数据"""
        stats = self.get_group_stats(group_id)
        text = "📊 **群宠物统计**\n\n"
        text += f"🐾 宠物总数: {stats['total_pets']}\n"
        text += f"👥 今日活跃: {stats['active_today']}\n"
        text += f"💰 金币总量: {stats['total_coins']}\n"
        text += f"💰 人均金币: {stats['coins_per_pet']}\n"
        text += f"🎯 经验总量: {stats['total_experience']}\n"
        text += f"💕 亲密总量: {stats['total_intimacy']}\n"
        text += f"🌟 平均照顾评分: {stats['avg_care_score']}%\n"
        return text