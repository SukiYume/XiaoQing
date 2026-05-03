"""Unified items CRUD API."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ...models.item import ItemType, get_item_type_value
from ...services.db import Database
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
from ..deps import get_current_user, get_db
from ..utils import amount_filter_cents

router = APIRouter()

DEFAULT_DATE_FIELDS: dict[str | None, str] = {
    "event": "start_time",
    "task": "plan_date",
    "diary": "diary_date",
    "ledger": "ledger_date",
    "note": "created_at",
    None: "created_at",
}

ALLOWED_DATE_FIELDS_BY_TYPE: dict[str | None, set[str]] = {
    "event": {"created_at", "start_time", "end_time"},
    "task": {"created_at", "plan_date", "deadline_at", "completed_at", "cancelled_at"},
    "diary": {"created_at", "diary_date", "entry_time"},
    "ledger": {"created_at", "ledger_date"},
    "note": {"created_at"},
    None: set(Database.ALLOWED_DATE_FIELDS),
}


def _entry_time_for_diary_date(now_iso: str, diary_date: str | None) -> str:
    date_part = str(diary_date or "").strip()
    if not date_part:
        return now_iso
    time_part = now_iso[11:19] if len(now_iso) >= 19 else "00:00:00"
    return f"{date_part}T{time_part}"


EVENT_MUTABLE_FIELDS = {
    "title",
    "category",
    "start_time",
    "end_time",
    "location",
    "timezone",
    "remind_times",
    "reminder_rules",
    "notes",
}

TASK_MUTABLE_FIELDS = {
    "title",
    "content",
    "category",
    "plan_date",
    "deadline_at",
    "priority",
    "status",
    "remind_times",
    "reminder_rules",
    "repeat_rule",
    "completed_at",
    "cancelled_at",
}

NOTE_MUTABLE_FIELDS = {
    "title",
    "content",
    "category",
    "tags",
    "references",
    "related_items",
    "last_viewed",
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
    "entry_time",
    "template_answers",
    "is_favorite",
}

LEDGER_MUTABLE_FIELDS = {
    "title",
    "content",
    "amount",
    "amount_cents",
    "currency",
    "transaction_type",
    "ledger_category",
    "ledger_date",
    "account_name",
    "counter_account_name",
    "merchant",
    "remark",
}


class ItemCreate(BaseModel):
    type: str
    title: str | None = ""
    content: str | None = ""
    tags: list[str] = []
    category: str | None = None
    # Event fields
    start_time: str | None = None
    end_time: str | None = None
    location: str | None = None
    timezone: str | None = None
    remind_times: list[str] | None = None
    reminder_rules: list[dict] | None = None
    notes: str | None = None
    # Task fields
    plan_date: str | None = None
    deadline_at: str | None = None
    priority: int | None = None
    status: str | None = None
    repeat_rule: str | None = None
    completed_at: str | None = None
    cancelled_at: str | None = None
    # Diary fields
    diary_date: str | None = None
    mood: str | None = None
    mood_score: int | None = None
    weather: str | None = None
    template_id: str | None = None
    entry_time: str | None = None
    template_answers: list[dict] | None = None
    is_favorite: bool | None = None
    # Ledger fields
    amount: float | None = None
    amount_cents: int | None = None
    currency: str | None = None
    transaction_type: str | None = None
    ledger_category: str | None = None
    ledger_date: str | None = None
    account_name: str | None = None
    counter_account_name: str | None = None
    merchant: str | None = None
    remark: str | None = None
    # Note fields
    references: list[dict] | None = None
    related_items: list[str] | None = None


class ItemUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    tags: list[str] | None = None
    category: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    location: str | None = None
    timezone: str | None = None
    remind_times: list[str] | None = None
    reminder_rules: list[dict] | None = None
    notes: str | None = None
    plan_date: str | None = None
    deadline_at: str | None = None
    priority: int | None = None
    status: str | None = None
    repeat_rule: str | None = None
    completed_at: str | None = None
    cancelled_at: str | None = None
    diary_date: str | None = None
    mood: str | None = None
    mood_score: int | None = None
    weather: str | None = None
    template_id: str | None = None
    entry_time: str | None = None
    template_answers: list[dict] | None = None
    is_favorite: bool | None = None
    amount: float | None = None
    amount_cents: int | None = None
    currency: str | None = None
    transaction_type: str | None = None
    ledger_category: str | None = None
    ledger_date: str | None = None
    account_name: str | None = None
    counter_account_name: str | None = None
    merchant: str | None = None
    remark: str | None = None
    references: list[dict] | None = None
    related_items: list[str] | None = None


def _item_to_dict(item) -> dict:
    """Convert Item dataclass to API response dict."""
    if hasattr(item, "to_dict"):
        return item.to_dict()
    return {}


def _snapshot_item_fields(item, fields: set[str]) -> dict:
    data = _item_to_dict(item)
    return {field: data.get(field) for field in fields if field in data and field != "updated_at"}


def _collect_note_reference_ids(payload: dict) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for ref in payload.get("references") or []:
        if not isinstance(ref, dict):
            continue
        ref_id = str(ref.get("id") or "").strip()
        if ref_id and ref_id not in seen:
            seen.add(ref_id)
            ids.append(ref_id)
    for raw_id in payload.get("related_items") or []:
        ref_id = str(raw_id or "").strip()
        if ref_id and ref_id not in seen:
            seen.add(ref_id)
            ids.append(ref_id)
    return ids


def _note_reference_ids_match(left: dict, right: dict) -> bool:
    left_ids = _collect_note_reference_ids(left)
    right_ids = _collect_note_reference_ids(right)
    return len(left_ids) == len(right_ids) and set(left_ids) == set(right_ids)


def _requested_note_reference_payload(updates: dict, requested_fields: set[str]) -> dict:
    payload = {}
    if "references" in requested_fields:
        payload["references"] = updates.get("references") or []
    if "related_items" in requested_fields:
        payload["related_items"] = updates.get("related_items") or []
    return normalize_note_fields(payload, partial=True)


def _preserve_existing_note_references(item, payload: dict) -> dict:
    current = _item_to_dict(item)
    references = current.get("references") if isinstance(current.get("references"), list) else []
    related_items = (
        current.get("related_items")
        if isinstance(current.get("related_items"), list)
        else _collect_note_reference_ids({"references": references})
    )
    payload["references"] = references
    payload["related_items"] = related_items
    return payload


def _resolve_note_reference_payload(db: Database, owner_id: str, payload: dict) -> dict:
    """Validate and enrich note references from references/related_items."""
    ids = _collect_note_reference_ids(payload)

    if not ids:
        payload["references"] = []
        payload["related_items"] = []
        return payload

    references: list[dict] = []
    for ref_id in ids:
        target = db.get_item(ref_id, owner_id=owner_id)
        if target is None:
            raise HTTPException(status_code=422, detail=f"Referenced item not found: {ref_id}")
        item_type = get_item_type_value(getattr(target, "type", None), default="item")
        references.append({
            "kind": "item",
            "id": ref_id,
            "type": item_type,
            "title": getattr(target, "title", "") or "无标题",
        })

    payload["references"] = references
    payload["related_items"] = ids
    return payload


def _resolve_date_field(type: str | None, date_field: str | None) -> str:
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


def _resolve_category_field(type: str | None) -> str:
    return "ledger_category" if type == "ledger" else "category"


def _ledger_amount_expr() -> str:
    return Database._LEDGER_AMOUNT_CENTS_EXPR


def _shift_event_end_time_if_start_moved(current: dict, updates: dict) -> None:
    """Preserve event duration when only start_time is patched."""
    if "start_time" not in updates or "end_time" in updates:
        return
    old_start = current.get("start_time")
    old_end = current.get("end_time")
    new_start = updates.get("start_time")
    if not old_start or not old_end or not new_start:
        return
    try:
        old_start_dt = datetime.fromisoformat(str(old_start))
        old_end_dt = datetime.fromisoformat(str(old_end))
        new_start_dt = datetime.fromisoformat(str(new_start))
    except ValueError:
        return
    if old_end_dt < old_start_dt:
        return
    updates["end_time"] = (new_start_dt + (old_end_dt - old_start_dt)).isoformat(timespec="seconds")


def _build_count_where(
    type, status, category, priority, start_date, end_date, date_field,
    amount_min, amount_max, owner_id, keyword: str | None = None,
    transaction_type: str | None = None, account_name: str | None = None,
    counter_account_name: str | None = None, merchant: str | None = None,
):
    where = ["owner_id = ?", "deleted = 0"]
    params: list = [owner_id]
    category_field = _resolve_category_field(type)
    if type:
        where.append("type = ?")
        params.append(type)
    if status:
        where.append("status = ?")
        params.append(status)
    if category:
        where.append(f"{category_field} = ?")
        params.append(category)
    if priority is not None:
        where.append("priority = ?")
        params.append(priority)
    if transaction_type:
        where.append("transaction_type = ?")
        params.append(transaction_type)
    if account_name:
        where.append("(account_name = ? OR counter_account_name = ?)")
        params.extend([account_name, account_name])
    if counter_account_name:
        where.append("counter_account_name = ?")
        params.append(counter_account_name)
    if merchant:
        where.append("merchant = ?")
        params.append(merchant)
    if amount_min is not None:
        where.append(f"{_ledger_amount_expr()} >= ?")
        params.append(amount_filter_cents(amount_min))
    if amount_max is not None:
        where.append(f"{_ledger_amount_expr()} <= ?")
        params.append(amount_filter_cents(amount_max))
    if start_date and end_date and date_field:
        where.append(f"{date_field} >= ?")
        params.append(start_date)
        where.append(f"{date_field} <= ?")
        params.append(end_date)
    if keyword:
        like = f"%{keyword}%"
        where.append(
            """(
                title LIKE ? OR content LIKE ? OR category LIKE ? OR tags LIKE ? OR
                ledger_category LIKE ? OR account_name LIKE ? OR counter_account_name LIKE ? OR
                merchant LIKE ? OR remark LIKE ? OR location LIKE ? OR notes LIKE ? OR weather LIKE ?
            )"""
        )
        params.extend([like] * 12)
    return where, params


@router.get("/items/aggregate")
def aggregate_items(
    type: str | None = None,
    transaction_type: str | None = None,
    account_name: str | None = None,
    counter_account_name: str | None = None,
    merchant: str | None = None,
    category: str | None = None,
    date_field: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Return income/expense totals for the given filters (full result set, not paginated)."""
    _df = _resolve_date_field(type, date_field) if (start_date and end_date) else None
    where, params = _build_count_where(
        type, None, category, None, start_date, end_date, _df, amount_min, amount_max, owner_id,
        transaction_type=transaction_type,
        account_name=account_name,
        counter_account_name=counter_account_name,
        merchant=merchant,
    )
    conn = db.get_connection()
    rows = conn.execute(
        f"""SELECT transaction_type, COALESCE(SUM({_ledger_amount_expr()}), 0), COUNT(*)
            FROM items WHERE {' AND '.join(where)}
            GROUP BY transaction_type""",
        params,
    ).fetchall()
    income = expense = transfer = count = 0
    for row in rows:
        total = round(float(row[1] or 0) / 100.0, 2)
        if row[0] == "income":
            income = total
        elif row[0] == "expense":
            expense = total
        elif row[0] == "transfer":
            transfer = total
        count += int(row[2] or 0)
    return {
        "ok": True,
        "data": {
            "income": income,
            "expense": expense,
            "transfer": transfer,
            "balance": income - expense,
            "count": count,
        },
    }


@router.get("/items/categories")
def list_categories(
    type: str | None = None,
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


@router.get("/items/ledger/accounts")
def list_ledger_accounts(
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Return distinct ledger account names from both sides of transfers."""
    conn = db.get_connection()
    rows = conn.execute(
        """
        SELECT account_name AS account FROM items
        WHERE owner_id = ? AND type = 'ledger' AND deleted = 0
          AND account_name IS NOT NULL AND TRIM(account_name) != ''
        UNION
        SELECT counter_account_name AS account FROM items
        WHERE owner_id = ? AND type = 'ledger' AND deleted = 0
          AND counter_account_name IS NOT NULL AND TRIM(counter_account_name) != ''
        ORDER BY account
        """,
        (owner_id, owner_id),
    ).fetchall()
    accounts = [str(row[0]) for row in rows if row[0]]
    if not accounts:
        accounts = ["现金"]
    return {"ok": True, "data": {"accounts": accounts}}


@router.get("/items")
def list_items(
    type: str | None = None,
    status: str | None = None,
    category: str | None = None,
    tags: str | None = None,
    keyword: str | None = None,
    priority: int | None = None,
    transaction_type: str | None = None,
    account_name: str | None = None,
    counter_account_name: str | None = None,
    merchant: str | None = None,
    date_field: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    range: str | None = Query(None, alias="range"),
    sort: str = "created_at",
    order: str = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """List items with filtering and pagination."""
    filters: dict = {}
    if type:
        filters["type"] = type
    if status:
        filters["status"] = status
    if category:
        filters[_resolve_category_field(type)] = category
    if tags:
        filters["tags"] = tags
    if keyword and keyword.strip():
        filters["keyword"] = keyword.strip()
    if priority is not None:
        filters["priority"] = priority
    if transaction_type:
        filters["transaction_type"] = transaction_type
    if account_name:
        filters["account_name"] = account_name
    if counter_account_name:
        filters["counter_account_name"] = counter_account_name
    if merchant:
        filters["merchant"] = merchant
    if amount_min is not None:
        filters["amount_min"] = amount_min
    if amount_max is not None:
        filters["amount_max"] = amount_max

    # Sorting
    _allowed_sort = {
        "created_at",
        "updated_at",
        "ledger_date",
        "plan_date",
        "deadline_at",
        "completed_at",
        "cancelled_at",
        "start_time",
        "diary_date",
        "entry_time",
        "amount",
        "amount_cents",
    }
    if sort in _allowed_sort:
        filters["sort_field"] = sort
        filters["sort_order"] = order.upper()

    # Date filtering: support both direct params and range="start..end" syntax
    resolved_df: str | None = None
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
        type, status, category, priority, start_date, end_date, resolved_df,
        amount_min, amount_max, owner_id, keyword.strip() if keyword else None,
        transaction_type=transaction_type,
        account_name=account_name,
        counter_account_name=counter_account_name,
        merchant=merchant,
    )
    conn = db.get_connection()
    if tags:
        total = conn.execute(
            f"SELECT COUNT(*) FROM items WHERE {' AND '.join(count_where)} AND tags LIKE ?",
            count_params + [Database.tag_filter_pattern(tags)],
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
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid type: {body.type}") from exc

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
    for field in type(body).model_fields:
        if field in ("type", "title", "content", "tags", "category"):
            continue
        value = getattr(body, field)
        if value is not None:
            item_data[field] = value

    if body.type == "event":
        try:
            item_data = normalize_event_fields(item_data, partial=False)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.type == "task":
        try:
            item_data = normalize_task_fields(item_data, partial=False)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.type == "note":
        try:
            item_data = normalize_note_fields(item_data, partial=False)
            if item_data.get("references") or item_data.get("related_items"):
                item_data = _resolve_note_reference_payload(db, owner_id, item_data)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.type == "diary":
        if not item_data.get("entry_time"):
            item_data["entry_time"] = _entry_time_for_diary_date(
                now,
                str(item_data.get("diary_date") or ""),
            )
        try:
            item_data = normalize_diary_fields(item_data, partial=False)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not str(item_data.get("title") or "").strip():
            entry_time = str(item_data.get("entry_time") or now)
            entry_label = entry_time[11:16] if len(entry_time) >= 16 else ""
            item_data["title"] = f"{item_data['diary_date']} {entry_label} 日记".strip()
    if body.type == "ledger":
        if not item_data.get("ledger_date"):
            item_data["ledger_date"] = now[:10]
        try:
            item_data = normalize_ledger_fields(item_data, partial=False)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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
    requested_update_fields = set(updates.keys())

    item_type = item.type.value if hasattr(item.type, "value") else item.type
    if item_type == "event":
        try:
            merged = item.to_dict()
            _shift_event_end_time_if_start_moved(merged, updates)
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
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if item_type == "task":
        try:
            merged = item.to_dict()
            merged.update(updates)
            normalized = normalize_task_fields(merged, partial=False)
            for field in TASK_MUTABLE_FIELDS:
                if field in normalized:
                    updates[field] = normalized[field]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if item_type == "note":
        try:
            merged = item.to_dict()
            merged.update(updates)
            normalized = normalize_note_fields(merged, partial=False)
            note_reference_requested = bool({"references", "related_items"} & requested_update_fields)
            if note_reference_requested:
                reference_payload = _requested_note_reference_payload(updates, requested_update_fields)
                if _note_reference_ids_match(reference_payload, _item_to_dict(item)):
                    reference_payload = _preserve_existing_note_references(item, reference_payload)
                else:
                    reference_payload = _resolve_note_reference_payload(db, owner_id, reference_payload)
                normalized["references"] = reference_payload["references"]
                normalized["related_items"] = reference_payload["related_items"]
            fields_to_apply = NOTE_MUTABLE_FIELDS & requested_update_fields
            if note_reference_requested:
                fields_to_apply.update({"references", "related_items"})
            updates = {
                field: normalized[field]
                for field in fields_to_apply
                if field in normalized
            }
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if item_type == "diary":
        try:
            merged = item.to_dict()
            merged.update(updates)
            normalized = normalize_diary_fields(merged, partial=False)
            for field in DIARY_MUTABLE_FIELDS:
                if field in normalized:
                    updates[field] = normalized[field]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if item_type == "ledger":
        try:
            merged = item.to_dict()
            if "amount" in updates and "amount_cents" not in updates:
                merged.pop("amount_cents", None)
            merged.update(updates)
            normalized = normalize_ledger_fields(merged, partial=False)
            updates = {
                field: normalized[field]
                for field in LEDGER_MUTABLE_FIELDS & requested_update_fields
                if field in normalized
            }
            if {"amount", "amount_cents"} & requested_update_fields:
                updates["amount"] = normalized["amount"]
                updates["amount_cents"] = normalized["amount_cents"]
            if "transaction_type" in requested_update_fields:
                updates["transaction_type"] = normalized["transaction_type"]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    note_old_values = None
    note_logged_updates = None
    if item_type == "note":
        note_logged_updates = {
            field: updates[field]
            for field in NOTE_MUTABLE_FIELDS
            if field in updates and field != "last_viewed"
        }
        if note_logged_updates:
            note_old_values = _snapshot_item_fields(item, set(note_logged_updates.keys()))

    updates["updated_at"] = now_in_timezone(owner_id, db).replace(tzinfo=None).isoformat()
    success = db.update_item(item_id, updates, owner_id=owner_id)
    if not success:
        raise HTTPException(status_code=500, detail="Update failed")

    if item_type == "note" and note_logged_updates:
        db.log_operation(
            owner_id,
            "edit_note",
            item_type="note",
            item_id=item_id,
            details={"updates": note_logged_updates, "old_values": note_old_values or {}},
        )
    else:
        db.log_operation(owner_id, "update", item_type=item_type, item_id=item_id)
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
