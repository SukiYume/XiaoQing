from dataclasses import dataclass
from ..utils.constants import ItemType, ItemRarity


@dataclass
class Item:
    item_id: str
    name: str
    item_type: ItemType
    rarity: ItemRarity
    
    price: int = 0
    hunger_gain: int = 0
    mood_gain: int = 0
    health_gain: int = 0
    clean_gain: int = 0
    energy_cost: int = 0
    exp_gain: int = 0
    intimacy_gain: int = 0
    trustee_hours: int = 0
    
    def is_food(self) -> bool:
        return self.item_type == ItemType.FOOD
    
    def is_toy(self) -> bool:
        return self.item_type == ItemType.TOY
    
    def is_medicine(self) -> bool:
        return self.item_type == ItemType.MEDICINE
    
    def is_decoration(self) -> bool:
        return self.item_type == ItemType.DECORATION
    
    def is_consumable(self) -> bool:
        return self.item_type in (ItemType.FOOD, ItemType.TOY, ItemType.MEDICINE,
                                  ItemType.ACCELERATION, ItemType.TRUSTEESHIP)