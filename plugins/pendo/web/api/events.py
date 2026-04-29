"""Events-specific API routes for the web UI."""

from datetime import datetime
from typing import Any, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...models.item import EventItem
from ...services.db import Database
from ...utils.settings_utils import resolve_default_category
from ...utils.time_utils import now_in_timezone
from ...utils.validators import (
    build_remind_times_from_rules,
    normalize_event_fields,
    normalize_reminder_rules,
)
from ..analytics.events_overview import (
    build_event_collection_detail,
    build_event_detail,
    build_events_overview,
)
from ..deps import get_current_user, get_db

router = APIRouter()


class EventCollectionChildCreate(BaseModel):
    title: str
    start_time: str
    end_time: Optional[str] = None
    notes: Optional[str] = ""


class EventCollectionCreate(BaseModel):
    kind: str = "multi_node"
    title: str
    content: Optional[str] = ""
    category: Optional[str] = None
    location: Optional[str] = ""
    tags: list[str] = []
    notes: Optional[str] = ""
    timezone: Optional[str] = None
    reminder_rules: Optional[list[dict[str, Any]]] = None
    children: list[EventCollectionChildCreate]


class EventCollectionUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    category: Optional[str] = None
    location: Optional[str] = None
    tags: Optional[list[str]] = None
    notes: Optional[str] = None
    timezone: Optional[str] = None
    reminder_rules: Optional[list[dict[str, Any]]] = None


@router.get("/events/overview")
def get_events_overview(
    start_date: str,
    end_date: str,
    keyword: str = "",
    category: str = "",
    kind: str = "all",
    reminder: str = "all",
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    return {
        "ok": True,
        "data": build_events_overview(
            db=db,
            owner_id=owner_id,
            start_date=start_date,
            end_date=end_date,
            keyword=keyword,
            category=category,
            kind=kind,
            reminder=reminder,
        ),
        "message": "",
    }


@router.get("/events/categories")
def get_event_categories(
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    overview = build_events_overview(
        db=db,
        owner_id=owner_id,
        start_date="1970-01-01",
        end_date="2099-12-31",
    )
    return {"ok": True, "data": {"categories": overview["categories"]}, "message": ""}


def _new_collection_id(db: Database, owner_id: str) -> str:
    for _ in range(20):
        candidate = uuid.uuid4().hex[:8]
        if not db.get_event_collection(candidate, owner_id) and not db.get_item(candidate, owner_id):
            return candidate
    return uuid.uuid4().hex[:12]


@router.post("/events/collections", status_code=201)
def create_event_collection(
    body: EventCollectionCreate,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    if body.kind != "multi_node":
        raise HTTPException(status_code=422, detail="Only multi_node collections can be created here")
    if len(body.children) < 2:
        raise HTTPException(status_code=422, detail="Multi-node collections require at least 2 children")

    category = body.category
    if not str(category or "").strip() or str(category or "").strip() == "未分类":
        category = resolve_default_category(db, owner_id)
    rules = normalize_reminder_rules(body.reminder_rules if body.reminder_rules is not None else [{"offset_seconds": 0}])
    collection_id = _new_collection_id(db, owner_id)
    now = now_in_timezone(owner_id, db).replace(tzinfo=None).isoformat()

    try:
        child_rows: list[tuple[str, dict[str, Any]]] = []
        for index, child in enumerate(body.children, 1):
            node_key = f"m{index:02d}"
            normalized = normalize_event_fields(
                {
                    "owner_id": owner_id,
                    "type": "event",
                    "title": child.title,
                    "content": body.content or "",
                    "category": category,
                    "start_time": child.start_time,
                    "end_time": child.end_time,
                    "location": body.location or "",
                    "tags": body.tags,
                    "notes": child.notes or "",
                    "timezone": body.timezone or "Asia/Shanghai",
                    "reminder_rules": rules,
                    "event_role": "multi_node_child",
                    "event_collection_id": collection_id,
                    "event_collection_kind": "multi_node",
                    "event_index": index,
                    "event_node_key": node_key,
                    "created_at": now,
                    "updated_at": now,
                },
                partial=False,
            )
            child_rows.append((f"{collection_id}_{node_key}", normalized))

        start_time = min(row["start_time"] for _, row in child_rows)
        end_time = max((row.get("end_time") or row["start_time"]) for _, row in child_rows)

        db.create_event_collection(
            {
                "id": collection_id,
                "owner_id": owner_id,
                "kind": "multi_node",
                "title": body.title,
                "content": body.content or "",
                "category": category,
                "location": body.location or "",
                "tags": body.tags,
                "notes": body.notes or "",
                "timezone": body.timezone or "Asia/Shanghai",
                "reminder_rules": rules,
                "start_time": start_time,
                "end_time": end_time,
                "created_at": now,
                "updated_at": now,
            }
        )

        child_ids: list[str] = []
        for node_id, normalized in child_rows:
            db.insert_item(EventItem(**{k: v for k, v in normalized.items() if k in EventItem.__dataclass_fields__}), node_id)
            child_ids.append(node_id)

        db.log_operation(
            owner_id,
            "create_event_collection",
            item_type="event",
            item_id=collection_id,
            details={"child_ids": child_ids},
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "ok": True,
        "data": {"id": collection_id, "child_ids": child_ids},
        "message": "创建成功",
    }


@router.get("/events/collections/{collection_id}/detail")
def get_collection_detail(
    collection_id: str,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    detail = build_event_collection_detail(db=db, owner_id=owner_id, collection_id=collection_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Event collection not found")
    return {"ok": True, "data": detail, "message": ""}


@router.put("/events/collections/{collection_id}")
def update_collection(
    collection_id: str,
    body: EventCollectionUpdate,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    updates = body.model_dump(exclude_unset=True)
    if "reminder_rules" in updates and updates["reminder_rules"] is not None:
        updates["reminder_rules"] = normalize_reminder_rules(updates["reminder_rules"])
        for child in db.get_collection_events(collection_id, owner_id):
            db.update_item(
                child.id,
                {
                    "reminder_rules": updates["reminder_rules"],
                    "remind_times": build_remind_times_from_rules(child.start_time, updates["reminder_rules"]),
                },
                owner_id=owner_id,
            )
    success = db.update_event_collection(collection_id, updates, owner_id)
    if not success:
        raise HTTPException(status_code=404, detail="Event collection not found")
    db.log_operation(owner_id, "update_event_collection", item_type="event", item_id=collection_id)
    return {"ok": True, "data": {"id": collection_id}, "message": "更新成功"}


@router.delete("/events/collections/{collection_id}")
def delete_collection(
    collection_id: str,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    success = db.delete_event_collection(collection_id, owner_id, cascade=True)
    if not success:
        raise HTTPException(status_code=404, detail="Event collection not found")
    db.log_operation(owner_id, "delete_event_collection", item_type="event", item_id=collection_id)
    return {"ok": True, "data": {"id": collection_id}, "message": "已删除"}


@router.get("/events/{event_id}/detail")
def get_event_detail(
    event_id: str,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    detail = build_event_detail(db=db, owner_id=owner_id, event_id=event_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Event not found")
    return {"ok": True, "data": detail, "message": ""}
