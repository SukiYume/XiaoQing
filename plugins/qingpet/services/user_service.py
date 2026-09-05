"""用户创建、称号授予与称号展示服务。"""

from ..models import User
from ..utils.constants import TITLES, PetStage
from .database import Database


class UserService:
    """协调用户持久化，并根据当前领域状态授予永久称号。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get_or_create_user(self, user_id: str, group_id: int) -> User:
        """读取群内用户；不存在时创建带默认资产的新用户。"""
        user = self.db.get_user(user_id, group_id)
        if user is None:
            user = User(user_id=user_id, group_id=group_id)
            self.db.create_user(user)
        return user

    def check_and_award_titles(self, user_id: str, group_id: int) -> list[str]:
        """检查全部永久称号规则，持久化并返回本次新获得的称号。"""
        user = self.db.get_user(user_id, group_id)
        if user is None:
            return []

        pet             = self.db.get_pet(user_id, group_id)
        eligible_titles = (
            ("新手铲屎官", pet is not None),
            ("勤劳养育员", user.total_feed_count >= 100),
            ("亲密伙伴", pet is not None and pet.intimacy >= 100),
            ("探索先锋", user.total_explore_count >= 50),
            ("社交达人", user.total_visit_count >= 50),
            ("慷慨之友", user.total_gift_count >= 30),
            (
                "宠物大师",
                pet is not None and pet.stage == PetStage.MATURE and pet.care_score >= 0.9,
            ),
            ("百万富翁", user.coins >= 10000),
        )

        new_titles = [
            title for title, eligible in eligible_titles if eligible and user.add_title(title)
        ]
        if new_titles and not self.db.update_user(user):
            return []
        return new_titles

    def get_user_titles(self, user_id: str, group_id: int) -> list[str]:
        """返回用户称号副本，避免调用方修改持久化模型。"""
        user = self.db.get_user(user_id, group_id)
        return [] if user is None else list(user.titles)

    def format_titles(self, user_id: str, group_id: int) -> str:
        """生成用户称号及其说明文本。"""
        titles = self.get_user_titles(user_id, group_id)
        if not titles:
            return "🏅 暂无称号\n\n继续努力，解锁更多称号吧！"

        lines = ["🏅 **我的称号**", ""]
        for title in titles:
            title_config = TITLES.get(title)
            description  = title_config["description"] if title_config is not None else ""
            suffix       = f" — {description}" if description else ""
            lines.append(f"• {title}{suffix}")
        return "\n".join(lines) + "\n"
