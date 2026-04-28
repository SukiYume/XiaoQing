from __future__ import annotations

import re
from typing import Any, Optional, Union

from ..utils.json_parsing import (
    normalize_llm_text,
    parse_first_json_array,
    parse_first_json_object,
    parse_first_json_value,
)

_RE_ARRAY = re.compile(r"\[[\s\S]*\]")
_RE_OBJ = re.compile(r"\{[\s\S]*\}")
_RE_JSON_BLOCK = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


def _strip_code_block(text: str) -> str:
    """Strip markdown code blocks from text before JSON extraction."""
    return normalize_llm_text(text)

def get_items_from_json(
    content: str,
    *items: str,
    default_values: Optional[dict[str, Any]] = None,
    required_types: Optional[dict[str, type]] = None,
    allow_array: bool = True,
) -> tuple[bool, Union[dict[str, Any], list[dict[str, Any]]]]:
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

def extract_first_json_list(text: str) -> list[dict[str, Any]]:
    """Extract the first JSON list from text, handling code blocks."""
    return parse_first_json_array(text)

def extract_first_json_dict(text: str) -> Optional[dict[str, Any]]:
    """Extract the first JSON object from text, handling code blocks."""
    return parse_first_json_object(text)
