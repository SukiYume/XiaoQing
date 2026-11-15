"""其他插件刚在会话里推送的内容：完整文本和配图，留给她主动开口时看。

聊天记录里长推送只记开头，够回复时知道推送过什么；她自己想就一条推送说点什么时，需要看到全文
和图。这里每个会话留最近几条；每次写入时清掉一天半以前的。配图只记本地文件路径，第一次要用时
看一遍图、把看到的内容写成一段文字存下来，之后起草和自检用的都是这段文字。
"""

from __future__ import annotations

import asyncio
import base64
import io
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from core.image_validation import ImageValidationError, ImageValidationLimits, validate_image_bytes

from ..llm.gateway import LLMError, chat_completions
from ..store_base import StoreBase, coerce_finite_float

_FILE          = ("life", "push_log.json")
MAX_PER_CHAT   = 6
KEEP_SECONDS   = 36 * 3600.0
MAX_TEXT_CHARS = 3000
MAX_IMAGES     = 2
IMAGE_LIMITS   = ImageValidationLimits(
    max_bytes=12 * 1024 * 1024, max_pixels=80_000_000, max_frames=1
)
_IMAGE_MAX_SIDE   = 1280
_NOTE_CHARS       = 400
_NOTE_INSTRUCTION = (
    "把这张图里看得到的东西如实写下来：主体是什么、在画面哪里、什么颜色和形状、前景和背景各有什么，"
    "图上有文字就照抄。只写看得到的，不解释、不评价、不猜拍摄信息。200 字以内。"
)


@dataclass(frozen=True)
class Push:
    ts      : float
    source  : str
    local_id: str
    text    : str
    images  : tuple[str, ...] = ()
    # 配图里看得到的内容；空表示还没看过（或者没看成）。
    image_note: str = ""


def image_paths(message: Any) -> tuple[str, ...]:
    """推送消息段里的本地图片路径；网络图片和 base64 不取。"""

    found: list[str] = []
    for segment in message if isinstance(message, list) else ():
        if not isinstance(segment, dict) or segment.get("type") != "image":
            continue
        data = segment.get("data")
        file = str(data.get("file") or "") if isinstance(data, dict) else ""
        if not file.startswith("file:"):
            continue
        parsed = urlparse(file)
        path   = unquote(parsed.path)
        # Windows 的 file:///C:/x 解析出来是 /C:/x
        if len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        if path:
            found.append(path)
    return tuple(found[:MAX_IMAGES])


def _parse(item: Any) -> Push | None:
    if not isinstance(item, dict):
        return None
    text = str(item.get("text") or "")
    ts = coerce_finite_float(item.get("ts"), default=0.0, minimum=0.0)
    if not text or ts <= 0:
        return None
    images = item.get("images")
    return Push(
        ts       = ts,
        source   = str(item.get("source") or ""),
        local_id = str(item.get("local_id") or ""),
        text     = text[:MAX_TEXT_CHARS],
        images   = tuple(str(p) for p in images if isinstance(p, str))[:MAX_IMAGES]
        if isinstance(images, list)
        else (),
        image_note=str(item.get("image_note") or "")[:_NOTE_CHARS],
    )


class PushLogStore(StoreBase):
    def __init__(self) -> None:
        super().__init__()
        self._lock                               = threading.Lock()
        self._rows: dict[str, list[Push]] | None = None

    def _drop_cache(self) -> None:
        with self._lock:
            self._rows = None

    def _load_locked(self) -> dict[str, list[Push]]:
        if self._rows is None:
            raw = self._load_json_from_path_parts(*_FILE, default={})
            self._rows = {
                str(chat_id): [push for push in map(_parse, items) if push is not None]
                for chat_id, items in (raw if isinstance(raw, dict) else {}).items()
                if isinstance(items, list)
            }
        return self._rows

    def _save_locked(self) -> None:
        self._save_json_to_path_parts(
            *_FILE,
            data={
                chat_id: [asdict(push) for push in pushes]
                for chat_id, pushes in (self._rows or {}).items()
                if pushes
            },
        )

    def append(self, chat_id: str, push: Push) -> None:
        with self._lock:
            rows  = self._load_locked()
            fresh = [p for p in rows.get(chat_id, []) if push.ts - p.ts <= KEEP_SECONDS]
            rows[chat_id] = [*fresh, replace(push, text=push.text[:MAX_TEXT_CHARS])][-MAX_PER_CHAT:]
            self._save_locked()

    def since(self, chat_id: str, after: float) -> list[Push]:
        with self._lock:
            return [p for p in self._load_locked().get(chat_id, []) if p.ts > after]

    def set_image_note(self, chat_id: str, push: Push, note: str) -> None:
        with self._lock:
            rows          = self._load_locked()
            rows[chat_id] = [
                replace(p, image_note=note[:_NOTE_CHARS]) if p == push else p
                for p in rows.get(chat_id, [])
            ]
            self._save_locked()


def _image_data_url(path: str) -> str:
    """读本地图片、缩到模型看得清又不太大的尺寸；读不了、不是图片或超出上限时抛 ``ValueError``。"""

    from PIL import Image

    try:
        with Path(path).open("rb") as stream:
            payload = stream.read(IMAGE_LIMITS.max_bytes + 1)
        validate_image_bytes(payload, limits=IMAGE_LIMITS, allow_animation=False)
        with Image.open(io.BytesIO(payload)) as image:
            frame = image.convert("RGB")
            frame.thumbnail((_IMAGE_MAX_SIDE, _IMAGE_MAX_SIDE))
            buffer = io.BytesIO()
            frame.save(buffer, format="JPEG", quality=85)
    except (OSError, ImageValidationError) as exc:
        raise ValueError("image unreadable") from exc
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


async def look_at_images(push: Push, *, secrets: dict[str, Any], llm: Any) -> str:
    """看一遍推送的配图，返回看到的内容；没有图、读不了或模型不可用时是空字符串。"""

    parts: list[dict[str, Any]] = [{"type": "text", "text": _NOTE_INSTRUCTION}]
    for path in push.images:
        try:
            url = await asyncio.to_thread(_image_data_url, path)
        except ValueError:
            continue
        parts.append({"type": "image_url", "image_url": {"url": url}})
    if len(parts) == 1:
        return ""
    kwargs = {**llm.as_kwargs(), "required_modalities": ("text", "image")}
    try:
        note = await chat_completions(
            route="vision", secrets=secrets, messages=[{"role": "user", "content": parts}], **kwargs
        )
    except LLMError:
        return ""
    return " ".join(str(note or "").split())[:_NOTE_CHARS]
