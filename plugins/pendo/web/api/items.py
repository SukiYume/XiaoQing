"""Unified items CRUD API."""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ...services.db import Database
from ...models.item import ItemType
from ..deps import get_db, get_current_user

router = APIRouter()


class ItemCreate(BaseModel):
    type: str
    title: str = ""
    content: str = ""
    tags: list[str] = []
    category: str = "未分类"
    # Event fields
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    location: Optional[str] = None
    timezone: Optional[str] = None
    remind_times: Optional[list[str]] = None
    rrule: Optional[str] = None
    notes: Optional[str] = None
    # Task fields
    due_time: Optional[str] = None
    priority: Optional[int] = None
    status: Optional[str] = None
    # Diary fields
    diary_date: Optional[str] = None
    mood: Optional[str] = None
    mood_score: Optional[int] = None
    weather: Optional[str] = None
    template_id: Optional[str] = None
    # Ledger fields
    amount: Optional[float] = None
    direction: Optional[str] = None
    ledger_category: Optional[str] = None
    ledger_date: Optional[str] = None
    remark: Optional[str] = None


class ItemUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[list[str]] = None
    category: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    location: Optional[str] = None
    timezone: Optional[str] = None
    remind_times: Optional[list[str]] = None
    rrule: Optional[str] = None
    notes: Optional[str] = None
    due_time: Optional[str] = None
    priority: Optional[int] = None
    status: Optional[str] = None
    diary_date: Optional[str] = None
    mood: Optional[str] = None
    mood_score: Optional[int] = None
    weather: Optional[str] = None
    template_id: Optional[str] = None
    amount: Optional[float] = None
    direction: Optional[str] = None
    ledger_category: Optional[str] = None
    ledger_date: Optional[str] = None
    remark: Optional[str] = None


def _item_to_dict(item) -> dict:
    """Convert Item dataclass to API response dict."""
    if hasattr(item, "to_dict"):
        return item.to_dict()
    return {}


@router.get("/items")
def list_items(
    type: Optional[str] = None,
    status: Optional[str] = None,
    category: Optional[str] = None,
    priority: Optional[int] = None,
    direction: Optional[str] = None,
    date_field: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    range: Optional[str] = Query(None, alias="range"),
    sort: str = "created_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 20,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """List items with filtering and pagination."""
    filters = {}
    if type:
        filters["type"] = type
    if status:
        filters["status"] = status
    if category:
        filters["category"] = category

    # Date filtering: support both direct params and range="start..end" syntax
    if start_date and end_date:
        # Direct params from frontend (start_date, end_date, date_field)
        if not date_field:
            date_field = "created_at"
            if type == "event":
                date_field = "start_time"
            elif type == "task":
                date_field = "due_time"
            elif type == "diary":
                date_field = "diary_date"
            elif type == "ledger":
                date_field = "ledger_date"
        filters["date_field"] = date_field
        filters["start_date"] = start_date
        filters["end_date"] = end_date
    elif range:
        # Legacy range param: "2026-03-01..2026-03-31"
        parts = range.split("..")
        if len(parts) == 2:
            _df = date_field or "created_at"
            if not date_field:
                if type == "event":
                    _df = "start_time"
                elif type == "task":
                    _df = "due_time"
                elif type == "diary":
                    _df = "diary_date"
                elif type == "ledger":
                    _df = "ledger_date"
            filters["date_field"] = _df
            filters["start_date"] = parts[0]
            filters["end_date"] = parts[1]

    offset = (page - 1) * page_size
    items = db.get_items(owner_id, filters=filters, limit=page_size, offset=offset)

    # Post-filter for fields not supported by get_items() directly
    if direction:
        items = [i for i in items if getattr(i, "direction", None) == direction]
    if priority is not None:
        items = [i for i in items if getattr(i, "priority", None) == priority]

    # Get total count via COUNT query for pagination
    conn = db.get_connection()
    count_where = ["owner_id = ?", "deleted = 0"]
    count_params: list = [owner_id]
    if type:
        count_where.append("type = ?")
        count_params.append(type)
    if status:
        count_where.append("status = ?")
        count_params.append(status)
    if category:
        count_where.append("category = ?")
        count_params.append(category)
    total = conn.execute(
        f"SELECT COUNT(*) FROM items WHERE {' AND '.join(count_where)}",
        count_params,
    ).fetchone()[0]

    return {
        "ok": True,
        "data": {
            "items": [_item_to_dict(item) for item in items],
            "total": total,
        },
        "message": "",
    }


@router.get("/items/{item_id}")
def get_item(
    item_id: str,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Get single item by ID."""
    item = db.get_item(item_id, owner_id=owner_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True, "data": _item_to_dict(item), "message": ""}


@router.post("/items", status_code=201)
def create_item(
    body: ItemCreate,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Create a new item."""
    try:
        ItemType(body.type)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid type: {body.type}")

    now = datetime.now().isoformat()
    item_data = {
        "type": body.type,
        "title": body.title,
        "content": body.content,
        "tags": body.tags,
        "category": body.category,
        "owner_id": owner_id,
        "created_at": now,
        "updated_at": now,
        "context": {},
        "deleted": False,
    }

    # Add type-specific fields (only non-None)
    for field in body.model_fields:
        if field in ("type", "title", "content", "tags", "category"):
            continue
        value = getattr(body, field)
        if value is not None:
            item_data[field] = value

    # Default ledger_date to today if not set
    if body.type == "ledger" and not body.ledger_date:
        item_data["ledger_date"] = datetime.now().strftime("%Y-%m-%d")

    # Default task status
    if body.type == "task" and not body.status:
        item_data["status"] = "todo"
    if body.type == "task" and not body.priority:
        item_data["priority"] = 3

    item_id = db.insert_item(item_data)
    db.log_operation(owner_id, "create", item_type=body.type, item_id=item_id)

    return {"ok": True, "data": {"id": item_id}, "message": "创建成功"}


@router.put("/items/{item_id}")
def update_item(
    item_id: str,
    body: ItemUpdate,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Update an item."""
    item = db.get_item(item_id, owner_id=owner_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    updates["updated_at"] = datetime.now().isoformat()
    success = db.update_item(item_id, updates, owner_id=owner_id)
    if not success:
        raise HTTPException(status_code=500, detail="Update failed")

    db.log_operation(owner_id, "update", item_id=item_id)
    return {"ok": True, "data": {"id": item_id}, "message": "更新成功"}


@router.delete("/items/{item_id}")
def delete_item(
    item_id: str,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Soft delete an item."""
    item = db.get_item(item_id, owner_id=owner_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    success = db.delete_item(item_id, soft=True, owner_id=owner_id)
    if not success:
        raise HTTPException(status_code=500, detail="Delete failed")

    db.log_operation(owner_id, "delete", item_id=item_id)
    return {"ok": True, "data": {"id": item_id}, "message": "已删除"}
