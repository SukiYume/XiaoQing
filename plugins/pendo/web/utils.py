"""Pendo Web API 共用的查询校验与数据转换辅助函数。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date, datetime
from math import isfinite
from typing import Any, Final

from fastapi import HTTPException

from ..models.item import ItemType
from ..utils.validators import ledger_amount_filter_to_cents

logger = logging.getLogger(__name__)
_COLLECTION_PAYLOAD_FIELDS: Final = (
    "id",
    "kind",
    "title",
    "category",
    "location",
    "notes",
)


def normalize_item_type_query(value: str | None) -> str | None:
    """把可选查询类型收敛到五类公开值，未知值明确返回 422。"""

    if value is None:
        return None
    try:
        return str(ItemType(str(value).strip()).value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid type: {value}") from exc


def normalize_choice_query(
    value: str | None,
    allowed: frozenset[str],
    field_name: str,
) -> str | None:
    """规范枚举查询值，空白视为未提供，未知值明确返回 422。"""

    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized not in allowed:
        raise HTTPException(status_code=422, detail=f"Invalid {field_name}: {value}")
    return normalized


def infer_item_query_type(
    item_type: str | None,
    *,
    has_task_filters: bool,
    has_ledger_filters: bool,
) -> str | None:
    """从类型专属筛选推断条目类型，并拒绝互相矛盾的组合。"""

    if has_task_filters and has_ledger_filters:
        raise HTTPException(status_code=422, detail="Task and ledger filters cannot be combined")
    if has_task_filters:
        if item_type not in (None, "task"):
            raise HTTPException(status_code=422, detail="Task filters require type=task")
        return "task"
    if has_ledger_filters:
        if item_type not in (None, "ledger"):
            raise HTTPException(status_code=422, detail="Ledger filters require type=ledger")
        return "ledger"
    return item_type


def amount_filter_cents(value: float) -> int:
    """把元金额筛选值按账目规则转换为非负整数分。"""
    if not isfinite(value):
        raise HTTPException(status_code=422, detail="金额筛选必须是有限数值")
    try:
        return ledger_amount_filter_to_cents(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def collection_payload(collection: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """只保留 API 允许公开的日程集合字段。"""
    if not collection:
        return None
    return {field: collection.get(field) for field in _COLLECTION_PAYLOAD_FIELDS}


def item_to_dict(item: object) -> dict[str, Any]:
    """把条目或映射复制为字符串键字典，并记录异常形状。"""
    payload: object = item
    if not isinstance(payload, Mapping):
        serializer = getattr(item, "to_dict", None)
        if not callable(serializer):
            logger.warning("Pendo item serialization skipped for type=%s", type(item).__name__)
            return {}
        payload = serializer()
    if not isinstance(payload, Mapping):
        logger.warning(
            "Pendo item serializer returned non-mapping type=%s",
            type(payload).__name__,
        )
        return {}
    return {str(key): value for key, value in payload.items()}


def parse_iso_date(value: str | None) -> date | None:
    """从 ISO 日期或日期时间中提取日期；非法值返回 ``None``。"""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None
