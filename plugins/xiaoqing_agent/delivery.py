"""把任务结果变成 QQ 消息：文字和图片同条发送，语音、视频各自单独发送，其他文件走 OneBot 文件上传。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.interfaces import ACTION_BYPASS_SINK_KEY
from core.plugin_base import build_action, image, record, text

from .tasks import ChatRef, Outcome

logger = logging.getLogger(__name__)

# 每发完一部分就回报一次：这次发送带出了哪些附件、结果如何。
PartReport = Callable[[list[Path], "bool | None"], Awaitable[None]]


async def _sent_part(
    report: PartReport | None,
    paths: list[Path],
    sending: Awaitable[bool | None],
) -> bool | None:
    """等一次发送并立刻回报；发送途中被取消时这部分可能已经发出，按未知回报后继续取消。"""

    try:
        sent = await sending
    except asyncio.CancelledError:
        if report is not None:
            await asyncio.shield(report(paths, None))
        raise
    if report is not None:
        await asyncio.shield(report(paths, sent))
    return sent


IMAGE_SUFFIXES  = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
AUDIO_SUFFIXES  = frozenset({".mp3", ".wav", ".silk", ".amr", ".ogg", ".m4a", ".flac"})
VIDEO_SUFFIXES  = frozenset({".mp4", ".mov", ".webm", ".mkv"})
MAX_IMAGES      = 9
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_FILE_BYTES  = 100 * 1024 * 1024
MAX_UPLOADS     = 5


def classify_files(paths: list[str]) -> tuple[list[Path], list[Path], list[str]]:
    """拆成可内嵌图片、随后发送的附件和无法发送的说明；重复路径只发一次。"""

    images  : list[Path] = []
    uploads : list[Path] = []
    problems: list[str]  = []
    seen    : set[Path]  = set()
    for raw in paths:
        path = Path(raw).expanduser()
        try:
            resolved = path.resolve()
            size     = resolved.stat().st_size if resolved.is_file() else None
        except OSError:
            size     = None
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        if size is None:
            problems.append(f"找不到文件 {path.name}")
        elif resolved.suffix.lower() in IMAGE_SUFFIXES and size <= MAX_IMAGE_BYTES:
            if len(images) < MAX_IMAGES:
                images.append(resolved)
            else:
                problems.append(f"图片太多，{resolved.name} 没有发出")
        elif size > MAX_FILE_BYTES:
            problems.append(f"{resolved.name} 超过 100MB，留在 {resolved}")
        elif len(uploads) < MAX_UPLOADS:
            uploads.append(resolved)
        else:
            problems.append(f"文件太多，{resolved.name} 留在 {resolved}")
    return images, uploads, problems


def outcome_message(outcome: Outcome) -> tuple[list[dict[str, Any]], list[Path]]:
    """返回同一条消息里的文字与图片段，以及之后单独发送的语音、视频和文件。"""

    images, uploads, problems = classify_files(outcome.files)
    body = outcome.text.strip() or "做完了，但没有可以说明的结果。"
    if problems:
        body = f"{body}\n（{'；'.join(problems)}）"
    message = [text(body), *(image(str(path)) for path in images)]
    return message, uploads


async def _dispatch(context: Any, action: dict[str, Any], what: str) -> bool | None:
    """发出一个动作：明确失败或出错为 ``False``，结果未知为 ``None``。"""

    action[ACTION_BYPASS_SINK_KEY] = True
    try:
        sent = await context.send_action(action)
    except Exception as exc:
        logger.warning("xiaoqing_agent %s failed error_type=%s", what, type(exc).__name__)
        return False
    return sent if isinstance(sent, bool) else None


async def send_message(context: Any, chat: ChatRef, message: list[dict[str, Any]]) -> bool | None:
    action = build_action(message, chat.user_id, chat.group_id)
    if action is None:
        return False
    return await _dispatch(context, action, "message delivery")


async def upload_file(context: Any, chat: ChatRef, path: Path) -> bool | None:
    params: dict[str, Any] = {"file": str(path), "name": path.name}
    if chat.group_id is not None:
        action = {"action": "upload_group_file", "params": {"group_id": chat.group_id, **params}}
    else:
        action = {"action": "upload_private_file", "params": {"user_id": chat.user_id, **params}}
    return await _dispatch(context, action, "file upload")


async def upload_each(
    context: Any,
    chat: ChatRef,
    uploads: list[Path],
    *,
    on_result: PartReport | None = None,
) -> list[tuple[Path, bool | None]]:
    """语音和视频在 QQ 里必须单独成条，其余文件走文件上传；逐个返回并回报发送结果。"""

    results: list[tuple[Path, bool | None]] = []
    for path in uploads:
        suffix = path.suffix.lower()
        if suffix in AUDIO_SUFFIXES:
            sending = send_message(context, chat, [record(str(path))])
        elif suffix in VIDEO_SUFFIXES:
            sending = send_message(
                context, chat, [{"type": "video", "data": {"file": path.as_uri()}}]
            )
        else:
            sending = upload_file(context, chat, path)
        results.append((path, await _sent_part(on_result, [path], sending)))
    return results


@dataclass
class SendReport:
    """一次交付里各部分的发送结果：内嵌图片随文字那条消息，其余附件各自一份。"""

    message: bool | None
    images : list[Path] = field(default_factory=list)
    uploads: list[tuple[Path, bool | None]] = field(default_factory=list)

    @property
    def result(self) -> bool | None:
        """全部明确送达为 ``True``，任一部分失败为 ``False``，其余有未知结果时为 ``None``。"""

        parts = [self.message, *(sent for _, sent in self.uploads)]
        if any(sent is False for sent in parts):
            return False
        if all(sent is True for sent in parts):
            return True
        return None


async def deliver_outcome(
    context: Any,
    chat: ChatRef,
    outcome: Outcome,
    *,
    mention: bool                = False,
    on_result: PartReport | None = None,
) -> SendReport:
    """主动发回原会话，返回各部分的发送结果；``mention`` 时在群里先 @ 发起人。

    ``on_result`` 在每部分发完时立即收到结果，中途取消也不会丢掉已经发出的部分。
    """

    images, _, _ = classify_files(outcome.files)
    message, uploads = outcome_message(outcome)
    if mention and chat.group_id is not None:
        message = [{"type": "at", "data": {"qq": str(chat.user_id)}}, text(" "), *message]
    sent = await _sent_part(on_result, images, send_message(context, chat, message))
    results = await upload_each(context, chat, uploads, on_result=on_result)
    return SendReport(sent, images, results)
