"""聊天记录的只读视图：``/qq look`` 和 agent 的 ``chat.history`` 服务共用。

看到的就是小青当时记下的：图片带着识图描述，还在本机的图片给出文件路径。agent 只能看发起人
所在的会话，会话由 Core 写进载荷；这里只按条数、关键词、发言人和“只看带图的”筛选。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
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


def _has_image(message: StoredMessage) -> bool:
    """这条消息发过图片（含图片表情），不论原图缓存还在不在：识图描述照样能看。"""

    return any(part.get("kind") in _IMAGE_KINDS for part in message.parts)


def _fingerprint(message: StoredMessage) -> str:
    """一条记录本身的指纹：同一秒的几条、没有消息 ID 的本地记录也分得开。"""

    identity = json.dumps(
        [
            message.role,
            message.name,
            message.user_id,
            message.message_id,
            message.local_id,
            repr(message.ts),
            render_stored_message(message),
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]


def page_cursor(history: Sequence[StoredMessage], index: int) -> str:
    """指向第 ``index`` 条记录的翻页位置（``序号@指纹``）：新消息追加在后面，序号不变。"""

    return f"{index}@{_fingerprint(history[index])}"


def _cursor_end(history: Sequence[StoredMessage], before: Any) -> int:
    """``before`` 指向的那条记录的位置（只看它之前的）；认不出时抛出 ``ValueError``。

    ``before`` 是翻页位置（``序号@指纹``），或某条消息的 QQ 消息 ID。存档前端滚动裁剪后序号
    会变小：在原序号及之前找同一指纹的记录，取最近的那条。
    """

    token = str(before).strip()
    index_text, sep, mark = token.partition("@")
    if sep:
        index = int(index_text) if index_text.isdigit() else -1
        for position in range(min(index, len(history) - 1), -1, -1):
            if _fingerprint(history[position]) == mark:
                return position
        raise ValueError(f"翻页位置 {token} 已失效，请不带 before 重新查。")
    for position, message in enumerate(history):
        if token in {str(remote) for remote in message.remote_ids()}:
            return position
    raise ValueError(f"本会话没有消息 ID 为 {token} 的记录。")


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
    try:
        end = _cursor_end(history, before) if before not in (None, "") else len(history)
    except ValueError as exc:
        return {"total": len(history), "matched": 0, "messages": [], "error": str(exc)}
    matched = [
        index
        for index in range(end)
        if _matches(history[index], words, sender)
        and (not images_only or _has_image(history[index]))
    ]
    shown = matched[-count:]
    return {
        "total"  : len(history),
        "matched": len(matched),
        "earlier": len(matched) > len(shown),
        # 接着往前翻时填进 before；指向本页最早的一条。
        "before"  : page_cursor(history, shown[0]) if len(matched) > len(shown) else "",
        "messages": [
            {
                "id"    : message.message_id,
                "time"  : message_stamp(message),
                "sender": sender_label(message),
                "role"  : message.role,
                "cursor": page_cursor(history, index),
                "text"  : message_body(message),
                "images": [
                    {
                        "path"       : str(part["file_path"]),
                        "description": str(part.get("description") or "").strip(),
                    }
                    for part in image_parts(message, inbox)
                ],
            }
            for index, message in ((index, history[index]) for index in shown)
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
