"""青宠领域枚举、数值规则和内置内容配置。"""

import enum
from typing import TypedDict


class _TitleConfig(TypedDict):
    """称号展示文案与可选有效期。"""

    description: str
    duration_days: int | None


class _TradeConfig(TypedDict):
    """交易市场的数值边界。"""

    tax_rate: float
    min_price: int
    max_price: int
    max_listings: int
    listing_expire_hours: int


class _PetShowConfig(TypedDict):
    """宠物展示会的时长、投票上限和奖励。"""

    duration_hours: int
    max_votes_per_user: int
    reward_first: int
    reward_second: int
    reward_third: int


class _TrainingConfig(TypedDict):
    """单项训练的基础消耗与收益。"""

    name: str
    exp_gain: int
    energy_cost: int
    extra_effects: dict[str, int]
    success_rate_base: float


class _TrainingSpecialEvent(TypedDict, total=False):
    """训练中随机触发的可选附加效果。"""

    msg: str
    intimacy: int
    exp_multiplier: float
    prob: float


class PetStage(enum.Enum):
    EGG    = "蛋"
    YOUNG  = "幼年"
    GROWTH = "成长期"
    MATURE = "成熟期"
    OLD    = "老年"


class PetPersonality(enum.Enum):
    LIVELY  = "活泼"
    CLINGY  = "粘人"
    SHY     = "怕生"
    CALM    = "温顺"
    NAUGHTY = "调皮"
    SMART   = "聪明"


class PetStatus(enum.Enum):
    """宠物运行状态的唯一枚举定义。"""

    NORMAL    = "正常"
    SICK      = "生病"
    SLEEPING  = "睡觉中"
    TRAVELING = "旅行中"
    DEAD      = "已死亡"


class ItemType(enum.Enum):
    FOOD         = "食物"
    TOY          = "玩具"
    MEDICINE     = "药品"
    DECORATION   = "装饰"
    ACCELERATION = "加速卡"
    TRUSTEESHIP  = "托管券"


class ItemRarity(enum.Enum):
    COMMON    = "普通"
    RARE      = "稀有"
    EPIC      = "史诗"
    LEGENDARY = "传说"


# 宠物装扮槽位
class DressSlot(enum.Enum):
    HAT        = "帽子"
    CLOTHES    = "衣服"
    ACCESSORY  = "饰品"
    BACKGROUND = "背景"


MAX_STAT_VALUE = 100

# 每次衰减量（每分钟调用一次）
DECAY_RATES: dict[str, float] = {
    "hunger": 0.5,
    "mood": 0.8,
    "clean": 0.6,
    "energy": 0.3,
    "health": 0.1,
}

# 冷却时间（秒）
COOLDOWN_TIMES: dict[str, int] = {
    "feed": 60,
    "clean": 180,
    "play": 120,
    "train": 600,
    "explore": 1800,
    "treat": 300,
    "visit": 3600,
    "gift": 600,
}

# 每日次数上限
DAILY_LIMITS: dict[str, int] = {
    "coins": 500,
    "feed": 20,
    "clean": 10,
    "play": 15,
    "train": 5,
    "explore": 3,
    "visit": 5,
    "gift": 3,
    "like_per_target": 3,  # 每人每日对同一宠物的点赞次数限制
    "message": 10,  # 每日留言次数限制
    "free_feed": 5,  # 每日免费苹果喂食次数限制
}

EVOLUTION_CONDITIONS: dict[tuple[PetStage, str], tuple[PetStage, str]] = {
    (PetStage.EGG, "hatched"): (PetStage.YOUNG, "破壳"),
    (PetStage.YOUNG, "excellent_care"): (PetStage.GROWTH, "优秀"),
    (PetStage.YOUNG, "good_care"): (PetStage.GROWTH, "良好"),
    (PetStage.YOUNG, "poor_care"): (PetStage.GROWTH, "普通"),
    (PetStage.GROWTH, "excellent_care"): (PetStage.MATURE, "精英"),
    (PetStage.GROWTH, "good_care"): (PetStage.MATURE, "成熟"),
    (PetStage.GROWTH, "poor_care"): (PetStage.MATURE, "平凡"),
    (PetStage.MATURE, "aged"): (PetStage.OLD, "长寿"),
}

# 每个非终态能够由业务规则生成的事件。此表既是状态机契约，也用于启动时
# 检查转移表，避免新增阶段或重命名事件后产生正常玩法永远不可达的分支。
EVOLUTION_EVENTS_BY_STAGE: dict[PetStage, frozenset[str]] = {
    PetStage.EGG: frozenset({"hatched"}),
    PetStage.YOUNG: frozenset({"excellent_care", "good_care", "poor_care"}),
    PetStage.GROWTH: frozenset({"excellent_care", "good_care", "poor_care"}),
    PetStage.MATURE: frozenset({"aged"}),
}

EVOLUTION_EXPERIENCE_THRESHOLDS: dict[PetStage, int] = {
    PetStage.EGG: 10,
    PetStage.YOUNG: 50,
    PetStage.GROWTH: 100,
    PetStage.MATURE: 150,
}

# 年龄触发进化的阈值（天）
AGE_EVOLUTION_THRESHOLDS: dict[PetStage, int] = {
    PetStage.EGG: 1,  # 1天后有资格孵化
    PetStage.YOUNG: 7,  # 7天后有资格进入成长期
    PetStage.GROWTH: 21,  # 21天后有资格进入成熟期
    PetStage.MATURE: 60,  # 60天后进入老年
}


def validate_evolution_state_machine() -> None:
    """在导入时验证每个非终态都有业务可生成且定义完整的转移。"""
    non_terminal_stages = set(PetStage) - {PetStage.OLD}
    configured_stages   = set(EVOLUTION_EVENTS_BY_STAGE)
    if configured_stages != non_terminal_stages:
        missing = sorted(stage.name for stage in non_terminal_stages - configured_stages)
        extra   = sorted(stage.name for stage in configured_stages - non_terminal_stages)
        raise RuntimeError(f"进化事件阶段配置不完整: missing={missing}, extra={extra}")

    for stage, generated_events in EVOLUTION_EVENTS_BY_STAGE.items():
        if not generated_events:
            raise RuntimeError(f"进化阶段 {stage.name} 没有可生成事件")
        for event in generated_events:
            transition = EVOLUTION_CONDITIONS.get((stage, event))
            if transition is None:
                raise RuntimeError(f"进化事件没有转移: {stage.name}/{event}")
            next_stage, _form = transition
            if next_stage == stage:
                raise RuntimeError(f"进化事件不能自循环: {stage.name}/{event}")

    threshold_stages = set(EVOLUTION_EXPERIENCE_THRESHOLDS)
    if threshold_stages != non_terminal_stages:
        raise RuntimeError("进化经验门槛必须覆盖所有非终态")
    if set(AGE_EVOLUTION_THRESHOLDS) != non_terminal_stages:
        raise RuntimeError("进化年龄门槛必须覆盖所有非终态")


validate_evolution_state_machine()

# 疾病概率：清洁度低于阈值时，每次衰减有概率生病
DISEASE_THRESHOLDS: dict[str, float] = {
    "clean_threshold": 30,  # 清洁度低于此值有概率生病
    "disease_chance": 0.05,  # 每次衰减检查5%概率生病
}

# 低照料状态触发旅行及召回的参数
TRAVEL_THRESHOLDS: dict[str, float] = {
    "care_score_threshold": 0.15,  # 综合评分低于15%触发旅行
    "travel_duration_hours": 24,  # 旅行持续时间（小时）
    "recall_cost_coins": 50,  # 召回费用
}

# 反脚本配置
ANTI_SPAM_CONFIG = {
    "window_seconds": 60,  # 时间窗口（秒）
    "max_commands": 10,  # 窗口内最大命令数
    "hard_block_commands": 20,  # 超过该值才彻底拦截，中间区间只做收益衰减
    "exponential_decay_base": 0.5,  # 超出后金币收益的衰减因子
}

# 群级响应频率限制
GROUP_RATE_LIMIT = {
    "window_seconds": 10,  # 时间窗口
    "max_responses": 5,  # 窗口内最大响应数
}

# 小游戏配置
MINIGAME_CONFIG = {
    "rock_paper_scissors": {
        "win_coins": 15,
        "draw_coins": 5,
        "lose_coins": 0,
        "win_exp": 5,
        "cooldown": 60,
    },
    "dice": {
        "win_coins": 20,
        "lose_coins": 0,
        "win_exp": 8,
        "cooldown": 120,
    },
    "race": {
        "win_coins": 30,
        "second_coins": 15,
        "lose_coins": 0,
        "win_exp": 10,
        "cooldown": 300,
        "energy_cost": 15,
    },
}

# 称号可配置有效期；永久称号使用空的期限语义。
TITLES: dict[str, _TitleConfig] = {
    "新手铲屎官": {
        "description": "初次领养宠物",
        "duration_days": None,
    },  # 永久
    "勤劳养育员": {
        "description": "累计喂食100次",
        "duration_days": None,
    },
    "亲密伙伴": {
        "description": "宠物亲密度达到100",
        "duration_days": None,
    },
    "探索先锋": {
        "description": "累计探索50次",
        "duration_days": None,
    },
    "社交达人": {
        "description": "累计互访50次",
        "duration_days": None,
    },
    "慷慨之友": {
        "description": "累计送礼30次",
        "duration_days": None,
    },
    "宠物大师": {
        "description": "养育出精英成熟期宠物",
        "duration_days": None,
    },
    "百万富翁": {
        "description": "拥有10000金币",
        "duration_days": None,
    },
    # 时效性称号（活动发放）
    "展示会冠军": {
        "description": "宠物展示会第一名",
        "duration_days": 7,
    },
    "本周之星": {"description": "本周活动第一名", "duration_days": 7},
}

# 只有实际喂入宠物喜好食物时才应用以下加成。
FAVORITE_FOOD_BONUS = {
    "hunger_multiplier": 1.5,  # 喂食喜好食物时，饥饿恢复×1.5
    "mood_multiplier": 2.0,  # 心情×2.0
    "exp_multiplier": 1.5,  # 经验×1.5
}

# 默认敏感词列表；部署方可在此基础上叠加自定义规则。
DEFAULT_SENSITIVE_WORDS = [
    "傻逼",
    "sb",
    "操你",
    "fuck",
    "shit",
    "死全家",
    "去死",
]

# 受控交易市场参数
TRADE_CONFIG: _TradeConfig = {
    "tax_rate": 0.05,  # 交易税率 5%
    "min_price": 1,  # 最低挂单价格
    "max_price": 10000,  # 最高挂单价格
    "max_listings": 5,  # 每人最大挂单数
    "listing_expire_hours": 72,  # 挂单过期时间（小时）
}

# 宠物展示会参数
PET_SHOW_CONFIG: _PetShowConfig = {
    "duration_hours": 48,  # 展示会持续时间（小时）
    "max_votes_per_user": 3,  # 每人最多投票次数
    "reward_first": 200,  # 第一名奖励
    "reward_second": 100,  # 第二名奖励
    "reward_third": 50,  # 第三名奖励
}

# 默认装扮道具
DEFAULT_DRESS_ITEMS: dict[str, dict] = {
    "red_hat": {
        "name": "红色小帽",
        "slot": DressSlot.HAT,
        "rarity": ItemRarity.COMMON,
        "price": 50,
        "mood_bonus": 2,
    },
    "crown": {
        "name": "金色皇冠",
        "slot": DressSlot.HAT,
        "rarity": ItemRarity.EPIC,
        "price": 500,
        "mood_bonus": 10,
    },
    "scarf": {
        "name": "温暖围巾",
        "slot": DressSlot.CLOTHES,
        "rarity": ItemRarity.COMMON,
        "price": 40,
        "mood_bonus": 3,
    },
    "tuxedo": {
        "name": "燕尾服",
        "slot": DressSlot.CLOTHES,
        "rarity": ItemRarity.RARE,
        "price": 200,
        "mood_bonus": 8,
    },
    "ribbon": {
        "name": "彩色丝带",
        "slot": DressSlot.ACCESSORY,
        "rarity": ItemRarity.COMMON,
        "price": 30,
        "mood_bonus": 2,
    },
    "diamond_collar": {
        "name": "钻石项圈",
        "slot": DressSlot.ACCESSORY,
        "rarity": ItemRarity.LEGENDARY,
        "price": 1000,
        "mood_bonus": 15,
    },
    "starry_bg": {
        "name": "星空背景",
        "slot": DressSlot.BACKGROUND,
        "rarity": ItemRarity.RARE,
        "price": 150,
        "mood_bonus": 5,
    },
    "garden_bg": {
        "name": "花园背景",
        "slot": DressSlot.BACKGROUND,
        "rarity": ItemRarity.COMMON,
        "price": 80,
        "mood_bonus": 3,
    },
    # 友情点商店专属道具
    "halo": {
        "name": "天使光环",
        "slot": DressSlot.HAT,
        "rarity": ItemRarity.RARE,
        "price": 100,
        "currency": "friendship",  # 使用友情点计价
        "mood_bonus": 5,
    },
    "heart_bg": {
        "name": "爱心背景",
        "slot": DressSlot.BACKGROUND,
        "rarity": ItemRarity.EPIC,
        "price": 200,
        "currency": "friendship",
        "mood_bonus": 8,
    },
}

DEFAULT_ITEMS: dict[str, dict] = {
    "apple": {
        "name": "苹果",
        "type": ItemType.FOOD,
        "rarity": ItemRarity.COMMON,
        "price": 10,
        "hunger_gain": 15,
        "mood_gain": 5,
        "exp_gain": 2,
    },
    "cake": {
        "name": "蛋糕",
        "type": ItemType.FOOD,
        "rarity": ItemRarity.RARE,
        "price": 50,
        "hunger_gain": 30,
        "mood_gain": 15,
        "exp_gain": 5,
        "intimacy_gain": 3,
    },
    "meat": {
        "name": "肉干",
        "type": ItemType.FOOD,
        "rarity": ItemRarity.COMMON,
        "price": 15,
        "hunger_gain": 20,
        "mood_gain": 8,
        "exp_gain": 3,
    },
    "ball": {
        "name": "小球",
        "type": ItemType.TOY,
        "rarity": ItemRarity.COMMON,
        "price": 20,
        "mood_gain": 10,
    },
    "medicine": {
        "name": "药品",
        "type": ItemType.MEDICINE,
        "rarity": ItemRarity.COMMON,
        "price": 30,
        "health_gain": 20,
        "clean_gain": 10,
    },
    "rare_medicine": {
        "name": "稀有药品",
        "type": ItemType.MEDICINE,
        "rarity": ItemRarity.RARE,
        "price": 100,
        "health_gain": 50,
        "clean_gain": 20,
    },
    "acceleration_card": {
        "name": "加速卡",
        "type": ItemType.ACCELERATION,
        "rarity": ItemRarity.EPIC,
        "price": 200,
        "exp_gain": 50,
    },
    "trusteeship_coupon": {
        "name": "托管券",
        "type": ItemType.TRUSTEESHIP,
        "rarity": ItemRarity.EPIC,
        "price": 150,
        "trustee_hours": 8,
    },
}

# 每日群累计任务模板
GROUP_TASK_TEMPLATES = [
    {
        "type": "group_feed",
        "name": "全群累计喂食",
        "target": 50,
        "reward_coins": 20,
        "description": "全群累计喂食50次",
    },
    {
        "type": "group_clean",
        "name": "全群累计清洁",
        "target": 30,
        "reward_coins": 15,
        "description": "全群累计清洁30次",
    },
    {
        "type": "group_explore",
        "name": "全群累计探索",
        "target": 20,
        "reward_coins": 30,
        "description": "全群累计探索20次",
    },
]

# 训练系统配置
TRAINING_CONFIG: dict[str, _TrainingConfig] = {
    "strength": {
        "name": "体力训练",
        "exp_gain": 15,
        "energy_cost": 20,
        "extra_effects": {"health": 2},
        "success_rate_base": 0.8,
    },
    "agility": {
        "name": "敏捷训练",
        "exp_gain": 12,
        "energy_cost": 15,
        "extra_effects": {"mood": 8},
        "success_rate_base": 0.85,
    },
    "intellect": {
        "name": "智力训练",
        "exp_gain": 20,
        "energy_cost": 25,
        "extra_effects": {},
        "success_rate_base": 0.7,
    },
}

TRAINING_SPECIAL_EVENTS: list[_TrainingSpecialEvent] = [
    {"msg": "训练时偶然学会了新技巧！", "intimacy": 3, "prob": 0.1},
    {"msg": "超常发挥！经验额外×1.5", "exp_multiplier": 1.5, "prob": 0.1},
    {"msg": "训练完后对你撒娇", "intimacy": 5, "prob": 0.08},
]

TRAINING_MESSAGES = {
    "success": [
        "{name}认真完成了训练，有所成长！",
        "{name}今天训练很努力！",
        "{name}挥洒汗水，收获满满！",
    ],
    "fail": [
        "{name}今天状态不佳，训练没什么效果",
        "{name}偷懒了一下，下次加油！",
        "{name}训练时分心了，继续努力吧",
    ],
}

# 探索地点配置
EXPLORE_LOCATIONS: dict[str, dict] = {
    "forest": {
        "name": "森林",
        "energy_cost": 30,
        "events": [
            {"msg": "发现了野果！", "coins": 5, "exp": 3, "item": "apple", "prob": 0.30},
            {
                "msg": "遇到了友善的小动物，亲密度提升了",
                "coins": 10,
                "exp": 5,
                "intimacy": 3,
                "prob": 0.20,
            },
            {"msg": "在树丛中发现了金币", "coins": 20, "exp": 5, "prob": 0.20},
            {"msg": "迷路了，折腾了半天", "coins": 0, "exp": 2, "mood": -10, "prob": 0.15},
            {"msg": "平静地走了一圈", "coins": 8, "exp": 3, "prob": 0.15},
        ],
    },
    "beach": {
        "name": "海边",
        "energy_cost": 30,
        "events": [
            {"msg": "在沙滩上捡到了贝壳！", "coins": 30, "exp": 5, "prob": 0.25},
            {"msg": "海浪打来，心情大好！", "coins": 15, "exp": 8, "mood": 10, "prob": 0.25},
            {"msg": "捡到一个漂流瓶，里面有金币！", "coins": 50, "exp": 5, "prob": 0.10},
            {"msg": "被海浪打湿了，需要清洁", "coins": 5, "exp": 3, "clean": -15, "prob": 0.20},
            {"msg": "悠闲地在海边散步", "coins": 12, "exp": 4, "prob": 0.20},
        ],
    },
    "cave": {
        "name": "山洞",
        "energy_cost": 40,
        "events": [
            {"msg": "发现了宝箱！", "coins": 50, "exp": 10, "prob": 0.20},
            {"msg": "找到了奇怪的药草", "coins": 10, "exp": 8, "item": "medicine", "prob": 0.20},
            {"msg": "在黑暗中摸索，积累了经验", "coins": 15, "exp": 15, "prob": 0.20},
            {"msg": "遭遇危险！受了点伤", "coins": 5, "exp": 5, "health": -20, "prob": 0.25},
            {
                "msg": "找到了稀有药品！",
                "coins": 5,
                "exp": 10,
                "item": "rare_medicine",
                "prob": 0.15,
            },
        ],
    },
    "ruins": {
        "name": "废墟",
        "energy_cost": 40,
        "events": [
            {
                "msg": "捡到了神秘卡片！",
                "coins": 10,
                "exp": 15,
                "item": "acceleration_card",
                "prob": 0.10,
            },
            {"msg": "发现了大宝藏！", "coins": 100, "exp": 20, "prob": 0.10},
            {"msg": "废墟中的气氛把你吓到了", "coins": 0, "exp": 5, "mood": -20, "prob": 0.30},
            {"msg": "被废墟中的机关伤到了", "coins": 0, "exp": 8, "health": -30, "prob": 0.25},
            {"msg": "探索了废墟遗迹，有所发现", "coins": 20, "exp": 12, "prob": 0.25},
        ],
    },
}
