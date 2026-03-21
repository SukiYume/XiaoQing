# QingPet 功能改进设计文档

**日期**: 2026-03-21
**范围**: plugins/qingpet
**状态**: 已批准

---

## 一、背景与问题

1. 宠物无法互动时只显示"宠物现在无法互动"，不说明原因（生病/睡觉/旅行/死亡）
2. 召回旅行中的宠物需要友情点，当群内只有一人养宠物时无法获得友情点，造成软锁
3. 训练和探索功能过于简单，体验与玩耍雷同，缺乏新鲜感

---

## 二、方案选择

采用**方案二：在现有服务层扩展**——扩展 `pet_service.py` 逻辑，在 `constants.py` 中增加配置数据，命令层做简单路由。符合现有架构，改动集中可控。

---

## 三、详细设计

### 3.1 "无法互动"消息改进

**文件**: `plugins/qingpet/services/pet_service.py`

新增 `_get_cannot_interact_msg(pet: Pet) -> str` 辅助方法，根据宠物状态返回具体提示：

| 状态 | 消息 |
|------|------|
| SLEEPING | "宠物在睡觉中，使用 /宠物 起床 唤醒它" |
| SICK | "宠物生病了，使用 /宠物 治疗 [药品] 治疗" |
| TRAVELING | "宠物正在旅行中，使用 /宠物 召回 召回它" |
| DEAD | "宠物已死亡" |
| 其他 | "宠物现在无法互动" |

所有互动方法（`feed_pet`、`clean_pet`、`play_with_pet`、`train_pet`、`explore`）统一调用此方法。

**文件**: `plugins/qingpet/utils/formatters.py`

`format_pet_card()` 中当 `pet.status == TRAVELING` 时，额外显示剩余旅行时间（从 `pet.status_expire_time` 计算）。

---

### 3.2 召回改动

**文件**: `plugins/qingpet/services/pet_service.py` — `recall_pet()`
**文件**: `plugins/qingpet/utils/constants.py` — `TRAVEL_THRESHOLDS`

- 移除友情点消耗，召回只需 **50金币**
- 将 `TRAVEL_THRESHOLDS["recall_cost_friendship"]` 改为 `0`
- 更新召回成功消息：`"🎉 {pet.name}被成功召回了！花费{recall_coins}金币"`

---

### 3.3 训练系统改进

**文件**: `plugins/qingpet/utils/constants.py`

新增 `TRAINING_CONFIG`：

```python
TRAINING_CONFIG = {
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
```

**性格影响**（在 `train_pet()` 中处理）：
- `SMART` → 智力训练成功率+10%，所有训练经验+10%
- `LIVELY` → 敏捷训练心情加成×1.5
- `CLINGY` → 特殊亲密度事件概率×2

**命令变更**：`/宠物 训练 [体力/敏捷/智力]`，不带参数默认体力训练。

**文件**: `plugins/qingpet/services/pet_service.py` — `train_pet()`
**文件**: `plugins/qingpet/commands/advanced_commands.py` — `handle_train()`

---

### 3.4 探索系统改进

**文件**: `plugins/qingpet/utils/constants.py`

新增 `EXPLORE_LOCATIONS` 配置：

```python
EXPLORE_LOCATIONS = {
    "forest": {
        "name": "森林",
        "energy_cost": 30,
        "events": [
            {"msg": "发现了野果！", "coins": 5, "exp": 3, "item": "apple", "prob": 0.3},
            {"msg": "遇到了友善的小动物", "coins": 10, "exp": 5, "intimacy": 3, "prob": 0.2},
            {"msg": "在树丛中发现了金币", "coins": 20, "exp": 5, "prob": 0.2},
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
            {"msg": "捡到一个漂流瓶，里面有金币！", "coins": 50, "exp": 5, "prob": 0.1},
            {"msg": "被海浪打湿了，需要清洁", "coins": 5, "exp": 3, "clean": -15, "prob": 0.2},
            {"msg": "悠闲地散步", "coins": 12, "exp": 4, "prob": 0.2},
        ],
    },
    "cave": {
        "name": "山洞",
        "energy_cost": 40,
        "events": [
            {"msg": "发现了宝箱！", "coins": 50, "exp": 10, "prob": 0.2},
            {"msg": "找到了奇怪的药草", "coins": 10, "exp": 8, "item": "medicine", "prob": 0.2},
            {"msg": "黑暗中摸索，积累了经验", "coins": 15, "exp": 15, "prob": 0.2},
            {"msg": "遭遇危险！受了点伤", "coins": 5, "exp": 5, "health": -20, "prob": 0.25},
            {"msg": "找到了稀有药品！", "coins": 5, "exp": 10, "item": "rare_medicine", "prob": 0.15},
        ],
    },
    "ruins": {
        "name": "废墟",
        "energy_cost": 40,
        "events": [
            {"msg": "捡到了神秘卡片！", "coins": 10, "exp": 15, "item": "acceleration_card", "prob": 0.1},
            {"msg": "发现了大宝藏！", "coins": 100, "exp": 20, "prob": 0.1},
            {"msg": "废墟中的气氛把你吓到了", "coins": 0, "exp": 5, "mood": -20, "prob": 0.3},
            {"msg": "被废墟中的机关伤到了", "coins": 0, "exp": 8, "health": -30, "prob": 0.25},
            {"msg": "探索了废墟遗迹，有所发现", "coins": 20, "exp": 12, "prob": 0.25},
        ],
    },
}
```

**性格影响**（在 `explore()` 中处理）：
- `LIVELY` → 森林额外事件触发概率+5%，废墟/山洞成功率+5%
- `SHY` → 废墟/山洞触发"害怕/受伤"事件概率+10%
- `SMART` → 山洞/废墟找到道具概率+10%

**掉落道具处理**：探索到道具时直接加入背包（`inventory.add_item`）。

**命令变更**：`/宠物 探索 [森林/海边/山洞/废墟]`，不带参数默认森林。

**文件**: `plugins/qingpet/services/pet_service.py` — `explore()`
**文件**: `plugins/qingpet/commands/advanced_commands.py` — `handle_explore()`

---

## 四、改动文件清单

| 文件 | 改动内容 |
|------|---------|
| `utils/constants.py` | 新增 `TRAINING_CONFIG`、`TRAINING_SPECIAL_EVENTS`、`TRAINING_MESSAGES`、`EXPLORE_LOCATIONS`；修改 `TRAVEL_THRESHOLDS["recall_cost_friendship"]` 为 0 |
| `services/pet_service.py` | 新增 `_get_cannot_interact_msg()`；改进 `recall_pet()`、`train_pet()`、`explore()` |
| `commands/advanced_commands.py` | `handle_train()` 支持训练类型参数；`handle_explore()` 支持地点参数 |
| `utils/formatters.py` | `format_pet_card()` 旅行状态显示剩余时间 |

---

## 五、不在范围内

- 不新增数据库表/字段（道具掉落使用现有背包系统）
- 不修改进化/任务/排行系统
- 不改变金币每日上限或反脚本机制
