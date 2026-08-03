from __future__ import annotations

import json
import re
from typing import Any

_JSON_BLOCK_RE = re.compile(r"^\s*```json[ \t]*\r?\n([\s\S]*?)\r?\n```\s*$", re.IGNORECASE)


def normalize_llm_text(text: str) -> str:
    if not text:
        return ""
    s = str(text).strip()
    match = _JSON_BLOCK_RE.fullmatch(s)
    if match:
        return match.group(1).strip()
    return s


def normalize_response_content(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return ""
    choice0 = choices[0] if isinstance(choices[0], dict) else {}
    msg = choice0.get("message")
    if not isinstance(msg, dict):
        delta = choice0.get("delta")
        msg = delta if isinstance(delta, dict) else {}
    content = msg.get("content")
    return normalize_llm_text(content) if isinstance(content, str) else ""


def extract_first_json_object_text(text: str) -> str:
    s = normalize_llm_text(text)
    if not s:
        return ""
    if not s.startswith("{"):
        return ""
    start = 0

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
                return s if not s[i + 1 :].strip() else ""
    return ""


def extract_first_json_array_text(text: str) -> str:
    s = normalize_llm_text(text)
    if not s:
        return ""
    if not s.startswith("["):
        return ""
    start = 0

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
        if ch == "[":
            depth += 1
            continue
        if ch == "]":
            depth -= 1
            if depth == 0:
                return s if not s[i + 1 :].strip() else ""
    return ""


def _remove_trailing_commas(text: str) -> str:
    out: list[str] = []
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            continue
        if ch == ",":
            j = i + 1
            while j < len(text) and text[j].isspace():
                j += 1
            if j < len(text) and text[j] in "}]":
                continue
        out.append(ch)
    return "".join(out)


def _quote_unquoted_keys(text: str) -> str:
    return re.sub(r"([{\[,]\s*)([A-Za-z_][\w\-]*)(\s*:)", r'\1"\2"\3', text)


def repair_json_text(text: str) -> str:
    s = normalize_llm_text(text)
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    s = _remove_trailing_commas(s)
    s = _quote_unquoted_keys(s)
    return s


def parse_first_json_object(text: str) -> dict[str, Any] | None:
    parsed, ok = parse_first_json_object_with_status(text)
    return parsed if ok else None


def parse_first_json_object_with_status(text: str) -> tuple[dict[str, Any], bool]:
    obj_text = extract_first_json_object_text(text)
    if not obj_text:
        return {}, False
    candidates = [obj_text, repair_json_text(obj_text)]
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed, True
    return {}, False


def parse_first_json_array(text: str) -> list[dict[str, Any]]:
    arr_text = extract_first_json_array_text(text)
    if not arr_text:
        return []
    candidates = [arr_text, repair_json_text(arr_text)]
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, list):
            return [it for it in parsed if isinstance(it, dict)]
    return []


def parse_first_json_value(text: str) -> dict[str, Any] | list[dict[str, Any]] | None:
    s = normalize_llm_text(text)
    if s.startswith("["):
        arr = parse_first_json_array(s)
        return arr if arr else None
    if s.startswith("{"):
        return parse_first_json_object(s)
    return None


def extract_named_list_field(obj: dict[str, Any] | None, field: str) -> list[Any]:
    if not isinstance(obj, dict):
        return []
    value = obj.get(field)
    return value if isinstance(value, list) else []


def strict_json_bool(value: Any) -> bool | None:
    """只接受 JSON 布尔值，不接受具有真值语义的字符串或数字。"""
    return value if type(value) is bool else None
