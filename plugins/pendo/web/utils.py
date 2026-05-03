"""Small shared helpers for Pendo web API and analytics modules."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


def amount_filter_cents(value: float) -> int:
    """Convert a yuan amount filter to non-negative cents."""
    return max(0, int(round(float(value) * 100)))


def collection_payload(collection: dict[str, Any] | None) -> dict[str, Any] | None:
    if not collection:
        return None
    return {
        "id": collection.get("id"),
        "kind": collection.get("kind"),
        "title": collection.get("title"),
        "category": collection.get("category"),
        "location": collection.get("location"),
        "notes": collection.get("notes"),
    }


def item_to_dict(item: Any) -> dict[str, Any]:
    return item.to_dict() if hasattr(item, "to_dict") else {}


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    text = str(value)
    try:
        if "T" in text:
            return datetime.fromisoformat(text).date()
        return datetime.fromisoformat(f"{text}T00:00:00").date()
    except ValueError:
        return None
