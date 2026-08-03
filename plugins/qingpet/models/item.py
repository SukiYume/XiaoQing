"""商店与背包展示所需的道具数据模型。"""

from dataclasses import dataclass

from ..utils.constants import ItemRarity, ItemType


@dataclass(frozen=True, slots=True)
class Item:
    """从内置道具表构建的只读式领域数据。"""

    name: str
    item_type: ItemType
    rarity: ItemRarity

    price: int = 0
    hunger_gain: int = 0
    mood_gain: int = 0
    health_gain: int = 0
    clean_gain: int = 0
    exp_gain: int = 0
    trustee_hours: int = 0
