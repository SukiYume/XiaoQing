"""媒体处理的数据类型与支持范围常量。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class MediaPayloadTooLarge(ValueError):
    """本地媒体文件在完整分配内存前已超过字节预算。"""

    def __init__(self, size: int, limit: int) -> None:
        super().__init__(f"media too large: {size} bytes (limit: {limit})")
        self.size  = size
        self.limit = limit


SUPPORTED_MEDIA_TYPES = frozenset({"image", "mface", "face"})


SUPPORTED_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})


@dataclass(frozen=True)
class ResolvedMedia:
    media_hash: str
    segment_type: str
    source_name: str
    mime_type: str
    cached_path: Path
    width: int        = 0
    height: int       = 0
    is_animated: bool = False


@dataclass(frozen=True)
class RenderedMedia:
    media_hash: str
    kind: str
    description: str
    emotion_tags: tuple[str, ...]
    marker: str
    cached_path: Path | None = None
    face_id: str             = ""
    # 二次分析得到的梗背景/语境说明；不知道就为空字符串，
    # 与 description 分开存放，便于将来重生成 marker。
    cultural_hint: str = ""


@dataclass(frozen=True)
class PreparedMediaForLLM:
    payload: bytes
    mime_type: str
    transcoded: bool
    source_mime_type: str
    is_animated: bool   = False
    frame_strategy: str = "original"
    frame_count: int    = 1


@dataclass(frozen=True)
class MediaAnalysisDraft:
    kind: str
    description: str
    visible_text: str
    emotion_tags: tuple[str, ...]
    raw_output: str    = ""
    parsed_json: bool  = False
    cultural_hint: str = ""


@dataclass(frozen=True, slots=True)
class ImagePayloadInfo:
    mime_type: str
    suffix: str
    width: int
    height: int
    is_animated: bool
    frame_count: int
