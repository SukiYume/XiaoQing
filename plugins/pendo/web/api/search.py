"""Search endpoint."""

from fastapi import APIRouter, Depends, HTTPException, Query

from ...services.db import Database
from ..deps import get_current_user, get_db
from ..utils import collection_payload

router = APIRouter()


@router.get("/search")
def search_items(
    q: str,
    type: str | None = None,
    category: str | None = None,
    ledger_category: str | None = None,
    status: str | None = None,
    transaction_type: str | None = None,
    account_name: str | None = None,
    merchant: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1, le=100),
    limit: int = Query(50, ge=1, le=100),
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Full-text search across all item types."""
    if not q.strip():
        raise HTTPException(status_code=422, detail="Search query cannot be empty")

    filters = {}
    if type:
        filters["type"] = type
    if ledger_category:
        filters["ledger_category"] = ledger_category
    elif category:
        if type == "ledger":
            filters["ledger_category"] = category
        else:
            filters["category"] = category
    if status:
        filters["status"] = status
    if transaction_type:
        filters["transaction_type"] = transaction_type
    if account_name:
        filters["account_name"] = account_name
    if merchant:
        filters["merchant"] = merchant

    resolved_page = max(1, int(page or 1))
    resolved_page_size = max(1, int(page_size or limit or 50))
    offset = (resolved_page - 1) * resolved_page_size

    results, total = db.search_items_page(
        owner_id,
        q.strip(),
        filters=filters,
        limit=resolved_page_size,
        offset=offset,
    )

    collection_cache = {}

    def to_dict(item):
        data = item.to_dict() if hasattr(item, "to_dict") else {}
        collection_id = data.get("event_collection_id")
        if data.get("type") == "event" and collection_id:
            if collection_id not in collection_cache:
                collection_cache[collection_id] = db.get_event_collection(collection_id, owner_id)
            data["collection"] = collection_payload(collection_cache[collection_id])
        return data

    return {
        "ok": True,
        "data": {
            "items": [to_dict(item) for item in results],
            "total": total,
            "page": resolved_page,
            "page_size": resolved_page_size,
        },
        "message": "",
    }
