"""内置道具查询、购买和背包读取服务。"""

from collections.abc import Mapping
from types import MappingProxyType

from ..models import Inventory, Item
from ..utils.constants import DEFAULT_ITEMS
from ..utils.validators import validate_item_amount
from .database import Database

_ITEMS: Mapping[str, Item] = MappingProxyType(
    {
        item_id: Item(
            name=data["name"],
            item_type=data["type"],
            rarity=data["rarity"],
            price=data["price"],
            hunger_gain=data.get("hunger_gain", 0),
            mood_gain=data.get("mood_gain", 0),
            health_gain=data.get("health_gain", 0),
            clean_gain=data.get("clean_gain", 0),
            exp_gain=data.get("exp_gain", 0),
            trustee_hours=data.get("trustee_hours", 0),
        )
        for item_id, data in DEFAULT_ITEMS.items()
    }
)


class ItemService:
    """解析内置道具，并把购买交给数据库原子事务结算。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get_item(self, item_id: str) -> Item | None:
        """按规范 ID 查询道具。"""
        return _ITEMS.get(item_id)

    def resolve_item(self, identifier: str) -> tuple[str, Item] | None:
        """优先按 ID、再按中文名称解析道具。"""
        item = _ITEMS.get(identifier)
        if item is not None:
            return identifier, item
        for item_id, candidate in _ITEMS.items():
            if candidate.name == identifier:
                return item_id, candidate
        return None

    def get_all_items(self) -> Mapping[str, Item]:
        """返回不可变的内置道具映射。"""
        return _ITEMS

    def buy_item(
        self,
        user_id: str,
        group_id: int,
        identifier: str,
        amount: int = 1,
    ) -> tuple[bool, str]:
        """校验数量和道具后，以单个数据库事务完成扣款与入库。"""
        valid, message = validate_item_amount(amount)
        if not valid:
            return False, message

        resolved = self.resolve_item(identifier)
        if resolved is None:
            return False, f"商品 '{identifier}' 不存在\n使用 /宠物 商店 查看可购买的道具"
        item_id, item = resolved

        total_cost = item.price * amount
        success, current_coins = self.db.purchase_item_atomic(
            user_id,
            group_id,
            item_id,
            amount,
            total_cost,
        )
        if success:
            return True, f"购买成功！花费{total_cost}金币，获得{amount}个{item.name}"
        if current_coins >= 0:
            return False, f"金币不足，需要{total_cost}金币，当前{current_coins}金币"
        return False, "购买失败"

    def get_inventory(self, user_id: str, group_id: int) -> Inventory:
        """读取用户背包；不存在时由数据库创建空背包。"""
        return self.db.get_or_create_inventory(user_id, group_id)
