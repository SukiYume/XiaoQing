"""宠物领养、照料、成长、衰减和消耗品服务。"""

import random
from dataclasses import replace
from datetime import timedelta

from ..models import Inventory, Pet, User
from ..utils.constants import (
    AGE_EVOLUTION_THRESHOLDS,
    COOLDOWN_TIMES,
    DAILY_LIMITS,
    DECAY_RATES,
    DEFAULT_ITEMS,
    DISEASE_THRESHOLDS,
    EVOLUTION_CONDITIONS,
    EVOLUTION_EXPERIENCE_THRESHOLDS,
    EXPLORE_LOCATIONS,
    FAVORITE_FOOD_BONUS,
    TRAINING_CONFIG,
    TRAINING_MESSAGES,
    TRAINING_SPECIAL_EVENTS,
    TRAVEL_THRESHOLDS,
    ItemType,
    PetPersonality,
    PetStage,
    PetStatus,
)
from ..utils.time import utc_now
from ..utils.validators import validate_cooling, validate_pet_name
from .database import Database
from .item_service import ItemService
from .user_service import UserService

_ACTION_LABELS = {
    "feed": "喂食",
    "clean": "清洁",
    "play": "玩耍",
    "train": "训练",
    "explore": "探索",
}
_PET_PERSONALITIES = tuple(PetPersonality)


class PetService:
    """协调领域规则与数据库原子事务，保持调用方模型和持久化状态一致。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _daily_limit_message(action: str, limit: int) -> str:
        """生成统一的每日动作上限提示。"""
        return f"今日{_ACTION_LABELS.get(action, action)}次数已达上限({limit}次)"

    @staticmethod
    def _sync_state(
        target: Pet | User | Inventory,
        source: Pet | User | Inventory,
    ) -> None:
        """数据库提交成功后，把候选模型的最终版本同步回调用方对象。"""
        target.__dict__.update(source.__dict__)

    def _persist_pet_candidate(self, target: Pet, candidate: Pet) -> bool:
        """仅在候选宠物写入成功后更新调用方持有的实例。"""
        if not self.db.update_pet(candidate):
            return False
        self._sync_state(target, candidate)
        return True

    def _evolution_suffix(self, pet: Pet) -> str:
        """执行一次进化检查，并生成追加到动作结果的可选文本。"""
        evolved, message = self.check_evolution(pet)
        return f"\n\n{message}" if evolved else ""

    def _check_daily_action_limit(self, user: User, action: str) -> tuple[bool, str]:
        limit = DAILY_LIMITS.get(action)
        if limit is None:
            return True, ""
        if not user.can_do_action(action, 1, limit):
            return False, self._daily_limit_message(action, limit)
        available, remaining = self.db.check_action_quota(
            user.user_id,
            user.group_id,
            action,
            limit,
        )
        if available:
            return True, ""
        if remaining > 0:
            return False, f"{action}冷却中，请等待{remaining}秒"
        return False, self._daily_limit_message(action, limit)

    def _commit_failure_message(self, action: str, reason: str, remaining: int) -> str:
        action_label = _ACTION_LABELS.get(action, action)
        if reason == "cooldown" and remaining > 0:
            return f"{action_label}冷却中，请等待{remaining}秒"
        if reason == "free_feed_limit":
            limit = DAILY_LIMITS.get("free_feed", 5)
            return f"今日免费喂食次数已达上限({limit}次)，请购买食物后喂食"
        if reason == "inventory":
            return "背包库存已变化，请重试"
        if reason == "daily_limit":
            return self._daily_limit_message(action, DAILY_LIMITS[action])
        return f"{action_label}失败"

    @staticmethod
    def _get_cannot_interact_msg(pet: Pet) -> str:
        """根据宠物状态返回具体的无法互动原因。"""
        if pet.status == PetStatus.SLEEPING:
            return "宠物在睡觉中，使用 /宠物 起床 唤醒它"
        if pet.status == PetStatus.SICK:
            return "宠物生病了，使用 /宠物 治疗 [药品] 治疗"
        if pet.status == PetStatus.TRAVELING:
            return "宠物正在旅行中，使用 /宠物 召回 召回它\n用法: /宠物 召回"
        if pet.status == PetStatus.DEAD:
            return "宠物已死亡"
        return "宠物现在无法互动"

    # ──────────────────── 领养 ────────────────────

    def adopt_pet(self, user_id: str, group_id: int, name: str) -> tuple[bool, str]:
        group_config = self.db.get_group_config(group_id)
        valid, message = validate_pet_name(name, group_config.sensitive_words)
        if not valid:
            return False, message

        existing_pet = self.db.get_pet(user_id, group_id)
        if existing_pet:
            return False, "你已经有一只宠物了，每个用户只能养一只"

        user_service = UserService(self.db)
        user_service.get_or_create_user(user_id, group_id)

        pet = Pet(
            id=0,
            user_id=user_id,
            group_id=group_id,
            name=name,
            stage=PetStage.EGG,
            personality=random.choice(_PET_PERSONALITIES),
        )

        success = self.db.create_pet(pet)
        if success:
            return True, f"恭喜！你领养了一颗{name}的宠物蛋，快去孵化吧！"
        return False, "领养失败，请稍后重试"

    # ──────────────────── 孵化 ────────────────────

    def hatch_egg(self, pet: Pet) -> tuple[bool, str]:
        if pet.stage != PetStage.EGG:
            return False, "只有宠物蛋才能孵化"

        experience_cost = EVOLUTION_EXPERIENCE_THRESHOLDS[PetStage.EGG]
        if pet.experience < experience_cost:
            return False, "宠物蛋还需要更多经验才能孵化，继续互动吧"

        candidate = replace(pet)
        candidate.stage = PetStage.YOUNG
        candidate.experience -= experience_cost
        candidate.last_update = utc_now()

        if self._persist_pet_candidate(pet, candidate):
            return True, f"破壳啦！{pet.name}变成了{pet.stage.value}"
        return False, "孵化失败"

    # ──────────────────── 喂食 ────────────────────

    def feed_pet(
        self, pet: Pet, user: User, item_id: str | None = None, spam_decay_factor: float = 1.0
    ) -> tuple[bool, str, int]:
        """喂食宠物，同时执行免费额度、喜好加成和奖励衰减约束。"""
        if not pet.can_interact():
            return False, self._get_cannot_interact_msg(pet), 0

        cooled, remaining = validate_cooling(pet.last_feed, COOLDOWN_TIMES["feed"])
        if not cooled:
            return False, f"喂食冷却中，请等待{remaining}秒", 0

        identifier = item_id or "apple"
        resolved_item = ItemService(self.db).resolve_item(identifier)
        if resolved_item is None:
            return False, f"道具 '{identifier}' 不存在", 0
        actual_item_id, item = resolved_item
        if item.item_type != ItemType.FOOD:
            return False, f"{item.name}不是食物，不能用于喂食", 0
        item_data = DEFAULT_ITEMS[actual_item_id]

        inventory = self.db.get_or_create_inventory(user.user_id, user.group_id)
        uses_inventory_item = inventory.has_item(actual_item_id)
        uses_free_apple = not uses_inventory_item
        if uses_free_apple:
            # 免费苹果受每日额度约束，背包道具不消耗这项额度。
            if actual_item_id != "apple":
                return False, f"背包中没有 {item_data['name']}，请先购买", 0
            # 检查免费喂食次数是否超限
            free_feed_limit = DAILY_LIMITS.get("free_feed", 5)
            if not user.can_do_action("free_feed", 1, free_feed_limit):
                return False, f"今日免费喂食次数已达上限({free_feed_limit}次)，请购买食物后喂食", 0
        allowed, reason = self._check_daily_action_limit(user, "feed")
        if not allowed:
            return False, reason, 0

        target_pet, target_user = pet, user
        pet, user = replace(pet), replace(user)

        hunger_gain = item_data.get("hunger_gain", 0)
        mood_gain = item_data.get("mood_gain", 0)
        exp_gain = item_data.get("exp_gain", 0)
        intimacy_gain = item_data.get("intimacy_gain", 0)

        # 喜好加成只由实际食物 ID 触发，不能由展示名称或默认值触发。
        if pet.favorite_food and actual_item_id == pet.favorite_food:
            hunger_gain = int(hunger_gain * FAVORITE_FOOD_BONUS["hunger_multiplier"])
            mood_gain = int(mood_gain * FAVORITE_FOOD_BONUS["mood_multiplier"])
            exp_gain = int(exp_gain * FAVORITE_FOOD_BONUS["exp_multiplier"])

        # 装扮心情加成
        mood_gain += pet.get_dress_mood_bonus()

        pet.update_stat("hunger", hunger_gain)
        pet.update_stat("mood", mood_gain)
        pet.experience += exp_gain
        pet.intimacy += intimacy_gain
        now = utc_now()
        pet.last_feed = now
        pet.last_update = now

        group_config = self.db.get_group_config(pet.group_id)
        # 经济奖励在提交原子事务前应用本次请求的反脚本衰减。
        requested_coins = int(5 * group_config.economy_multiplier * spam_decay_factor)

        result = self.db.commit_pet_action(
            pet,
            user,
            action="feed",
            daily_limit=DAILY_LIMITS["feed"],
            cooldown_seconds=COOLDOWN_TIMES["feed"],
            requested_coins=requested_coins,
            consume_item_id=actual_item_id if uses_inventory_item else None,
            free_feed_increment=1 if uses_free_apple else 0,
            free_feed_limit=DAILY_LIMITS.get("free_feed", 5),
            task_type="feed",
            group_task_type="group_feed",
        )

        if result.success:
            self._sync_state(target_pet, pet)
            self._sync_state(target_user, user)
            extra_msg = self._evolution_suffix(target_pet)
            fav_msg = (
                " 💖喂了喜欢的食物！"
                if target_pet.favorite_food and actual_item_id == target_pet.favorite_food
                else ""
            )
            free_apple_hint = ""
            if uses_free_apple:
                free_feed_limit = DAILY_LIMITS.get("free_feed", 5)
                free_left = max(0, free_feed_limit - target_user.today_free_feed_count)
                free_apple_hint = f"\n🍎 今日免费苹果剩余: {free_left}/{free_feed_limit}"
            return (
                True,
                f"喂食成功！{target_pet.name}很开心，获得{exp_gain}经验{fav_msg}{free_apple_hint}{extra_msg}",
                result.coins_granted,
            )
        return False, self._commit_failure_message("feed", result.reason, result.remaining), 0

    # ──────────────────── 清洁 ────────────────────

    def clean_pet(
        self, pet: Pet, user: User, spam_decay_factor: float = 1.0
    ) -> tuple[bool, str, int]:
        if not pet.can_interact():
            return False, self._get_cannot_interact_msg(pet), 0

        cooled, remaining = validate_cooling(pet.last_clean, COOLDOWN_TIMES["clean"])
        if not cooled:
            return False, f"清洁冷却中，请等待{remaining}秒", 0

        allowed, reason = self._check_daily_action_limit(user, "clean")
        if not allowed:
            return False, reason, 0

        target_pet, target_user = pet, user
        pet, user = replace(pet), replace(user)

        clean_gain = 20
        health_gain = 5

        pet.update_stat("clean", clean_gain)
        pet.update_stat("health", health_gain)
        now = utc_now()
        pet.last_clean = now
        pet.last_update = now

        group_config = self.db.get_group_config(pet.group_id)
        # 经济奖励在提交原子事务前应用本次请求的反脚本衰减。
        requested_coins = int(3 * group_config.economy_multiplier * spam_decay_factor)

        result = self.db.commit_pet_action(
            pet,
            user,
            action="clean",
            daily_limit=DAILY_LIMITS["clean"],
            cooldown_seconds=COOLDOWN_TIMES["clean"],
            requested_coins=requested_coins,
            task_type="clean",
            group_task_type="group_clean",
        )

        if result.success:
            self._sync_state(target_pet, pet)
            self._sync_state(target_user, user)
            extra_msg = self._evolution_suffix(target_pet)
            return (
                True,
                f"清洁完成！{target_pet.name}变得香喷喷的{extra_msg}",
                result.coins_granted,
            )
        return False, self._commit_failure_message("clean", result.reason, result.remaining), 0

    # ──────────────────── 玩耍 ────────────────────

    def play_with_pet(
        self, pet: Pet, user: User, spam_decay_factor: float = 1.0
    ) -> tuple[bool, str, int]:
        if not pet.can_interact():
            return False, self._get_cannot_interact_msg(pet), 0

        cooled, remaining = validate_cooling(pet.last_play, COOLDOWN_TIMES["play"])
        if not cooled:
            return False, f"玩耍冷却中，请等待{remaining}秒", 0

        allowed, reason = self._check_daily_action_limit(user, "play")
        if not allowed:
            return False, reason, 0

        target_pet, target_user = pet, user
        pet, user = replace(pet), replace(user)

        mood_gain = 15
        intimacy_gain = 2
        energy_cost = 10

        pet.update_stat("mood", mood_gain)
        pet.update_stat("energy", -energy_cost, min_val=0)
        pet.intimacy += intimacy_gain
        now = utc_now()
        pet.last_play = now
        pet.last_update = now

        group_config = self.db.get_group_config(pet.group_id)
        requested_coins = int(5 * group_config.economy_multiplier * spam_decay_factor)

        result = self.db.commit_pet_action(
            pet,
            user,
            action="play",
            daily_limit=DAILY_LIMITS["play"],
            cooldown_seconds=COOLDOWN_TIMES["play"],
            requested_coins=requested_coins,
            task_type="play",
        )

        if result.success:
            self._sync_state(target_pet, pet)
            self._sync_state(target_user, user)
            extra_msg = self._evolution_suffix(target_pet)
            return (
                True,
                f"玩得很开心！{target_pet.name}的亲密度提升了{extra_msg}",
                result.coins_granted,
            )
        return False, self._commit_failure_message("play", result.reason, result.remaining), 0

    # ──────────────────── 训练 ────────────────────

    def train_pet(
        self, pet: Pet, user: User, training_type: str = "strength", spam_decay_factor: float = 1.0
    ) -> tuple[bool, str, int]:
        if not pet.can_interact():
            return False, self._get_cannot_interact_msg(pet), 0

        cooled, remaining = validate_cooling(pet.last_train, COOLDOWN_TIMES["train"])
        if not cooled:
            return False, f"训练冷却中，请等待{remaining}秒", 0

        # 未识别的训练类型统一回退，后续概率和展示都使用规范键。
        if training_type not in TRAINING_CONFIG:
            training_type = "strength"
        config = TRAINING_CONFIG[training_type]

        if pet.energy < config["energy_cost"]:
            return False, "宠物精力不足，无法训练", 0

        allowed, reason = self._check_daily_action_limit(user, "train")
        if not allowed:
            return False, reason, 0

        target_pet, target_user = pet, user
        pet, user = replace(pet), replace(user)

        # 成功率：基础 + SMART 性格对智力训练加成
        success_rate = config["success_rate_base"]
        if pet.personality == PetPersonality.SMART and training_type == "intellect":
            success_rate = min(1.0, success_rate + 0.1)

        energy_cost = config["energy_cost"]
        pet.update_stat("energy", -energy_cost, min_val=0)
        now = utc_now()
        pet.last_train = now
        pet.last_update = now

        if random.random() > success_rate:
            result = self.db.commit_pet_action(
                pet,
                user,
                action="train",
                daily_limit=DAILY_LIMITS["train"],
                cooldown_seconds=COOLDOWN_TIMES["train"],
                requested_coins=0,
            )
            if not result.success:
                return (
                    False,
                    self._commit_failure_message("train", result.reason, result.remaining),
                    0,
                )
            self._sync_state(target_pet, pet)
            self._sync_state(target_user, user)
            fail_msg = random.choice(TRAINING_MESSAGES["fail"]).format(name=target_pet.name)
            return True, fail_msg, 0

        # 训练成功
        exp_gain = config["exp_gain"]

        # SMART 性格：所有训练经验 ×1.1
        if pet.personality == PetPersonality.SMART:
            exp_gain = int(exp_gain * 1.1)

        # 检查特殊事件（最多触发一个，顺序检查）
        special_msg = ""
        for event in TRAINING_SPECIAL_EVENTS:
            prob = event["prob"]
            # CLINGY 性格：亲密度相关特殊事件概率 ×2
            if pet.personality == PetPersonality.CLINGY and "intimacy" in event:
                prob = min(1.0, prob * 2)
            if random.random() < prob:
                special_msg = f"\n✨ {event['msg']}"
                if "intimacy" in event:
                    pet.intimacy += event["intimacy"]
                if "exp_multiplier" in event:
                    exp_gain = int(exp_gain * event["exp_multiplier"])
                break  # 最多一个

        # 敏捷训练：心情加成（LIVELY 性格 ×1.5）
        extra_effects = config.get("extra_effects", {})
        for stat, delta in extra_effects.items():
            actual_delta = delta
            if stat == "mood" and pet.personality == PetPersonality.LIVELY:
                actual_delta = int(delta * 1.5)
            pet.update_stat(stat, actual_delta)

        pet.experience += exp_gain
        pet.intimacy += 1

        group_config = self.db.get_group_config(pet.group_id)
        requested_coins = int(10 * group_config.economy_multiplier * spam_decay_factor)

        result = self.db.commit_pet_action(
            pet,
            user,
            action="train",
            daily_limit=DAILY_LIMITS["train"],
            cooldown_seconds=COOLDOWN_TIMES["train"],
            requested_coins=requested_coins,
        )
        if result.success:
            self._sync_state(target_pet, pet)
            self._sync_state(target_user, user)
            extra_msg = self._evolution_suffix(target_pet)
            base_msg = random.choice(TRAINING_MESSAGES["success"]).format(name=target_pet.name)
            type_name = config["name"]
            return (
                True,
                f"[{type_name}] {base_msg} 获得{exp_gain}经验{special_msg}{extra_msg}",
                result.coins_granted,
            )
        return False, self._commit_failure_message("train", result.reason, result.remaining), 0

    # ──────────────────── 探索 ────────────────────

    def explore(
        self, pet: Pet, user: User, location: str = "forest", spam_decay_factor: float = 1.0
    ) -> tuple[bool, str, int]:
        if not pet.can_interact():
            return False, self._get_cannot_interact_msg(pet), 0

        cooled, remaining = validate_cooling(pet.last_explore, COOLDOWN_TIMES["explore"])
        if not cooled:
            return False, f"探索冷却中，请等待{remaining}秒", 0

        # 未识别的地点统一回退，前置条件、事件权重和展示使用同一规范键。
        if location not in EXPLORE_LOCATIONS:
            location = "forest"
        loc_config = EXPLORE_LOCATIONS[location]

        energy_cost = loc_config["energy_cost"]
        if pet.energy < energy_cost:
            return False, "宠物精力不足，无法探索", 0

        # 山洞/废墟健康前置检查
        if location in ("cave", "ruins") and pet.health < 40:
            return False, "宠物健康值过低，不建议进入危险地点（需健康≥40）", 0

        allowed, reason = self._check_daily_action_limit(user, "explore")
        if not allowed:
            return False, reason, 0

        target_pet, target_user = pet, user
        pet, user = replace(pet), replace(user)

        pet.update_stat("energy", -energy_cost, min_val=0)

        # 按性格调整事件概率权重，加权随机选一个事件
        events = loc_config["events"]
        weights = []
        for event in events:
            prob = event["prob"]
            if pet.personality == PetPersonality.LIVELY:
                if location == "forest" and ("intimacy" in event or event.get("coins", 0) > 0):
                    prob += 0.1
                elif location in ("cave", "ruins") and "health" not in event:
                    prob += 0.05
            if pet.personality == PetPersonality.SHY:
                if location in ("cave", "ruins") and ("health" in event or "mood" in event):
                    prob += 0.1
            if pet.personality == PetPersonality.SMART:
                if location in ("cave", "ruins") and "item" in event:
                    prob += 0.1
            weights.append(max(prob, 0.01))

        chosen = random.choices(events, weights=weights, k=1)[0]

        group_config = self.db.get_group_config(pet.group_id)
        exp_gain = int(chosen.get("exp", 0))
        coins_gain = int(
            chosen.get("coins", 0) * group_config.economy_multiplier * spam_decay_factor
        )

        pet.experience += exp_gain
        now = utc_now()
        pet.last_explore = now
        pet.last_update = now

        for stat in ("mood", "clean", "health"):
            if stat in chosen:
                pet.update_stat(stat, chosen[stat], min_val=0)
        if "intimacy" in chosen:
            pet.intimacy += chosen["intimacy"]

        # 道具掉落
        item_msg = ""
        inventory_grants = None
        if "item" in chosen:
            item_id = chosen["item"]
            item_name = DEFAULT_ITEMS.get(item_id, {}).get("name", item_id)
            inventory_grants = {item_id: 1}
            item_msg = f"（获得 {item_name} ×1）"

        result = self.db.commit_pet_action(
            pet,
            user,
            action="explore",
            daily_limit=DAILY_LIMITS["explore"],
            cooldown_seconds=COOLDOWN_TIMES["explore"],
            requested_coins=coins_gain,
            inventory_grants=inventory_grants,
            group_task_type="group_explore",
        )
        if result.success:
            self._sync_state(target_pet, pet)
            self._sync_state(target_user, user)
            extra_msg = self._evolution_suffix(target_pet)
            loc_name = loc_config["name"]
            return (
                True,
                f"[{loc_name}] {chosen['msg']}{item_msg} 获得{exp_gain}经验{extra_msg}",
                result.coins_granted,
            )
        return False, self._commit_failure_message("explore", result.reason, result.remaining), 0

    # ──────────────────── 睡觉 / 起床 ────────────────────

    def sleep_pet(self, pet: Pet) -> tuple[bool, str]:
        if pet.status == PetStatus.SLEEPING:
            return False, "宠物已经在睡觉了"
        if not pet.can_interact():
            return False, self._get_cannot_interact_msg(pet)

        candidate = replace(pet)
        candidate.status = PetStatus.SLEEPING
        candidate.last_update = utc_now()
        candidate.status_expire_time = candidate.last_update + timedelta(seconds=60)

        if self._persist_pet_candidate(pet, candidate):
            return True, f"{pet.name}开始睡觉了，Zzz..."
        return False, "让宠物睡觉失败"

    def wake_pet(self, pet: Pet) -> tuple[bool, str]:
        if pet.status != PetStatus.SLEEPING:
            return False, "宠物现在没有在睡觉"
        now = utc_now()

        candidate = replace(pet)
        candidate.status = PetStatus.NORMAL
        candidate.status_expire_time = None
        candidate.last_update = now

        if self._persist_pet_candidate(pet, candidate):
            return True, f"{pet.name}睡醒了！睡眠期间已按实际时长恢复精力（立即唤醒不恢复）。"
        return False, "唤醒宠物失败"

    # ──────────────────── 进化检查 ────────────────────────────────

    def check_evolution(self, pet: Pet) -> tuple[bool, str]:
        """按显式状态机检查并持久化一次阶段转移。"""
        if pet.stage == PetStage.OLD:
            return False, ""

        if pet.stage == PetStage.EGG:
            if pet.experience >= EVOLUTION_EXPERIENCE_THRESHOLDS[PetStage.EGG]:
                return self.hatch_egg(pet)
            return False, ""

        if (
            pet.experience < EVOLUTION_EXPERIENCE_THRESHOLDS[pet.stage]
            or pet.age < AGE_EVOLUTION_THRESHOLDS[pet.stage]
        ):
            return False, ""

        if pet.stage == PetStage.MATURE:
            condition = "aged"
        elif pet.care_score >= 0.8:
            condition = "excellent_care"
        elif pet.care_score >= 0.6:
            condition = "good_care"
        else:
            condition = "poor_care"

        new_stage, new_form = EVOLUTION_CONDITIONS[(pet.stage, condition)]
        old_stage = pet.stage.value
        candidate = replace(pet)
        candidate.stage = new_stage
        candidate.form = new_form
        candidate.experience = 0

        if self._persist_pet_candidate(pet, candidate):
            return (
                True,
                f"🎉 恭喜！{pet.name}从{old_stage}进化成了{new_stage.value}({new_form})！",
            )
        return False, ""

    # ──────────────────── 衰减与疾病概率 ──────────────────────────

    def apply_decay(
        self,
        pet: Pet,
        decay_multiplier: float = 1.0,
        *,
        is_trustee_override: bool | None = None,
    ) -> str | None:
        """应用状态衰减，并检查疾病概率。返回警报消息或None。"""
        now = utc_now()
        elapsed_minutes = max(0.0, (now - pet.last_update).total_seconds() / 60.0)

        target_pet = pet
        pet = replace(pet)
        status_message: str | None = None

        if pet.status == PetStatus.SLEEPING:
            # 睡眠是固定一分钟的有限状态。旧数据若缺少 expiry，则从最后更新时间
            # 推导一次并持久化；到期后只结算 expiry 之前的恢复，剩余离线时间继续衰减。
            latest_valid_expiry = pet.last_update + timedelta(seconds=60)
            stored_expiry = pet.status_expire_time
            sleep_expires_at = min(
                max(stored_expiry or latest_valid_expiry, pet.last_update),
                latest_valid_expiry,
            )
            repaired_expiry = stored_expiry != sleep_expires_at
            pet.status_expire_time = sleep_expires_at
            if now < sleep_expires_at:
                if repaired_expiry:
                    self._persist_pet_candidate(target_pet, pet)
                return None

            sleeping_minutes = max(
                0.0,
                (sleep_expires_at - pet.last_update).total_seconds() / 60.0,
            )
            energy_gain = int(2 * sleeping_minutes)
            if energy_gain > 0:
                pet.update_stat("energy", energy_gain)
            pet.status = PetStatus.NORMAL
            pet.status_expire_time = None
            pet.last_update = sleep_expires_at
            elapsed_minutes = max(
                0.0,
                (now - sleep_expires_at).total_seconds() / 60.0,
            )
            status_message = f"☀️ {pet.name}睡醒了！"

        if pet.status == PetStatus.TRAVELING:
            # 检查旅行是否到期
            if pet.status_expire_time and now >= pet.status_expire_time:
                pet.status = PetStatus.NORMAL
                pet.hunger = 50
                pet.mood = 50
                pet.clean = 50
                pet.energy = 50
                pet.health = 80
                pet.status_expire_time = None
                pet.last_update = now
                if self._persist_pet_candidate(target_pet, pet):
                    return f"🎒 {pet.name}旅行回来了！快去照顾它吧！"
            return None

        if pet.status != PetStatus.NORMAL:
            return None

        if elapsed_minutes < 1.0:
            if status_message is not None:
                if self._persist_pet_candidate(target_pet, pet):
                    return status_message
            return None

        if is_trustee_override is None:
            user = self.db.get_user(pet.user_id, pet.group_id)
            is_trustee = bool(user and user.is_trustee_active())
        else:
            is_trustee = is_trustee_override
        actual_multiplier = decay_multiplier * (0.5 if is_trustee else 1.0)

        changed = status_message is not None
        for stat, rate in DECAY_RATES.items():
            decay = int(rate * actual_multiplier * elapsed_minutes)
            if decay > 0:
                pet.update_stat(stat, -decay, min_val=0)
                changed = True

        if not changed:
            return None

        # 疾病概率系统
        if pet.clean < DISEASE_THRESHOLDS["clean_threshold"]:
            if random.random() < DISEASE_THRESHOLDS["disease_chance"]:
                pet.status = PetStatus.SICK
                # 生病必须伴随可见健康损失，否则治疗状态没有实际意义。
                pet.health = 30
                pet.last_update = now
                if self._persist_pet_candidate(target_pet, pet):
                    return f"🤒 {pet.name}因为环境太脏生病了！健康值大幅下降，快使用'/宠物 治疗'！"
                return None

        if pet.health <= 0:
            pet.status = PetStatus.SICK
            pet.health = 10

        # 照顾分过低时进入可恢复的旅行状态，避免不可逆地删除宠物。
        if pet.care_score < TRAVEL_THRESHOLDS["care_score_threshold"]:
            pet.status = PetStatus.TRAVELING
            travel_hours = TRAVEL_THRESHOLDS["travel_duration_hours"]
            pet.status_expire_time = now + timedelta(hours=travel_hours)
            pet.last_update = now
            if not self._persist_pet_candidate(target_pet, pet):
                return None
            recall_coins = int(TRAVEL_THRESHOLDS["recall_cost_coins"])
            return (
                f"😿 {pet.name}因为照顾不周，离家旅行了...\n"
                f"它将在{travel_hours}小时后自动回来\n"
                f"或者使用 /宠物 召回 提前召回（需要{recall_coins}金币）"
            )

        pet.last_update = now
        if self._persist_pet_candidate(target_pet, pet):
            return status_message
        return None

    # ──────────────────── 改名 ────────────────────

    def rename_pet(self, pet: Pet, new_name: str) -> tuple[bool, str]:
        group_config = self.db.get_group_config(pet.group_id)
        valid, message = validate_pet_name(new_name, group_config.sensitive_words)
        if not valid:
            return False, message

        old_name = pet.name
        candidate = replace(pet)
        candidate.name = new_name

        if self._persist_pet_candidate(pet, candidate):
            return True, f"宠物已从{old_name}改名为{new_name}"
        return False, "改名失败"

    # ──────────────────── 使用加速卡 ────────────────────

    def use_acceleration_card(self, pet: Pet, user: User) -> tuple[bool, str]:
        """原子扣除一张加速卡并增加宠物经验。"""
        exp_gain = int(DEFAULT_ITEMS["acceleration_card"]["exp_gain"])
        updated_pet = self.db.use_acceleration_card_atomic(
            user.user_id,
            user.group_id,
            pet.id,
            exp_gain,
        )
        if updated_pet is not None:
            self._sync_state(pet, updated_pet)
            extra_msg = self._evolution_suffix(pet)
            return True, f"使用加速卡成功！{pet.name}获得了{exp_gain}经验{extra_msg}"
        return False, "背包中没有加速卡或使用失败"

    # ──────────────────── 使用托管券 ────────────────────

    def use_trusteeship_coupon(self, pet: Pet, user: User) -> tuple[bool, str]:
        """原子扣除托管券，并在指定时间内将衰减速度减半。"""
        inventory = self.db.get_or_create_inventory(user.user_id, user.group_id)
        if not inventory.has_item("trusteeship_coupon"):
            return False, "背包中没有托管券"

        candidate_pet = replace(pet)
        candidate_user = replace(user)
        candidate_inventory = replace(inventory, items=dict(inventory.items))
        hours = int(DEFAULT_ITEMS["trusteeship_coupon"]["trustee_hours"])
        candidate_user.trustee_until = utc_now() + timedelta(hours=hours)
        candidate_inventory.remove_item("trusteeship_coupon")

        if self.db.atomic_update_pet_and_user(
            candidate_pet,
            candidate_user,
            inventory=candidate_inventory,
        ):
            self._sync_state(pet, candidate_pet)
            self._sync_state(user, candidate_user)
            self._sync_state(inventory, candidate_inventory)
            return (
                True,
                f"托管券使用成功！{pet.name}将在{hours}小时内由系统代为照顾（衰减速度减半）",
            )
        return False, "使用失败"

    # ──────────────────── 召回旅行中的宠物 ────────────────────

    def recall_pet(self, pet: Pet, user: User) -> tuple[bool, str]:
        """原子扣除金币并恢复旅行中宠物的可互动状态。"""
        if pet.status != PetStatus.TRAVELING:
            return False, "宠物没有在旅行中"

        recall_coins = int(TRAVEL_THRESHOLDS["recall_cost_coins"])

        if user.coins < recall_coins:
            return False, f"金币不足，召回需要{recall_coins}金币"

        candidate_pet = replace(pet)
        candidate_user = replace(user)
        candidate_user.coins -= recall_coins

        candidate_pet.status = PetStatus.NORMAL
        candidate_pet.status_expire_time = None
        candidate_pet.hunger = 60
        candidate_pet.mood = 60
        candidate_pet.clean = 60
        candidate_pet.energy = 60
        candidate_pet.health = 80
        candidate_pet.last_update = utc_now()

        if self.db.atomic_update_pet_and_user(candidate_pet, candidate_user):
            self._sync_state(pet, candidate_pet)
            self._sync_state(user, candidate_user)
            return True, f"🎉 {pet.name}被成功召回了！花费{recall_coins}金币"
        return False, "召回失败"
