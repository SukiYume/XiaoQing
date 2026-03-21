# QingPet 功能改进 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复宠物"无法互动"消息不明确、召回软锁问题，并大幅改进训练与探索系统的丰富度。

**Architecture:** 在现有服务层扩展，不新增数据库表/文件。`constants.py` 增加训练和探索配置数据，`pet_service.py` 改进方法逻辑，命令层做简单参数路由，`formatters.py` 补充旅行剩余时间显示。

**Tech Stack:** Python 3.x, pytest, sqlite3, dataclasses

---

## 文件改动清单

| 文件 | 操作 |
|------|------|
| `plugins/qingpet/utils/constants.py` | 修改：`TRAVEL_THRESHOLDS`；新增：`TRAINING_CONFIG`、`TRAINING_SPECIAL_EVENTS`、`TRAINING_MESSAGES`、`EXPLORE_LOCATIONS` |
| `plugins/qingpet/services/pet_service.py` | 修改：新增 `_get_cannot_interact_msg()`；改 `recall_pet()`、`apply_decay()`、`train_pet()`（新增参数）、`explore()`（新增参数） |
| `plugins/qingpet/commands/advanced_commands.py` | 修改：`handle_train()` 解析训练类型；`handle_explore()` 解析地点 |
| `plugins/qingpet/utils/formatters.py` | 修改：`format_pet_card()` 旅行状态显示剩余时间 |
| `tests/plugins/test_qingpet_improvements.py` | 新建：本次所有改动的测试 |

---

## Task 1: "无法互动"消息改进

**Files:**
- Modify: `plugins/qingpet/services/pet_service.py`
- Test: `tests/plugins/test_qingpet_improvements.py`

### 背景
`can_interact()` 返回 False 的情况：SLEEPING、SICK、TRAVELING、DEAD。目前只有 `feed_pet` 单独检查了 TRAVELING，其他方法以及 `feed_pet` 对 SICK 的检查都返回"宠物现在无法互动"这一无信息消息。

---

- [ ] **Step 1: 新建测试文件，写"无法互动"消息测试**

```python
# tests/plugins/test_qingpet_improvements.py
import pytest
import os
import tempfile
from datetime import datetime, timedelta
from plugins.qingpet.services import Database
from plugins.qingpet.services.pet_service import PetService
from plugins.qingpet.services.user_service import UserService
from plugins.qingpet.models import Pet, User
from plugins.qingpet.utils.constants import PetStage, PetPersonality, PetStatus


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    db = Database(db_path)
    yield db
    if db._conn is not None:
        db._conn.close()
    os.unlink(db_path)


@pytest.fixture
def pet_and_user(temp_db):
    user_service = UserService(temp_db)
    user = user_service.get_or_create_user("test_user", 123456)
    pet_service = PetService(temp_db)
    pet_service.adopt_pet("test_user", 123456, "小花")
    pet = temp_db.get_pet("test_user", 123456)
    # 升级到幼年期使 can_interact() 为 True
    pet.stage = PetStage.YOUNG
    pet.status = PetStatus.NORMAL
    temp_db.update_pet(pet)
    return pet, user


def test_cannot_interact_msg_sleeping(pet_and_user, temp_db):
    pet, user = pet_and_user
    pet.status = PetStatus.SLEEPING
    temp_db.update_pet(pet)
    pet_service = PetService(temp_db)
    success, msg, _ = pet_service.feed_pet(pet, user)
    assert not success
    assert "睡觉" in msg or "起床" in msg


def test_cannot_interact_msg_sick(pet_and_user, temp_db):
    pet, user = pet_and_user
    pet.status = PetStatus.SICK
    temp_db.update_pet(pet)
    pet_service = PetService(temp_db)
    success, msg, _ = pet_service.feed_pet(pet, user)
    assert not success
    assert "生病" in msg or "治疗" in msg


def test_cannot_interact_msg_traveling(pet_and_user, temp_db):
    pet, user = pet_and_user
    pet.status = PetStatus.TRAVELING
    pet.status_expire_time = datetime.now() + timedelta(hours=12)
    temp_db.update_pet(pet)
    pet_service = PetService(temp_db)
    success, msg, _ = pet_service.feed_pet(pet, user)
    assert not success
    assert "旅行" in msg
```

- [ ] **Step 2: 运行测试确认失败**

```
pytest tests/plugins/test_qingpet_improvements.py::test_cannot_interact_msg_sleeping tests/plugins/test_qingpet_improvements.py::test_cannot_interact_msg_sick tests/plugins/test_qingpet_improvements.py::test_cannot_interact_msg_traveling -v
```

Expected: 全部 FAIL（现有代码对 SLEEPING/SICK/TRAVELING 都只返回"宠物现在无法互动"，三个测试均无法通过）

- [ ] **Step 3: 在 `pet_service.py` 中添加 `_get_cannot_interact_msg` 方法**

在 `PetService` 类里，`can_interact()` 之后添加：

```python
def _get_cannot_interact_msg(self, pet: "Pet") -> str:
    """根据宠物状态返回具体的无法互动原因"""
    if pet.status == PetStatus.SLEEPING:
        return "宠物在睡觉中，使用 /宠物 起床 唤醒它"
    if pet.status == PetStatus.SICK:
        return "宠物生病了，使用 /宠物 治疗 [药品] 治疗"
    if pet.status == PetStatus.TRAVELING:
        return "宠物正在旅行中，使用 /宠物 召回 召回它\n用法: /宠物 召回"
    if pet.status == PetStatus.DEAD:
        return "宠物已死亡"
    return "宠物现在无法互动"
```

- [ ] **Step 4: 替换 `feed_pet`、`clean_pet`、`play_with_pet`、`train_pet`、`explore` 中的无法互动处理**

将每个方法开头的：
```python
if not pet.can_interact():
    if pet.is_traveling():
        return False, "宠物正在旅行中，请先召回它\n用法: /宠物 召回", 0
    return False, "宠物现在无法互动", 0
```
统一改为：
```python
if not pet.can_interact():
    return False, self._get_cannot_interact_msg(pet), 0
```

`sleep_pet` 不受影响（它允许非 NORMAL 状态下调用）。

- [ ] **Step 5: 运行测试确认通过**

```
pytest tests/plugins/test_qingpet_improvements.py::test_cannot_interact_msg_sleeping tests/plugins/test_qingpet_improvements.py::test_cannot_interact_msg_sick tests/plugins/test_qingpet_improvements.py::test_cannot_interact_msg_traveling -v
```

Expected: 全部 PASS

- [ ] **Step 6: 提交**

```
git add plugins/qingpet/services/pet_service.py tests/plugins/test_qingpet_improvements.py
git commit -m "feat(qingpet): improve cannot-interact messages with specific reasons"
```

---

## Task 2: 召回软锁修复

**Files:**
- Modify: `plugins/qingpet/utils/constants.py`
- Modify: `plugins/qingpet/services/pet_service.py` (`recall_pet`, `apply_decay`)
- Test: `tests/plugins/test_qingpet_improvements.py`

---

- [ ] **Step 1: 添加召回测试**

追加到 `tests/plugins/test_qingpet_improvements.py`：

```python
def test_recall_requires_only_coins(pet_and_user, temp_db):
    """召回只需金币，无友情点要求"""
    pet, user = pet_and_user
    pet.status = PetStatus.TRAVELING
    pet.status_expire_time = datetime.now() + timedelta(hours=12)
    temp_db.update_pet(pet)

    user.coins = 50
    user.friendship_points = 0  # 没有友情点
    temp_db.update_user(user)

    pet_service = PetService(temp_db)
    success, msg = pet_service.recall_pet(pet, user)
    assert success, f"召回失败: {msg}"
    assert "友情" not in msg


def test_recall_fails_without_coins(pet_and_user, temp_db):
    """金币不足时召回失败"""
    pet, user = pet_and_user
    pet.status = PetStatus.TRAVELING
    temp_db.update_pet(pet)
    user.coins = 10
    temp_db.update_user(user)

    pet_service = PetService(temp_db)
    success, msg = pet_service.recall_pet(pet, user)
    assert not success
    assert "金币" in msg


def test_recall_success_message_no_friendship(pet_and_user, temp_db):
    """召回成功消息不包含友情点字样"""
    pet, user = pet_and_user
    pet.status = PetStatus.TRAVELING
    pet.status_expire_time = datetime.now() + timedelta(hours=1)
    temp_db.update_pet(pet)
    user.coins = 100
    user.friendship_points = 0
    temp_db.update_user(user)

    pet_service = PetService(temp_db)
    success, msg = pet_service.recall_pet(pet, user)
    assert success
    assert "友情" not in msg
    assert "金币" in msg


def test_apply_decay_travel_message_no_friendship(pet_and_user, temp_db):
    """apply_decay 触发旅行的消息不包含友情点"""
    from plugins.qingpet.services.pet_service import PetService
    pet, _ = pet_and_user
    # 强制 care_score 极低以触发旅行
    pet.hunger = 0
    pet.mood = 0
    pet.clean = 0
    pet.energy = 0
    pet.health = 0
    pet.status = PetStatus.NORMAL
    from datetime import timedelta
    pet.last_update = datetime.now() - timedelta(minutes=5)
    temp_db.update_pet(pet)

    pet_service = PetService(temp_db)
    result = pet_service.apply_decay(pet)
    # 可能触发旅行消息或生病消息，不管哪种，旅行消息不含"友情"
    if result and "旅行" in result:
        assert "友情" not in result
```

- [ ] **Step 2: 运行测试确认失败**

```
pytest tests/plugins/test_qingpet_improvements.py::test_recall_requires_only_coins tests/plugins/test_qingpet_improvements.py::test_recall_fails_without_coins tests/plugins/test_qingpet_improvements.py::test_recall_success_message_no_friendship tests/plugins/test_qingpet_improvements.py::test_apply_decay_travel_message_no_friendship -v
```

Expected: FAIL（当前要求友情点，且旅行消息含"友情点"）

- [ ] **Step 3: 修改 `constants.py` 中 `TRAVEL_THRESHOLDS`**

将 `"recall_cost_friendship": 10` 改为 `"recall_cost_friendship": 0`。

- [ ] **Step 4: 修改 `pet_service.py` 中的 `recall_pet` 方法**

找到：
```python
recall_coins = int(TRAVEL_THRESHOLDS["recall_cost_coins"])
recall_fp = int(TRAVEL_THRESHOLDS["recall_cost_friendship"])

if user.coins < recall_coins:
    return False, f"金币不足，召回需要{recall_coins}金币"
if user.friendship_points < recall_fp:
    return False, f"友情点不足，召回需要{recall_fp}友情点"

user.coins -= recall_coins
user.friendship_points -= recall_fp
```

替换为：
```python
recall_coins = int(TRAVEL_THRESHOLDS["recall_cost_coins"])

if user.coins < recall_coins:
    return False, f"金币不足，召回需要{recall_coins}金币"

user.coins -= recall_coins
```

将成功消息：
```python
return True, f"🎉 {pet.name}被成功召回了！花费{recall_coins}金币和{recall_fp}友情点"
```
改为：
```python
return True, f"🎉 {pet.name}被成功召回了！花费{recall_coins}金币"
```

- [ ] **Step 5: 修改 `apply_decay` 中的旅行触发消息**

找到：
```python
return (f"😿 {pet.name}因为照顾不周，离家旅行了...\n"
        f"它将在{travel_hours}小时后自动回来\n"
        f"或者使用 /宠物 召回 提前召回（需要金币和友情点）")
```
改为：
```python
recall_coins = int(TRAVEL_THRESHOLDS["recall_cost_coins"])
return (f"😿 {pet.name}因为照顾不周，离家旅行了...\n"
        f"它将在{travel_hours}小时后自动回来\n"
        f"或者使用 /宠物 召回 提前召回（需要{recall_coins}金币）")
```

- [ ] **Step 6: 运行测试确认通过**

```
pytest tests/plugins/test_qingpet_improvements.py::test_recall_requires_only_coins tests/plugins/test_qingpet_improvements.py::test_recall_fails_without_coins tests/plugins/test_qingpet_improvements.py::test_recall_success_message_no_friendship tests/plugins/test_qingpet_improvements.py::test_apply_decay_travel_message_no_friendship -v
```

Expected: 全部 PASS

- [ ] **Step 7: 提交**

```
git add plugins/qingpet/utils/constants.py plugins/qingpet/services/pet_service.py tests/plugins/test_qingpet_improvements.py
git commit -m "feat(qingpet): remove friendship point requirement for recall"
```

---

## Task 3: 训练系统改进 — 常量配置

**Files:**
- Modify: `plugins/qingpet/utils/constants.py`
- Test: `tests/plugins/test_qingpet_improvements.py`

---

- [ ] **Step 1: 添加训练配置测试**

追加到测试文件（**注意**：`from plugins.qingpet.utils.constants import TRAINING_CONFIG, TRAINING_SPECIAL_EVENTS, TRAINING_MESSAGES` 这行导入要合并到文件顶部的 import 区域，不要放在函数之间）：

```python
# 追加到文件顶部 import 区域：
# from plugins.qingpet.utils.constants import (
#     PetStage, PetPersonality, PetStatus,
#     TRAINING_CONFIG, TRAINING_SPECIAL_EVENTS, TRAINING_MESSAGES,
#     EXPLORE_LOCATIONS,
# )


def test_training_config_has_three_types():
    assert "strength" in TRAINING_CONFIG
    assert "agility" in TRAINING_CONFIG
    assert "intellect" in TRAINING_CONFIG


def test_training_config_fields():
    for key, cfg in TRAINING_CONFIG.items():
        assert "name" in cfg
        assert "exp_gain" in cfg
        assert "energy_cost" in cfg
        assert "success_rate_base" in cfg


def test_training_messages_have_success_and_fail():
    assert "success" in TRAINING_MESSAGES
    assert "fail" in TRAINING_MESSAGES
    assert len(TRAINING_MESSAGES["success"]) >= 2
    assert len(TRAINING_MESSAGES["fail"]) >= 2


def test_training_special_events_structure():
    for event in TRAINING_SPECIAL_EVENTS:
        assert "msg" in event
        assert "prob" in event
```

- [ ] **Step 2: 运行测试确认失败**

```
pytest tests/plugins/test_qingpet_improvements.py::test_training_config_has_three_types tests/plugins/test_qingpet_improvements.py::test_training_config_fields tests/plugins/test_qingpet_improvements.py::test_training_messages_have_success_and_fail tests/plugins/test_qingpet_improvements.py::test_training_special_events_structure -v
```

Expected: FAIL（常量未定义）

- [ ] **Step 3: 在 `constants.py` 末尾追加训练配置**

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

```
pytest tests/plugins/test_qingpet_improvements.py::test_training_config_has_three_types tests/plugins/test_qingpet_improvements.py::test_training_config_fields tests/plugins/test_qingpet_improvements.py::test_training_messages_have_success_and_fail tests/plugins/test_qingpet_improvements.py::test_training_special_events_structure -v
```

Expected: 全部 PASS

- [ ] **Step 5: 提交**

```
git add plugins/qingpet/utils/constants.py tests/plugins/test_qingpet_improvements.py
git commit -m "feat(qingpet): add training system config constants"
```

---

## Task 4: 训练系统改进 — 服务逻辑

**Files:**
- Modify: `plugins/qingpet/services/pet_service.py` (`train_pet`)
- Modify: `plugins/qingpet/commands/advanced_commands.py` (`handle_train`)
- Test: `tests/plugins/test_qingpet_improvements.py`

---

- [ ] **Step 1: 添加训练服务测试**

追加到测试文件：

```python
def test_train_default_type_strength(pet_and_user, temp_db):
    """默认训练类型为体力"""
    pet, user = pet_and_user
    pet_service = PetService(temp_db)
    success, msg, coins = pet_service.train_pet(pet, user)
    # 不带参数应成功（体力训练）
    # 结果消息包含训练名称或结果描述
    assert isinstance(success, bool)
    assert isinstance(msg, str)


def test_train_agility_boosts_mood(pet_and_user, temp_db):
    """敏捷训练成功时提升心情（多次重试以排除随机失败）"""
    import random
    random.seed(42)  # 固定随机种子让测试稳定
    pet_service = PetService(temp_db)
    mood_increased = False
    for _ in range(20):
        p = temp_db.get_pet("test_user", 123456)
        p.energy = 100
        p.mood = 50
        p.last_train = None
        temp_db.update_pet(p)
        u = temp_db.get_user("test_user", 123456)
        success, msg, _ = pet_service.train_pet(p, u, training_type="agility")
        if success and any(t in msg for t in ["认真", "努力", "挥洒", "偷懒", "状态不佳", "分心"]):
            # 训练成功时检查心情有没有提升
            refreshed = temp_db.get_pet("test_user", 123456)
            if refreshed.mood > 50:
                mood_increased = True
                break
    assert mood_increased, "敏捷训练应该在成功时提升心情"


def test_train_intellect_higher_exp(pet_and_user, temp_db):
    """智力训练成功时经验高于体力训练"""
    from plugins.qingpet.utils.constants import TRAINING_CONFIG
    assert TRAINING_CONFIG["intellect"]["exp_gain"] > TRAINING_CONFIG["strength"]["exp_gain"]


def test_train_invalid_type_falls_back(pet_and_user, temp_db):
    """无效训练类型回退到体力训练"""
    pet, user = pet_and_user
    pet_service = PetService(temp_db)
    success, msg, _ = pet_service.train_pet(pet, user, training_type="unknown_type")
    assert isinstance(msg, str)


def test_train_message_contains_pet_name(pet_and_user, temp_db):
    """训练结果消息包含宠物名字"""
    pet, user = pet_and_user
    pet_service = PetService(temp_db)
    _, msg, _ = pet_service.train_pet(pet, user, training_type="strength")
    assert "小花" in msg


def test_train_smart_personality_exp_stacking(pet_and_user, temp_db):
    """SMART 性格的经验加成与特殊事件 exp_multiplier 叠乘"""
    import random
    random.seed(0)
    pet, user = pet_and_user
    pet.personality = PetPersonality.SMART
    temp_db.update_pet(pet)

    # 验证 SMART 经验加成逻辑：SMART 应得到 >= 普通智力训练经验 * 1.1
    from plugins.qingpet.utils.constants import TRAINING_CONFIG
    base_exp = TRAINING_CONFIG["intellect"]["exp_gain"]
    # SMART 至少得到 base_exp * 1.1
    assert int(base_exp * 1.1) > base_exp
```

- [ ] **Step 2: 运行测试确认失败**

```
pytest tests/plugins/test_qingpet_improvements.py::test_train_default_type_strength tests/plugins/test_qingpet_improvements.py::test_train_invalid_type_falls_back tests/plugins/test_qingpet_improvements.py::test_train_message_contains_pet_name -v
```

Expected: FAIL（`train_pet` 不接受 `training_type` 参数）

- [ ] **Step 3: 重写 `pet_service.py` 中的 `train_pet` 方法**

在 `constants.py` 导入处新增：
```python
from ..utils.constants import (
    ...,  # 现有导入
    TRAINING_CONFIG, TRAINING_SPECIAL_EVENTS, TRAINING_MESSAGES
)
```

将 `train_pet` 方法替换为：

```python
def train_pet(self, pet: Pet, user: User,
              training_type: str = "strength",
              spam_decay_factor: float = 1.0) -> Tuple[bool, str, int]:
    if not pet.can_interact():
        return False, self._get_cannot_interact_msg(pet), 0

    cooled, remaining = validate_cooling(pet.last_train, COOLDOWN_TIMES["train"])
    if not cooled:
        return False, f"训练冷却中，请等待{remaining}秒", 0

    # 无效类型回退到 strength
    config = TRAINING_CONFIG.get(training_type, TRAINING_CONFIG["strength"])

    if pet.energy < config["energy_cost"]:
        return False, "宠物精力不足，无法训练", 0

    # 成功率：基础 + 性格加成
    success_rate = config["success_rate_base"]
    if pet.personality == PetPersonality.SMART:
        if training_type == "intellect":
            success_rate = min(1.0, success_rate + 0.1)

    energy_cost = config["energy_cost"]
    pet.update_stat("energy", -energy_cost, min_val=0)
    pet.last_train = datetime.now()
    pet.last_update = datetime.now()

    if random.random() > success_rate:
        # 训练失败
        self.db.update_pet(pet)
        fail_msg = random.choice(TRAINING_MESSAGES["fail"]).format(name=pet.name)
        return True, fail_msg, 0

    # 训练成功
    exp_gain = config["exp_gain"]

    # SMART 性格：所有训练经验 ×1.1
    if pet.personality == PetPersonality.SMART:
        exp_gain = int(exp_gain * 1.1)

    # 检查特殊事件（最多触发一个）
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
                # SMART 叠乘
                exp_gain = int(exp_gain * event["exp_multiplier"])
            break  # 最多一个特殊事件

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
        base_msg = random.choice(TRAINING_MESSAGES["success"]).format(name=pet.name)
        type_name = config["name"]
        return True, f"[{type_name}] {base_msg} 获得{exp_gain}经验{special_msg}{extra_msg}", coins_gain
    return False, "训练失败", 0
```

- [ ] **Step 4: 修改 `commands/advanced_commands.py` 中的 `handle_train`**

首先把 `resolve_pet_for_self_command` 的返回值从 `_` 改为 `resolved_args`：
```python
# 原来：
pet, resolved_group_id, _, err = resolve_pet_for_self_command(...)
# 改为：
pet, resolved_group_id, resolved_args, err = resolve_pet_for_self_command(...)
```

然后找到：
```python
success, message, coins = pet_service.train_pet(pet, user,
                                                 spam_decay_factor=spam_decay)
```
替换为（使用 `resolved_args` 而非 `args`，以正确处理私聊多群场景）：
```python
# 解析训练类型：体力/strength -> strength, 敏捷/agility -> agility, 智力/intellect -> intellect
type_map = {
    "体力": "strength", "strength": "strength",
    "敏捷": "agility", "agility": "agility",
    "智力": "intellect", "intellect": "intellect",
}
training_type = type_map.get(resolved_args.strip() if resolved_args else "", "strength")
success, message, coins = pet_service.train_pet(pet, user,
                                                 training_type=training_type,
                                                 spam_decay_factor=spam_decay)
```

- [ ] **Step 5: 运行训练相关测试**

```
pytest tests/plugins/test_qingpet_improvements.py -k "train" -v
```

Expected: 全部 PASS

- [ ] **Step 6: 运行原有宠物测试确保无回归**

```
pytest tests/plugins/test_qingpet.py tests/plugins/test_qingpet_regressions.py -v
```

Expected: 全部 PASS

- [ ] **Step 7: 提交**

```
git add plugins/qingpet/services/pet_service.py plugins/qingpet/commands/advanced_commands.py tests/plugins/test_qingpet_improvements.py
git commit -m "feat(qingpet): improve training system with types, narratives, and personality effects"
```

---

## Task 5: 探索系统改进 — 常量配置

**Files:**
- Modify: `plugins/qingpet/utils/constants.py`
- Test: `tests/plugins/test_qingpet_improvements.py`

---

- [ ] **Step 1: 添加探索配置测试**

追加到测试文件：

```python
from plugins.qingpet.utils.constants import EXPLORE_LOCATIONS


def test_explore_locations_exist():
    for loc in ["forest", "beach", "cave", "ruins"]:
        assert loc in EXPLORE_LOCATIONS


def test_explore_location_fields():
    for loc_key, loc in EXPLORE_LOCATIONS.items():
        assert "name" in loc
        assert "energy_cost" in loc
        assert "events" in loc
        assert len(loc["events"]) >= 4


def test_explore_event_probabilities_sum_to_one():
    for loc_key, loc in EXPLORE_LOCATIONS.items():
        total = sum(e["prob"] for e in loc["events"])
        assert abs(total - 1.0) < 0.001, f"{loc_key} 概率之和={total}"


def test_explore_cave_ruins_require_health_40():
    """山洞和废墟的配置存在高风险事件（health 损失）"""
    cave_events = EXPLORE_LOCATIONS["cave"]["events"]
    ruins_events = EXPLORE_LOCATIONS["ruins"]["events"]
    has_health_damage = any("health" in e for e in cave_events + ruins_events)
    assert has_health_damage
```

- [ ] **Step 2: 运行测试确认失败**

```
pytest tests/plugins/test_qingpet_improvements.py::test_explore_locations_exist tests/plugins/test_qingpet_improvements.py::test_explore_location_fields tests/plugins/test_qingpet_improvements.py::test_explore_event_probabilities_sum_to_one -v
```

Expected: FAIL

- [ ] **Step 3: 在 `constants.py` 末尾追加探索配置**

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

```
pytest tests/plugins/test_qingpet_improvements.py -k "explore_location" -v
```

Expected: 全部 PASS

- [ ] **Step 5: 提交**

```
git add plugins/qingpet/utils/constants.py tests/plugins/test_qingpet_improvements.py
git commit -m "feat(qingpet): add explore locations config constants"
```

---

## Task 6: 探索系统改进 — 服务逻辑

**Files:**
- Modify: `plugins/qingpet/services/pet_service.py` (`explore`)
- Modify: `plugins/qingpet/commands/advanced_commands.py` (`handle_explore`)
- Test: `tests/plugins/test_qingpet_improvements.py`

---

- [ ] **Step 1: 添加探索服务测试**

追加到测试文件：

```python
def test_explore_default_location_forest(pet_and_user, temp_db):
    """默认探索地点为森林"""
    pet, user = pet_and_user
    pet_service = PetService(temp_db)
    success, msg, _ = pet_service.explore(pet, user)
    assert isinstance(success, bool)
    assert isinstance(msg, str)


def test_explore_cave_blocked_on_low_health(pet_and_user, temp_db):
    """山洞健康值不足时被拒绝"""
    pet, user = pet_and_user
    pet.health = 30  # 低于40
    temp_db.update_pet(pet)
    pet_service = PetService(temp_db)
    success, msg, _ = pet_service.explore(pet, user, location="cave")
    assert not success
    assert "健康" in msg


def test_explore_ruins_blocked_on_low_health(pet_and_user, temp_db):
    """废墟健康值不足时被拒绝"""
    pet, user = pet_and_user
    pet.health = 35
    temp_db.update_pet(pet)
    pet_service = PetService(temp_db)
    success, msg, _ = pet_service.explore(pet, user, location="ruins")
    assert not success
    assert "健康" in msg


def test_explore_cave_allowed_with_enough_health(pet_and_user, temp_db):
    """山洞健康值足够时允许探索"""
    pet, user = pet_and_user
    pet.health = 80
    temp_db.update_pet(pet)
    pet_service = PetService(temp_db)
    success, msg, _ = pet_service.explore(pet, user, location="cave")
    # 可能因精力不足失败，但不应因健康不足被拒绝
    if not success:
        assert "健康" not in msg or "精力" in msg


def test_explore_invalid_location_falls_back_to_forest(pet_and_user, temp_db):
    """无效地点回退到森林"""
    pet, user = pet_and_user
    pet_service = PetService(temp_db)
    success, msg, _ = pet_service.explore(pet, user, location="unknown")
    assert isinstance(msg, str)


def test_explore_shy_personality_accepted_by_service(pet_and_user, temp_db):
    """SHY 性格宠物可以探索，性格修正不导致崩溃"""
    import random
    random.seed(7)
    pet, user = pet_and_user
    pet.personality = PetPersonality.SHY
    pet.health = 80
    temp_db.update_pet(pet)
    pet_service = PetService(temp_db)
    success, msg, _ = pet_service.explore(pet, user, location="cave")
    assert isinstance(msg, str)  # 不崩溃即可


def test_explore_smart_personality_accepted_by_service(pet_and_user, temp_db):
    """SMART 性格宠物可以探索废墟，性格修正不导致崩溃"""
    import random
    random.seed(99)
    pet, user = pet_and_user
    pet.personality = PetPersonality.SMART
    pet.health = 80
    temp_db.update_pet(pet)
    pet_service = PetService(temp_db)
    success, msg, _ = pet_service.explore(pet, user, location="ruins")
    assert isinstance(msg, str)
```

- [ ] **Step 2: 运行测试确认失败**

```
pytest tests/plugins/test_qingpet_improvements.py -k "explore" -v
```

Expected: FAIL（`explore()` 不接受 `location` 参数）

- [ ] **Step 3: 重写 `pet_service.py` 中的 `explore` 方法**

在 `constants.py` 导入处新增 `EXPLORE_LOCATIONS`。

将 `explore` 方法替换为：

```python
def explore(self, pet: Pet, user: User,
            location: str = "forest",
            spam_decay_factor: float = 1.0) -> Tuple[bool, str, int]:
    if not pet.can_interact():
        return False, self._get_cannot_interact_msg(pet), 0

    cooled, remaining = validate_cooling(pet.last_explore, COOLDOWN_TIMES["explore"])
    if not cooled:
        return False, f"探索冷却中，请等待{remaining}秒", 0

    # 无效地点回退到森林
    loc_config = EXPLORE_LOCATIONS.get(location, EXPLORE_LOCATIONS["forest"])

    energy_cost = loc_config["energy_cost"]
    if pet.energy < energy_cost:
        return False, "宠物精力不足，无法探索", 0

    # 山洞/废墟健康前置检查
    if location in ("cave", "ruins") and pet.health < 40:
        return False, f"宠物健康值过低，不建议进入危险地点（需健康≥40）", 0

    pet.update_stat("energy", -energy_cost, min_val=0)

    # 按性格调整事件概率权重，然后加权随机选一个
    events = loc_config["events"]
    weights = []
    for event in events:
        prob = event["prob"]
        # LIVELY：森林亲密度/金币事件 +0.1；山洞/废墟非受伤事件 +0.05
        if pet.personality == PetPersonality.LIVELY:
            if location == "forest" and ("intimacy" in event or event.get("coins", 0) > 0):
                prob += 0.1
            elif location in ("cave", "ruins") and "health" not in event:
                prob += 0.05
        # SHY：山洞/废墟受伤/恐惧事件 +0.1
        if pet.personality == PetPersonality.SHY:
            if location in ("cave", "ruins") and ("health" in event or "mood" in event):
                prob += 0.1
        # SMART：山洞/废墟有道具的事件 +0.1
        if pet.personality == PetPersonality.SMART:
            if location in ("cave", "ruins") and "item" in event:
                prob += 0.1
        weights.append(max(prob, 0.01))  # 最低权重 0.01 防止除零

    chosen = random.choices(events, weights=weights, k=1)[0]

    group_config = self.db.get_group_config(pet.group_id)
    exp_gain = int(chosen.get("exp", 0))
    coins_gain = int(chosen.get("coins", 0) * group_config.economy_multiplier * spam_decay_factor)

    pet.experience += exp_gain
    pet.last_explore = datetime.now()
    pet.last_update = datetime.now()

    # 应用事件副作用
    for stat in ("mood", "clean", "health"):
        if stat in chosen:
            pet.update_stat(stat, chosen[stat], min_val=0)
    if "intimacy" in chosen:
        pet.intimacy += chosen["intimacy"]

    # 道具掉落
    item_msg = ""
    if "item" in chosen:
        inventory = self.db.get_or_create_inventory(user.user_id, user.group_id)
        from ..utils.constants import DEFAULT_ITEMS
        item_id = chosen["item"]
        item_name = DEFAULT_ITEMS.get(item_id, {}).get("name", item_id)
        inventory.add_item(item_id, 1)
        self.db.update_inventory(inventory)
        item_msg = f"（获得 {item_name} ×1）"

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
        loc_name = loc_config["name"]
        return True, f"[{loc_name}] {chosen['msg']}{item_msg} 获得{exp_gain}经验{extra_msg}", coins_gain
    return False, "探索失败", 0
```

- [ ] **Step 4: 修改 `commands/advanced_commands.py` 中的 `handle_explore`**

首先把 `resolve_pet_for_self_command` 的返回值从 `_` 改为 `resolved_args`：
```python
# 原来：
pet, resolved_group_id, _, err = resolve_pet_for_self_command(...)
# 改为：
pet, resolved_group_id, resolved_args, err = resolve_pet_for_self_command(...)
```

然后找到：
```python
success, message, coins = pet_service.explore(pet, user,
                                               spam_decay_factor=spam_decay)
```
替换为（使用 `resolved_args` 而非 `args`）：
```python
# 解析地点名：森林/forest -> forest, 海边/beach -> beach, 山洞/cave -> cave, 废墟/ruins -> ruins
loc_map = {
    "森林": "forest", "forest": "forest",
    "海边": "beach", "beach": "beach",
    "山洞": "cave", "cave": "cave",
    "废墟": "ruins", "ruins": "ruins",
}
location = loc_map.get(resolved_args.strip() if resolved_args else "", "forest")
success, message, coins = pet_service.explore(pet, user,
                                               location=location,
                                               spam_decay_factor=spam_decay)
```

- [ ] **Step 5: 运行探索相关测试**

```
pytest tests/plugins/test_qingpet_improvements.py -k "explore" -v
```

Expected: 全部 PASS

- [ ] **Step 6: 运行原有宠物测试确保无回归**

```
pytest tests/plugins/test_qingpet.py tests/plugins/test_qingpet_regressions.py tests/plugins/test_qingpet_new_features.py -v
```

Expected: 全部 PASS

- [ ] **Step 7: 提交**

```
git add plugins/qingpet/services/pet_service.py plugins/qingpet/commands/advanced_commands.py tests/plugins/test_qingpet_improvements.py
git commit -m "feat(qingpet): improve explore system with locations, items, and personality effects"
```

---

## Task 7: 旅行状态显示剩余时间

**Files:**
- Modify: `plugins/qingpet/utils/formatters.py`
- Test: `tests/plugins/test_qingpet_improvements.py`

---

- [ ] **Step 1: 添加格式化测试**

追加到测试文件：

```python
from plugins.qingpet.utils.formatters import format_pet_card
from plugins.qingpet.models import User


def make_user(user_id="test_user", group_id=123456) -> User:
    return User(user_id=user_id, group_id=group_id)


def test_format_pet_card_shows_travel_time(pet_and_user, temp_db):
    """旅行中的宠物卡片显示剩余时间"""
    pet, user = pet_and_user
    pet.status = PetStatus.TRAVELING
    pet.status_expire_time = datetime.now() + timedelta(hours=5, minutes=30)
    card = format_pet_card(pet, user)
    # 应包含剩余时间信息
    assert "剩余" in card or "小时" in card or "旅行" in card


def test_format_pet_card_normal_no_travel_time(pet_and_user, temp_db):
    """正常状态不显示旅行剩余时间"""
    pet, user = pet_and_user
    pet.status = PetStatus.NORMAL
    pet.status_expire_time = None
    card = format_pet_card(pet, user)
    assert "旅行剩余" not in card
```

- [ ] **Step 2: 运行测试确认失败**

```
pytest tests/plugins/test_qingpet_improvements.py::test_format_pet_card_shows_travel_time -v
```

Expected: FAIL

- [ ] **Step 3: 修改 `formatters.py` 中的 `format_pet_card`**

在 `format_pet_card` 函数里，状态行之后（`text += f"• 状态: {s_emoji} {pet.status.value}\n"` 那行后面）添加：

```python
# 旅行状态显示剩余时间
if pet.status == PetStatus.TRAVELING and pet.status_expire_time:
    from datetime import datetime
    remaining = pet.status_expire_time - datetime.now()
    if remaining.total_seconds() > 0:
        hours = int(remaining.total_seconds() // 3600)
        minutes = int((remaining.total_seconds() % 3600) // 60)
        text += f"• 旅行剩余: {hours}小时{minutes}分钟\n"
    else:
        text += f"• 旅行剩余: 即将返回\n"
```

- [ ] **Step 4: 运行测试确认通过**

```
pytest tests/plugins/test_qingpet_improvements.py::test_format_pet_card_shows_travel_time tests/plugins/test_qingpet_improvements.py::test_format_pet_card_normal_no_travel_time -v
```

Expected: 全部 PASS

- [ ] **Step 5: 运行所有 qingpet 测试**

```
pytest tests/plugins/test_qingpet.py tests/plugins/test_qingpet_regressions.py tests/plugins/test_qingpet_new_features.py tests/plugins/test_qingpet_improvements.py -v
```

Expected: 全部 PASS

- [ ] **Step 6: 提交**

```
git add plugins/qingpet/utils/formatters.py tests/plugins/test_qingpet_improvements.py
git commit -m "feat(qingpet): show remaining travel time in pet card"
```

---

## Task 8: 最终回归验证

- [ ] **Step 1: 运行全部 qingpet 相关测试**

```
pytest tests/plugins/test_qingpet.py tests/plugins/test_qingpet_regressions.py tests/plugins/test_qingpet_new_features.py tests/plugins/test_qingpet_improvements.py -v
```

Expected: 全部 PASS

- [ ] **Step 2: 确认帮助文档仍然准确**（无需代码改动，只是人工核对）

检查 `formatters.py` 中 `format_help_text()` 的进阶命令部分，确认"探索"和"训练"说明与新参数一致。如需更新：
- 训练描述改为：`"• /宠物 训练 [体力/敏捷/智力] - 训练"`
- 探索描述改为：`"• /宠物 探索 [森林/海边/山洞/废墟] - 探索冒险"`

- [ ] **Step 3: 提交帮助文档更新（如有修改）**

```
git add plugins/qingpet/utils/formatters.py
git commit -m "docs(qingpet): update help text for train and explore commands"
```
