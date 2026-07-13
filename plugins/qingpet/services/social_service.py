import hashlib
import logging
import random
import secrets

from ..models import Pet
from ..utils.constants import (
    COOLDOWN_TIMES,
    DAILY_LIMITS,
    MINIGAME_CONFIG,
    PET_SHOW_CONFIG,
    PetPersonality,
)
from ..utils.validators import validate_sensitive_content
from .database import Database, MinigameOutcome
from .user_service import UserService

logger = logging.getLogger(__name__)


class SocialService:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _minigame_reference(
        game_type: str,
        user_id: str,
        group_id: int,
        *,
        opponent_user_id: str | None = None,
        message_id: str | None = None,
    ) -> str:
        request_token = str(message_id or secrets.token_hex(16))
        material = (
            f"{request_token}\0{game_type}\0{group_id}\0{user_id}\0"
            f"{opponent_user_id or ''}"
        )
        return f"pet-minigame:v1:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _reward_suffix(result, *, offered_coins: bool) -> str:
        suffix = ""
        if offered_coins:
            suffix += f" 获得{result.coin_grant}金币"
        if result.experience_grant > 0:
            suffix += f" + {result.experience_grant}经验"
        if result.energy_cost > 0:
            suffix += f" 消耗{result.energy_cost}精力"
        return suffix

    # ──────────────────── 互访 ────────────────────

    def visit_pet(
        self,
        visitor_user_id: str,
        target_user_id: str,
        group_id: int,
        *,
        message_id: str | None = None,
    ) -> tuple[bool, str]:
        request_token = str(message_id or secrets.token_hex(16))
        material = f"{request_token}\0{group_id}\0{visitor_user_id}\0{target_user_id}"
        reference_id = f"pet-visit:v1:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"
        result = self.db.visit_pet_atomic(
            visitor_user_id,
            target_user_id,
            group_id,
            coin_reward=5,
            daily_visit_limit=DAILY_LIMITS["visit"],
            daily_coin_limit=DAILY_LIMITS["coins"],
            cooldown_seconds=COOLDOWN_TIMES["visit"],
            reference_id=reference_id,
        )
        if not result.success:
            return False, result.reason or "访问失败"

        if result.visitor_grant == result.target_grant:
            reward_text = f"双方各获得{result.visitor_grant}金币"
        else:
            reward_text = (
                f"访客获得{result.visitor_grant}金币，"
                f"宠物主人获得{result.target_grant}金币"
            )
        return (
            True,
            f"访问了{result.pet_name}，宠物亲密度+{result.intimacy_grant}；{reward_text}",
        )

    # ──────────────────── 送礼 ────────────────────

    def gift_item(self, from_user_id: str, to_user_id: str, group_id: int,
                  item_id: str, amount: int = 1) -> tuple[bool, str]:
        if from_user_id == to_user_id:
            return False, "不能给自己送礼物"

        friendship_gain = 2
        success, reason = self.db.gift_item_atomic(
            from_user_id,
            to_user_id,
            group_id,
            item_id,
            amount,
            friendship_gain,
            DAILY_LIMITS["gift"],
            COOLDOWN_TIMES["gift"],
        )

        if success:
            return True, f"礼物发送成功！双方各获得{friendship_gain}友情点"
        return False, reason or "送礼失败"

    # ──────────────────── 查看他人宠物卡片（Issue #42）────────────────────

    def view_pet_card(self, viewer_user_id: str, target_user_id: str,
                      group_id: int) -> tuple[bool, str]:
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

    def like_pet(self, user_id: str, target_user_id: str, group_id: int) -> tuple[bool, str]:
        """CR Review Issue #4/#9: 添加每日每用户点赞次数限制"""
        if user_id == target_user_id:
            return False, "不能给自己点赞"

        target_pet = self.db.get_pet(target_user_id, group_id)
        if not target_pet:
            return False, "对方没有宠物"

        like_limit = DAILY_LIMITS.get("like_per_target", 3)
        success = self.db.like_pet_atomic(user_id, target_user_id, group_id, like_limit)
        if success:
            return True, f"你摸了摸{target_pet.name}，它看起来很开心！👋"
        return False, f"今日对该宠物的点赞次数已达上限({like_limit}次)或操作失败"

    # ──────────────────── 留言板（Issue #44）────────────────────

    def leave_message(self, from_user_id: str, to_user_id: str,
                      group_id: int, message: str) -> tuple[bool, str]:
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

        result = self.db.leave_message_atomic(
            from_user_id,
            to_user_id,
            group_id,
            message,
            DAILY_LIMITS.get("message", 10),
        )
        if result.success:
            return True, f"已给{result.pet_name}留言：{message}"
        return False, result.reason or "留言失败"

    def get_messages(self, user_id: str, group_id: int) -> tuple[bool, str]:
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
                    limit: int = 10) -> list[tuple[str, str, float]]:
        if ranking_type in {"care_score", "intimacy", "experience"}:
            rows = self.db.get_pet_ranking(group_id, ranking_type, limit)
            return [
                (
                    row["user_id"],
                    row["pet_name"],
                    round(float(row["score"]), 1),
                )
                for row in rows
            ]
        if ranking_type == "coins":
            # CR Review: 使用优化的 JOIN 查询替代 N+1 循环
            rows = self.db.get_coins_ranking(group_id, limit)
            return [(r['user_id'], r['pet_name'], r['coins']) for r in rows]

        return []

    # ──────────────────── 小游戏（Issue #46）────────────────────

    def play_rock_paper_scissors(
        self,
        user_id: str,
        group_id: int,
        player_choice: str,
        *,
        message_id: str | None = None,
    ) -> tuple[bool, str]:
        """猜拳小游戏"""
        choices = {"石头": "rock", "剪刀": "scissors", "布": "paper",
                   "rock": "rock", "scissors": "scissors", "paper": "paper"}

        normalized = choices.get(player_choice)
        if not normalized:
            return False, "请选择：石头、剪刀 或 布"

        cn = {"rock": "石头", "scissors": "剪刀", "paper": "布"}
        config = MINIGAME_CONFIG["rock_paper_scissors"]

        def outcome_factory(_pet: Pet, _opponent: Pet | None) -> MinigameOutcome:
            npc_choice = random.choice(["rock", "scissors", "paper"])
            if normalized == npc_choice:
                outcome_text = "平局"
                coins = config["draw_coins"]
                exp = 0
            elif (
                (normalized == "rock" and npc_choice == "scissors")
                or (normalized == "scissors" and npc_choice == "paper")
                or (normalized == "paper" and npc_choice == "rock")
            ):
                outcome_text = "你赢了"
                coins = config["win_coins"]
                exp = config["win_exp"]
            else:
                outcome_text = "你输了"
                coins = config["lose_coins"]
                exp = 0
            return MinigameOutcome(
                requested_coins=coins,
                experience=exp,
                payload={
                    "player_choice": normalized,
                    "npc_choice": npc_choice,
                    "result": outcome_text,
                    "offered_coins": coins > 0,
                },
            )

        settlement = self.db.settle_minigame_atomic(
            user_id,
            group_id,
            "rock_paper_scissors",
            reference_id=self._minigame_reference(
                "rock_paper_scissors", user_id, group_id, message_id=message_id
            ),
            daily_coin_limit=DAILY_LIMITS["coins"],
            cooldown_seconds=int(config.get("cooldown", 0) or 0),
            outcome_factory=outcome_factory,
        )
        if not settlement.success:
            return False, settlement.reason or "猜拳结算失败"
        payload = settlement.payload or {}

        msg = "✊✌️✋ **猜拳**\n\n"
        msg += f"你出了：{cn[str(payload['player_choice'])]}\n"
        msg += f"{settlement.pet_name}出了：{cn[str(payload['npc_choice'])]}\n\n"
        msg += f"**{payload['result']}！**"
        msg += self._reward_suffix(
            settlement,
            offered_coins=bool(payload.get("offered_coins")),
        )
        return True, msg

    def play_dice(
        self,
        user_id: str,
        group_id: int,
        *,
        message_id: str | None = None,
    ) -> tuple[bool, str]:
        """骰子小游戏"""
        config = MINIGAME_CONFIG["dice"]

        def outcome_factory(_pet: Pet, _opponent: Pet | None) -> MinigameOutcome:
            player_dice = random.randint(1, 6)
            pet_dice = random.randint(1, 6)
            if player_dice > pet_dice:
                outcome_text = "你赢了"
                coins = config["win_coins"]
                exp = config["win_exp"]
            elif player_dice == pet_dice:
                outcome_text = "平局"
                coins = 5
                exp = 0
            else:
                outcome_text = "你输了"
                coins = config["lose_coins"]
                exp = 0
            return MinigameOutcome(
                requested_coins=coins,
                experience=exp,
                payload={
                    "player_dice": player_dice,
                    "pet_dice": pet_dice,
                    "result": outcome_text,
                    "offered_coins": coins > 0,
                },
            )

        settlement = self.db.settle_minigame_atomic(
            user_id,
            group_id,
            "dice",
            reference_id=self._minigame_reference(
                "dice", user_id, group_id, message_id=message_id
            ),
            daily_coin_limit=DAILY_LIMITS["coins"],
            cooldown_seconds=int(config.get("cooldown", 0) or 0),
            outcome_factory=outcome_factory,
        )
        if not settlement.success:
            return False, settlement.reason or "骰子结算失败"
        payload = settlement.payload or {}

        msg = "🎲 **骰子**\n\n"
        msg += f"你掷出了：{payload['player_dice']}\n"
        msg += f"{settlement.pet_name}掷出了：{payload['pet_dice']}\n\n"
        msg += f"**{payload['result']}！**"
        msg += self._reward_suffix(
            settlement,
            offered_coins=bool(payload.get("offered_coins")),
        )
        return True, msg

    def race_pet(
        self,
        user_id: str,
        target_user_id: str,
        group_id: int,
        *,
        message_id: str | None = None,
    ) -> tuple[bool, str]:
        """宠物赛跑"""
        if user_id == target_user_id:
            return False, "不能跟自己的宠物赛跑"

        config = MINIGAME_CONFIG["race"]

        def outcome_factory(pet: Pet, opponent: Pet | None) -> MinigameOutcome:
            if opponent is None:
                raise RuntimeError("race opponent disappeared")
            energy_cost = int(config["energy_cost"])
            my_speed = random.randint(1, 100) + (pet.energy - energy_cost) // 5
            target_speed = random.randint(1, 100) + opponent.energy // 5
            if pet.personality == PetPersonality.LIVELY:
                my_speed += 10
            if opponent.personality == PetPersonality.LIVELY:
                target_speed += 10
            if my_speed > target_speed:
                coins = config["win_coins"]
                exp = config["win_exp"]
                outcome_key = "win"
            elif my_speed == target_speed:
                coins = config["second_coins"]
                exp = 3
                outcome_key = "draw"
            else:
                coins = config["lose_coins"]
                exp = 2
                outcome_key = "lose"
            return MinigameOutcome(
                requested_coins=coins,
                experience=exp,
                energy_cost=energy_cost,
                payload={
                    "result": outcome_key,
                    "offered_coins": coins > 0,
                },
            )

        settlement = self.db.settle_minigame_atomic(
            user_id,
            group_id,
            "race",
            opponent_user_id=target_user_id,
            reference_id=self._minigame_reference(
                "race",
                user_id,
                group_id,
                opponent_user_id=target_user_id,
                message_id=message_id,
            ),
            daily_coin_limit=DAILY_LIMITS["coins"],
            cooldown_seconds=int(config.get("cooldown", 0) or 0),
            minimum_energy=int(config["energy_cost"]),
            outcome_factory=outcome_factory,
        )
        if not settlement.success:
            return False, settlement.reason or "赛跑结算失败"
        payload = settlement.payload or {}
        outcome_key = payload.get("result")
        if outcome_key == "win":
            outcome_text = f"🏆 {settlement.pet_name}赢了！"
        elif outcome_key == "draw":
            outcome_text = "🤝 平局！"
        else:
            outcome_text = f"😔 {settlement.opponent_pet_name}赢了！"

        msg = "🏃 **宠物赛跑**\n\n"
        msg += f"{settlement.pet_name} 🆚 {settlement.opponent_pet_name}\n\n"
        msg += outcome_text
        msg += self._reward_suffix(
            settlement,
            offered_coins=bool(payload.get("offered_coins")),
        )
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
                    self.db.credit_coins_atomic(
                        uid,
                        group_id,
                        reward,
                        reason="pet_show",
                        reference_id=f"show:{show['id']}:{i}:{uid}",
                    )
            if i == 0:
                UserService(self.db).grant_temporary_title(uid, group_id, "展示会冠军")
                text += " 🏅展示会冠军"
            text += "\n"

        self.db.end_pet_show(show['id'])
        text += "\n🎉 展示会已结束，感谢参与！"
        return text
