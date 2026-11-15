"""Qingpet 数据库仓储共享的每日金币上限、背包字典读写、评分 SQL 与安全错误日志。"""

import json
import logging
import re

from ..utils.constants import DAILY_LIMITS

logger = logging.getLogger(__name__)

_SAFE_OPERATION_RE  = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SAFE_ERROR_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,95}\Z")

_DAILY_COIN_LIMIT = int(DAILY_LIMITS["coins"])

# 照顾评分的 SQL 表达式：五项状态的平均值，范围 0~100（``Pet.care_score`` 是同一公式的 0~1 比例）。
_CARE_SCORE_SQL = "(hunger + mood + clean + energy + health) / 5.0"


def _parse_inventory_items(raw: str | None) -> dict[str, int]:
    """把 ``inventories.items`` 列解析成背包字典；空值是空背包。"""
    items: dict[str, int] = json.loads(raw or "{}")
    return items


def _adjust_item_count(items: dict[str, int], item_id: str, delta: int) -> None:
    """按增量改背包字典；数量降到 0 或以下时删掉这一项。"""
    remaining = int(items.get(item_id, 0)) + delta
    if remaining > 0:
        items[item_id] = remaining
    else:
        items.pop(item_id, None)


def _log_database_failure(operation: str, exc: BaseException) -> None:
    """仅记录稳定的操作名和异常类型，避免数据库内容进入日志。"""

    safe_operation = operation if _SAFE_OPERATION_RE.fullmatch(operation) else "unknown"
    error_type     = type(exc).__name__
    if not _SAFE_ERROR_TYPE_RE.fullmatch(error_type):
        error_type = "Exception"
    logger.error(
        "QingPet database operation failed operation=%s error_type=%s",
        safe_operation,
        error_type,
    )
