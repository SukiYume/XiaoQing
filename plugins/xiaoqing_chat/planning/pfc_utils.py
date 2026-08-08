"""从模型返回值中保守提取结构化规划列表。"""

from __future__ import annotations

from typing import Any

from ..utils.json_parsing import (
    parse_first_json_object,
    parse_first_json_value,
)


def get_items_from_json(
    content: str,
    *items: str,
    default_values: dict[str, Any] | None = None,
    required_types: dict[str, type] | None = None,
    allow_array: bool = True,
) -> tuple[bool, dict[str, Any] | list[dict[str, Any]]]:
    s = (content or "").strip()
    result: dict[str, Any] = {}
    if default_values:
        result.update(default_values)

    parsed = parse_first_json_value(s) if allow_array else parse_first_json_object(s)
    if isinstance(parsed, list):
        valid: list[dict[str, Any]] = []
        for it in parsed:
            if not all(k in it for k in items):
                continue
            if required_types:
                ok = True
                for k, tp in required_types.items():
                    if k in it and not isinstance(it[k], tp):
                        ok = False
                        break
                if not ok:
                    continue
            empty = False
            for k in items:
                v = it.get(k)
                if isinstance(v, str) and not v.strip():
                    empty = True
                    break
            if empty:
                continue
            valid.append(it)
        if valid:
            return True, valid
        return False, result

    if parsed is None:
        return False, result

    if not isinstance(parsed, dict):
        return False, result

    for k in items:
        if k in parsed:
            result[k] = parsed[k]

    if not all(k in result for k in items):
        return False, result

    if required_types:
        for k, tp in required_types.items():
            if k in result and not isinstance(result[k], tp):
                return False, result

    for k in items:
        v = result.get(k)
        if isinstance(v, str) and not v.strip():
            return False, result

    return True, result
