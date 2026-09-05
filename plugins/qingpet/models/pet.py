"""宠物状态、装扮和乐观并发合并模型。"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import ClassVar, cast

from ..utils.constants import (
    DEFAULT_DRESS_ITEMS,
    MAX_STAT_VALUE,
    PetPersonality,
    PetStage,
    PetStatus,
)
from ..utils.time import utc_now

_STAT_FIELDS       = ("hunger", "mood", "clean", "energy", "health")
_DRESS_SLOT_FIELDS = (
    ("帽子", "dress_hat"),
    ("衣服", "dress_clothes"),
    ("饰品", "dress_accessory"),
    ("背景", "dress_background"),
)


@dataclass
class Pet:
    """保存宠物领域状态及从最近持久化版本产生的本地修改。"""

    id: int
    user_id: str
    group_id: int
    name: str
    stage: PetStage
    form: str = "普通"

    hunger: int = 100
    mood: int   = 100
    clean: int  = 100
    energy: int = 100
    health: int = 100

    age: int        = 0
    experience: int = 0
    intimacy: int   = 0

    personality: PetPersonality = PetPersonality.LIVELY
    favorite_food: str | None   = None

    status: PetStatus                   = PetStatus.NORMAL
    status_expire_time: datetime | None = None

    # 当前装备的装扮道具
    dress_hat: str | None        = None
    dress_clothes: str | None    = None
    dress_accessory: str | None  = None
    dress_background: str | None = None

    last_update: datetime = field(default_factory=utc_now)
    # 每项衰减的小数余量随状态持久化，保证短周期与离线结算一致。
    decay_remainders: dict[str, float] = field(default_factory=dict)
    last_feed: datetime | None    = None
    last_clean: datetime | None   = None
    last_play: datetime | None    = None
    last_train: datetime | None   = None
    last_explore: datetime | None = None

    likes: int = 0

    # 数据库乐观锁版本不参与业务属性增量。
    version: int = 0

    created_at: datetime = field(default_factory=utc_now)
    _persisted_state: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    _DELTA_FIELDS: ClassVar[frozenset[str]] = frozenset(
        (*_STAT_FIELDS, "age", "experience", "intimacy", "likes")
    )
    _MERGE_FIELDS: ClassVar[tuple[str, ...]] = (
        "name",
        "stage",
        "form",
        *_STAT_FIELDS,
        "age",
        "experience",
        "intimacy",
        "personality",
        "favorite_food",
        "status",
        "status_expire_time",
        *(field_name for _slot_name, field_name in _DRESS_SLOT_FIELDS),
        "last_update",
        "decay_remainders",
        "last_feed",
        "last_clean",
        "last_play",
        "last_train",
        "last_explore",
        "likes",
    )

    def mark_persisted(self) -> None:
        """记录当前业务字段，作为后续三方合并的共同基线。"""
        self._persisted_state = {name: getattr(self, name) for name in self._MERGE_FIELDS}

    def merged_onto(self, latest: "Pet") -> "Pet":
        """把本地修改三方合并到数据库中的最新宠物快照。"""
        if not self._persisted_state:
            merged = replace(self, version=latest.version)
            merged._persisted_state = dict(latest._persisted_state)
            return merged

        merged = replace(latest)
        for name in self._MERGE_FIELDS:
            original = self._persisted_state[name]
            desired  = getattr(self, name)
            if desired == original:
                continue
            if name in self._DELTA_FIELDS:
                latest_value   = cast(int, getattr(latest, name))
                desired_value  = cast(int, desired)
                original_value = cast(int, original)
                value          = latest_value + desired_value - original_value
                if name in _STAT_FIELDS:
                    value = min(MAX_STAT_VALUE, max(0, value))
                setattr(merged, name, value)
            else:
                setattr(merged, name, desired)
        merged._persisted_state = dict(latest._persisted_state)
        return merged

    @property
    def care_score(self) -> float:
        """返回 0.0～1.0 的五项属性平均照顾评分。"""
        avg_stats = sum(cast(int, getattr(self, name)) for name in _STAT_FIELDS) / len(_STAT_FIELDS)
        return avg_stats / MAX_STAT_VALUE

    def update_stat(
        self, stat: str, delta: int, max_val: int = MAX_STAT_VALUE, min_val: int = 0
    ) -> int:
        """在指定上下界内增减一个合法的宠物属性。"""
        if stat not in _STAT_FIELDS:
            raise ValueError(f"未知宠物属性: {stat}")
        current   = cast(int, getattr(self, stat))
        new_value = min(max_val, max(min_val, current + delta))
        setattr(self, stat, new_value)
        return new_value

    def can_interact(self) -> bool:
        """只有正常状态的宠物允许执行日常互动。"""
        return self.status == PetStatus.NORMAL

    def get_dress_slots(self) -> dict[str, str | None]:
        """按展示名称返回全部装扮槽位。"""
        return {
            slot_name: cast(str | None, getattr(self, field_name))
            for slot_name, field_name in _DRESS_SLOT_FIELDS
        }

    def get_dress_mood_bonus(self) -> int:
        """汇总当前有效装扮带来的心情加成。"""
        bonus = 0
        for _slot_name, field_name in _DRESS_SLOT_FIELDS:
            item_id = getattr(self, field_name)
            item    = DEFAULT_DRESS_ITEMS.get(item_id) if item_id else None
            if item is not None:
                bonus += int(item.get("mood_bonus", 0))
        return bonus
