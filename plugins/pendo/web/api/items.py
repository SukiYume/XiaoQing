"""Unified items CRUD API."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ...services.db import Database
from ...models.item import ItemType
from ...utils.settings_utils import resolve_default_category
from ...utils.time_utils import now_in_timezone
from ...utils.validators import (
    derive_reminder_rules,
    normalize_diary_fields,
    normalize_event_fields,
    normalize_ledger_fields,
    normalize_note_fields,
    normalize_task_fields,
)
from ..deps import get_db, get_current_user

router = APIRouter()

DEFAULT_DATE_FIELDS: dict[str | None, str] = {
    "event": "start_time",
    "task": "due_time",
    "diary": "diary_date",
    "ledger": "ledger_date",
    "note": "created_at",
    None: "created_at",
}

ALLOWED_DATE_FIELDS_BY_TYPE: dict[str | None, set[str]] = {
    "event": {"created_at", "start_time", "end_time"},
    "task": {"created_at", "due_time"},
    "diary": {"created_at", "diary_date"},
    "ledger": {"created_at", "ledger_date"},
    "note": {"created_at"},
    None: set(Database.ALLOWED_DATE_FIELDS),
}

EVENT_MUTABLE_FIELDS = {
    "title",
    "category",
    "start_time",
    "end_time",
    "location",
    "timezone",
    "remind_times",
    "reminder_rules",
    "rrule",
    "notes",
}

TASK_MUTABLE_FIELDS = {
    "title",
    "content",
    "category",
    "due_time",
    "priority",
    "status",
    "completed_at",
    "progress",
    "estimate",
}

NOTE_MUTABLE_FIELDS = {
    "title",
    "content",
    "category",
    "tags",
}

DIARY_MUTABLE_FIELDS = {
    "title",
    "content",
    "diary_date",
    "mood",
    "mood_score",
    "weather",
    "location",
    "template_id",
}


class ItemCreate(BaseModel):
    type: str
    title: Optional[str] = ""
    content: Optional[str] = ""
    tags: list[str] = []
    category: Optional[str] = None
    # Event fields
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    location: Optional[str] = None
    timezone: Optional[str] = None
    remind_times: Optional[list[str]] = None
    reminder_rules: Optional[list[dict]] = None
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
    reminder_rules: Optional[list[dict]] = None
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


def _resolve_date_field(type: Optional[str], date_field: Optional[str]) -> str:
    item_type = type if type in ALLOWED_DATE_FIELDS_BY_TYPE else None
    if not date_field:
        return DEFAULT_DATE_FIELDS.get(item_type, "created_at")

    allowed_fields = ALLOWED_DATE_FIELDS_BY_TYPE[item_type]
    if date_field not in allowed_fields:
        allowed_display = ", ".join(sorted(allowed_fields))
        type_label = item_type or "all"
        raise HTTPException(
            status_code=422,
            detail=f"Invalid date_field '{date_field}' for type '{type_label}'. Allowed values: {allowed_display}",
        )
    return date_field


def _resolve_category_field(type: Optional[str]) -> str:
    return "ledger_category" if type == "ledger" else "category"


def _has_other_diary_for_date(
    db: Database,
    owner_id: str,
    diary_date: str,
    exclude_item_id: Optional[str] = None,
) -> bool:
    conn = db.get_connection()
    if exclude_item_id:
        row = conn.execute(
            """
            SELECT 1 FROM items
            WHERE owner_id = ? AND type = 'diary' AND deleted = 0
              AND diary_date = ? AND id != ?
            LIMIT 1
            """,
            (owner_id, diary_date, exclude_item_id),
        ).fetchone()
        return row is not None

    return db.has_diary_for_date(owner_id, diary_date)


def _build_count_where(
    type, status, category, priority, direction, start_date, end_date, date_field,
    amount_min, amount_max, owner_id
):
    where = ["owner_id = ?", "deleted = 0"]
    params: list = [owner_id]
    category_field = _resolve_category_field(type)
    if type:       where.append("type = ?");             params.append(type)
    if status:     where.append("status = ?");           params.append(status)
    if category:   where.append(f"{category_field} = ?"); params.append(category)
    if priority is not None:
        where.append("priority = ?"); params.append(priority)
    if direction:  where.append("direction = ?");        params.append(direction)
    if amount_min is not None:
        where.append("amount >= ?"); params.append(amount_min)
    if amount_max is not None:
        where.append("amount <= ?"); params.append(amount_max)
    if start_date and end_date and date_field:
        where.append(f"{date_field} >= ?"); params.append(start_date)
        where.append(f"{date_field} <= ?"); params.append(end_date)
    return where, params


@router.get("/items/aggregate")
def aggregate_items(
    type: Optional[str] = None,
    direction: Optional[str] = None,
    category: Optional[str] = None,
    date_field: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    amount_min: Optional[float] = None,
    amount_max: Optional[float] = None,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Return income/expense totals for the given filters (full result set, not paginated)."""
    _df = _resolve_date_field(type, date_field) if (start_date and end_date) else None
    where, params = _build_count_where(
        type, None, category, None, direction, start_date, end_date, _df, amount_min, amount_max, owner_id
    )
    conn = db.get_connection()
    rows = conn.execute(
        f"SELECT direction, SUM(amount), COUNT(*) FROM items WHERE {' AND '.join(where)} GROUP BY direction",
        params,
    ).fetchall()
    income = expense = count = 0
    for row in rows:
        if row[0] == "income":   income  = float(row[1] or 0)
        elif row[0] == "expense": expense = float(row[1] or 0)
        count += int(row[2] or 0)
    return {"ok": True, "data": {"income": income, "expense": expense, "balance": income - expense, "count": count}}


@router.get("/items/categories")
def list_categories(
    type: Optional[str] = None,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Return distinct category values for the given type."""
    conn = db.get_connection()
    category_field = _resolve_category_field(type)
    where = ["owner_id = ?", "deleted = 0", f"{category_field} IS NOT NULL", f"{category_field} != ''"]
    params: list = [owner_id]
    if type:
        where.append("type = ?")
        params.append(type)
    rows = conn.execute(
        f"SELECT DISTINCT {category_field} FROM items WHERE {' AND '.join(where)} ORDER BY {category_field}",
        params,
    ).fetchall()
    return {"ok": True, "data": {"categories": [r[0] for r in rows]}}


@router.get("/items")
def list_items(
    type: Optional[str] = None,
    status: Optional[str] = None,
    category: Optional[str] = None,
    tags: Optional[str] = None,
    priority: Optional[int] = None,
    direction: Optional[str] = None,
    date_field: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    amount_min: Optional[float] = None,
    amount_max: Optional[float] = None,
    range: Optional[str] = Query(None, alias="range"),
    sort: str = "created_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 20,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """List items with filtering and pagination."""
    filters: dict = {}
    if type:      filters["type"] = type
    if status:    filters["status"] = status
    if category:
        filters[_resolve_category_field(type)] = category
    if tags:      filters["tags"] = tags
    if priority is not None: filters["priority"] = priority
    if direction: filters["direction"] = direction
    if amount_min is not None: filters["amount_min"] = amount_min
    if amount_max is not None: filters["amount_max"] = amount_max

    # Sorting
    _allowed_sort = {"created_at", "updated_at", "ledger_date", "due_time", "start_time", "diary_date", "amount"}
    if sort in _allowed_sort:
        filters["sort_field"] = sort
        filters["sort_order"] = order.upper()

    # Date filtering: support both direct params and range="start..end" syntax
    resolved_df: Optional[str] = None
    if start_date and end_date:
        resolved_df = _resolve_date_field(type, date_field)
        filters["date_field"] = resolved_df
        filters["start_date"] = start_date
        filters["end_date"] = end_date
    elif range:
        parts = range.split("..")
        if len(parts) == 2:
            resolved_df = _resolve_date_field(type, date_field)
            filters["date_field"] = resolved_df
            filters["start_date"] = parts[0]
            filters["end_date"] = parts[1]
            start_date, end_date = parts[0], parts[1]

    offset = (page - 1) * page_size
    items = db.get_items(owner_id, filters=filters, limit=page_size, offset=offset)

    # Count matching all filters
    count_where, count_params = _build_count_where(
        type, status, category, priority, direction, start_date, end_date, resolved_df,
        amount_min, amount_max, owner_id
    )
    conn = db.get_connection()
    if tags:
        total = conn.execute(
            f"SELECT COUNT(*) FROM items WHERE {' AND '.join(count_where)} AND tags LIKE ?",
            count_params + [f"%{tags}%"],
        ).fetchone()[0]
    else:
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

    now = now_in_timezone(owner_id, db).replace(tzinfo=None).isoformat()
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

    if body.type in {"event", "note"} and not str(item_data.get("category") or "").strip():
        item_data["category"] = resolve_default_category(db, owner_id)
    elif body.type in {"event", "note"} and str(item_data.get("category") or "").strip() == "未分类":
        item_data["category"] = resolve_default_category(db, owner_id)

    # Add type-specific fields (only non-None)
    for field in body.model_fields:
        if field in ("type", "title", "content", "tags", "category"):
            continue
        value = getattr(body, field)
        if value is not None:
            item_data[field] = value

    if body.type == "event":
        try:
            item_data = normalize_event_fields(item_data, partial=False)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    if body.type == "task":
        try:
            item_data = normalize_task_fields(item_data, partial=False)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    if body.type == "note":
        try:
            item_data = normalize_note_fields(item_data, partial=False)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    if body.type == "diary":
        try:
            item_data = normalize_diary_fields(item_data, partial=False)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if _has_other_diary_for_date(db, owner_id, item_data["diary_date"]):
            raise HTTPException(status_code=409, detail="Diary already exists for this date")
    if body.type == "ledger":
        try:
            item_data = normalize_ledger_fields(item_data, partial=False)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

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

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    item_type = item.type.value if hasattr(item.type, "value") else item.type
    if item_type == "event":
        try:
            merged = item.to_dict()
            if "reminder_rules" in updates and updates.get("reminder_rules") == []:
                merged["remind_times"] = []
                updates["remind_times"] = []
            elif "start_time" in updates and "reminder_rules" not in updates:
                old_rules = getattr(item, "reminder_rules", None) or derive_reminder_rules(
                    getattr(item, "start_time", None),
                    getattr(item, "remind_times", None),
                )
                if old_rules:
                    merged["reminder_rules"] = old_rules
            merged.update(updates)
            normalized = normalize_event_fields(merged, partial=False)
            for field in EVENT_MUTABLE_FIELDS:
                if field in normalized:
                    updates[field] = normalized[field]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    if item_type == "task":
        try:
            merged = item.to_dict()
            merged.update(updates)
            normalized = normalize_task_fields(merged, partial=False)
            for field in TASK_MUTABLE_FIELDS:
                if field in normalized:
                    updates[field] = normalized[field]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    if item_type == "note":
        try:
            merged = item.to_dict()
            merged.update(updates)
            normalized = normalize_note_fields(merged, partial=False)
            for field in NOTE_MUTABLE_FIELDS:
                if field in normalized:
                    updates[field] = normalized[field]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    if item_type == "diary":
        try:
            merged = item.to_dict()
            merged.update(updates)
            normalized = normalize_diary_fields(merged, partial=False)
            if _has_other_diary_for_date(db, owner_id, normalized["diary_date"], exclude_item_id=item_id):
                raise HTTPException(status_code=409, detail="Diary already exists for this date")
            for field in DIARY_MUTABLE_FIELDS:
                if field in normalized:
                    updates[field] = normalized[field]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    if item_type == "ledger":
        try:
            updates = normalize_ledger_fields(updates, partial=True)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    updates["updated_at"] = now_in_timezone(owner_id, db).replace(tzinfo=None).isoformat()
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
