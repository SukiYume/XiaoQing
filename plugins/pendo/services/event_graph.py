"""Event graph loading helpers.

The graph model treats only item rows as schedulable leaves. Collections are
non-schedulable headers used to group multi-node and recurring leaves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..models.item import EventItem


@dataclass
class EventFamily:
    kind: str
    collection: dict[str, Any] | None = None
    leaf: EventItem | None = None
    children: list[EventItem] = field(default_factory=list)


class EventGraphService:
    """Load event leaves and their optional collection context."""

    def __init__(self, db: Any):
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

    def list_leaf_events_for_range(
        self, owner_id: str, start_time: str, end_time: str
    ) -> list[EventItem]:
        """Return leaf events whose concrete time overlaps a range."""
        range_start = datetime.fromisoformat(start_time)
        range_end = datetime.fromisoformat(end_time)
        normal_events, repeat_events = self.db.get_events_for_range(owner_id, start_time, end_time)

        leaves: list[EventItem] = []
        for event in normal_events + repeat_events:
            if not isinstance(event, EventItem) or not event.start_time:
                continue
            if event.event_role not in {"single", "multi_node_child", "recurring_occurrence"}:
                continue
            event_start = datetime.fromisoformat(event.start_time)
            event_end = datetime.fromisoformat(event.end_time) if event.end_time else event_start
            if event_start <= range_end and event_end >= range_start:
                leaves.append(event)
        leaves.sort(key=lambda item: (item.start_time or "", item.id))
        return leaves

    @staticmethod
    def format_title_context(leaf: EventItem, collection: dict[str, Any] | None) -> str:
        if collection:
            return f"{collection.get('title') or '无标题'} · {leaf.title or '无标题'}"
        return leaf.title or "无标题"
