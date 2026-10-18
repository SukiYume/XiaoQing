"""从模型返回值中保守提取结构化规划列表。"""

from __future__ import annotations

from typing import Any

from ..utils.json_parsing import (
    parse_first_json_object,
    parse_first_json_value,
)


def _has_required_items(
    record: Any, items: tuple[str, ...], required_types: dict[str, type] | None
) -> bool:
    """必填键齐全、类型符合约束，且字符串必填项非空白。"""
    if not isinstance(record, dict) or not all(k in record for k in items):
        return False
    if any(
        k in record and not isinstance(record[k], tp) for k, tp in (required_types or {}).items()
    ):
        return False
    return not any(isinstance(record.get(k), str) and not record[k].strip() for k in items)


def get_items_from_json(
    content: str,
    *items: str,
    default_values: dict[str, Any] | None  = None,
    required_types: dict[str, type] | None = None,
    optional_items: tuple[str, ...]        = (),
    allow_array: bool                      = True,
) -> tuple[bool, dict[str, Any] | list[dict[str, Any]]]:
    """解析模型输出的对象或对象数组。

    数组只保留合格的条目，全部不合格时返回失败与默认值；对象在默认值之上合并
    必填和可选键，失败时也返回已合并的部分结果供调用方兜底。
    """
    s = (content or "").strip()
    result: dict[str, Any] = dict(default_values or {})
    parsed = parse_first_json_value(s) if allow_array else parse_first_json_object(s)
    if isinstance(parsed, list):
        valid = [it for it in parsed if _has_required_items(it, items, required_types)]
        return (True, valid) if valid else (False, result)
    if not isinstance(parsed, dict):
        return False, result
    result.update({k: parsed[k] for k in (*items, *optional_items) if k in parsed})
    return _has_required_items(result, items, required_types), result
