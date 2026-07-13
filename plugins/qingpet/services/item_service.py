import logging

from ..models import Inventory, Item
from ..utils.constants import DEFAULT_ITEMS
from ..utils.validators import validate_item_amount
from .database import Database

logger = logging.getLogger(__name__)


class ItemService:
    def __init__(self, db: Database):
        self.db = db
        self.items_cache = self._load_items()

    def _load_items(self) -> dict[str, Item]:
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

    def get_item_by_name(self, name: str) -> tuple[str, Item]:
        """通过道具名搜索道具"""
        for item_id, item in self.items_cache.items():
            if item.name == name or item_id == name:
                return item_id, item
        return None, None

    def get_all_items(self) -> dict[str, Item]:
        return self.items_cache

    def buy_item(self, user_id: str, group_id: int, item_id: str, amount: int = 1) -> tuple[bool, str]:
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
        success, current_coins = self.db.purchase_item_atomic(
            user_id, group_id, item_id, amount, total_cost
        )
        if success:
            return True, f"购买成功！花费{total_cost}金币，获得{amount}个{item.name}"
        if current_coins >= 0:
            return False, f"金币不足，需要{total_cost}金币，当前{current_coins}金币"
        return False, "购买失败"

    def get_inventory(self, user_id: str, group_id: int) -> Inventory:
        return self.db.get_or_create_inventory(user_id, group_id)

    def add_item_to_inventory(self, user_id: str, group_id: int, item_id: str, amount: int = 1) -> bool:
        inventory = self.db.get_or_create_inventory(user_id, group_id)
        inventory.add_item(item_id, amount)
        return self.db.update_inventory(inventory)
