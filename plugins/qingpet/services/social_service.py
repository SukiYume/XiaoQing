"""宠物互访、礼物、留言、排行榜、小游戏和展示会服务。"""

import hashlib
import random
import secrets

from ..models import Pet
from ..utils.constants import (
    COOLDOWN_TIMES,
    DAILY_LIMITS,
    DEFAULT_ITEMS,
    MINIGAME_CONFIG,
    PetPersonality,
)
from ..utils.formatters import format_pet_card
from ..utils.validators import validate_item_amount, validate_sensitive_content
from .database import Database, MinigameAtomicResult, MinigameOutcome

_RPS_CHOICES = {
    "石头": "rock",
    "剪刀": "scissors",
    "布": "paper",
    "rock": "rock",
    "scissors": "scissors",
    "paper": "paper",
}
_RPS_NAMES = {"rock": "石头", "scissors": "剪刀", "paper": "布"}


class SocialService:
    """校验社交请求，并把涉及资产的操作交给数据库原子结算。"""

    def __init__(self, db: Database) -> None:
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
        material = f"{request_token}\0{game_type}\0{group_id}\0{user_id}\0{opponent_user_id or ''}"
        return f"pet-minigame:v1:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _reward_suffix(result: MinigameAtomicResult, *, offered_coins: bool) -> str:
        """按实际结算值生成小游戏奖励摘要。"""
        parts: list[str] = []
        if offered_coins:
            parts.append(f"获得{result.coin_grant}金币")
        if result.experience_grant > 0:
            parts.append(f"+ {result.experience_grant}经验")
        if result.energy_cost > 0:
            parts.append(f"消耗{result.energy_cost}精力")
        return f" {' '.join(parts)}" if parts else ""

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
                f"访客获得{result.visitor_grant}金币，宠物主人获得{result.target_grant}金币"
            )
        return (
            True,
            f"访问了{result.pet_name}，宠物亲密度+{result.intimacy_grant}；{reward_text}",
        )

    # ──────────────────── 送礼 ────────────────────

    def gift_item(
        self,
        from_user_id: str,
        to_user_id: str,
        group_id: int,
        item_id: str,
        amount: int = 1,
    ) -> tuple[bool, str]:
        if from_user_id == to_user_id:
            return False, "不能给自己送礼物"
        if type(amount) is not int:
            return False, "数量必须是整数"
        valid, message = validate_item_amount(amount)
        if not valid:
            return False, message
        if item_id not in DEFAULT_ITEMS:
            return False, "道具不存在或不可赠送"

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

    # ──────────────────── 查看他人宠物卡片 ────────────────────

    def view_pet_card(self, target_user_id: str, group_id: int) -> tuple[bool, str]:
        """读取指定用户的宠物和账户信息，并生成公开卡片。"""
        target_pet = self.db.get_pet(target_user_id, group_id)
        if target_pet is None:
            return False, "该用户没有宠物"

        target_user = self.db.get_user(target_user_id, group_id)
        if target_user is None:
            return False, "用户不存在"

        card = format_pet_card(target_pet, target_user)
        return True, f"📋 {target_user_id} 的宠物卡片\n\n{card}"

    # ──────────────────── 点赞/摸摸 ───────────────────────────────

    def like_pet(self, user_id: str, target_user_id: str, group_id: int) -> tuple[bool, str]:
        """点赞目标宠物，并执行每用户、每目标的每日次数限制。"""
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

    # ──────────────────── 留言板 ──────────────────────────────────

    def leave_message(
        self, from_user_id: str, to_user_id: str, group_id: int, message: str
    ) -> tuple[bool, str]:
        """给另一用户留言，并执行发送者的每日留言次数限制。"""
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

        lines = ["📝 **留言板**", ""]
        for msg in messages:
            created = msg.get("created_at", "未知时间")
            if isinstance(created, str) and len(created) > 16:
                created = created[:16]
            lines.append(f"• [{created}] 来自 {msg['from_user_id']}: {msg['message']}")
        return True, "\n".join(lines)

    # ──────────────────── 排行榜（展示持久化 care_score）───────────

    def get_ranking(
        self, group_id: int, ranking_type: str = "care_score", limit: int = 10
    ) -> list[tuple[str, str, float]]:
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
            # 排行榜由数据库一次 JOIN 聚合，避免随榜单长度增加查询次数。
            rows = self.db.get_coins_ranking(group_id, limit)
            return [(row["user_id"], row["pet_name"], row["coins"]) for row in rows]

        return []

    # ──────────────────── 小游戏 ──────────────────────────────────

    def play_rock_paper_scissors(
        self,
        user_id: str,
        group_id: int,
        player_choice: str,
        *,
        message_id: str | None = None,
    ) -> tuple[bool, str]:
        """与宠物进行猜拳，并原子结算冷却和奖励。"""
        normalized = _RPS_CHOICES.get(player_choice)
        if not normalized:
            return False, "请选择：石头、剪刀 或 布"

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

        lines = [
            "✊✌️✋ **猜拳**",
            "",
            f"你出了：{_RPS_NAMES[str(payload['player_choice'])]}",
            f"{settlement.pet_name}出了：{_RPS_NAMES[str(payload['npc_choice'])]}",
            "",
            f"**{payload['result']}！**",
        ]
        message = "\n".join(lines) + self._reward_suffix(
            settlement,
            offered_coins=bool(payload.get("offered_coins")),
        )
        return True, message

    def play_dice(
        self,
        user_id: str,
        group_id: int,
        *,
        message_id: str | None = None,
    ) -> tuple[bool, str]:
        """与宠物掷骰子，并原子结算冷却和奖励。"""
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
            reference_id=self._minigame_reference("dice", user_id, group_id, message_id=message_id),
            daily_coin_limit=DAILY_LIMITS["coins"],
            cooldown_seconds=int(config.get("cooldown", 0) or 0),
            outcome_factory=outcome_factory,
        )
        if not settlement.success:
            return False, settlement.reason or "骰子结算失败"
        payload = settlement.payload or {}

        lines = [
            "🎲 **骰子**",
            "",
            f"你掷出了：{payload['player_dice']}",
            f"{settlement.pet_name}掷出了：{payload['pet_dice']}",
            "",
            f"**{payload['result']}！**",
        ]
        message = "\n".join(lines) + self._reward_suffix(
            settlement,
            offered_coins=bool(payload.get("offered_coins")),
        )
        return True, message

    def race_pet(
        self,
        user_id: str,
        target_user_id: str,
        group_id: int,
        *,
        message_id: str | None = None,
    ) -> tuple[bool, str]:
        """让两只宠物赛跑，并原子结算精力、冷却和奖励。"""
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

        message = (
            f"🏃 **宠物赛跑**\n\n"
            f"{settlement.pet_name} 🆚 {settlement.opponent_pet_name}\n\n"
            f"{outcome_text}"
        ) + self._reward_suffix(
            settlement,
            offered_coins=bool(payload.get("offered_coins")),
        )
        return True, message

    # ──────────────────── 展示会结算 ────────────────────

    def settle_pet_show(self, group_id: int, *, force: bool = True) -> str:
        """原子结算一个已截止或被管理员明确结束的展示会。"""
        settlement = self.db.settle_pet_show_atomic(group_id, force=force)
        if settlement is None:
            return ""

        if not settlement.winners:
            return "🏆 展示会已结束（无投票数据）"

        medals = ["🥇", "🥈", "🥉"]
        lines = [f"🏆 **{settlement.title} 结果**", ""]

        for index, winner in enumerate(settlement.winners):
            medal = medals[index] if index < len(medals) else f"#{index + 1}"
            line = f"{medal} {winner.pet_name} ({winner.user_id}) - {winner.vote_count}票"
            if winner.coins_granted > 0:
                line += f" +{winner.coins_granted}金币"
            if index == 0:
                line += " 🏅展示会冠军"
            lines.append(line)

        lines.extend(("", "🎉 展示会已结束，感谢参与！"))
        return "\n".join(lines)
