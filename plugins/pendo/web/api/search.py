"""Search endpoint."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ...services.db import Database
from ..deps import get_db, get_current_user

router = APIRouter()


@router.get("/search")
def search_items(
    q: str,
    type: Optional[str] = None,
    category: Optional[str] = None,
    ledger_category: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
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

    results = db.search_items(owner_id, q.strip(), filters=filters, limit=limit)

    def to_dict(item):
        return item.to_dict() if hasattr(item, "to_dict") else {}

    return {
        "ok": True,
        "data": {
            "items": [to_dict(item) for item in results],
            "total": len(results),
        },
        "message": "",
    }
