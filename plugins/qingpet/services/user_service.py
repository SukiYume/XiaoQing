import logging
from typing import Optional, Tuple, List

from ..models import User, Pet
from ..utils.constants import TITLES, PetStage
from .database import Database

logger = logging.getLogger(__name__)


class UserService:
    def __init__(self, db: Database):
        self.db = db

    def get_or_create_user(self, user_id: str, group_id: int) -> User:
        user = self.db.get_user(user_id, group_id)
        if user is None:
            user = User(user_id=user_id, group_id=group_id)
            self.db.create_user(user)
        return user

    def update_user(self, user: User) -> bool:
        return self.db.update_user(user)

    def reset_daily(self, group_id: int) -> int:
        """批量重置每日计数（Issue #13: 改为单条 SQL，消除 N+1 问题）"""
        return self.db.batch_daily_reset(group_id)

    def reset_daily_all(self) -> int:
        """批量重置所有群的每日计数"""
        return self.db.batch_daily_reset_all()

    # ──────────────────── 称号系统（Issue #48）────────────────────

    def check_and_award_titles(self, user_id: str, group_id: int) -> List[str]:
        """检查并颁发称号，返回新获得的称号列表"""
        user = self.db.get_user(user_id, group_id)
        if not user:
            return []

        pet = self.db.get_pet(user_id, group_id)
        new_titles = []

        # 检查各称号条件
        if not user.has_title("新手铲屎官") and pet is not None:
            if user.add_title("新手铲屎官"):
                new_titles.append("新手铲屎官")

        if not user.has_title("勤劳养育员") and user.total_feed_count >= 100:
            if user.add_title("勤劳养育员"):
                new_titles.append("勤劳养育员")

        if not user.has_title("亲密伙伴") and pet and pet.intimacy >= 100:
            if user.add_title("亲密伙伴"):
                new_titles.append("亲密伙伴")

        if not user.has_title("探索先锋") and user.total_explore_count >= 50:
            if user.add_title("探索先锋"):
                new_titles.append("探索先锋")

        if not user.has_title("社交达人") and user.total_visit_count >= 50:
            if user.add_title("社交达人"):
                new_titles.append("社交达人")

        if not user.has_title("慷慨之友") and user.total_gift_count >= 30:
            if user.add_title("慷慨之友"):
                new_titles.append("慷慨之友")

        if not user.has_title("宠物大师") and pet and pet.stage == PetStage.MATURE and pet.care_score >= 0.9:
            if user.add_title("宠物大师"):
                new_titles.append("宠物大师")

        if not user.has_title("百万富翁") and user.coins >= 10000:
            if user.add_title("百万富翁"):
                new_titles.append("百万富翁")

        if new_titles:
            self.db.update_user(user)

        return new_titles

    def get_user_titles(self, user_id: str, group_id: int) -> List[str]:
        """获取用户称号列表"""
        user = self.db.get_user(user_id, group_id)
        if not user:
            return []
        return user.titles

    def format_titles(self, user_id: str, group_id: int) -> str:
        """格式化显示称号"""
        titles = self.get_user_titles(user_id, group_id)
        if not titles:
            return "🏅 暂无称号\n\n继续努力，解锁更多称号吧！"

        text = "🏅 **我的称号**\n\n"
        for title in titles:
            desc = TITLES.get(title, {}).get("description", "")
            text += f"• {title}"
            if desc:
                text += f" — {desc}"
            text += "\n"
        return text