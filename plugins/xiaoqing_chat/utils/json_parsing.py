from __future__ import annotations

import json
from typing import Any


def extract_first_json_object_text(text: str) -> str:
    if not text:
        return ""
    s = text.strip()
    start = s.find("{")
    if start < 0:
        return ""

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return ""


def parse_first_json_object(text: str) -> dict[str, Any] | None:
    parsed, ok = parse_first_json_object_with_status(text)
    return parsed if ok else None


def parse_first_json_object_with_status(text: str) -> tuple[dict[str, Any], bool]:
    obj_text = extract_first_json_object_text(text)
    if not obj_text:
        return {}, False
    try:
        parsed = json.loads(obj_text)
    except Exception:
        return {}, False
    return (parsed, True) if isinstance(parsed, dict) else ({}, False)


def extract_named_list_field(obj: dict[str, Any] | None, field: str) -> list[Any]:
    if not isinstance(obj, dict):
        return []
    value = obj.get(field)
    return value if isinstance(value, list) else []
