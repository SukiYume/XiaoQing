"""把 OneBot QQ 表情段解析为稳定、可读的文本描述。"""

from __future__ import annotations

import re
from typing import Any

_FACE_TEXT_KEYS = (
    "text",
    "name",
    "summary",
    "desc",
    "description",
    "wording",
    "displayText",
    "content",
    "faceText",
    "emojiText",
    "label",
    "title",
)

_GENERIC_FACE_LABELS = frozenset(
    {"face", "emoji", "qqface", "qq emoji", "qq表情", "表情", "系统表情"}
)


def _strip_wrapping_brackets(text: str) -> str:
    pairs = (("[", "]"), ("【", "】"), ("(", ")"), ("（", "）"))
    cleaned = text.strip()
    while cleaned:
        stripped = cleaned
        for left, right in pairs:
            if (
                cleaned.startswith(left)
                and cleaned.endswith(right)
                and len(cleaned) > len(left) + len(right)
            ):
                stripped = cleaned[len(left) : -len(right)].strip()
                break
        if stripped == cleaned:
            return cleaned
        cleaned = stripped
    return ""


def _clean_face_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = _strip_wrapping_brackets(text)
    if not text:
        return ""
    if text.lower() in _GENERIC_FACE_LABELS:
        return ""
    text = re.sub(r"\s+", " ", text)
    if text.startswith(("http://", "https://")):
        return ""
    return text[:32]


def _extract_label_from_raw(raw: Any) -> str:
    if isinstance(raw, str):
        return _clean_face_text(raw)
    if isinstance(raw, dict):
        for key in _FACE_TEXT_KEYS:
            label = _clean_face_text(raw.get(key))
            if label:
                return label
        for value in raw.values():
            label = _extract_label_from_raw(value)
            if label:
                return label
        return ""
    if isinstance(raw, list):
        for item in raw:
            label = _extract_label_from_raw(item)
            if label:
                return label
    return ""


def describe_face_segment(segment: dict[str, Any]) -> str:
    data = segment.get("data", {}) or {}

    for key in _FACE_TEXT_KEYS:
        label = _clean_face_text(data.get(key))
        if label:
            break
    else:
        label = _extract_label_from_raw(data.get("raw"))

    face_id = str(data.get("id", "") or "").strip()
    if not label and face_id:
        label = f"id={face_id}"
    if not label:
        label = "系统表情"

    chain_count = data.get("chainCount")
    try:
        chain_count_value = int(chain_count or 0)
    except (TypeError, ValueError):
        chain_count_value = 0
    if chain_count_value > 1:
        label = f"{label}，连发{chain_count_value}次"

    return label
