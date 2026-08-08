"""回复检查器测试共享的消息构造器。"""

from __future__ import annotations

import time

from plugins.xiaoqing_chat.memory.memory import StoredMessage


def stored_message(role: str, content: str, name: str = "") -> StoredMessage:
    """构造带当前时间戳的最小历史消息。"""

    return StoredMessage(role=role, content=content, name=name, ts=time.time())
