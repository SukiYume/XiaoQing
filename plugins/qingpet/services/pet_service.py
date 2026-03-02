import logging
import random
from datetime import datetime, timedelta
from typing import Optional, Tuple

from ..models import Pet, User
from ..utils.constants import (
    PetStage, PetPersonality, PetStatus, MAX_STAT_VALUE, MIN_STAT_VALUE,
    DECAY_RATES, COOLDOWN_TIMES, EVOLUTION_CONDITIONS, DEFAULT_ITEMS,
    DISEASE_THRESHOLDS, TRAVEL_THRESHOLDS, AGE_EVOLUTION_THRESHOLDS,
    DAILY_LIMITS, FAVORITE_FOOD_BONUS
)
from ..utils.validators import validate_cooling, validate_sensitive_content
from .database import Database

logger = logging.getLogger(__name__)


class PetService:
    def __init__(self, db: Database):
        self.db = db

    # ──────────────────── 领养 ────────────────────

    def adopt_pet(self, user_id: str, group_id: int, name: str) -> Tuple[bool, str]:
        if len(name) > 20:
            return False, "宠物名字不能超过20个字符"

        # 敏感词检查
        group_config = self.db.get_group_config(group_id)
        ok, err = validate_sensitive_content(name, group_config.sensitive_words)
        if not ok:
            return False, err

        existing_pet = self.db.get_pet(user_id, group_id)
        if existing_pet:
            return False, "你已经有一只宠物了，每个用户只能养一只"

        # 同时创建用户记录（Issue #6: handle_adopt 未创建 User 记录）
        from .user_service import UserService
        user_service = UserService(self.db)
        user_service.get_or_create_user(user_id, group_id)

        pet = Pet(
            id=0,
            user_id=user_id,
            group_id=group_id,
            name=name,
            stage=PetStage.EGG,
            personality=random.choice(list(PetPersonality))
        )

        success = self.db.create_pet(pet)
        if success:
            return True, f"恭喜！你领养了一颗{name}的宠物蛋，快去孵化吧！"
        return False, "领养失败，请稍后重试"

    # ──────────────────── 孵化 ────────────────────

    def hatch_egg(self, pet: Pet) -> Tuple[bool, str]:
        if pet.stage != PetStage.EGG:
            return False, "只有宠物蛋才能孵化"

        if pet.experience < 10:
            return False, "宠物蛋还需要更多经验才能孵化，继续互动吧"

        pet.stage = PetStage.YOUNG
        pet.experience = pet.experience - 10
        pet.last_update = datetime.now()

        success = self.db.update_pet(pet)
        if success:
            return True, f"破壳啦！{pet.name}变成了{pet.stage.value}"
        return False, "孵化失败"

    # ──────────────────── 喂食 ────────────────────

    def feed_pet(self, pet: Pet, user: User, item_id: Optional[str] = None,
                 spam_decay_factor: float = 1.0) -> Tuple[bool, str, int]:
        """CR Review: 应用反脚本衰减因子，实现免费喂食限制，启用喜好食物加成"""
        if not pet.can_interact():
            if pet.is_traveling():
                return False, "宠物正在旅行中，请先召回它\n用法: /宠物 召回", 0
            return False, "宠物现在无法互动", 0

        cooled, remaining = validate_cooling(pet.last_feed, COOLDOWN_TIMES["feed"])
        if not cooled:
            return False, f"喂食冷却中，请等待{remaining}秒", 0

        actual_item_id = item_id if item_id else "apple"
        item_data = DEFAULT_ITEMS.get(actual_item_id)
        if not item_data:
            return False, f"道具 '{actual_item_id}' 不存在", 0

        inventory = self.db.get_or_create_inventory(user.user_id, user.group_id)
        free_apple_hint = ""
        if not inventory.has_item(actual_item_id):
            # CR Review #6: 免费苹果每日限制
            if actual_item_id != "apple":
                return False, f"背包中没有 {item_data['name']}，请先购买", 0
            # 检查免费喂食次数是否超限
            free_feed_limit = DAILY_LIMITS.get("free_feed", 5)
            if not user.can_do_action("free_feed", 1, free_feed_limit):
                return False, f"今日免费喂食次数已达上限({free_feed_limit}次)，请购买食物后喂食", 0
            user.increment_action("free_feed")
            free_left = max(0, free_feed_limit - user.today_free_feed_count)
            free_apple_hint = f"\n🍎 今日免费苹果剩余: {free_left}/{free_feed_limit}"
        else:
            inventory.remove_item(actual_item_id)
            self.db.update_inventory(inventory)

        hunger_gain = item_data.get("hunger_gain", 0)
        mood_gain = item_data.get("mood_gain", 0)
        exp_gain = item_data.get("exp_gain", 0)
        intimacy_gain = item_data.get("intimacy_gain", 0)

        # CR Review #8: 喜好食物加成（favorite_food 之前未使用）
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
        pet.last_feed = datetime.now()
        pet.last_update = datetime.now()

        group_config = self.db.get_group_config(pet.group_id)
        # CR Review Issue #2: 应用反脚本衰减因子
        coins_gain = int(5 * group_config.economy_multiplier * spam_decay_factor)
        if user.can_earn_coins(coins_gain, 500):
            user.coins += coins_gain
            user.today_coins_earned += coins_gain
            user.increment_action("feed")
        else:
            coins_gain = 0

        success = self.db.atomic_update_pet_and_user(pet, user)

        self.db.update_task_progress(user.user_id, pet.group_id, "feed")
        self.db.update_group_task_progress(pet.group_id, "group_feed")

        if success:
            evo_success, evo_msg = self.check_evolution(pet)
            extra_msg = f"\n\n{evo_msg}" if evo_success else ""
            fav_msg = " 💖喂了喜欢的食物！" if pet.favorite_food and actual_item_id == pet.favorite_food else ""
            return True, f"喂食成功！{pet.name}很开心，获得{exp_gain}经验{fav_msg}{free_apple_hint}{extra_msg}", coins_gain
        return False, "喂食失败", 0

    # ──────────────────── 清洁 ────────────────────

    def clean_pet(self, pet: Pet, user: User,
                  spam_decay_factor: float = 1.0) -> Tuple[bool, str, int]:
        if not pet.can_interact():
            return False, "宠物现在无法互动", 0

        cooled, remaining = validate_cooling(pet.last_clean, COOLDOWN_TIMES["clean"])
        if not cooled:
            return False, f"清洁冷却中，请等待{remaining}秒", 0

        clean_gain = 20
        health_gain = 5

        pet.update_stat("clean", clean_gain)
        pet.update_stat("health", health_gain)
        pet.last_clean = datetime.now()
        pet.last_update = datetime.now()

        group_config = self.db.get_group_config(pet.group_id)
        # CR Review Issue #2: 应用反脚本衰减因子
        coins_gain = int(3 * group_config.economy_multiplier * spam_decay_factor)
        if user.can_earn_coins(coins_gain, 500):
            user.coins += coins_gain
            user.today_coins_earned += coins_gain
            user.increment_action("clean")
        else:
            coins_gain = 0

        success = self.db.atomic_update_pet_and_user(pet, user)
        self.db.update_task_progress(user.user_id, pet.group_id, "clean")
        self.db.update_group_task_progress(pet.group_id, "group_clean")

        if success:
            evo_success, evo_msg = self.check_evolution(pet)
            extra_msg = f"\n\n{evo_msg}" if evo_success else ""
            return True, f"清洁完成！{pet.name}变得香喷喷的{extra_msg}", coins_gain
        return False, "清洁失败", 0

    # ──────────────────── 玩耍 ────────────────────

    def play_with_pet(self, pet: Pet, user: User,
                      spam_decay_factor: float = 1.0) -> Tuple[bool, str, int]:
        if not pet.can_interact():
            return False, "宠物现在无法互动", 0

        cooled, remaining = validate_cooling(pet.last_play, COOLDOWN_TIMES["play"])
        if not cooled:
            return False, f"玩耍冷却中，请等待{remaining}秒", 0

        mood_gain = 15
        intimacy_gain = 2
        energy_cost = 10

        pet.update_stat("mood", mood_gain)
        pet.update_stat("energy", -energy_cost, min_val=0)
        pet.intimacy += intimacy_gain
        pet.last_play = datetime.now()
        pet.last_update = datetime.now()

        group_config = self.db.get_group_config(pet.group_id)
        coins_gain = int(5 * group_config.economy_multiplier * spam_decay_factor)
        if user.can_earn_coins(coins_gain, 500):
            user.coins += coins_gain
            user.today_coins_earned += coins_gain
            user.increment_action("play")
        else:
            coins_gain = 0

        success = self.db.atomic_update_pet_and_user(pet, user)
        self.db.update_task_progress(user.user_id, pet.group_id, "play")

        if success:
            evo_success, evo_msg = self.check_evolution(pet)
            extra_msg = f"\n\n{evo_msg}" if evo_success else ""
            return True, f"玩得很开心！{pet.name}的亲密度提升了{extra_msg}", coins_gain
        return False, "玩耍失败", 0

    # ──────────────────── 训练 ────────────────────

    def train_pet(self, pet: Pet, user: User,
                  spam_decay_factor: float = 1.0) -> Tuple[bool, str, int]:
        if not pet.can_interact():
            return False, "宠物现在无法互动", 0

        cooled, remaining = validate_cooling(pet.last_train, COOLDOWN_TIMES["train"])
        if not cooled:
            return False, f"训练冷却中，请等待{remaining}秒", 0

        if pet.energy < 20:
            return False, "宠物精力不足，无法训练", 0

        exp_gain = 15
        energy_cost = 20

        success_rate = 0.8
        if pet.personality == PetPersonality.SMART:
            success_rate = 0.95

        if random.random() > success_rate:
            pet.update_stat("energy", -energy_cost, min_val=0)
            pet.last_train = datetime.now()
            pet.last_update = datetime.now()
            self.db.update_pet(pet)
            return True, "训练失败，但不要灰心，再试一次吧！", 0

        pet.experience += exp_gain
        pet.update_stat("energy", -energy_cost, min_val=0)
        pet.last_train = datetime.now()
        pet.last_update = datetime.now()

        group_config = self.db.get_group_config(pet.group_id)
        coins_gain = int(10 * group_config.economy_multiplier * spam_decay_factor)
        if user.can_earn_coins(coins_gain, 500):
            user.coins += coins_gain
            user.today_coins_earned += coins_gain
            user.increment_action("train")
        else:
            coins_gain = 0

        success = self.db.atomic_update_pet_and_user(pet, user)
        if success:
            evo_success, evo_msg = self.check_evolution(pet)
            extra_msg = f"\n\n{evo_msg}" if evo_success else ""
            return True, f"训练成功！{pet.name}获得了{exp_gain}经验{extra_msg}", coins_gain
        return False, "训练失败", 0

    # ──────────────────── 探索 ────────────────────

    def explore(self, pet: Pet, user: User,
                spam_decay_factor: float = 1.0) -> Tuple[bool, str, int]:
        if not pet.can_interact():
            return False, "宠物现在无法互动", 0

        cooled, remaining = validate_cooling(pet.last_explore, COOLDOWN_TIMES["explore"])
        if not cooled:
            return False, f"探索冷却中，请等待{remaining}秒", 0

        if pet.energy < 30:
            return False, "宠物精力不足，无法探索", 0

        energy_cost = 30
        pet.update_stat("energy", -energy_cost, min_val=0)

        events: list[dict[str, int | str]] = [
            {"msg": "探索到了一些金币！", "coins": 20, "exp": 5},
            {"msg": "发现了一个宝藏！", "coins": 50, "exp": 10},
            {"msg": "遇到了小困难，但成功克服了", "coins": 5, "exp": 15},
            {"msg": "只是一次普通的探索", "coins": 10, "exp": 3},
            {"msg": "探索失败，什么也没找到", "coins": 0, "exp": 0}
        ]

        event = random.choice(events)
        group_config = self.db.get_group_config(pet.group_id)
        # CR Review Issue #2: 应用反脚本衰减因子
        event_coins = int(event["coins"])
        exp_gain = int(event["exp"])
        coins_gain = int(event_coins * group_config.economy_multiplier * spam_decay_factor)

        pet.experience += exp_gain
        pet.last_explore = datetime.now()
        pet.last_update = datetime.now()

        if user.can_earn_coins(coins_gain, 500):
            user.coins += coins_gain
            user.today_coins_earned += coins_gain
            user.increment_action("explore")
        else:
            coins_gain = 0

        self.db.update_group_task_progress(pet.group_id, "group_explore")
        success = self.db.atomic_update_pet_and_user(pet, user)
        if success:
            evo_success, evo_msg = self.check_evolution(pet)
            extra_msg = f"\n\n{evo_msg}" if evo_success else ""
            return True, f"{event['msg']} 获得{exp_gain}经验{extra_msg}", coins_gain
        return False, "探索失败", 0

    # ──────────────────── 睡觉 / 起床 ────────────────────

    def sleep_pet(self, pet: Pet) -> Tuple[bool, str]:
        if not pet.can_interact():
            return False, "宠物现在无法互动"

        if pet.status == PetStatus.SLEEPING:
            return False, "宠物已经在睡觉了"

        pet.status = PetStatus.SLEEPING
        pet.last_update = datetime.now()

        success = self.db.update_pet(pet)
        if success:
            return True, f"{pet.name}开始睡觉了，Zzz..."
        return False, "让宠物睡觉失败"

    def wake_pet(self, pet: Pet) -> Tuple[bool, str]:
        if pet.status != PetStatus.SLEEPING:
            return False, "宠物现在没有在睡觉"

        pet.status = PetStatus.NORMAL
        pet.update_stat("energy", 50)
        pet.last_update = datetime.now()

        success = self.db.update_pet(pet)
        if success:
            return True, f"{pet.name}睡醒了，精神饱满！"
        return False, "唤醒宠物失败"

    # ──────────────────── 进化检查（Issue #10: 之前从未被调用）────────────────────

    def check_evolution(self, pet: Pet) -> Tuple[bool, str]:
        """CR Review Issue #5: 进化现在同时检查经验和年龄阈值"""
        if pet.stage == PetStage.OLD:
            return False, ""

        care_score = pet.care_score

        if pet.stage == PetStage.EGG:
            if pet.experience >= 10:
                return self.hatch_egg(pet)
            return False, ""

        # 经验阈值
        exp_thresholds = {
            PetStage.YOUNG: 50,
            PetStage.GROWTH: 100,
            PetStage.MATURE: 150,
        }
        threshold = exp_thresholds.get(pet.stage, 999)

        # CR Review Issue #5: 同时检查年龄阈值（之前 AGE_EVOLUTION_THRESHOLDS 已定义但未使用）
        age_threshold = AGE_EVOLUTION_THRESHOLDS.get(pet.stage, 999)
        meets_age = pet.age >= age_threshold
        meets_exp = pet.experience >= threshold

        # 需要同时满足经验和年龄条件才能进化
        if meets_exp and meets_age:
            if care_score >= 0.8:
                condition = "excellent_care"
            elif care_score >= 0.6:
                condition = "good_care"
            else:
                condition = "poor_care"

            if (pet.stage, condition) in EVOLUTION_CONDITIONS:
                new_stage, new_form = EVOLUTION_CONDITIONS[(pet.stage, condition)]
                old_stage = pet.stage.value
                pet.stage = new_stage
                pet.form = new_form
                pet.experience = 0

                success = self.db.update_pet(pet)
                if success:
                    return True, f"🎉 恭喜！{pet.name}从{old_stage}进化成了{new_stage.value}({new_form})！"

        return False, ""

    # ──────────────────── 衰减（含疾病概率系统 Issue #41）────────────────────

    def apply_decay(self, pet: Pet, decay_multiplier: float = 1.0) -> Optional[str]:
        """应用状态衰减，并检查疾病概率。返回警报消息或None。"""
        now = datetime.now()
        elapsed_minutes = max(0.0, (now - pet.last_update).total_seconds() / 60.0)
        if elapsed_minutes < 1.0:
            return None

        if pet.status == PetStatus.SLEEPING:
            energy_gain = int(2 * elapsed_minutes)
            if energy_gain <= 0:
                return None
            pet.update_stat("energy", energy_gain)
            pet.last_update = now
            self.db.update_pet(pet)
            return None

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
                self.db.update_pet(pet)
                return f"🎒 {pet.name}旅行回来了！快去照顾它吧！"
            return None

        if pet.status != PetStatus.NORMAL:
            return None

        user = self.db.get_user(pet.user_id, pet.group_id)
        is_trustee = user and user.is_trustee_active()
        actual_multiplier = decay_multiplier * (0.5 if is_trustee else 1.0)

        changed = False
        for stat, rate in DECAY_RATES.items():
            decay = int(rate * actual_multiplier * elapsed_minutes)
            if decay > 0:
                pet.update_stat(stat, -decay, min_val=0)
                changed = True

        if not changed:
            return None

        alert_msg = None

        # 疾病概率系统
        if pet.clean < DISEASE_THRESHOLDS["clean_threshold"]:
            if random.random() < DISEASE_THRESHOLDS["disease_chance"]:
                pet.status = PetStatus.SICK
                # CR Fix: 生病时降低健康值，迫使需要治疗
                pet.health = 30
                pet.last_update = now
                self.db.update_pet(pet)
                return f"🤒 {pet.name}因为环境太脏生病了！健康值大幅下降，快使用'/宠物 治疗'！"

        if pet.health <= 0:
            pet.status = PetStatus.SICK
            pet.health = 10

        # CR New: 温和旅行机制（替代直接死亡）
        if pet.care_score < TRAVEL_THRESHOLDS["care_score_threshold"]:
            pet.status = PetStatus.TRAVELING
            travel_hours = TRAVEL_THRESHOLDS["travel_duration_hours"]
            pet.status_expire_time = now + timedelta(hours=travel_hours)
            pet.last_update = now
            self.db.update_pet(pet)
            return (f"😿 {pet.name}因为照顾不周，离家旅行了...\n"
                    f"它将在{travel_hours}小时后自动回来\n"
                    f"或者使用 /宠物 召回 提前召回（需要金币和友情点）")

        pet.last_update = now
        self.db.update_pet(pet)
        return alert_msg

    # ──────────────────── 改名 ────────────────────

    def rename_pet(self, pet: Pet, new_name: str) -> Tuple[bool, str]:
        if len(new_name) > 20:
            return False, "宠物名字不能超过20个字符"

        # 敏感词检查
        group_config = self.db.get_group_config(pet.group_id)
        ok, err = validate_sensitive_content(new_name, group_config.sensitive_words)
        if not ok:
            return False, err

        old_name = pet.name
        pet.name = new_name

        success = self.db.update_pet(pet)
        if success:
            return True, f"宠物已从{old_name}改名为{new_name}"
        return False, "改名失败"

    # ──────────────────── 使用加速卡 ────────────────────

    def use_acceleration_card(self, pet: Pet, user: User) -> Tuple[bool, str]:
        """使用加速卡加给宠物加经验"""
        from .item_service import ItemService
        item_service = ItemService(self.db)
        inventory = item_service.get_inventory(user.user_id, user.group_id)
        if not inventory.has_item("acceleration_card"):
            return False, "背包中没有加速卡"

        exp_gain = DEFAULT_ITEMS["acceleration_card"]["exp_gain"]
        pet.experience += exp_gain
        inventory.remove_item("acceleration_card")

        success = self.db.update_pet(pet) and self.db.update_inventory(inventory)
        if success:
            evo_success, evo_msg = self.check_evolution(pet)
            extra_msg = f"\n\n{evo_msg}" if evo_success else ""
            return True, f"使用加速卡成功！{pet.name}获得了{exp_gain}经验{extra_msg}"
        return False, "使用失败"

    # ──────────────────── 使用托管券 ────────────────────

    def use_trusteeship_coupon(self, pet: Pet, user: User) -> Tuple[bool, str]:
        """使用托管券，在指定时间内衰减减半"""
        from .item_service import ItemService
        item_service = ItemService(self.db)
        inventory = item_service.get_inventory(user.user_id, user.group_id)
        if not inventory.has_item("trusteeship_coupon"):
            return False, "背包中没有托管券"

        hours = DEFAULT_ITEMS["trusteeship_coupon"]["trustee_hours"]
        user.trustee_until = datetime.now() + timedelta(hours=hours)
        inventory.remove_item("trusteeship_coupon")

        success = self.db.update_user(user) and self.db.update_inventory(inventory)
        if success:
            return True, f"托管券使用成功！{pet.name}将在{hours}小时内由系统代为照顾（衰减速度减半）"
        return False, "使用失败"

    # ──────────────────── 召回旅行中的宠物（新增）────────────────────

    def recall_pet(self, pet: Pet, user: User) -> Tuple[bool, str]:
        """召回旅行中的宠物"""
        if not pet.is_traveling():
            return False, "宠物没有在旅行中"

        recall_coins = int(TRAVEL_THRESHOLDS["recall_cost_coins"])
        recall_fp = int(TRAVEL_THRESHOLDS["recall_cost_friendship"])

        if user.coins < recall_coins:
            return False, f"金币不足，召回需要{recall_coins}金币"
        if user.friendship_points < recall_fp:
            return False, f"友情点不足，召回需要{recall_fp}友情点"

        user.coins -= recall_coins
        user.friendship_points -= recall_fp

        pet.status = PetStatus.NORMAL
        pet.status_expire_time = None
        pet.hunger = 60
        pet.mood = 60
        pet.clean = 60
        pet.energy = 60
        pet.health = 80
        pet.last_update = datetime.now()

        success = self.db.atomic_update_pet_and_user(pet, user)
        if success:
            return True, f"🎉 {pet.name}被成功召回了！花费{recall_coins}金币和{recall_fp}友情点"
        return False, "召回失败"
