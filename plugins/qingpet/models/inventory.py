"""用户在单个群内的道具背包及乐观并发合并状态。"""

from dataclasses import dataclass, field


@dataclass
class Inventory:
    """保存背包数量，并记录最近一次持久化快照以合并并发增量。"""

    user_id: str
    group_id: int
    items: dict[str, int] = field(default_factory=dict)

    # ``version`` 由数据库乐观锁维护；私有快照只参与当前进程内的增量合并。
    version: int = 0
    _persisted_items: dict[str, int] = field(default_factory=dict, repr=False, compare=False)
    _has_persisted_state: bool = field(default=False, repr=False, compare=False)

    def mark_persisted(self) -> None:
        """把当前背包记为下一次并发合并的持久化基线。"""
        self._persisted_items     = dict(self.items)
        self._has_persisted_state = True

    def merged_onto(self, latest_items: dict[str, int]) -> dict[str, int]:
        """把本地相对基线的数量增量叠加到数据库最新背包。"""
        if not self._has_persisted_state:
            return dict(self.items)
        merged = dict(latest_items)
        for item_id in set(self._persisted_items) | set(self.items):
            delta = int(self.items.get(item_id, 0)) - int(self._persisted_items.get(item_id, 0))
            if delta == 0:
                continue
            updated = int(merged.get(item_id, 0)) + delta
            if updated > 0:
                merged[item_id] = updated
            else:
                merged.pop(item_id, None)
        return merged

    def has_item(self, item_id: str, amount: int = 1) -> bool:
        """判断指定道具是否达到所需数量。"""
        return self.items.get(item_id, 0) >= amount

    def add_item(self, item_id: str, amount: int = 1) -> None:
        """增加指定道具数量。"""
        self.items[item_id] = self.items.get(item_id, 0) + amount

    def remove_item(self, item_id: str, amount: int = 1) -> bool:
        """库存充足时扣除道具，并在数量归零时移除键。"""
        current = self.items.get(item_id, 0)
        if current < amount:
            return False
        self.items[item_id] = current - amount
        if self.items[item_id] == 0:
            del self.items[item_id]
        return True

    def get_item_count(self, item_id: str) -> int:
        """返回指定道具数量；背包中不存在时返回 0。"""
        return self.items.get(item_id, 0)
