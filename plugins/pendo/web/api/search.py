"""提供跨五类 Pendo 条目的全文搜索与分页端点。"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from ...models.item import Item
from ...services.db import Database
from ...utils.time_utils import now_in_timezone
from ...utils.validators import LEDGER_TRANSACTION_TYPES, TASK_STATUSES
from ..deps import get_current_user, get_db
from ..utils import (
    collection_payload,
    infer_item_query_type,
    item_to_dict,
    normalize_choice_query,
    normalize_item_type_query,
)

router = APIRouter()


def _clean_optional_text(value: str | None) -> str | None:
    """去除查询文本首尾空格，空白值视为未提供。"""

    return str(value or "").strip() or None


def _build_search_filters(
    item_type: str | None,
    category: str | None,
    ledger_category: str | None,
    status: str | None,
    transaction_type: str | None,
    account_name: str | None,
    merchant: str | None,
) -> dict[str, Any]:
    """规范搜索筛选，并拒绝被旧实现静默忽略的冲突组合。"""

    item_type        = normalize_item_type_query(item_type)
    category         = _clean_optional_text(category)
    ledger_category  = _clean_optional_text(ledger_category)
    status           = normalize_choice_query(status, TASK_STATUSES, "status")
    transaction_type = normalize_choice_query(
        transaction_type,
        LEDGER_TRANSACTION_TYPES,
        "transaction_type",
    )
    account_name = _clean_optional_text(account_name)
    merchant     = _clean_optional_text(merchant)
    if category and ledger_category:
        raise HTTPException(
            status_code = 422,
            detail      = "category and ledger_category cannot be combined",
        )

    item_type = infer_item_query_type(
        item_type,
        has_task_filters   = status is not None,
        has_ledger_filters = any(
            value is not None
            for value in (ledger_category, transaction_type, account_name, merchant)
        ),
    )
    filters: dict[str, Any] = {
        key: value
        for key, value in (
            ("type", item_type),
            ("status", status),
            ("transaction_type", transaction_type),
            ("account_name", account_name),
            ("merchant", merchant),
        )
        if value is not None
    }
    if category:
        filters["ledger_category" if item_type == "ledger" else "category"] = category
    if ledger_category:
        filters["ledger_category"] = ledger_category
    return filters


def _serialize_results(
    db: Database,
    owner_id: str,
    results: list[Item],
) -> list[dict[str, Any]]:
    """序列化结果，并一次批量补齐当前所有者的日程集合摘要。"""

    payloads       = [item_to_dict(item) for item in results]
    collection_ids = [
        str(collection_id)
        for payload in payloads
        if payload.get("type") == "event" and (collection_id := payload.get("event_collection_id"))
    ]
    collections = db.get_event_collections_by_ids(owner_id, collection_ids)
    for payload in payloads:
        collection_id = payload.get("event_collection_id")
        if payload.get("type") == "event" and collection_id:
            payload["collection"] = collection_payload(collections.get(str(collection_id)))
    return payloads


@router.get("/search")
def search_items(
    q: str = "",
    item_type: Annotated[str | None, Query(alias="type")] = None,
    category: str | None         = None,
    ledger_category: str | None  = None,
    status: str | None           = None,
    transaction_type: str | None = None,
    account_name: str | None     = None,
    merchant: str | None         = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
    owner_id: str                                     = Depends(get_current_user),
    db: Database                                      = Depends(get_db),
    preset: Literal["overdue", "recent_diary"] | None = None,
) -> dict[str, object]:
    """按统一筛选契约返回全文搜索当前页、完整总数和集合摘要。"""

    query = str(q or "").strip()
    if not query and not preset:
        raise HTTPException(status_code=422, detail="Search query cannot be empty")
    if page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(status_code=422, detail="Invalid pagination")
    filters = _build_search_filters(
        item_type,
        category,
        ledger_category,
        status,
        transaction_type,
        account_name,
        merchant,
    )
    if preset:
        expected_type = "task" if preset == "overdue" else "diary"
        if filters.get("type", expected_type) != expected_type:
            raise HTTPException(status_code=422, detail="搜索条件与快捷筛选类型冲突")
        filters["type"] = expected_type
        today           = now_in_timezone(owner_id, db).date()
        if preset == "overdue":
            if filters.get("status", "open") != "open":
                raise HTTPException(status_code=422, detail="逾期筛选适用于未完成待办")
            filters.update(status="open", overdue_date=today.isoformat())
        else:
            filters.update(
                date_field="diary_date",
                start_date=(today - timedelta(days=6)).isoformat(),
                end_date=today.isoformat(),
            )
    offset = (page - 1) * page_size
    if query:
        results, total = db.search_items_page(
            owner_id, query, filters=filters, limit=page_size, offset=offset
        )
    else:
        results = db.get_items(owner_id, filters=filters, limit=page_size, offset=offset)
        total = db.count_items(owner_id, filters=filters)

    return {
        "ok"  : True,
        "data": {
            "items"    : _serialize_results(db, owner_id, results),
            "total"    : total,
            "page"     : page,
            "page_size": page_size,
        },
        "message": "",
    }
