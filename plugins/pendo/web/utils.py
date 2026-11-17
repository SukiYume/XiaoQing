"""Pendo Web API 共用的查询校验与数据转换辅助函数。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date, datetime, timedelta, tzinfo
from math import isfinite
from typing import TYPE_CHECKING, Any, Final, cast

from fastapi import HTTPException

from core.log import LogEvent

from ..config import PendoConfig
from ..models.item import ItemType
from ..utils.identifiers import public_id
from ..utils.settings_utils import resolve_default_category
from ..utils.time_utils import now_in_timezone
from ..utils.validators import ledger_amount_filter_to_cents

if TYPE_CHECKING:
    from ..services.db import Database

log                               = logging.getLogger(__name__)
_COLLECTION_PAYLOAD_FIELDS: Final = (
    "id",
    "version",
    "kind",
    "title",
    "category",
    "location",
    "notes",
)


def clean_optional_text(value: str | None) -> str | None:
    """去除查询文本首尾空白；空白或缺省视为未提供。"""

    return str(value or "").strip() or None


def normalize_currency_query(value: str | None) -> str | None:
    """规范币种查询值为大写三字母代码；空白视为未提供，其他写法返回 422。"""

    code = (value or "").strip().upper()
    if not code:
        return None
    if len(code) != 3 or not code.isascii() or not code.isalpha():
        raise HTTPException(status_code=422, detail="currency 必须为三个字母的币种代码")
    return code


def category_or_default(db: Database, owner_id: str, value: object) -> str:
    """空白或占位的“未分类”换成用户设置的默认分类；其他值只去首尾空白。"""

    category = str(value or "").strip()
    if not category or category == PendoConfig.DEFAULT_CATEGORY:
        return resolve_default_category(db, owner_id)
    return category


def timezone_name_of(local_now: datetime) -> str:
    """用户本地时间所在的 IANA 时区名；时间不带命名时区时用 Pendo 默认时区。"""

    return str(getattr(local_now.tzinfo, "key", None) or PendoConfig.DEFAULT_TIMEZONE)


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
    payload               = {field: collection.get(field) for field in _COLLECTION_PAYLOAD_FIELDS}
    payload["display_id"] = public_id(payload.get("id"))
    return payload


def item_to_dict(item: object) -> dict[str, Any]:
    """把条目或映射复制为字符串键字典，并记录异常形状。"""
    payload: object = item
    if not isinstance(payload, Mapping):
        serializer = getattr(item, "to_dict", None)
        if not callable(serializer):
            log.warning(LogEvent("item.serialization_skipped", value_type=type(item).__name__))
            return {}
        payload = serializer()
    if not isinstance(payload, Mapping):
        log.warning(LogEvent("item.serializer_non_mapping", value_type=type(payload).__name__))
        return {}
    result               = {str(key): value for key, value in payload.items()}
    result["display_id"] = public_id(result.get("id"))
    references           = result.get("references")
    if isinstance(references, list):
        result["references"] = [
            {
                **{str(key): value for key, value in reference.items()},
                "display_id": public_id(reference.get("id")),
            }
            if isinstance(reference, Mapping)
            else reference
            for reference in references
        ]
    return result


def parse_iso_date(value: object, zone: tzinfo | None = None) -> date | None:
    """从 ISO 日期或日期时间中提取日期；非法值返回 ``None``。

    给了 ``zone`` 时，带偏移的时间先换到这个时区再取日期。
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if zone is not None and parsed.tzinfo is not None:
        parsed = parsed.astimezone(zone)
    return parsed.date()


def parse_argument_day(value: str | None, field_name: str) -> date | None:
    """解析可选日期参数；空白视为未提供，显式的非法值抛 ``ValueError``。"""

    if value is None or not value.strip():
        return None
    parsed = parse_iso_date(value)
    if parsed is None:
        raise ValueError(f"{field_name} must be a valid ISO date")
    return cast(date, parsed)


def resolve_today(db: Database, owner_id: str, today: str | None = None) -> date:
    """显式给出的 ``today`` 优先（非法值抛 ``ValueError``），缺省是用户时区的今天。"""

    return parse_argument_day(today, "today") or now_in_timezone(owner_id, db).date()


def preset_range(preset: str, today: date) -> tuple[date, date] | None:
    """week / month / quarter / year / last_year 预设对应的闭区间日期；其他名字返回 ``None``。

    本周从周一起算，本季从季首月起算，区间都截止到 ``today``（去年是整年）。
    """

    quarter_month = ((today.month - 1) // 3) * 3 + 1
    bounds        = {
        "week"     : (today - timedelta(days=today.weekday()), today),
        "month"    : (today.replace(day=1), today),
        "quarter"  : (date(today.year, quarter_month, 1), today),
        "year"     : (date(today.year, 1, 1), today),
        "last_year": (date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)),
    }
    return bounds.get(preset)
