"""Shared utilities for the expression module."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..memory.memory import StoredMessage
from ..message_parts import render_stored_message
from ..utils.json_parsing import parse_first_json_array, parse_first_json_object


def extract_json_obj(text: str) -> dict[str, Any]:
    """Extract the first JSON object from LLM response text."""
    return parse_first_json_object(text) or {}


def extract_json_array(text: str) -> list[dict[str, Any]]:
    """Extract the first JSON array from LLM response text."""
    return parse_first_json_array(text)


def render_dialogue(
    messages: Sequence[StoredMessage], *, max_lines: int = 30, max_text_len: int = 200
) -> str:
    """Render a sequence of messages into a human-readable dialogue string."""
    lines: list[str] = []
    for msg in messages[-max_lines:]:
        t = render_stored_message(msg)
        if not t:
            continue
        if len(t) > max_text_len:
            t = t[: max_text_len - 40].rstrip() + "…"
        name = msg.name or ("小青" if msg.role == "assistant" else "用户")
        role = "小青" if msg.role == "assistant" else "对方"
        lines.append(f"{role}({name})：{t}")
    return "\n".join(lines).strip()
