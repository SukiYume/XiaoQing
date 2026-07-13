from __future__ import annotations

from typing import Any


def _split_chat_reply(text: str) -> list[str]:
    """
    将聊天回复按连贯文本块拆分成多条消息，同时保护代码块不被拆分

    Args:
        text: 完整的聊天回复文本

    Returns:
        拆分后的消息列表
    """
    if not text:
        return []

    lines = text.split("\n")
    messages = []
    current_message = []
    in_code_block = False

    for line in lines:
        stripped = line.strip()

        # 处理代码块标记
        if stripped.startswith("```"):
            if in_code_block:
                # 代码块结束
                current_message.append(line)
                messages.append("\n".join(current_message).strip())
                current_message = []
                in_code_block = False
            else:
                # 代码块开始
                if current_message:
                    messages.append("\n".join(current_message).strip())
                    current_message = []
                in_code_block = True
                current_message.append(line)
            continue

        if in_code_block:
            current_message.append(line)
        else:
            # 普通文本，遇到空行则拆分
            if not stripped:
                if current_message:
                    messages.append("\n".join(current_message).strip())
                    current_message = []
            else:
                current_message.append(line)

    # 处理未闭合的代码块或普通最后一段
    if current_message:
        messages.append("\n".join(current_message).strip())

    return [m for m in messages if m]

def _build_reply_segments(text: str, *, reply_to_message_id: int | None) -> list[dict[str, Any]]:
    """
    构建回复消息段

    Args:
        text: 回复文本
        reply_to_message_id: 引用的消息ID（可选）

    Returns:
        消息段列表
    """
    from core.plugin_base import segments

    segs = segments(text)
    if reply_to_message_id is None:
        return segs
    try:
        msg_id = int(reply_to_message_id)
    except (TypeError, ValueError):
        return segs
    return [{"type": "reply", "data": {"id": str(msg_id)}}, *segs]
