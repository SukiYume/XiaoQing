"""加载事件叶节点及其可选集合上下文。

事件图只把 ``items`` 行视为可调度叶节点；集合只是多节点或重复事件的分组头，
自身不参与调度。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..models.item import EventItem

if TYPE_CHECKING:
    from .db import Database


@dataclass(slots=True)
class EventFamily:
    """一个事件叶节点或集合及其完整子节点。"""

    kind: str
    collection: dict[str, Any] | None = None
    leaf: EventItem | None = None
    children: list[EventItem] = field(default_factory=list)


class EventGraphService:
    """按 ID 统一解析单事件、集合子节点和集合头。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def load_by_id(self, owner_id: str, event_or_collection_id: str) -> EventFamily:
        item = self.db.get_item(event_or_collection_id, owner_id=owner_id)
        if isinstance(item, EventItem):
            collection = None
            children: list[EventItem] = []
            kind = "single"
            if item.event_collection_id:
                collection = self.db.get_event_collection(item.event_collection_id, owner_id)
                if collection:
                    kind = str(collection.get("kind") or item.event_collection_kind or "single")
                    children = self.db.get_collection_events(item.event_collection_id, owner_id)
                else:
                    kind = item.event_collection_kind or "single"
            return EventFamily(kind=kind, collection=collection, leaf=item, children=children)

        collection = self.db.get_event_collection(event_or_collection_id, owner_id)
        if collection:
            children = self.db.get_collection_events(event_or_collection_id, owner_id)
            return EventFamily(
                kind=str(collection.get("kind") or "single"),
                collection=collection,
                leaf=None,
                children=children,
            )

        return EventFamily(kind="missing")
