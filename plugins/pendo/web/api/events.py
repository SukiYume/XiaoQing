"""提供日程概览、集合图增删改查和单个日程详情端点。"""

from typing import Any, cast
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...config import PendoConfig
from ...core.exceptions import ItemNotFoundException
from ...services.db import Database
from ...utils.identifiers import new_internal_id, public_id
from ...utils.settings_utils import resolve_default_category
from ...utils.time_utils import TimezoneHelper, now_in_timezone
from ...utils.validators import (
    build_remind_times_from_rules,
    normalize_event_fields,
    normalize_reminder_rules,
    sanitize_text,
    validate_title,
)
from ..analytics.events_overview import (
    build_event_collection_detail,
    build_event_detail,
    build_events_overview,
    list_event_categories,
)
from ..deps import get_current_user, get_db

router = APIRouter()


# 当前 Pydantic 运行依赖没有向 Mypy 暴露基类类型，请求字段仍由 FastAPI 校验。
class EventCollectionChildCreate(BaseModel):  # type: ignore[misc]
    """创建多节点集合时提交的一个叶子日程。"""

    title: str
    start_time: str
    end_time: str | None = None
    notes: str | None    = ""


class EventCollectionCreate(BaseModel):  # type: ignore[misc]
    """创建多节点日程集合的请求体。"""

    kind: str = "multi_node"
    title: str
    content: str | None  = ""
    category: str | None = None
    location: str | None = ""
    tags: list[str] = Field(default_factory=list)
    notes: str | None                           = ""
    timezone: str | None                        = None
    reminder_rules: list[dict[str, Any]] | None = None
    children: list[EventCollectionChildCreate]


class EventCollectionUpdate(BaseModel):  # type: ignore[misc]
    """部分更新日程集合的请求体；显式空值用于清空可选字段。"""

    title: str | None                           = None
    content: str | None                         = None
    category: str | None                        = None
    location: str | None                        = None
    tags: list[str] | None                      = None
    notes: str | None                           = None
    timezone: str | None                        = None
    reminder_rules: list[dict[str, Any]] | None = None


class EventReminderConfirmationUpdate(BaseModel):  # type: ignore[misc]
    """切换单条未来日程提醒确认状态的请求体。"""

    remind_time: str = Field(min_length=1)
    confirmed: bool


@router.get("/events/overview")
def get_events_overview(
    start_date: str,
    end_date: str,
    keyword: str  = "",
    category: str = "",
    kind: str     = "all",
    reminder: str = "all",
    owner_id: str = Depends(get_current_user),
    db: Database  = Depends(get_db),
) -> dict[str, object]:
    """按日期范围和筛选条件返回当前所有者的日程概览。"""

    try:
        overview = build_events_overview(
            db         = db,
            owner_id   = owner_id,
            start_date = start_date,
            end_date   = end_date,
            keyword    = keyword,
            category   = category,
            kind       = kind,
            reminder   = reminder,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "ok": True,
        "data": overview,
        "message": "",
    }


@router.get("/events/categories")
def get_event_categories(
    owner_id: str = Depends(get_current_user),
    db: Database  = Depends(get_db),
) -> dict[str, object]:
    """只读取日程分类，避免为分类下拉框构造跨 130 年的完整概览。"""

    return {
        "ok": True,
        "data": {"categories": list_event_categories(db, owner_id)},
        "message": "",
    }


def _normalize_collection_updates(
    body: EventCollectionUpdate,
    db: Database,
    owner_id: str,
    *,
    anchor_start_time: object,
    default_timezone: str,
) -> dict[str, Any]:
    """复用日程规范化器清洗集合字段，不重复维护另一套校验规则。"""

    updates = cast(dict[str, Any], body.model_dump(exclude_unset=True))
    if not updates:
        raise ValueError("No collection fields to update")

    if "category" in updates:
        category = str(updates["category"] or "").strip()
        if not category or category == PendoConfig.DEFAULT_CATEGORY:
            updates["category"] = resolve_default_category(db, owner_id)
    if "timezone" in updates and not str(updates["timezone"] or "").strip():
        updates["timezone"] = default_timezone

    # 集合更新不修改起始时间，但共享规范化器需要时间锚点来校验提醒规则。
    normalized = normalize_event_fields(
        {
            "start_time": str(anchor_start_time or "2000-01-01T00:00:00"),
            **updates,
        },
        partial=True,
    )
    return {field: normalized[field] for field in updates}


@router.post("/events/collections", status_code=201)
def create_event_collection(
    body: EventCollectionCreate,
    owner_id: str = Depends(get_current_user),
    db: Database  = Depends(get_db),
) -> dict[str, object]:
    """校验并原子创建集合头、全部叶子日程和创建审计记录。"""

    if body.kind != "multi_node":
        raise HTTPException(
            status_code=422, detail="Only multi_node collections can be created here"
        )
    if len(body.children) < 2:
        raise HTTPException(
            status_code=422, detail="Multi-node collections require at least 2 children"
        )

    category_text = str(body.category or "").strip()
    category      = (
        resolve_default_category(db, owner_id)
        if not category_text or category_text == PendoConfig.DEFAULT_CATEGORY
        else category_text
    )
    try:
        rules = normalize_reminder_rules(
            body.reminder_rules if body.reminder_rules is not None else [{"offset_seconds": 0}]
        )
        collection_id    = new_internal_id()
        local_now        = now_in_timezone(owner_id, db)
        default_timezone = str(
            getattr(local_now.tzinfo, "key", None) or PendoConfig.DEFAULT_TIMEZONE
        )
        timezone_name = str(body.timezone or "").strip() or default_timezone
        now = local_now.replace(tzinfo=None).isoformat()
        collection_title = validate_title(body.title)
        collection_notes = sanitize_text(str(body.notes or ""), 50000)

        child_rows: list[tuple[str, dict[str, Any]]] = []
        for index, child in enumerate(body.children, 1):
            node_key   = f"m{index:02d}"
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
                    "timezone": timezone_name,
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
            child_rows.append((new_internal_id(), normalized))

        # 有偏移和无偏移时间统一映射到集合时区后比较，避免 ISO 字符串字典序误判。
        shared_fields  = child_rows[0][1]
        event_timezone = ZoneInfo(str(shared_fields["timezone"]))
        start_time     = min(
            (str(row["start_time"]) for _, row in child_rows),
            key=lambda value: TimezoneHelper.parse(value, event_timezone),
        )
        end_time = max(
            (str(row.get("end_time") or row["start_time"]) for _, row in child_rows),
            key=lambda value: TimezoneHelper.parse(value, event_timezone),
        )

        db.create_event_collection(
            {
                "id": collection_id,
                "owner_id": owner_id,
                "kind": "multi_node",
                "title": collection_title,
                "content": shared_fields["content"],
                "category": shared_fields["category"],
                "location": shared_fields["location"],
                "tags": list(shared_fields["tags"]),
                "notes": collection_notes,
                "timezone": shared_fields["timezone"],
                "reminder_rules": list(shared_fields["reminder_rules"]),
                "start_time": start_time,
                "end_time": end_time,
                "created_at": now,
                "updated_at": now,
            },
            child_rows,
            operation_action="create_event_collection",
        )
        child_ids = [node_id for node_id, _ in child_rows]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "ok": True,
        "data": {
            "id": collection_id,
            "display_id": public_id(collection_id),
            "child_ids": child_ids,
            "child_display_ids": [public_id(child_id) for child_id in child_ids],
        },
        "message": "创建成功",
    }


@router.get("/events/collections/{collection_id}/detail")
def get_collection_detail(
    collection_id: str,
    owner_id: str = Depends(get_current_user),
    db: Database  = Depends(get_db),
) -> dict[str, object]:
    """返回集合头及其全部叶子日程。"""

    detail = build_event_collection_detail(db=db, owner_id=owner_id, collection_id=collection_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Event collection not found")
    return {"ok": True, "data": detail, "message": ""}


@router.put("/events/collections/{collection_id}")
def update_collection(
    collection_id: str,
    body: EventCollectionUpdate,
    owner_id: str = Depends(get_current_user),
    db: Database  = Depends(get_db),
) -> dict[str, object]:
    """原子更新集合字段、全部叶子提醒和对应审计记录。"""

    collection = db.get_event_collection(collection_id, owner_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="Event collection not found")

    local_now        = now_in_timezone(owner_id, db)
    default_timezone = str(getattr(local_now.tzinfo, "key", None) or PendoConfig.DEFAULT_TIMEZONE)
    try:
        updates = _normalize_collection_updates(
            body,
            db,
            owner_id,
            anchor_start_time = collection.get("start_time"),
            default_timezone  = default_timezone,
        )
        audit_updates = dict(updates)
        operation_log = {
            "user_id": owner_id,
            "action": "update_event_collection",
            "item_type": "event",
            "item_id": str(collection["id"]),
            "details": {"updates": audit_updates},
        }

        if "reminder_rules" in updates:
            rules         = cast(list[dict[str, int]], updates.pop("reminder_rules"))
            child_updates = {
                child.id: (
                    build_remind_times_from_rules(child.start_time, rules),
                    rules,
                )
                for child in db.get_collection_events(collection_id, owner_id)
            }
            db.update_event_collection_reminders(
                collection_id,
                owner_id,
                child_updates,
                rules,
                collection_updates = updates,
                operation_log      = operation_log,
            )
        elif not db.update_event_collection(
            collection_id,
            updates,
            owner_id,
            operation_log=operation_log,
        ):
            raise HTTPException(status_code=404, detail="Event collection not found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ItemNotFoundException as exc:
        status_code = 404 if exc.item_id == collection_id else 409
        detail      = (
            "Event collection not found"
            if status_code == 404
            else "Event collection changed; reload and retry"
        )
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return {
        "ok": True,
        "data": {
            "id": str(collection["id"]),
            "display_id": public_id(collection["id"]),
        },
        "message": "更新成功",
    }


@router.delete("/events/collections/{collection_id}")
def delete_collection(
    collection_id: str,
    owner_id: str = Depends(get_current_user),
    db: Database  = Depends(get_db),
) -> dict[str, object]:
    """在一个事务内软删除集合、叶子日程和提醒，并写入删除审计。"""

    collection = db.get_event_collection(collection_id, owner_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="Event collection not found")
    resolved_collection_id = str(collection["id"])
    child_ids = [child.id for child in db.get_collection_events(resolved_collection_id, owner_id)]
    success = db.delete_event_collection(
        resolved_collection_id,
        owner_id,
        cascade       = True,
        operation_log = {
            "user_id": owner_id,
            "action": "delete_event_collection",
            "item_type": "event",
            "item_id": resolved_collection_id,
            "details": {"child_ids": child_ids},
        },
    )
    if not success:
        raise HTTPException(status_code=404, detail="Event collection not found")
    return {
        "ok": True,
        "data": {
            "id": resolved_collection_id,
            "display_id": public_id(resolved_collection_id),
        },
        "message": "已删除",
    }


@router.get("/events/{event_id}/detail")
def get_event_detail(
    event_id: str,
    owner_id: str = Depends(get_current_user),
    db: Database  = Depends(get_db),
) -> dict[str, object]:
    """返回一个叶子日程及其提醒和同集合关联信息。"""

    detail = build_event_detail(db=db, owner_id=owner_id, event_id=event_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Event not found")
    return {"ok": True, "data": detail, "message": ""}


@router.put("/events/{event_id}/reminders/confirmation")
def set_event_reminder_confirmation(
    event_id: str,
    body: EventReminderConfirmationUpdate,
    owner_id: str = Depends(get_current_user),
    db: Database  = Depends(get_db),
) -> dict[str, object]:
    """提前确认或重新开启当前用户的一条未到期日程提醒。"""

    try:
        reminder = db.set_future_reminder_confirmation(
            event_id,
            body.remind_time,
            owner_id,
            confirmed=body.confirmed,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if reminder is None:
        raise HTTPException(status_code=404, detail="Event reminder not found")
    if reminder.get("outcome") == "expired":
        raise HTTPException(status_code=409, detail="提醒时间已到，不能再修改确认状态")

    return {
        "ok": True,
        "data": {"reminder": reminder},
        "message": "提醒已提前确认" if body.confirmed else "提醒已重新开启",
    }
