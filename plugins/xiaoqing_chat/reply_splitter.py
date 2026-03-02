from __future__ import annotations

from typing import Any, Optional

def _split_chat_reply(text: str) -> list[str]:
    """
    将聊天回复按换行符拆分成多条消息
    
    Args:
        text: 完整的聊天回复文本
    
    Returns:
        拆分后的消息列表
    """
    if not text:
        return []

    lines = text.split("\n")
    messages = []
    current_code_block = []
    in_code_block = False

    for line in lines:
        stripped = line.strip()
        
        # 处理代码块标记
        if stripped.startswith("```"):
            if in_code_block:
                # 代码块结束
                current_code_block.append(line)
                messages.append("\n".join(current_code_block))
                current_code_block = []
                in_code_block = False
            else:
                # 代码块开始
                in_code_block = True
                current_code_block.append(line)
            continue

        if in_code_block:
            current_code_block.append(line)
        else:
            # 普通文本，按行拆分
            if stripped:
                messages.append(stripped)

    # 处理未闭合的代码块（作为最后一条消息）
    if current_code_block:
        messages.append("\n".join(current_code_block))
    
    return messages

def _build_reply_segments(text: str, *, reply_to_message_id: Optional[int]) -> list[dict[str, Any]]:
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
