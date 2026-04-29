from __future__ import annotations

import json
import re
from typing import Any

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)
_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>[\s\S]*?</think>", re.IGNORECASE)
_LEADING_THINK_RE = re.compile(
    r"^\s*(?:思考|推理|reasoning|thinking)\s*[:：][\s\S]*?(?=[{\[])",
    re.IGNORECASE,
)


def normalize_llm_text(text: str) -> str:
    if not text:
        return ""
    s = str(text).strip()
    s = _THINK_BLOCK_RE.sub("", s).strip()
    s = _LEADING_THINK_RE.sub("", s).strip()
    blocks = _JSON_BLOCK_RE.findall(s)
    if blocks:
        return blocks[0].strip()
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


def extract_first_json_array_text(text: str) -> str:
    s = normalize_llm_text(text)
    if not s:
        return ""
    start = s.find("[")
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
        if ch == "[":
            depth += 1
            continue
        if ch == "]":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
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
    return re.sub(r'([{\[,]\s*)([A-Za-z_][\w\-]*)(\s*:)', r'\1"\2"\3', text)


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
    obj_pos = s.find("{") if s else -1
    arr_pos = s.find("[") if s else -1
    if obj_pos < 0 and arr_pos < 0:
        return None
    if arr_pos >= 0 and (obj_pos < 0 or arr_pos < obj_pos):
        arr = parse_first_json_array(s)
        return arr if arr else None
    return parse_first_json_object(s)


def extract_named_list_field(obj: dict[str, Any] | None, field: str) -> list[Any]:
    if not isinstance(obj, dict):
        return []
    value = obj.get(field)
    return value if isinstance(value, list) else []
