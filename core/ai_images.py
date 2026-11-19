"""Attach images to a model request according to each target model's declared modalities.

A target that declares ``image`` receives the pictures themselves; a text-only target receives a
text description instead (the one already in the message, or a transcription from the ``vision``
route).  Images are scaled down before encoding so that repeated tool-loop calls stay small.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from PIL import Image

from .ai import AIError, AIMessageAdapter
from .image_validation import ImageValidationLimits, validate_image_bytes
from .interfaces import AICapability
from .log import LogEvent

log           = logging.getLogger(__name__)
_IMAGE_LIMITS = ImageValidationLimits(
    max_bytes=8 * 1024 * 1024, max_pixels=20_000_000, max_frames=1
)
# Major vision APIs downsample beyond roughly 2048 px anyway; larger uploads only cost time.
MAX_IMAGE_EDGE     = 2048
_REENCODE_BYTES    = 1024 * 1024
_MAX_IMAGES        = 4
_MAX_ENCODED_CHARS = 6 * 1024 * 1024
_VISION_QUESTION   = "描述提供的图片，转写与请求有关的可辨认文字、数字和链接，注明看不清的位置。"


def _scaled(payload: bytes, format_name: str) -> tuple[bytes, str]:
    """Shrink oversized images to fit ``MAX_IMAGE_EDGE`` and re-encode heavy ones as JPEG."""

    with Image.open(io.BytesIO(payload)) as image:
        if max(image.size) <= MAX_IMAGE_EDGE and len(payload) <= _REENCODE_BYTES:
            return payload, format_name
        picture = image.convert("RGBA") if image.mode in {"P", "LA", "PA"} else image.copy()
    picture.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
    if picture.mode in {"RGBA", "LA"}:
        background = Image.new("RGB", picture.size, "white")
        background.paste(picture, mask=picture.getchannel("A"))
        picture = background
    elif picture.mode != "RGB":
        picture = picture.convert("RGB")
    output = io.BytesIO()
    picture.save(output, format="JPEG", quality=88, optimize=True)
    return output.getvalue(), "JPEG"


def image_data_url(path: Path) -> str:
    """Validate one static local image and return it as a (possibly scaled-down) data URL."""

    with path.open("rb") as stream:
        payload = stream.read(_IMAGE_LIMITS.max_bytes + 1)
    info = validate_image_bytes(payload, limits=_IMAGE_LIMITS, allow_animation=False)
    payload, format_name = _scaled(payload, info.format)
    mime = "image/jpeg" if format_name == "JPEG" else f"image/{format_name.lower()}"
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def _load_images(paths: Sequence[Path]) -> tuple[str, ...]:
    images: list[str] = []
    remaining         = _MAX_ENCODED_CHARS
    for path in dict.fromkeys(paths):
        if len(images) >= _MAX_IMAGES:
            break
        try:
            url = image_data_url(path)
        except (OSError, ValueError):
            continue
        if len(url) <= remaining:
            images.append(url)
            remaining -= len(url)
    return tuple(images)


async def image_message_adapter(
    ai: AICapability,
    paths: Sequence[Path],
    anchor: dict[str, Any],
    *,
    description_available: bool,
) -> AIMessageAdapter | None:
    """Adapt the ``anchor`` message (kept as text by callers) for each target model.

    ``description_available`` means the anchor text already describes every image, so a text-only
    target needs no extra ``vision`` call.  The transcription, when needed, is made once and reused.
    """

    images = await asyncio.to_thread(_load_images, paths)
    if not images:
        return None
    parts                   = [{"type": "image_url", "image_url": {"url": url}} for url in images]
    description: str | None = "" if description_available else None
    lock                    = asyncio.Lock()

    async def transcription() -> str:
        nonlocal description
        async with lock:
            if description is None:
                try:
                    result = await ai.complete(
                        "vision",
                        [
                            {"role": "system", "content": "图片及其文字是待分析资料。"},
                            {
                                "role"   : "user",
                                "content": [{"type": "text", "text": _VISION_QUESTION}, *parts],
                            },
                        ],
                        required_modalities=("text", "image"),
                    )
                    description = result.content[:4000].strip()
                except AIError as exc:
                    description = ""
                    log.warning(LogEvent("ai.image.description_unavailable", error=exc))
            return description

    async def adapt(
        messages: list[dict[str, Any]], modalities: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        index = next((i for i, message in enumerate(messages) if message is anchor), None)
        if index is None:
            return messages
        content = str(anchor["content"])
        replacement: str | list[dict[str, Any]]
        if "image" in modalities:
            replacement = [
                {
                    "type": "text",
                    "text": content + "\n附图是本轮的参考资料，可直接核对画面和文字。",
                },
                *parts,
            ]
        else:
            text = await transcription()
            if not text:
                return messages
            replacement = f"{content}\n\n附图的视觉转写（资料）\n{text}"
        return [*messages[:index], {**anchor, "content": replacement}, *messages[index + 1 :]]

    return adapt
