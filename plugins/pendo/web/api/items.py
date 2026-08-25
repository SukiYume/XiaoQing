"""提供五类 Pendo 条目的统一查询、汇总和原子增删改查端点。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ...models.item import get_item_type_value
from ...services.db import Database
from ...utils.identifiers import public_id
from ...utils.settings_utils import resolve_default_category
from ...utils.time_utils import now_in_timezone
from ...utils.validators import (
    LEDGER_TRANSACTION_TYPES,
    TASK_STATUSES,
    derive_reminder_rules,
    normalize_item_fields,
    normalize_note_fields,
)
from ..deps import get_current_user, get_db
from ..utils import (
    amount_filter_cents,
    infer_item_query_type,
    item_to_dict,
    normalize_choice_query,
    normalize_item_type_query,
)

router = APIRouter()

DEFAULT_DATE_FIELDS: Final[dict[str | None, str]] = {
    "event": "start_time",
    "task": "plan_date",
    "diary": "diary_date",
    "ledger": "ledger_date",
    "note": "created_at",
    None: "created_at",
}

ALLOWED_DATE_FIELDS_BY_TYPE: Final[dict[str | None, frozenset[str]]] = {
    "event": frozenset({"created_at", "start_time", "end_time"}),
    "task": frozenset({"created_at", "plan_date", "deadline_at", "completed_at", "cancelled_at"}),
    "diary": frozenset({"created_at", "diary_date", "entry_time"}),
    "ledger": frozenset({"created_at", "ledger_date"}),
    "note": frozenset({"created_at"}),
    None: frozenset(Database.ALLOWED_DATE_FIELDS),
}

DATE_ONLY_FIELDS: Final = frozenset({"plan_date", "diary_date", "ledger_date"})
ALLOWED_SORT_FIELDS: Final = frozenset(Database._ALLOWED_SORT_FIELDS)


def _entry_time_for_diary_date(now_iso: str, diary_date: str | None) -> str:
    """把当前本地时间的时分秒附到用户选择的日记日期。"""

    date_part = str(diary_date or "").strip()
    if not date_part:
        return now_iso
    time_part = now_iso[11:19] if len(now_iso) >= 19 else "00:00:00"
    return f"{date_part}T{time_part}"


EVENT_MUTABLE_FIELDS: Final = frozenset(
    {
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
)

TASK_MUTABLE_FIELDS: Final = frozenset(
    {
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
)

NOTE_MUTABLE_FIELDS: Final = frozenset(
    {
        "title",
        "content",
        "category",
        "tags",
        "references",
        "related_items",
        "last_viewed",
    }
)

DIARY_MUTABLE_FIELDS: Final = frozenset(
    {
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
)

LEDGER_MUTABLE_FIELDS: Final = frozenset(
    {
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
)

MUTABLE_FIELDS_BY_TYPE: Final[dict[str, frozenset[str]]] = {
    "event": EVENT_MUTABLE_FIELDS,
    "task": TASK_MUTABLE_FIELDS,
    "note": NOTE_MUTABLE_FIELDS,
    "diary": DIARY_MUTABLE_FIELDS,
    "ledger": LEDGER_MUTABLE_FIELDS,
}

FieldDependency = tuple[frozenset[str], frozenset[str]]
UPDATE_FIELD_DEPENDENCIES: Final[dict[str, tuple[FieldDependency, ...]]] = {
    "event": (
        (
            frozenset({"start_time"}),
            frozenset({"end_time", "reminder_rules", "remind_times"}),
        ),
        (
            frozenset({"reminder_rules", "remind_times"}),
            frozenset({"reminder_rules", "remind_times"}),
        ),
    ),
    "task": (
        (
            frozenset({"deadline_at", "reminder_rules", "remind_times"}),
            frozenset({"reminder_rules", "remind_times"}),
        ),
        (
            frozenset({"status", "completed_at", "cancelled_at"}),
            frozenset({"completed_at", "cancelled_at"}),
        ),
    ),
    "note": (
        (
            frozenset({"references", "related_items"}),
            frozenset({"references", "related_items"}),
        ),
    ),
    "ledger": (
        (
            frozenset({"amount", "amount_cents"}),
            frozenset({"amount", "amount_cents"}),
        ),
    ),
}


# 当前 Pydantic 运行依赖没有向 Mypy 暴露基类类型，请求字段仍由 FastAPI 校验。
class _ItemPayload(BaseModel):  # type: ignore[misc]
    """创建与更新接口共用的可写字段。"""

    title: str | None = None
    content: str | None = None
    tags: list[str] | None = None
    category: str | None = None
    # 日程字段
    start_time: str | None = None
    end_time: str | None = None
    location: str | None = None
    timezone: str | None = None
    remind_times: list[str] | None = None
    reminder_rules: list[dict[str, Any]] | None = None
    notes: str | None = None
    # 待办字段
    plan_date: str | None = None
    deadline_at: str | None = None
    priority: int | None = None
    status: str | None = None
    repeat_rule: str | None = None
    completed_at: str | None = None
    cancelled_at: str | None = None
    # 日记字段
    diary_date: str | None = None
    mood: str | None = None
    mood_score: int | None = None
    weather: str | None = None
    template_id: str | None = None
    entry_time: str | None = None
    template_answers: list[dict[str, Any]] | None = None
    is_favorite: bool | None = None
    # 账目字段
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
    # 笔记字段
    references: list[dict[str, Any]] | None = None
    related_items: list[str] | None = None


class ItemCreate(_ItemPayload):
    """创建一个受支持类型条目的请求体。"""

    type: str
    title: str | None = ""
    content: str | None = ""
    tags: list[str] = Field(default_factory=list)


class ItemUpdate(_ItemPayload):
    """按乐观版本号部分更新一个条目的请求体。"""

    version: int | None = None


def _collect_note_reference_ids(payload: dict[str, Any]) -> list[str]:
    """按首次出现顺序合并笔记引用对象和关联 ID。"""

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


def _resolve_note_reference_payload(
    db: Database,
    owner_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """在所有者范围内批量验证并补齐笔记引用快照。"""
    ids = _collect_note_reference_ids(payload)

    if not ids:
        payload["references"] = []
        payload["related_items"] = []
        return payload

    canonical_ids: list[str] = []
    missing_ids: list[str] = []
    seen_canonical: set[str] = set()
    for ref_id in ids:
        resolved_id = db.resolve_item_id(owner_id, ref_id)
        if resolved_id is None:
            missing_ids.append(ref_id)
        elif resolved_id not in seen_canonical:
            seen_canonical.add(resolved_id)
            canonical_ids.append(resolved_id)
    if missing_ids:
        raise HTTPException(
            status_code=422,
            detail=f"Referenced item not found: {missing_ids[0]}",
        )
    targets = db.get_items_by_ids(owner_id, canonical_ids)

    references: list[dict[str, str]] = []
    for ref_id in canonical_ids:
        target = targets[ref_id]
        item_type = get_item_type_value(getattr(target, "type", None), default="item")
        references.append(
            {
                "kind": "item",
                "id": ref_id,
                "type": item_type,
                "title": getattr(target, "title", "") or "无标题",
            }
        )

    payload["references"] = references
    payload["related_items"] = canonical_ids
    return payload


def _resolve_date_field(item_type: str | None, date_field: str | None) -> str:
    """按条目类型选择并校验可用于 SQL 的日期列。"""

    item_type = normalize_item_type_query(item_type)
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


def _resolve_category_field(item_type: str | None) -> str:
    """账目使用专属分类列，其余类型使用通用分类列。"""

    return "ledger_category" if item_type == "ledger" else "category"


def _normalize_amount_bounds(
    amount_min: float | None,
    amount_max: float | None,
) -> tuple[float | None, float | None]:
    """按账目整数分规则校验金额边界，并拒绝反向区间。"""

    try:
        minimum_cents = amount_filter_cents(amount_min) if amount_min is not None else None
        maximum_cents = amount_filter_cents(amount_max) if amount_max is not None else None
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if minimum_cents is not None and maximum_cents is not None and minimum_cents > maximum_cents:
        raise HTTPException(status_code=422, detail="amount_min cannot exceed amount_max")
    return (
        minimum_cents / 100 if minimum_cents is not None else None,
        maximum_cents / 100 if maximum_cents is not None else None,
    )


def _merge_date_range_inputs(
    start_date: str | None,
    end_date: str | None,
    date_range: str | None,
) -> tuple[str, str]:
    """合并两种日期区间输入形式，并拒绝残缺或冲突的参数。"""

    start_text = str(start_date or "").strip()
    end_text = str(end_date or "").strip()
    range_text = str(date_range or "").strip()
    if range_text:
        if start_text or end_text:
            raise HTTPException(
                status_code=422,
                detail="range cannot be combined with start_date or end_date",
            )
        parts = [part.strip() for part in range_text.split("..")]
        if len(parts) != 2 or not all(parts):
            raise HTTPException(status_code=422, detail="range must use start..end")
        start_text, end_text = parts
    if bool(start_text) != bool(end_text):
        raise HTTPException(
            status_code=422,
            detail="start_date and end_date must be provided together",
        )
    return start_text, end_text


def _validate_iso_date_order(start_text: str, end_text: str) -> None:
    """校验 ISO 日期可解析且起点不晚于终点。"""

    try:
        parsed_start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
        parsed_end = datetime.fromisoformat(end_text.replace("Z", "+00:00"))
        if parsed_start > parsed_end:
            raise HTTPException(status_code=422, detail="start_date cannot exceed end_date")
    except HTTPException:
        raise
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid ISO date range") from exc


def _resolve_date_filters(
    item_type: str | None,
    date_field: str | None,
    start_date: str | None,
    end_date: str | None,
    date_range: str | None = None,
) -> dict[str, str]:
    """校验直接或 ``start..end`` 日期区间，并生成数据库过滤字段。"""

    start_text, end_text = _merge_date_range_inputs(start_date, end_date, date_range)
    resolved_field = _resolve_date_field(item_type, date_field) if date_field else None
    if not start_text:
        return {}
    resolved_field = resolved_field or _resolve_date_field(item_type, None)
    _validate_iso_date_order(start_text, end_text)
    if resolved_field not in DATE_ONLY_FIELDS:
        if len(start_text) == 10:
            start_text = f"{start_text}T00:00:00"
        if len(end_text) == 10:
            end_text = f"{end_text}T23:59:59"
    return {
        "date_field": resolved_field,
        "start_date": start_text,
        "end_date": end_text,
    }


def _shift_event_end_time_if_start_moved(
    current: dict[str, Any],
    updates: dict[str, Any],
) -> None:
    """只修改起始时间时保留原日程持续时长。"""

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
    except (TypeError, ValueError):
        return
    if old_end_dt < old_start_dt:
        return
    updates["end_time"] = (new_start_dt + (old_end_dt - old_start_dt)).isoformat(timespec="seconds")


def _prepare_event_update(
    current: dict[str, Any],
    updates: dict[str, Any],
) -> None:
    """补齐移动日程时必须同步更新的结束时间和相对提醒规则。"""

    _shift_event_end_time_if_start_moved(current, updates)
    if "reminder_rules" in updates and not updates.get("reminder_rules"):
        updates["reminder_rules"] = []
        updates["remind_times"] = []
    elif "start_time" in updates and "reminder_rules" not in updates:
        old_rules = current.get("reminder_rules") or derive_reminder_rules(
            current.get("start_time"),
            current.get("remind_times"),
        )
        if old_rules:
            current["reminder_rules"] = old_rules


def _dependent_update_fields(item_type: str, requested_fields: set[str]) -> set[str]:
    """返回规范化时由请求字段连带生成、也必须落库的字段。"""

    fields: set[str] = set()
    for triggers, dependents in UPDATE_FIELD_DEPENDENCIES.get(item_type, ()):
        if triggers & requested_fields:
            fields.update(dependents)
    return fields


def _resolve_note_reference_update(
    db: Database,
    owner_id: str,
    current: dict[str, Any],
    updates: dict[str, Any],
    requested_fields: set[str],
    normalized: dict[str, Any],
) -> None:
    """解析新引用；ID 集合未变时保留旧快照，允许编辑含悬空旧引用的笔记。"""

    reference_fields = {"references", "related_items"} & requested_fields
    if not reference_fields:
        return
    reference_payload = normalize_note_fields(
        {field: updates.get(field) or [] for field in reference_fields},
        partial=True,
    )
    if set(_collect_note_reference_ids(reference_payload)) == set(
        _collect_note_reference_ids(current)
    ):
        references = current.get("references")
        reference_payload["references"] = references if isinstance(references, list) else []
        related_items = current.get("related_items")
        reference_payload["related_items"] = (
            related_items
            if isinstance(related_items, list)
            else _collect_note_reference_ids({"references": reference_payload["references"]})
        )
    else:
        reference_payload = _resolve_note_reference_payload(db, owner_id, reference_payload)
    normalized["references"] = reference_payload["references"]
    normalized["related_items"] = reference_payload["related_items"]


def _normalize_update_payload(
    db: Database,
    owner_id: str,
    item_type: str,
    current: dict[str, Any],
    requested_updates: dict[str, Any],
    requested_fields: set[str],
) -> dict[str, Any]:
    """合并当前条目，执行统一规范化，并只返回请求字段及必要联动字段。"""

    updates = dict(requested_updates)
    merged = dict(current)
    if item_type == "event":
        _prepare_event_update(merged, updates)
    if item_type == "ledger" and "amount" in requested_fields:
        if "amount_cents" not in requested_fields:
            merged.pop("amount_cents", None)
    merged.update(updates)
    normalized = normalize_item_fields(merged, partial=False)
    if item_type == "note":
        _resolve_note_reference_update(
            db,
            owner_id,
            current,
            updates,
            requested_fields,
            normalized,
        )
    fields_to_apply = requested_fields | _dependent_update_fields(item_type, requested_fields)
    return {field: normalized[field] for field in fields_to_apply if field in normalized}


def _build_update_operation_log(
    owner_id: str,
    item_type: str,
    current: dict[str, Any],
    updates: dict[str, Any],
) -> dict[str, Any]:
    """构造更新审计；笔记记录可恢复的字段前后值。"""

    note_updates = {
        field: value
        for field, value in updates.items()
        if item_type == "note" and field in NOTE_MUTABLE_FIELDS and field != "last_viewed"
    }
    if not note_updates:
        return {"user_id": owner_id, "action": "update", "item_type": item_type}
    return {
        "user_id": owner_id,
        "action": "edit_note",
        "item_type": "note",
        "details": {
            "updates": note_updates,
            "old_values": {field: current.get(field) for field in note_updates if field in current},
        },
    }


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
) -> dict[str, object]:
    """按完整过滤结果汇总账目收入、支出、转账和条目数。"""

    item_type = normalize_item_type_query(type or "ledger")
    if item_type != "ledger":
        raise HTTPException(status_code=422, detail="Aggregate endpoint requires type=ledger")
    transaction_type = normalize_choice_query(
        transaction_type,
        LEDGER_TRANSACTION_TYPES,
        "transaction_type",
    )
    amount_min, amount_max = _normalize_amount_bounds(amount_min, amount_max)
    filters: dict[str, Any] = {
        key: value
        for key, value in (
            ("type", item_type),
            ("transaction_type", transaction_type),
            ("account_name", str(account_name or "").strip() or None),
            ("counter_account_name", str(counter_account_name or "").strip() or None),
            ("merchant", str(merchant or "").strip() or None),
            ("amount_min", amount_min),
            ("amount_max", amount_max),
        )
        if value is not None
    }
    if category and category.strip():
        filters["ledger_category"] = category.strip()
    filters.update(_resolve_date_filters(item_type, date_field, start_date, end_date))

    summary = db.aggregate_item_amounts(owner_id, filters)
    amounts: dict[str, int] = dict.fromkeys(LEDGER_TRANSACTION_TYPES, 0)
    count = 0
    for kind, (amount_cents, item_count) in summary.items():
        if kind in amounts:
            amounts[kind] = amount_cents
        count += item_count
    return {
        "ok": True,
        "data": {
            "income": amounts["income"] / 100,
            "expense": amounts["expense"] / 100,
            "transfer": amounts["transfer"] / 100,
            "balance": (amounts["income"] - amounts["expense"]) / 100,
            "count": count,
        },
        "message": "",
    }


@router.get("/items/categories")
def list_categories(
    type: str | None = None,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> dict[str, object]:
    """返回当前所有者指定类型的非空去重分类。"""

    item_type = normalize_item_type_query(type)
    conn = db.get_connection()
    category_field = _resolve_category_field(item_type)
    where = [
        "owner_id = ?",
        "deleted = 0",
        f"{category_field} IS NOT NULL",
        f"TRIM({category_field}) != ''",
    ]
    params: list[Any] = [owner_id]
    if item_type:
        where.append("type = ?")
        params.append(item_type)
    rows = conn.execute(
        f"SELECT DISTINCT TRIM({category_field}) AS category "
        f"FROM items WHERE {' AND '.join(where)} ORDER BY category",
        params,
    ).fetchall()
    categories = [str(row[0]) for row in rows]
    return {"ok": True, "data": {"categories": categories}, "message": ""}


@router.get("/items/ledger/accounts")
def list_ledger_accounts(
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> dict[str, object]:
    """返回账目两侧出现过的账户名；空数据提供现金默认项。"""

    conn = db.get_connection()
    rows = conn.execute(
        """
        SELECT TRIM(account_name) AS account FROM items
        WHERE owner_id = ? AND type = 'ledger' AND deleted = 0
          AND account_name IS NOT NULL AND TRIM(account_name) != ''
        UNION
        SELECT TRIM(counter_account_name) AS account FROM items
        WHERE owner_id = ? AND type = 'ledger' AND deleted = 0
          AND counter_account_name IS NOT NULL AND TRIM(counter_account_name) != ''
        ORDER BY account
        """,
        (owner_id, owner_id),
    ).fetchall()
    accounts = [str(row[0]) for row in rows if row[0]]
    if not accounts:
        accounts = ["现金"]
    return {"ok": True, "data": {"accounts": accounts}, "message": ""}


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
    date_range: Annotated[str | None, Query(alias="range")] = None,
    sort: str = "created_at",
    order: str = "desc",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> dict[str, object]:
    """按同一过滤契约返回当前页条目和完整匹配总数。"""

    item_type = normalize_item_type_query(type)
    status = normalize_choice_query(status, TASK_STATUSES, "status")
    transaction_type = normalize_choice_query(
        transaction_type,
        LEDGER_TRANSACTION_TYPES,
        "transaction_type",
    )
    if priority is not None and not 1 <= priority <= 5:
        raise HTTPException(status_code=422, detail="priority must be between 1 and 5")
    item_type = infer_item_query_type(
        item_type,
        has_task_filters=status is not None or priority is not None,
        has_ledger_filters=any(
            value is not None
            for value in (
                transaction_type,
                account_name,
                counter_account_name,
                merchant,
                amount_min,
                amount_max,
            )
        ),
    )
    amount_min, amount_max = _normalize_amount_bounds(amount_min, amount_max)
    if not 1 <= page or not 1 <= page_size <= 100:
        raise HTTPException(status_code=422, detail="Invalid pagination")
    if sort not in ALLOWED_SORT_FIELDS:
        raise HTTPException(status_code=422, detail=f"Invalid sort field: {sort}")
    normalized_order = str(order).strip().upper()
    if normalized_order not in {"ASC", "DESC"}:
        raise HTTPException(status_code=422, detail=f"Invalid sort order: {order}")

    filters: dict[str, Any] = {
        key: value
        for key, value in (
            ("type", item_type),
            ("status", status),
            ("tags", str(tags or "").strip() or None),
            ("keyword", str(keyword or "").strip() or None),
            ("priority", priority),
            ("transaction_type", transaction_type),
            ("account_name", str(account_name or "").strip() or None),
            ("counter_account_name", str(counter_account_name or "").strip() or None),
            ("merchant", str(merchant or "").strip() or None),
            ("amount_min", amount_min),
            ("amount_max", amount_max),
        )
        if value is not None
    }
    if category and category.strip():
        filters[_resolve_category_field(item_type)] = category.strip()
    filters.update(_resolve_date_filters(item_type, date_field, start_date, end_date, date_range))
    filters["sort_field"] = sort
    filters["sort_order"] = normalized_order

    offset = (page - 1) * page_size
    items = db.get_items(
        owner_id,
        filters=filters,
        limit=page_size,
        offset=offset,
        use_cache=True,
    )
    total = db.count_items(owner_id, filters)

    return {
        "ok": True,
        "data": {
            "items": [item_to_dict(item) for item in items],
            "total": total,
        },
        "message": "",
    }


@router.get("/items/{item_id}")
def get_item(
    item_id: str,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> dict[str, object]:
    """按所有者和 ID 返回一个未删除条目。"""

    item = db.get_item(item_id, owner_id=owner_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True, "data": item_to_dict(item), "message": ""}


@router.post("/items", status_code=201)
def create_item(
    body: ItemCreate,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> dict[str, object]:
    """统一规范化请求，并原子写入条目及创建审计。"""

    item_type = normalize_item_type_query(body.type)
    if item_type is None:  # ``type`` 是必填字段，仅用于帮助类型检查器收窄。
        raise HTTPException(status_code=422, detail="Item type is required")

    local_now = now_in_timezone(owner_id, db)
    local_now_iso = local_now.replace(tzinfo=None).isoformat()
    storage_now = local_now.astimezone(timezone.utc).isoformat(timespec="seconds")
    item_data = body.model_dump(exclude_none=True)
    item_data.update(
        {
            "type": item_type,
            "owner_id": owner_id,
            "created_at": storage_now,
            "updated_at": storage_now,
            "context": {},
            "deleted": False,
        }
    )

    category = str(item_data.get("category") or "").strip()
    if item_type in {"event", "note"} and (not category or category == "未分类"):
        item_data["category"] = resolve_default_category(db, owner_id)
    if item_type == "diary":
        if not item_data.get("entry_time"):
            # 日记发生时间是用户墙钟；数据库入口会按用户时区唯一化并转 UTC。
            item_data["entry_time"] = _entry_time_for_diary_date(
                local_now_iso,
                str(item_data.get("diary_date") or ""),
            )
    if item_type == "ledger" and not item_data.get("ledger_date"):
        item_data["ledger_date"] = local_now_iso[:10]

    try:
        item_data = normalize_item_fields(item_data, partial=False)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if item_type == "note" and _collect_note_reference_ids(item_data):
        item_data = _resolve_note_reference_payload(db, owner_id, item_data)
    if item_type == "diary" and not str(item_data.get("title") or "").strip():
        entry_time = str(item_data["entry_time"])
        entry_label = entry_time[11:16] if len(entry_time) >= 16 else ""
        item_data["title"] = f"{item_data['diary_date']} {entry_label} 日记".strip()

    item_id = db.insert_item(
        item_data,
        operation_log={
            "user_id": owner_id,
            "action": "create",
            "item_type": item_type,
        },
    )

    return {
        "ok": True,
        "data": {"id": item_id, "display_id": public_id(item_id)},
        "message": "创建成功",
    }


@router.put("/items/{item_id}")
def update_item(
    item_id: str,
    body: ItemUpdate,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> dict[str, object]:
    """规范化有效变更，并以乐观版本号原子更新条目和审计。"""

    item = db.get_item(item_id, owner_id=owner_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    requested_updates = body.model_dump(exclude_unset=True)
    if not requested_updates:
        raise HTTPException(status_code=422, detail="No fields to update")
    requested_updates.pop("version", None)
    requested_fields = set(requested_updates)
    if not requested_fields:
        raise HTTPException(status_code=422, detail="No mutable fields to update")

    item_type = get_item_type_value(getattr(item, "type", None), default="")
    allowed_fields = MUTABLE_FIELDS_BY_TYPE.get(item_type)
    if allowed_fields is None:
        raise HTTPException(status_code=500, detail="Stored item has an unsupported type")
    invalid_fields = requested_fields - allowed_fields
    if invalid_fields:
        raise HTTPException(
            status_code=422,
            detail=f"Fields are not valid for {item_type}: {', '.join(sorted(invalid_fields))}",
        )
    current = {str(key): value for key, value in item.to_dict().items()}
    current_version = int(current.get("version") or 0)
    if body.version is not None and body.version != current_version:
        raise HTTPException(
            status_code=409,
            detail="Item changed by another request; refresh and retry",
        )
    try:
        normalized_updates = _normalize_update_payload(
            db,
            owner_id,
            item_type,
            current,
            requested_updates,
            requested_fields,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    updates = {
        field: value for field, value in normalized_updates.items() if current.get(field) != value
    }
    if not updates:
        return {
            "ok": True,
            "data": {
                "id": str(item.id),
                "display_id": item.display_id,
                "version": current_version,
            },
            "message": "无变化",
        }

    operation_log = _build_update_operation_log(owner_id, item_type, current, updates)

    success = db.update_item(
        item_id,
        updates,
        owner_id=owner_id,
        expected_version=current_version,
        item_type=item_type,
        operation_log=operation_log,
    )
    if not success:
        raise HTTPException(
            status_code=409, detail="Item changed by another request; refresh and retry"
        )

    return {
        "ok": True,
        "data": {
            "id": str(item.id),
            "display_id": item.display_id,
            "version": current_version + 1,
        },
        "message": "更新成功",
    }


@router.delete("/items/{item_id}")
def delete_item(
    item_id: str,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> dict[str, object]:
    """原子软删除条目；日程使用图感知删除，避免遗留空集合头。"""

    item = db.get_item(item_id, owner_id=owner_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    item_type = get_item_type_value(getattr(item, "type", None), default="")
    if item_type == "event":
        success = db.delete_event_instance(item_id, owner_id) is not None
    else:
        success = db.delete_item(
            item_id,
            soft=True,
            owner_id=owner_id,
            operation_log={
                "user_id": owner_id,
                "action": "delete",
                "item_type": item_type,
            },
        )
    if not success:
        raise HTTPException(
            status_code=409,
            detail="Item changed by another request; refresh and retry",
        )

    return {
        "ok": True,
        "data": {"id": str(item.id), "display_id": item.display_id},
        "message": "已删除",
    }
