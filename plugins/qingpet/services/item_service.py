import logging
from typing import Dict, Tuple

from ..models import Item, Inventory
from ..utils.constants import DEFAULT_ITEMS
from ..utils.validators import validate_item_amount
from .database import Database

logger = logging.getLogger(__name__)


class ItemService:
    def __init__(self, db: Database):
        self.db = db
        self.items_cache = self._load_items()

    def _load_items(self) -> Dict[str, Item]:
        items = {}
        for item_id, data in DEFAULT_ITEMS.items():
            items[item_id] = Item(
                item_id=item_id,
                name=data["name"],
                item_type=data["type"],
                rarity=data["rarity"],
                price=data["price"],
                hunger_gain=data.get("hunger_gain", 0),
                mood_gain=data.get("mood_gain", 0),
                health_gain=data.get("health_gain", 0),
                clean_gain=data.get("clean_gain", 0),
                energy_cost=data.get("energy_cost", 0),
                exp_gain=data.get("exp_gain", 0),
                intimacy_gain=data.get("intimacy_gain", 0),
                trustee_hours=data.get("trustee_hours", 0)
            )
        return items

    def get_item(self, item_id: str) -> Item:
        return self.items_cache.get(item_id)

    def get_item_by_name(self, name: str) -> Tuple[str, Item]:
        """通过道具名搜索道具"""
        for item_id, item in self.items_cache.items():
            if item.name == name or item_id == name:
                return item_id, item
        return None, None

    def get_all_items(self) -> Dict[str, Item]:
        return self.items_cache

    def buy_item(self, user_id: str, group_id: int, item_id: str, amount: int = 1) -> Tuple[bool, str]:
        # 校验购买数量（Issue #17）
        valid, msg = validate_item_amount(amount)
        if not valid:
            return False, msg

        # 支持通过名称搜索
        item = self.get_item(item_id)
        if not item:
            found_id, found_item = self.get_item_by_name(item_id)
            if found_item:
                item = found_item
                item_id = found_id
            else:
                return False, f"商品 '{item_id}' 不存在\n使用 /宠物 商店 查看可购买的道具"

        total_cost = item.price * amount
        user = self.db.get_user(user_id, group_id)
        if not user:
            return False, "用户不存在"

        if user.coins < total_cost:
            return False, f"金币不足，需要{total_cost}金币，当前{user.coins}金币"

        user.coins -= total_cost

        inventory = self.db.get_or_create_inventory(user_id, group_id)
        inventory.add_item(item_id, amount)

        success = self.db.update_user(user) and self.db.update_inventory(inventory)
        if success:
            return True, f"购买成功！花费{total_cost}金币，获得{amount}个{item.name}"
        return False, "购买失败"

    def use_item(self, user_id: str, group_id: int, item_id: str) -> Tuple[bool, str]:
        item = self.get_item(item_id)
        if not item:
            return False, "道具不存在"

        if not item.is_consumable():
            return False, "该道具无法使用"

        inventory = self.db.get_or_create_inventory(user_id, group_id)
        if not inventory.has_item(item_id):
            return False, "背包中没有该道具"

        inventory.remove_item(item_id)

        result = {
            "item": item,
            "effects": {}
        }

        return True, result

    def get_inventory(self, user_id: str, group_id: int) -> Inventory:
        return self.db.get_or_create_inventory(user_id, group_id)

    def add_item_to_inventory(self, user_id: str, group_id: int, item_id: str, amount: int = 1) -> bool:
        inventory = self.db.get_or_create_inventory(user_id, group_id)
        inventory.add_item(item_id, amount)
        return self.db.update_inventory(inventory)