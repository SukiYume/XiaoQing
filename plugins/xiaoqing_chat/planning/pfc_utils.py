from __future__ import annotations

import json
import re
from typing import Any, Optional, Union

_RE_ARRAY = re.compile(r"\[[\s\S]*\]")
_RE_OBJ = re.compile(r"\{[\s\S]*\}")
_RE_JSON_BLOCK = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


def _strip_code_block(text: str) -> str:
    """Strip markdown code blocks from text before JSON extraction."""
    s = (text or "").strip()
    blocks = _RE_JSON_BLOCK.findall(s)
    if blocks:
        return blocks[0].strip()
    return s

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

    if allow_array:
        m = _RE_ARRAY.search(s)
        if m:
            try:
                arr = json.loads(m.group(0))
            except Exception:
                arr = None
            if isinstance(arr, list):
                valid: list[dict[str, Any]] = []
                for it in arr:
                    if not isinstance(it, dict):
                        continue
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

    obj_text = s
    try:
        parsed = json.loads(obj_text)
    except Exception:
        m = _RE_OBJ.search(s)
        if not m:
            return False, result
        try:
            parsed = json.loads(m.group(0))
        except Exception:
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
    s = _strip_code_block(text)
    m = _RE_ARRAY.search(s)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
        if isinstance(arr, list):
            return [it for it in arr if isinstance(it, dict)]
    except Exception:
        pass
    return []

def extract_first_json_dict(text: str) -> Optional[dict[str, Any]]:
    """Extract the first JSON object from text, handling code blocks."""
    s = _strip_code_block(text)
    m = _RE_OBJ.search(s)
    if not m:
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None
