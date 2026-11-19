"""聊天记录的只读视图：``/qq look`` 和 agent 的 ``chat.history`` 服务共用。

看到的就是小青当时记下的：图片带着识图描述，还在本机的图片给出文件路径。agent 只能看发起人
所在的会话，会话由 Core 写进载荷；这里只按条数、关键词、发言人和“只看带图的”筛选。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from core.args import parse_int

from .media.settings import figures_inbox_dir
from .memory.memory import StoredMessage
from .message_parts import render_stored_message
from .store_binding import bound_state
from .utils.wall_clock import format_local

_IMAGE_KINDS = frozenset({"image", "emoji"})
# 一条消息连同图片描述最多显示这么多字；截图的识图描述常有几百字。
MESSAGE_CHARS  = 800
_DEFAULT_COUNT = 20
_MAX_COUNT     = 50
_MAX_QUERY     = 100


def sender_label(message: StoredMessage) -> str:
    who = message.name.strip() or "用户"
    if message.role != "assistant" and message.user_id is not None:
        who += f"({message.user_id})"
    return who


def message_body(message: StoredMessage) -> str:
    body = render_stored_message(message).strip()
    return body[:MESSAGE_CHARS] + "…" if len(body) > MESSAGE_CHARS else body


def message_stamp(message: StoredMessage) -> str:
    return format_local("%m-%d %H:%M", message.ts) if message.ts else "时间未知"


def image_parts(message: StoredMessage, inbox: Path | None = None) -> list[dict[str, Any]]:
    """这条消息里还在本机的图片；给了 ``inbox`` 时只算收图目录里的文件。"""

    found: list[dict[str, Any]] = []
    for part in message.parts:
        raw = str(part.get("file_path") or "")
        if part.get("kind") not in _IMAGE_KINDS or not raw:
            continue
        path = Path(raw)
        if inbox is not None and not path.resolve().is_relative_to(inbox):
            continue
        if path.is_file():
            found.append(part)
    return found


def _matches(message: StoredMessage, words: Sequence[str], sender: str) -> bool:
    if sender:
        label = sender_label(message).casefold()
        if sender not in label:
            return False
    if words:
        haystack = f"{sender_label(message)} {render_stored_message(message)}".casefold()
        return all(word in haystack for word in words)
    return True


def _select(
    history: Sequence[StoredMessage], payload: dict[str, Any], inbox: Path
) -> dict[str, Any]:
    count = parse_int(str(payload.get("count") or _DEFAULT_COUNT), minimum=1, maximum=_MAX_COUNT)
    count       = count or _DEFAULT_COUNT
    words       = str(payload.get("query") or "")[:_MAX_QUERY].casefold().split()
    sender      = str(payload.get("sender") or "")[:_MAX_QUERY].strip().casefold()
    images_only = payload.get("images_only") is True
    before      = payload.get("before")
    end         = len(history)
    if before is not None:
        end = next(
            (
                index
                for index, message in enumerate(history)
                if str(before) in {str(remote) for remote in message.remote_ids()}
            ),
            end,
        )
    matched = [
        message
        for message in history[:end]
        if _matches(message, words, sender) and (not images_only or image_parts(message, inbox))
    ]
    shown = matched[-count:]
    return {
        "total"   : len(history),
        "matched" : len(matched),
        "earlier" : len(matched) > len(shown),
        "messages": [
            {
                "id"    : message.message_id,
                "time"  : message_stamp(message),
                "sender": sender_label(message),
                "role"  : message.role,
                "text"  : message_body(message),
                "images": [
                    {
                        "path"       : str(part["file_path"]),
                        "description": str(part.get("description") or "").strip(),
                    }
                    for part in image_parts(message, inbox)
                ],
            }
            for message in shown
        ],
    }


async def read_history(payload: dict[str, Any], context: Any) -> dict[str, Any]:
    """``chat.history`` 服务：载荷里的 ``chat`` 由 Core 写入，其余字段是可选的筛选条件。"""

    chat = str(payload.get("chat") or "")
    if not chat:
        raise ValueError("chat history requires a chat scope")
    history = await bound_state(context).memory_store.get_async(chat)
    inbox   = figures_inbox_dir(context).resolve()
    # 逐张图片确认文件还在要碰磁盘，放到事件循环之外。
    result = await asyncio.to_thread(_select, history, payload, inbox)
    return {"chat": chat, **result}
