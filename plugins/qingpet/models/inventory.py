from dataclasses import dataclass, field
from typing import Dict


@dataclass
class Inventory:
    user_id: str
    group_id: int
    items: Dict[str, int] = field(default_factory=dict)
    
    def has_item(self, item_id: str, amount: int = 1) -> bool:
        return self.items.get(item_id, 0) >= amount
    
    def add_item(self, item_id: str, amount: int = 1) -> None:
        current = self.items.get(item_id, 0)
        self.items[item_id] = current + amount
    
    def remove_item(self, item_id: str, amount: int = 1) -> bool:
        current = self.items.get(item_id, 0)
        if current < amount:
            return False
        self.items[item_id] = current - amount
        if self.items[item_id] == 0:
            del self.items[item_id]
        return True
    
    def get_item_count(self, item_id: str) -> int:
        return self.items.get(item_id, 0)