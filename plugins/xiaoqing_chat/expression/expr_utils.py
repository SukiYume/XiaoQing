"""表达学习模块的共享工具。"""

from __future__ import annotations

from collections.abc import Sequence

from ..memory.memory import StoredMessage
from ..message_parts import render_stored_message


def render_dialogue(
    messages: Sequence[StoredMessage], *, max_lines: int = 30, max_text_len: int = 200
) -> str:
    """把消息序列渲染为便于阅读的对话文本。"""
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
