"""媒体读取与解码：阻塞任务限流、有界读文件、来源识别、图片检测与动图拼帧。"""

from __future__ import annotations

import asyncio
import functools
import io
import mimetypes
import re
import stat
import threading
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from core.image_validation import ImageValidationLimits, validate_image_bytes

if TYPE_CHECKING:
    pass

from .models import ImagePayloadInfo, MediaPayloadTooLarge

_MEDIA_BLOCKING_MAX_CONCURRENCY = 2


_media_blocking_limiters: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()


_media_blocking_limiters_lock = threading.Lock()


def _media_blocking_limiter(loop: asyncio.AbstractEventLoop) -> asyncio.Semaphore:
    with _media_blocking_limiters_lock:
        limiter = _media_blocking_limiters.get(loop)
        if limiter is None:
            limiter                        = asyncio.Semaphore(_MEDIA_BLOCKING_MAX_CONCURRENCY)
            _media_blocking_limiters[loop] = limiter
        return limiter


async def run_media_blocking(
    callback: Callable[..., Any],
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """在事件循环外执行媒体磁盘或编解码工作，并施加插件级背压。"""

    loop      = asyncio.get_running_loop()
    limiter   = _media_blocking_limiter(loop)
    operation = functools.partial(callback, *args, **kwargs)
    await limiter.acquire()
    try:
        task = asyncio.ensure_future(asyncio.to_thread(operation))
    except BaseException:
        limiter.release()
        raise
    # 额度跟随线程的真实生命周期：等待方被取消时线程仍在解码，线程结束后才归还额度。
    task.add_done_callback(lambda _done: limiter.release())
    return await asyncio.shield(task)


def read_file_bounded(path: Path, *, max_bytes: int) -> bytes:
    """先读取文件状态，再最多读取到配置上限之外一个字节。"""

    file_stat = path.stat()
    if not stat.S_ISREG(file_stat.st_mode):
        raise FileNotFoundError(f"media source is not a regular file: {path}")
    limit = int(max_bytes)
    if limit > 0 and file_stat.st_size > limit:
        raise MediaPayloadTooLarge(file_stat.st_size, limit)
    read_size = limit + 1 if limit > 0 else -1
    with path.open("rb") as handle:
        payload = handle.read(read_size)
    if limit > 0 and len(payload) > limit:
        raise MediaPayloadTooLarge(len(payload), limit)
    return payload


_GENERIC_SOURCE_LABELS = frozenset(
    {
        "download",
        "image",
        "img",
        "photo",
        "picture",
        "file",
        "attachment",
        "sticker",
        "emoji",
        "cache",
        "blob",
        "view",
    }
)


_SOURCE_QUERY_HINTS = (
    "download?",
    "appid=",
    "fileid=",
    "file_id=",
    "authkey=",
    "rkey=",
    "spec=",
    "cache=",
    "uuid=",
)


def looks_like_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def looks_like_base64_source(value: str) -> bool:
    return value.startswith("base64://")


def looks_like_data_url(value: str) -> bool:
    return value.startswith("data:")


def safe_source_name(value: str) -> str:
    normalized = normalize_source_label(value)
    return normalized[:40]


def looks_like_unusable_source_label(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    lowered = text.lower()
    compact = re.sub(r"[\s_\-]+", "", lowered)
    if any(token in lowered for token in _SOURCE_QUERY_HINTS):
        return True
    if compact in _GENERIC_SOURCE_LABELS:
        return True
    if re.fullmatch(
        r"(?:img|image|photo|picture|screenshot|file|attachment|download)\d{0,8}", compact
    ):
        return True
    if re.fullmatch(r"[0-9a-f]{24,64}", compact):
        return True
    return bool(
        re.fullmatch(r"[a-z0-9_-]{28,64}", lowered) and sum(ch.isdigit() for ch in compact) >= 6
    )


def normalize_source_label(value: str) -> str:
    if not value:
        return ""
    if looks_like_base64_source(value) or looks_like_data_url(value):
        return ""
    if looks_like_url(value):
        parsed = urlparse(value)
        name   = Path(unquote(parsed.path or "")).stem or ""
        if parsed.query and looks_like_unusable_source_label(f"{name}?{parsed.query}"):
            return ""
    else:
        name = Path(value).stem if any(ch in value for ch in ("/", "\\")) else value
    name = re.sub(r"[_\-]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    if looks_like_unusable_source_label(name):
        return ""
    return name


def _guess_mime_type(path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(str(path))
    return mime_type or "image/png"


def image_validation_limits(
    *, max_bytes: int, max_pixels: int, max_frames: int
) -> ImageValidationLimits:
    """从插件媒体预算构造统一的图片校验边界。"""

    return ImageValidationLimits(
        max_bytes  = int(max_bytes),
        max_pixels = int(max_pixels),
        max_frames = int(max_frames),
    )


def inspect_image_payload_details(
    payload: bytes,
    *,
    fallback_suffix: str  = ".png",
    max_bytes: int | None = None,
    max_pixels: int       = 16_000_000,
    max_frames: int       = 120,
) -> ImagePayloadInfo:
    try:
        validated = validate_image_bytes(
            payload,
            limits=image_validation_limits(
                max_bytes  = len(payload) if max_bytes is None else max_bytes,
                max_pixels = max_pixels,
                max_frames = max_frames,
            ),
        )
        suffix = str(validated.extension or fallback_suffix).strip().lower()
        return ImagePayloadInfo(
            mime_type   = _guess_mime_type(Path(f"image{suffix}")),
            suffix      = suffix,
            width       = validated.width,
            height      = validated.height,
            is_animated = validated.frames > 1,
            frame_count = validated.frames,
        )
    except Exception as exc:
        raise ValueError("invalid or undecodable image payload") from exc


def _animation_sample_indexes(frame_count: int) -> list[int]:
    total = max(1, int(frame_count))
    if total <= 1:
        return [0]
    candidates         = [0, total // 2, total - 1]
    indexes: list[int] = []
    for idx in candidates:
        normalized = min(max(0, int(idx)), total - 1)
        if normalized not in indexes:
            indexes.append(normalized)
    return indexes


def render_animation_contact_sheet(payload: bytes) -> tuple[bytes, int]:
    """把已验证的动画字节转换为有界联系表。"""

    from PIL import Image

    gap            = 8
    frame_max_side = 320
    with Image.open(io.BytesIO(payload)) as image:
        frame_total       = int(getattr(image, "n_frames", 1) or 1)
        indexes           = _animation_sample_indexes(frame_total)
        frames: list[Any] = []
        for idx in indexes:
            image.seek(idx)
            frame = image.convert("RGBA")
            frame.thumbnail((frame_max_side, frame_max_side))
            frames.append(frame.copy())

    if not frames:
        raise ValueError("animation has no usable frames")
    if len(frames) == 1:
        buffer = io.BytesIO()
        frames[0].save(buffer, format="PNG")
        return buffer.getvalue(), 1

    total_width  = sum(frame.width for frame in frames) + gap * (len(frames) - 1)
    total_height = max(frame.height for frame in frames)
    sheet        = Image.new("RGBA", (total_width, total_height), (255, 255, 255, 255))
    cursor_x     = 0
    for frame in frames:
        offset_y = (total_height - frame.height) // 2
        sheet.alpha_composite(frame, (cursor_x, offset_y))
        cursor_x += frame.width + gap

    buffer = io.BytesIO()
    sheet.save(buffer, format="PNG")
    return buffer.getvalue(), len(frames)
