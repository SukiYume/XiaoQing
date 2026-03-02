import logging
import random
from datetime import datetime
from typing import List, Tuple, Optional, Dict

from ..models import Pet, User
from ..utils.constants import (
    COOLDOWN_TIMES, DAILY_LIMITS, MINIGAME_CONFIG, PetPersonality, PET_SHOW_CONFIG,
    DEFAULT_ITEMS, PetStage, PetStatus # Added new constants
)
from ..utils.validators import validate_cooling, validate_sensitive_content
from .database import Database

logger = logging.getLogger(__name__)


class SocialService:
    def __init__(self, db: Database):
        self.db = db

    # ──────────────────── 互访 ────────────────────

    def visit_pet(self, visitor_user_id: str, target_user_id: str, group_id: int) -> Tuple[bool, str]:
        if visitor_user_id == target_user_id:
            return False, "不能访问自己的宠物"

        visitor = self.db.get_user(visitor_user_id, group_id)
        if not visitor:
            return False, "访客用户不存在"

        target_pet = self.db.get_pet(target_user_id, group_id)
        if not target_pet:
            return False, "目标宠物不存在"

        if not target_pet.can_interact():
            return False, "该宠物现在无法互动"

        # 使用 validate_cooling 替代硬编码冷却检查（Issue #3 & #16）
        cooled, remaining = validate_cooling(visitor.last_visit_time, COOLDOWN_TIMES["visit"])
        if not cooled:
            return False, f"访问冷却中，请等待{remaining}秒"

        if not visitor.can_do_action("visit", 1, DAILY_LIMITS["visit"]):
            return False, "今日访问次数已达上限"

        coins_gain = 5
        if visitor.can_earn_coins(coins_gain, DAILY_LIMITS["coins"]):
            visitor.coins += coins_gain
            visitor.today_coins_earned += coins_gain
        else:
            coins_gain = 0

        visitor.increment_action("visit")
        visitor.last_visit_time = datetime.now()

        target_user = self.db.get_user(target_user_id, group_id)
        if target_user and target_user.can_earn_coins(coins_gain, DAILY_LIMITS["coins"]):
            target_user.coins += coins_gain
            target_user.today_coins_earned += coins_gain
            self.db.update_user(target_user)

        target_pet.intimacy += 1
        self.db.update_pet(target_pet)

        success = self.db.update_user(visitor)

        # 更新任务进度
        self.db.update_task_progress(visitor_user_id, group_id, "visit")

        if success:
            return True, f"访问了{target_pet.name}，双方都获得了亲密度和{coins_gain}金币"
        return False, "访问失败"

    # ──────────────────── 送礼 ────────────────────

    def gift_item(self, from_user_id: str, to_user_id: str, group_id: int,
                  item_id: str, amount: int = 1) -> Tuple[bool, str]:
        if from_user_id == to_user_id:
            return False, "不能给自己送礼物"

        from_user = self.db.get_user(from_user_id, group_id)
        if not from_user:
            return False, "发送者用户不存在"

        to_user = self.db.get_user(to_user_id, group_id)
        if not to_user:
            return False, "接收者用户不存在"

        # 使用 validate_cooling（Issue #3）
        cooled, remaining = validate_cooling(from_user.last_gift_time, COOLDOWN_TIMES["gift"])
        if not cooled:
            return False, f"送礼冷却中，请等待{remaining}秒"

        if not from_user.can_do_action("gift", 1, DAILY_LIMITS["gift"]):
            return False, "今日送礼次数已达上限"

        from_inventory = self.db.get_or_create_inventory(from_user_id, group_id)
        if not from_inventory.has_item(item_id, amount):
            return False, "背包中没有足够的道具"

        from_inventory.remove_item(item_id, amount)
        to_inventory = self.db.get_or_create_inventory(to_user_id, group_id)
        to_inventory.add_item(item_id, amount)

        friendship_gain = 2
        from_user.friendship_points += friendship_gain
        to_user.friendship_points += friendship_gain
        from_user.last_gift_time = datetime.now()
        from_user.increment_action("gift")

        success = (self.db.update_inventory(from_inventory) and
                   self.db.update_inventory(to_inventory) and
                   self.db.update_user(from_user) and
                   self.db.update_user(to_user))

        if success:
            return True, f"礼物发送成功！双方各获得{friendship_gain}友情点"
        return False, "送礼失败"

    # ──────────────────── 查看他人宠物卡片（Issue #42）────────────────────

    def view_pet_card(self, viewer_user_id: str, target_user_id: str,
                      group_id: int) -> Tuple[bool, str]:
        """查看他人的宠物卡片"""
        target_pet = self.db.get_pet(target_user_id, group_id)
        if not target_pet:
            return False, "该用户没有宠物"

        target_user = self.db.get_user(target_user_id, group_id)
        if not target_user:
            return False, "用户不存在"

        from ..utils.formatters import format_pet_card
        card = format_pet_card(target_pet, target_user)
        return True, f"📋 {target_user_id} 的宠物卡片\n\n{card}"

    # ──────────────────── 点赞/摸摸（Issue #43）────────────────────

    def like_pet(self, user_id: str, target_user_id: str, group_id: int) -> Tuple[bool, str]:
        """CR Review Issue #4/#9: 添加每日每用户点赞次数限制"""
        if user_id == target_user_id:
            return False, "不能给自己点赞"

        target_pet = self.db.get_pet(target_user_id, group_id)
        if not target_pet:
            return False, "对方没有宠物"

        # CR Review Issue #4: 检查每日对同一目标的点赞次数
        like_limit = DAILY_LIMITS.get("like_per_target", 3)
        today_like_count = self.db.get_daily_like_count(user_id, target_user_id, group_id)
        if today_like_count >= like_limit:
            return False, f"今日对该宠物的点赞次数已达上限({like_limit}次)"

        success = self.db.like_pet(target_user_id, group_id)
        if success:
            target_pet.intimacy += 1
            self.db.update_pet(target_pet)
            # 记录点赞
            self.db.record_daily_like(user_id, target_user_id, group_id)
            return True, f"你摸了摸{target_pet.name}，它看起来很开心！👋"
        return False, "操作失败"

    # ──────────────────── 留言板（Issue #44）────────────────────

    def leave_message(self, from_user_id: str, to_user_id: str,
                      group_id: int, message: str) -> Tuple[bool, str]:
        """CR Review Issue #3/#8: 添加每日留言次数限制"""
        if from_user_id == to_user_id:
            return False, "不能给自己留言"

        # 敏感词检查
        group_config = self.db.get_group_config(group_id)
        ok, err = validate_sensitive_content(message, group_config.sensitive_words)
        if not ok:
            return False, err

        if len(message) > 200:
            return False, "留言内容不能超过200字"

        # CR Review Issue #3: 留言频率限制
        from_user = self.db.get_user(from_user_id, group_id)
        if from_user:
            msg_limit = DAILY_LIMITS.get("message", 10)
            if not from_user.can_do_action("message", 1, msg_limit):
                return False, f"今日留言次数已达上限({msg_limit}次)"

        target_pet = self.db.get_pet(to_user_id, group_id)
        if not target_pet:
            return False, "该用户没有宠物"

        success = self.db.add_message(group_id, from_user_id, to_user_id, message)
        if success:
            # 更新每日留言计数
            if from_user:
                from_user.increment_action("message")
                self.db.update_user(from_user)
            return True, f"已给{target_pet.name}留言：{message}"
        return False, "留言失败"

    def get_messages(self, user_id: str, group_id: int) -> Tuple[bool, str]:
        """查看我的宠物收到的留言"""
        messages = self.db.get_messages(user_id, group_id)
        if not messages:
            return True, "📝 暂无留言"

        text = "📝 **留言板**\n\n"
        for msg in messages:
            created = msg.get('created_at', '未知时间')
            if isinstance(created, str) and len(created) > 16:
                created = created[:16]
            text += f"• [{created}] 来自 {msg['from_user_id']}: {msg['message']}\n"
        return True, text

    # ──────────────────── 排行榜（修复 care_score 显示问题 Issue #15）────────────

    def get_ranking(self, group_id: int, ranking_type: str = "care_score",
                    limit: int = 10) -> List[Tuple[str, str, float]]:
        pets = self.db.get_all_pets_in_group(group_id)

        if ranking_type == "care_score":
            pets.sort(key=lambda p: p.care_score, reverse=True)
            # 返回百分制而非 int(0~1)（Issue #15）
            return [(pet.user_id, pet.name,
                     round(pet.care_score * 100, 1))
                    for pet in pets[:limit]]

        elif ranking_type == "intimacy":
            pets.sort(key=lambda p: p.intimacy, reverse=True)
            return [(pet.user_id, pet.name, pet.intimacy)
                    for pet in pets[:limit]]

        elif ranking_type == "experience":
            pets.sort(key=lambda p: p.experience, reverse=True)
            return [(pet.user_id, pet.name, pet.experience)
                    for pet in pets[:limit]]

        elif ranking_type == "coins":
            # CR Review: 使用优化的 JOIN 查询替代 N+1 循环
            rows = self.db.get_coins_ranking(group_id, limit)
            return [(r['user_id'], r['pet_name'], r['coins']) for r in rows]

        return []

    # ──────────────────── 小游戏（Issue #46）────────────────────

    def play_rock_paper_scissors(self, user_id: str, group_id: int,
                                  player_choice: str) -> Tuple[bool, str]:
        """猜拳小游戏"""
        choices = {"石头": "rock", "剪刀": "scissors", "布": "paper",
                   "rock": "rock", "scissors": "scissors", "paper": "paper"}

        normalized = choices.get(player_choice)
        if not normalized:
            return False, "请选择：石头、剪刀 或 布"

        pet = self.db.get_pet(user_id, group_id)
        if not pet:
            return False, "你还没有宠物"

        if not pet.can_interact():
            return False, "宠物现在无法互动"

        npc_choice = random.choice(["rock", "scissors", "paper"])
        cn = {"rock": "石头", "scissors": "剪刀", "paper": "布"}

        config = MINIGAME_CONFIG["rock_paper_scissors"]

        if normalized == npc_choice:
            result = "平局"
            coins = config["draw_coins"]
            exp = 0
        elif (normalized == "rock" and npc_choice == "scissors") or \
             (normalized == "scissors" and npc_choice == "paper") or \
             (normalized == "paper" and npc_choice == "rock"):
            result = "你赢了"
            coins = config["win_coins"]
            exp = config["win_exp"]
        else:
            result = "你输了"
            coins = config["lose_coins"]
            exp = 0

        pet.experience += exp
        user = self.db.get_user(user_id, group_id)
        if user and coins > 0 and user.can_earn_coins(coins, DAILY_LIMITS["coins"]):
            user.coins += coins
            user.today_coins_earned += coins
            self.db.update_user(user)
        self.db.update_pet(pet)

        msg = f"✊✌️✋ **猜拳**\n\n"
        msg += f"你出了：{cn[normalized]}\n"
        msg += f"{pet.name}出了：{cn[npc_choice]}\n\n"
        msg += f"**{result}！**"
        if coins > 0:
            msg += f" 获得{coins}金币"
        if exp > 0:
            msg += f" + {exp}经验"
        return True, msg

    def play_dice(self, user_id: str, group_id: int) -> Tuple[bool, str]:
        """骰子小游戏"""
        pet = self.db.get_pet(user_id, group_id)
        if not pet:
            return False, "你还没有宠物"

        if not pet.can_interact():
            return False, "宠物现在无法互动"

        config = MINIGAME_CONFIG["dice"]

        player_dice = random.randint(1, 6)
        pet_dice = random.randint(1, 6)

        if player_dice > pet_dice:
            result = "你赢了"
            coins = config["win_coins"]
            exp = config["win_exp"]
        elif player_dice == pet_dice:
            result = "平局"
            coins = 5
            exp = 0
        else:
            result = "你输了"
            coins = config["lose_coins"]
            exp = 0

        pet.experience += exp
        user = self.db.get_user(user_id, group_id)
        if user and coins > 0 and user.can_earn_coins(coins, DAILY_LIMITS["coins"]):
            user.coins += coins
            user.today_coins_earned += coins
            self.db.update_user(user)
        self.db.update_pet(pet)

        msg = f"🎲 **骰子**\n\n"
        msg += f"你掷出了：{player_dice}\n"
        msg += f"{pet.name}掷出了：{pet_dice}\n\n"
        msg += f"**{result}！**"
        if coins > 0:
            msg += f" 获得{coins}金币"
        if exp > 0:
            msg += f" + {exp}经验"
        return True, msg

    def race_pet(self, user_id: str, target_user_id: str, group_id: int) -> Tuple[bool, str]:
        """宠物赛跑"""
        if user_id == target_user_id:
            return False, "不能跟自己的宠物赛跑"

        pet = self.db.get_pet(user_id, group_id)
        if not pet or not pet.can_interact():
            return False, "你的宠物无法参赛"

        target_pet = self.db.get_pet(target_user_id, group_id)
        if not target_pet or not target_pet.can_interact():
            return False, "对方的宠物无法参赛"

        config = MINIGAME_CONFIG["race"]
        if pet.energy < config["energy_cost"]:
            return False, "你的宠物精力不足"

        pet.update_stat("energy", -config["energy_cost"], min_val=0)

        # 速度受精力和性格影响
        my_speed = random.randint(1, 100) + pet.energy // 5
        target_speed = random.randint(1, 100) + target_pet.energy // 5

        if pet.personality == PetPersonality.LIVELY:
            my_speed += 10
        if target_pet.personality == PetPersonality.LIVELY:
            target_speed += 10

        user = self.db.get_user(user_id, group_id)

        if my_speed > target_speed:
            coins = config["win_coins"]
            exp = config["win_exp"]
            result = f"🏆 {pet.name}赢了！"
        elif my_speed == target_speed:
            coins = config["second_coins"]
            exp = 3
            result = f"🤝 平局！"
        else:
            coins = config["lose_coins"]
            exp = 2
            result = f"😔 {target_pet.name}赢了！"

        pet.experience += exp
        if user and coins > 0 and user.can_earn_coins(coins, DAILY_LIMITS["coins"]):
            user.coins += coins
            user.today_coins_earned += coins
            self.db.update_user(user)
        self.db.update_pet(pet)

        msg = f"🏃 **宠物赛跑**\n\n"
        msg += f"{pet.name} 🆚 {target_pet.name}\n\n"
        msg += f"{result}"
        if coins > 0:
            msg += f" 获得{coins}金币"
        if exp > 0:
            msg += f" + {exp}经验"
        return True, msg

    # ──────────────────── 展示会结算（新增）────────────────────

    def settle_pet_show(self, group_id: int) -> str:
        """结算展示会，发放奖励"""
        show = self.db.get_active_pet_show(group_id)
        if not show:
            return ""

        votes = self.db.get_pet_show_votes(show['id'])
        if not votes:
            self.db.end_pet_show(show['id'])
            return "🏆 展示会已结束（无投票数据）"

        sorted_votes = sorted(votes.items(), key=lambda x: x[1], reverse=True)
        rewards = [
            PET_SHOW_CONFIG["reward_first"],
            PET_SHOW_CONFIG["reward_second"],
            PET_SHOW_CONFIG["reward_third"]
        ]
        medals = ["🥇", "🥈", "🥉"]

        text = f"🏆 **{show.get('title', '展示会')} 结果**\n\n"

        for i, (uid, vote_count) in enumerate(sorted_votes[:3]):
            pet = self.db.get_pet(uid, group_id)
            name = pet.name if pet else uid
            medal = medals[i] if i < len(medals) else f"#{i+1}"
            reward = rewards[i] if i < len(rewards) else 0

            text += f"{medal} {name} ({uid}) - {vote_count}票"
            if reward > 0:
                text += f" +{reward}金币"
                user = self.db.get_user(uid, group_id)
                if user:
                    user.coins += reward
                    self.db.update_user(user)
            text += "\n"

        self.db.end_pet_show(show['id'])
        text += "\n🎉 展示会已结束，感谢参与！"
        return text