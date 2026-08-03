"""群内用户资产、行为计数、权限状态和并发合并模型。"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import ClassVar, cast

from ..utils.time import utc_now

_ACTION_NAMES = (
    "feed",
    "clean",
    "play",
    "train",
    "explore",
    "visit",
    "gift",
    "free_feed",
    "message",
)
_DAILY_COUNTER_FIELDS = tuple(f"today_{action}_count" for action in _ACTION_NAMES)
_TOTAL_COUNTER_FIELDS = tuple(f"total_{action}_count" for action in _ACTION_NAMES)


@dataclass
class User:
    """保存单个用户在一个群内的资产、限额计数和管理状态。"""

    user_id: str
    group_id: int

    coins: int = 100
    friendship_points: int = 0

    # 每日计数
    today_coins_earned: int = 0
    today_feed_count: int = 0
    today_clean_count: int = 0
    today_play_count: int = 0
    today_train_count: int = 0
    today_explore_count: int = 0
    today_visit_count: int = 0
    today_gift_count: int = 0
    today_free_feed_count: int = 0
    today_message_count: int = 0

    # 累计计数（用于称号系统）
    total_feed_count: int = 0
    total_clean_count: int = 0
    total_play_count: int = 0
    total_train_count: int = 0
    total_explore_count: int = 0
    total_visit_count: int = 0
    total_gift_count: int = 0
    total_free_feed_count: int = 0
    total_message_count: int = 0

    # 称号列表
    titles: list[str] = field(default_factory=list)

    last_visit_time: datetime | None = None
    last_gift_time: datetime | None = None

    trustee_until: datetime | None = None

    is_banned: bool = False
    ban_until: datetime | None = None

    created_at: datetime = field(default_factory=utc_now)
    last_active: datetime = field(default_factory=utc_now)

    # 数据库乐观锁版本不参与业务计数增量。
    version: int = 0
    _persisted_state: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    _DELTA_FIELDS: ClassVar[frozenset[str]] = frozenset(
        (
            "coins",
            "friendship_points",
            "today_coins_earned",
            *_DAILY_COUNTER_FIELDS,
            *_TOTAL_COUNTER_FIELDS,
        )
    )
    _MERGE_FIELDS: ClassVar[tuple[str, ...]] = (
        "coins",
        "friendship_points",
        "today_coins_earned",
        *_DAILY_COUNTER_FIELDS,
        *_TOTAL_COUNTER_FIELDS,
        "titles",
        "last_visit_time",
        "last_gift_time",
        "trustee_until",
        "is_banned",
        "ban_until",
        "last_active",
    )

    def mark_persisted(self) -> None:
        """记录当前业务字段，作为后续三方合并的共同基线。"""
        self._persisted_state = {
            name: list(getattr(self, name)) if name == "titles" else getattr(self, name)
            for name in self._MERGE_FIELDS
        }

    def merged_onto(self, latest: "User") -> "User":
        """把本地资产、计数和状态修改三方合并到数据库最新快照。"""
        if not self._persisted_state:
            merged = replace(self, version=latest.version)
            merged._persisted_state = dict(latest._persisted_state)
            return merged

        merged = replace(latest, titles=list(latest.titles))
        for name in self._MERGE_FIELDS:
            original = self._persisted_state[name]
            desired = getattr(self, name)
            if desired == original:
                continue
            if name in self._DELTA_FIELDS:
                latest_value = cast(int, getattr(latest, name))
                desired_value = cast(int, desired)
                original_value = cast(int, original)
                setattr(merged, name, latest_value + desired_value - original_value)
            elif name == "titles":
                original_titles = cast(list[str], original)
                desired_titles = cast(list[str], desired)
                removed = set(original_titles) - set(desired_titles)
                additions = [title for title in desired_titles if title not in original_titles]
                merged.titles = [title for title in latest.titles if title not in removed]
                merged.titles.extend(title for title in additions if title not in merged.titles)
            else:
                setattr(merged, name, desired)
        merged._persisted_state = dict(latest._persisted_state)
        return merged

    def can_do_action(self, action: str, count: int, daily_limit: int) -> bool:
        """判断已知动作增加指定次数后是否仍在每日上限内。"""
        if action not in _ACTION_NAMES:
            raise ValueError(f"未知每日动作: {action}")
        current_count = cast(int, getattr(self, f"today_{action}_count"))
        return current_count + count <= daily_limit

    def increment_action(self, action: str, count: int = 1) -> None:
        """同时增加已知动作的每日计数和累计计数。"""
        if action not in _ACTION_NAMES:
            raise ValueError(f"未知计数动作: {action}")
        if count <= 0:
            raise ValueError("动作增量必须为正数")

        today_attr = f"today_{action}_count"
        current = cast(int, getattr(self, today_attr))
        setattr(self, today_attr, current + count)

        total_attr = f"total_{action}_count"
        total_current = cast(int, getattr(self, total_attr))
        setattr(self, total_attr, total_current + count)

    def is_trustee_active(self) -> bool:
        """判断付费托管是否仍在有效期内。"""
        return self.trustee_until is not None and utc_now() < self.trustee_until

    def is_banned_active(self) -> bool:
        """判断封禁是否有效，并在内存中清除已经到期的封禁。"""
        if not self.is_banned:
            return False
        if self.ban_until is None:
            return True
        if utc_now() >= self.ban_until:
            # 自动解除过期封禁
            self.is_banned = False
            self.ban_until = None
            return False
        return True

    def add_title(self, title: str) -> bool:
        """添加称号，并返回本次是否为新获得。"""
        if title not in self.titles:
            self.titles.append(title)
            return True
        return False
