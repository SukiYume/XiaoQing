import enum
from typing import Dict, Tuple


class PetStage(enum.Enum):
    EGG = "蛋"
    YOUNG = "幼年"
    GROWTH = "成长期"
    MATURE = "成熟期"
    OLD = "老年"


class PetPersonality(enum.Enum):
    LIVELY = "活泼"
    CLINGY = "粘人"
    SHY = "怕生"
    CALM = "温顺"
    NAUGHTY = "调皮"
    SMART = "聪明"


class PetStatus(enum.Enum):
    """宠物状态枚举 - 唯一定义点，不要在其他地方重复定义"""
    NORMAL = "正常"
    SICK = "生病"
    SLEEPING = "睡觉中"
    TRAVELING = "旅行中"
    DEAD = "已死亡"


class ItemType(enum.Enum):
    FOOD = "食物"
    TOY = "玩具"
    MEDICINE = "药品"
    DECORATION = "装饰"
    ACCELERATION = "加速卡"
    TRUSTEESHIP = "托管券"


class ItemRarity(enum.Enum):
    COMMON = "普通"
    RARE = "稀有"
    EPIC = "史诗"
    LEGENDARY = "传说"


# 装扮槽位定义 (新增功能: 装扮系统)
class DressSlot(enum.Enum):
    HAT = "帽子"
    CLOTHES = "衣服"
    ACCESSORY = "饰品"
    BACKGROUND = "背景"


MAX_STAT_VALUE = 100
MIN_STAT_VALUE = 0

# 每次衰减量（每分钟调用一次）
DECAY_RATES: Dict[str, float] = {
    "hunger": 0.5,
    "mood": 0.8,
    "clean": 0.6,
    "energy": 0.3,
    "health": 0.1
}

# 冷却时间（秒）
COOLDOWN_TIMES: Dict[str, int] = {
    "feed": 60,
    "clean": 180,
    "play": 120,
    "train": 600,
    "explore": 1800,
    "treat": 300,
    "visit": 3600,
    "gift": 600
}

# 每日次数上限
DAILY_LIMITS: Dict[str, int] = {
    "coins": 500,
    "feed": 20,
    "clean": 10,
    "play": 15,
    "train": 5,
    "explore": 3,
    "visit": 5,
    "gift": 3,
    "like_per_target": 3,    # CR Review #9: 每人每日对同一宠物的点赞次数限制
    "message": 10,           # CR Review #8: 每日留言次数限制
    "free_feed": 5,          # CR Review #6: 每日免费苹果喂食次数限制
}

EVOLUTION_CONDITIONS: Dict[Tuple[PetStage, str], Tuple[PetStage, str]] = {
    (PetStage.EGG, "hatched"): (PetStage.YOUNG, "破壳"),
    (PetStage.YOUNG, "excellent_care"): (PetStage.GROWTH, "优秀"),
    (PetStage.YOUNG, "good_care"): (PetStage.GROWTH, "良好"),
    (PetStage.YOUNG, "poor_care"): (PetStage.GROWTH, "普通"),
    (PetStage.GROWTH, "excellent_care"): (PetStage.MATURE, "精英"),
    (PetStage.GROWTH, "good_care"): (PetStage.MATURE, "成熟"),
    (PetStage.GROWTH, "poor_care"): (PetStage.MATURE, "平凡"),
    (PetStage.MATURE, "aged"): (PetStage.OLD, "长寿"),
}

# 年龄触发进化的阈值（Day计）— CR Fix #8: age 从未递增
AGE_EVOLUTION_THRESHOLDS: Dict[PetStage, int] = {
    PetStage.EGG: 1,         # 1天后有资格孵化
    PetStage.YOUNG: 7,       # 7天后有资格进入成长期
    PetStage.GROWTH: 21,     # 21天后有资格进入成熟期
    PetStage.MATURE: 60,     # 60天后进入老年
}

# 疾病概率：清洁度低于阈值时，每次衰减有概率生病
DISEASE_THRESHOLDS: Dict[str, float] = {
    "clean_threshold": 30,   # 清洁度低于此值有概率生病
    "disease_chance": 0.05,  # 每次衰减检查5%概率生病
}

# 温和离家旅行阈值 (新增功能: 替代直接死亡)
TRAVEL_THRESHOLDS: Dict[str, float] = {
    "care_score_threshold": 0.15,   # 综合评分低于15%触发旅行
    "travel_duration_hours": 24,     # 旅行持续时间（小时）
    "recall_cost_coins": 50,         # 召回费用
    "recall_cost_friendship": 0,     # 召回所需友情点（已移除此要求）
}

# 反脚本配置
ANTI_SPAM_CONFIG = {
    "window_seconds": 60,          # 时间窗口（秒）
    "max_commands": 10,            # 窗口内最大命令数
    "hard_block_commands": 20,     # 超过该值才彻底拦截，中间区间只做收益衰减
    "exponential_decay_base": 0.5, # 超出后金币收益的衰减因子
}

# 群级响应频率限制
GROUP_RATE_LIMIT = {
    "window_seconds": 10,   # 时间窗口
    "max_responses": 5,     # 窗口内最大响应数
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
    }
}

# 称号系统 — CR Review #9: 添加时效性支持
TITLES = {
    "新手铲屎官": {"condition": "adopt_count >= 1", "description": "初次领养宠物", "duration_days": None},  # 永久
    "勤劳养育员": {"condition": "total_feed >= 100", "description": "累计喂食100次", "duration_days": None},
    "亲密伙伴": {"condition": "intimacy >= 100", "description": "宠物亲密度达到100", "duration_days": None},
    "探索先锋": {"condition": "total_explore >= 50", "description": "累计探索50次", "duration_days": None},
    "社交达人": {"condition": "total_visit >= 50", "description": "累计互访50次", "duration_days": None},
    "慷慨之友": {"condition": "total_gift >= 30", "description": "累计送礼30次", "duration_days": None},
    "宠物大师": {"condition": "stage == MATURE and care_score >= 0.9", "description": "养育出精英成熟期宠物", "duration_days": None},
    "百万富翁": {"condition": "coins >= 10000", "description": "拥有10000金币", "duration_days": None},
    # 时效性称号（活动发放）
    "展示会冠军": {"condition": "show_winner", "description": "宠物展示会第一名", "duration_days": 7},
    "本周之星": {"condition": "weekly_top", "description": "本周活动第一名", "duration_days": 7},
}

# 宠物喜好食物加成 — CR Review #8: favorite_food 未使用
FAVORITE_FOOD_BONUS = {
    "hunger_multiplier": 1.5,  # 喂食喜好食物时，饥饿恢复×1.5
    "mood_multiplier": 2.0,    # 心情×2.0
    "exp_multiplier": 1.5,     # 经验×1.5
}

# 敏感词列表（默认）— CR Issue #14: 添加基础预设敏感词
DEFAULT_SENSITIVE_WORDS = [
    "傻逼", "sb", "操你", "fuck", "shit", "死全家", "去死",
]

# 交易系统配置 (新增功能: 受控交易市场)
TRADE_CONFIG = {
    "tax_rate": 0.05,           # 交易税率 5%
    "min_price": 1,             # 最低挂单价格
    "max_price": 10000,         # 最高挂单价格
    "max_listings": 5,          # 每人最大挂单数
    "listing_expire_hours": 72, # 挂单过期时间（小时）
}

# 宠物展示会配置 (新增功能)
PET_SHOW_CONFIG = {
    "duration_hours": 48,       # 展示会持续时间（小时）
    "max_votes_per_user": 3,    # 每人最多投票次数
    "reward_first": 200,        # 第一名奖励
    "reward_second": 100,       # 第二名奖励
    "reward_third": 50,         # 第三名奖励
}

# 装扮系统道具 (新增功能)
DEFAULT_DRESS_ITEMS: Dict[str, Dict] = {
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
    # 友情点商店道具 (新增功能: 友情商店)
    "halo": {
        "name": "天使光环",
        "slot": DressSlot.HAT,
        "rarity": ItemRarity.RARE,
        "price": 100,
        "currency": "friendship",  # 新增货币类型字段
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

DEFAULT_ITEMS: Dict[str, Dict] = {
    "apple": {
        "name": "苹果",
        "type": ItemType.FOOD,
        "rarity": ItemRarity.COMMON,
        "price": 10,
        "hunger_gain": 15,
        "mood_gain": 5,
        "exp_gain": 2
    },
    "cake": {
        "name": "蛋糕",
        "type": ItemType.FOOD,
        "rarity": ItemRarity.RARE,
        "price": 50,
        "hunger_gain": 30,
        "mood_gain": 15,
        "exp_gain": 5,
        "intimacy_gain": 3
    },
    "meat": {
        "name": "肉干",
        "type": ItemType.FOOD,
        "rarity": ItemRarity.COMMON,
        "price": 15,
        "hunger_gain": 20,
        "mood_gain": 8,
        "exp_gain": 3
    },
    "ball": {
        "name": "小球",
        "type": ItemType.TOY,
        "rarity": ItemRarity.COMMON,
        "price": 20,
        "mood_gain": 10,
        "energy_cost": 5
    },
    "medicine": {
        "name": "药品",
        "type": ItemType.MEDICINE,
        "rarity": ItemRarity.COMMON,
        "price": 30,
        "health_gain": 20,
        "clean_gain": 10
    },
    "rare_medicine": {
        "name": "稀有药品",
        "type": ItemType.MEDICINE,
        "rarity": ItemRarity.RARE,
        "price": 100,
        "health_gain": 50,
        "clean_gain": 20
    },
    "acceleration_card": {
        "name": "加速卡",
        "type": ItemType.ACCELERATION,
        "rarity": ItemRarity.EPIC,
        "price": 200,
        "exp_gain": 50
    },
    "trusteeship_coupon": {
        "name": "托管券",
        "type": ItemType.TRUSTEESHIP,
        "rarity": ItemRarity.EPIC,
        "price": 150,
        "trustee_hours": 8
    }
}

# 群累计任务配置 (新增功能)
GROUP_TASK_TEMPLATES = [
    {"type": "group_feed", "name": "全群累计喂食", "target": 50, "reward_coins": 20, "description": "全群累计喂食50次"},
    {"type": "group_clean", "name": "全群累计清洁", "target": 30, "reward_coins": 15, "description": "全群累计清洁30次"},
    {"type": "group_explore", "name": "全群累计探索", "target": 20, "reward_coins": 30, "description": "全群累计探索20次"},
]

# 训练系统配置
TRAINING_CONFIG: Dict[str, Dict] = {
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

TRAINING_SPECIAL_EVENTS = [
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
EXPLORE_LOCATIONS: Dict[str, Dict] = {
    "forest": {
        "name": "森林",
        "energy_cost": 30,
        "events": [
            {"msg": "发现了野果！", "coins": 5, "exp": 3, "item": "apple", "prob": 0.30},
            {"msg": "遇到了友善的小动物，亲密度提升了", "coins": 10, "exp": 5, "intimacy": 3, "prob": 0.20},
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
            {"msg": "找到了稀有药品！", "coins": 5, "exp": 10, "item": "rare_medicine", "prob": 0.15},
        ],
    },
    "ruins": {
        "name": "废墟",
        "energy_cost": 40,
        "events": [
            {"msg": "捡到了神秘卡片！", "coins": 10, "exp": 15, "item": "acceleration_card", "prob": 0.10},
            {"msg": "发现了大宝藏！", "coins": 100, "exp": 20, "prob": 0.10},
            {"msg": "废墟中的气氛把你吓到了", "coins": 0, "exp": 5, "mood": -20, "prob": 0.30},
            {"msg": "被废墟中的机关伤到了", "coins": 0, "exp": 8, "health": -30, "prob": 0.25},
            {"msg": "探索了废墟遗迹，有所发现", "coins": 20, "exp": 12, "prob": 0.25},
        ],
    },
}
