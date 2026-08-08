"""集中处理媒体读取、解码预算、渲染缓存和降级描述。"""

from __future__ import annotations

import asyncio
import functools
import io
import json
import math
import mimetypes
import re
import stat
import threading
import time
import weakref
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

import aiohttp

from core.atomic_store import keyed_path_lock
from core.image_validation import ImageValidationLimits, validate_image_bytes
from core.plugin_base import load_json, write_json

if TYPE_CHECKING:
    from ..config.config import MediaConfig
    from ..runtime_state import _ChatRuntime

_MEDIA_BLOCKING_MAX_CONCURRENCY = 2
_media_blocking_limiters: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()
_media_blocking_limiters_lock = threading.Lock()


class MediaPayloadTooLarge(ValueError):
    """本地媒体文件在完整分配内存前已超过字节预算。"""

    def __init__(self, size: int, limit: int) -> None:
        super().__init__(f"media too large: {size} bytes (limit: {limit})")
        self.size = size
        self.limit = limit


def _media_blocking_limiter(loop: asyncio.AbstractEventLoop) -> asyncio.Semaphore:
    with _media_blocking_limiters_lock:
        limiter = _media_blocking_limiters.get(loop)
        if limiter is None:
            limiter = asyncio.Semaphore(_MEDIA_BLOCKING_MAX_CONCURRENCY)
            _media_blocking_limiters[loop] = limiter
        return limiter


async def _run_media_blocking(
    callback: Callable[..., Any],
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """在事件循环外执行媒体磁盘或编解码工作，并施加插件级背压。"""

    loop = asyncio.get_running_loop()
    async with _media_blocking_limiter(loop):
        operation = functools.partial(callback, *args, **kwargs)
        return await asyncio.to_thread(operation)


def _read_file_bounded(path: Path, *, max_bytes: int) -> bytes:
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


_SUPPORTED_MEDIA_TYPES = frozenset({"image", "mface", "face"})
_SUPPORTED_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
_EMOJI_HINT_RE = re.compile(
    r"(表情|emoji|sticker|meme|梗图|mface|贴纸|无语|开心|委屈|猫猫|商城表情|收藏表情)",
    re.IGNORECASE,
)
_GENERIC_MEDIA_HINTS = frozenset({"[图片]", "[动画表情]", "[表情]", "图片", "表情"})
_GENERIC_MEDIA_LABELS = frozenset(
    {
        "图片",
        "表情",
        "表情包",
        "动画表情",
        "聊天表情包",
        "一张图片",
        "一张表情包",
        "一张聊天表情包",
        "图片内容读取失败",
        "图片内容暂时无法识别",
        "无法读取图片内容",
    }
)
_STRUCTURED_MEDIA_TAG_STOPWORDS = frozenset(
    {
        "json",
        "kind",
        "emoji",
        "image",
        "description",
        "summary",
        "label",
        "visibletext",
        "detaileddescription",
        "detaildescription",
        "emotiontags",
        "emotions",
    }
)
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
_MEDIA_ANALYSIS_PROMPT_VERSION = 6
_RENDER_CACHE_MAX_ENTRIES = 1_000
_RENDER_CACHE_MAX_BYTES = 4 * 1024 * 1024
_MEDIA_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=10, sock_read=15)
_ONEBOT_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)


@dataclass(frozen=True)
class ResolvedMedia:
    media_hash: str
    segment_type: str
    source_name: str
    mime_type: str
    cached_path: Path
    width: int = 0
    height: int = 0
    is_animated: bool = False


@dataclass(frozen=True)
class RenderedMedia:
    media_hash: str
    kind: str
    description: str
    emotion_tags: tuple[str, ...]
    marker: str
    cached_path: Path | None = None
    face_id: str = ""
    # 二次分析得到的梗背景/语境说明；不知道就为空字符串，
    # 与 description 分开存放，便于将来重生成 marker。
    cultural_hint: str = ""


@dataclass(frozen=True)
class PreparedMediaForLLM:
    payload: bytes
    mime_type: str
    transcoded: bool
    source_mime_type: str
    is_animated: bool = False
    frame_strategy: str = "original"
    frame_count: int = 1


@dataclass(frozen=True)
class MediaAnalysisDraft:
    kind: str
    description: str
    visible_text: str
    emotion_tags: tuple[str, ...]
    raw_output: str = ""
    parsed_json: bool = False
    cultural_hint: str = ""


def _media_cfg(runtime: _ChatRuntime) -> MediaConfig:
    return runtime.cfg.media


def _media_log(
    context, runtime, *, step: str, fields: dict[str, Any] | None = None, level: str = "info"
) -> None:
    from ..logging_utils import sanitize_log_fields

    if not runtime.cfg.debug.log_steps:
        return
    logger = getattr(context, "logger", None)
    if logger is None:
        return

    payload: dict[str, Any] = {"step": str(step)}
    if fields:
        payload.update(sanitize_log_fields(fields))
    try:
        log_fn = getattr(logger, level, None) or logger.info
        log_fn("xiaoqing_chat media=%s", json.dumps(payload, ensure_ascii=False))
    except Exception:
        return


def _figures_inbox_dir(context) -> Path:
    return Path(context.data_dir) / "media" / "inbox"


def _render_cache_path(data_dir: Path) -> Path:
    return data_dir / "media" / "render_cache.json"


def _load_render_cache(
    data_dir: Path,
    *,
    max_bytes: int = _RENDER_CACHE_MAX_BYTES,
) -> dict[str, Any]:
    cache_path = _render_cache_path(data_dir)
    byte_limit = max(0, int(max_bytes))
    if byte_limit == 0:
        return {"items": {}}
    for candidate in (cache_path, cache_path.with_name(f"{cache_path.name}.bak")):
        try:
            if candidate.stat().st_size > byte_limit:
                return {"items": {}}
        except FileNotFoundError:
            continue
        except OSError:
            return {"items": {}}
    loaded: object = load_json(cache_path, default={"items": {}})
    if not isinstance(loaded, dict):
        return {"items": {}}
    payload = loaded
    items = payload.get("items")
    if not isinstance(items, dict):
        payload["items"] = {}
    return payload


def _save_render_cache(data_dir: Path, cache: dict[str, Any]) -> None:
    write_json(_render_cache_path(data_dir), cache)


def _render_cache_lock(data_dir: Path) -> AbstractContextManager[None]:
    return keyed_path_lock(_render_cache_path(data_dir))


def _render_cache_entry_timestamp(item: tuple[str, Any]) -> tuple[float, str]:
    key, payload = item
    try:
        timestamp = float(payload.get("updated_at", 0.0)) if isinstance(payload, dict) else 0.0
    except (TypeError, ValueError):
        timestamp = 0.0
    if not math.isfinite(timestamp):
        timestamp = 0.0
    return timestamp, key


def _serialized_render_cache_size(cache: dict[str, Any]) -> int:
    return len(
        json.dumps(
            cache,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ).encode("utf-8")
    )


def _prune_render_cache(
    cache: dict[str, Any],
    *,
    max_entries: int,
    max_bytes: int,
) -> dict[str, Any]:
    items = cache.get("items")
    normalized_items = dict(items) if isinstance(items, dict) else {}
    bounded_cache: dict[str, Any] = {"items": normalized_items}
    entry_limit = max(0, int(max_entries))
    byte_limit = max(0, int(max_bytes))
    oldest_first = sorted(normalized_items.items(), key=_render_cache_entry_timestamp)

    while len(normalized_items) > entry_limit and oldest_first:
        key, _payload = oldest_first.pop(0)
        normalized_items.pop(key, None)
    while normalized_items and (
        byte_limit == 0 or _serialized_render_cache_size(bounded_cache) > byte_limit
    ):
        key, _payload = oldest_first.pop(0)
        normalized_items.pop(key, None)
    return bounded_cache


def write_render_cache_entry(
    data_dir: Path,
    resolved: ResolvedMedia,
    rendered: RenderedMedia,
    *,
    source: str,
    quality: str,
    prompt_version: int | None = None,
    max_entries: int = _RENDER_CACHE_MAX_ENTRIES,
    max_bytes: int = _RENDER_CACHE_MAX_BYTES,
) -> None:
    normalized_source = str(source or "").strip() or "fallback"
    if prompt_version is None:
        prompt_version = _MEDIA_ANALYSIS_PROMPT_VERSION if normalized_source == "llm" else 0
    cache_payload = {
        "kind": rendered.kind,
        "description": rendered.description,
        "emotion_tags": list(rendered.emotion_tags),
        "marker": rendered.marker,
        "analysis_source": normalized_source,
        "analysis_quality": str(quality or "").strip(),
        "analysis_prompt_version": int(prompt_version or 0),
        "cultural_hint": str(getattr(rendered, "cultural_hint", "") or "").strip(),
        "updated_at": time.time(),
    }
    with _render_cache_lock(data_dir):
        cache = _load_render_cache(data_dir, max_bytes=max_bytes)
        items = cache.setdefault("items", {})
        items[resolved.media_hash] = cache_payload
        bounded_cache = _prune_render_cache(
            cache,
            max_entries=max_entries,
            max_bytes=max_bytes,
        )
        _save_render_cache(data_dir, bounded_cache)


def _parse_file_uri(value: str) -> Path | None:
    parsed = urlparse(value)
    if parsed.scheme != "file":
        return None
    raw_netloc = unquote(parsed.netloc or "")
    raw_path = unquote(parsed.path or "")
    if raw_netloc and raw_path:
        if re.fullmatch(r"[A-Za-z]:", raw_netloc):
            raw_path = f"{raw_netloc}{raw_path}"
        else:
            raw_path = f"//{raw_netloc}{raw_path}"
    elif raw_netloc and not raw_path:
        raw_path = raw_netloc
    if not raw_path:
        return None
    if raw_path.startswith("/") and len(raw_path) > 2 and raw_path[2] == ":":
        raw_path = raw_path[1:]
    return Path(raw_path)


def _looks_like_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _looks_like_base64_source(value: str) -> bool:
    return value.startswith("base64://")


def _looks_like_data_url(value: str) -> bool:
    return value.startswith("data:")


def _safe_source_name(value: str) -> str:
    normalized = _normalize_source_label(value)
    return normalized[:40]


def _looks_like_unusable_source_label(value: str) -> bool:
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


def _normalize_source_label(value: str) -> str:
    if not value:
        return ""
    if _looks_like_base64_source(value) or _looks_like_data_url(value):
        return ""
    if _looks_like_url(value):
        parsed = urlparse(value)
        name = Path(unquote(parsed.path or "")).stem or ""
        if parsed.query and _looks_like_unusable_source_label(f"{name}?{parsed.query}"):
            return ""
    else:
        name = Path(value).stem if any(ch in value for ch in ("/", "\\")) else value
    name = re.sub(r"[_\-]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    if _looks_like_unusable_source_label(name):
        return ""
    return name


def _guess_mime_type(path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(str(path))
    return mime_type or "image/png"


def _image_validation_limits(
    *, max_bytes: int, max_pixels: int, max_frames: int
) -> ImageValidationLimits:
    """从插件媒体预算构造统一的图片校验边界。"""

    return ImageValidationLimits(
        max_bytes=int(max_bytes),
        max_pixels=int(max_pixels),
        max_frames=int(max_frames),
    )


@dataclass(frozen=True, slots=True)
class ImagePayloadInfo:
    mime_type: str
    suffix: str
    width: int
    height: int
    is_animated: bool
    frame_count: int


def _inspect_image_payload_details(
    payload: bytes,
    *,
    fallback_suffix: str = ".png",
    max_bytes: int | None = None,
    max_pixels: int = 16_000_000,
    max_frames: int = 120,
) -> ImagePayloadInfo:
    try:
        validated = validate_image_bytes(
            payload,
            limits=_image_validation_limits(
                max_bytes=len(payload) if max_bytes is None else max_bytes,
                max_pixels=max_pixels,
                max_frames=max_frames,
            ),
        )
        suffix = str(validated.extension or fallback_suffix).strip().lower()
        return ImagePayloadInfo(
            mime_type=_guess_mime_type(Path(f"image{suffix}")),
            suffix=suffix,
            width=validated.width,
            height=validated.height,
            is_animated=validated.frames > 1,
            frame_count=validated.frames,
        )
    except Exception as exc:
        raise ValueError("invalid or undecodable image payload") from exc


def _animation_sample_indexes(frame_count: int) -> list[int]:
    total = max(1, int(frame_count))
    if total <= 1:
        return [0]
    candidates = [0, total // 2, total - 1]
    indexes: list[int] = []
    for idx in candidates:
        normalized = min(max(0, int(idx)), total - 1)
        if normalized not in indexes:
            indexes.append(normalized)
    return indexes


def _render_animation_contact_sheet(payload: bytes) -> tuple[bytes, int]:
    """把已验证的动画字节转换为有界联系表。"""

    from PIL import Image

    gap = 8
    frame_max_side = 320
    with Image.open(io.BytesIO(payload)) as image:
        frame_total = int(getattr(image, "n_frames", 1) or 1)
        indexes = _animation_sample_indexes(frame_total)
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

    total_width = sum(frame.width for frame in frames) + gap * (len(frames) - 1)
    total_height = max(frame.height for frame in frames)
    sheet = Image.new("RGBA", (total_width, total_height), (255, 255, 255, 255))
    cursor_x = 0
    for frame in frames:
        offset_y = (total_height - frame.height) // 2
        sheet.alpha_composite(frame, (cursor_x, offset_y))
        cursor_x += frame.width + gap

    buffer = io.BytesIO()
    sheet.save(buffer, format="PNG")
    return buffer.getvalue(), len(frames)


def _normalize_emotion_tags(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        candidates = re.split(r"[,，/\s]+", value)
    elif isinstance(value, list):
        candidates = [str(item) for item in value]
    else:
        candidates = []

    tags: list[str] = []
    for item in candidates:
        if _looks_like_structured_media_text(item):
            continue
        cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "", item or "").strip()
        lowered = cleaned.lower()
        if not cleaned:
            continue
        if lowered in _STRUCTURED_MEDIA_TAG_STOPWORDS:
            continue
        if cleaned.startswith("think") or "用户" in cleaned or "输入数据" in cleaned:
            continue
        if cleaned not in tags:
            tags.append(cleaned[:12])
    return tuple(tags[:4])


def _segment_summary_hint(segment: dict[str, Any]) -> str:
    data = segment.get("data", {}) or {}
    generic = ""
    for key in ("summary", "text", "name", "key", "emoji_id"):
        value = str(data.get(key, "") or "").strip()
        if not value:
            continue
        if value in _GENERIC_MEDIA_HINTS:
            if not generic:
                generic = value
            continue
        return value
    return generic


def _segment_prefers_emoji(segment: dict[str, Any]) -> bool:
    segment_type = str(segment.get("type", "") or "")
    if segment_type == "mface":
        return True

    data = segment.get("data", {}) or {}
    for key in ("emoji_id", "emoji_package_id", "key"):
        value = data.get(key)
        if value not in (None, ""):
            return True

    return bool(_EMOJI_HINT_RE.search(_segment_summary_hint(segment)))


def _fallback_kind(source_name: str, *, width: int, height: int, segment_type: str) -> str:
    if segment_type == "mface":
        return "emoji"
    if _EMOJI_HINT_RE.search(source_name or ""):
        return "emoji"
    if (
        not source_name
        and width
        and height
        and max(width, height) <= 512
        and abs(width - height) <= 96
    ):
        return "emoji"
    return "image"


def _build_marker(kind: str, description: str, emotion_tags: tuple[str, ...]) -> str:
    if kind == "emoji":
        label = "，".join(emotion_tags[:2]).strip() or description.strip() or "一张表情包"
        return f"[表情包：{label}]"
    desc = description.strip() or "一张图片"
    return f"[图片：{desc}]"


def _build_context_marker(rendered: RenderedMedia) -> str:
    marker = rendered.marker.strip()
    cultural_hint = str(getattr(rendered, "cultural_hint", "") or "").strip()
    if rendered.kind != "emoji" or not marker.startswith("[表情包："):
        return _append_cultural_hint(marker, cultural_hint)

    description = rendered.description.strip()
    if not description or _is_generic_media_label(description):
        return _append_cultural_hint(marker, cultural_hint)

    label = "，".join(rendered.emotion_tags[:2]).strip()
    if not label:
        return _append_cultural_hint(marker, cultural_hint)

    clean_desc, visible_text = split_emoji_visible_text(description)
    if visible_text:
        return _append_cultural_hint(f"[表情包：{label}；写着“{visible_text}”]", cultural_hint)

    normalized_label = _normalize_media_label(label)
    normalized_description = _normalize_media_label(clean_desc)
    if not normalized_description or normalized_description == normalized_label:
        return _append_cultural_hint(marker, cultural_hint)

    return _append_cultural_hint(f"[表情包：{label}；内容：{clean_desc}]", cultural_hint)


def _append_cultural_hint(marker: str, cultural_hint: str) -> str:
    base = str(marker or "").strip()
    hint = str(cultural_hint or "").strip()
    if not base or not hint:
        return base
    if not (base.startswith("[") and base.endswith("]")):
        return base
    inner = base[1:-1].strip()
    if not inner:
        return base
    if "梗背景" in inner:
        return base
    return f"[{inner}；梗背景：{hint}]"


def _normalize_media_label(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    text = re.sub(r"^(QQ表情|表情包|图片)\s*[：:]", "", text)
    return re.sub(r"\s+", "", text)


def _is_generic_media_label(value: str) -> bool:
    normalized = _normalize_media_label(value)
    if not normalized:
        return True
    return normalized in _GENERIC_MEDIA_LABELS


_EMOJI_VISIBLE_TEXT_RE = re.compile(r"^(?:(.+?)，)?文字内容是“(.+?)”$")


def split_emoji_visible_text(description: str) -> tuple[str, str]:
    """把 ``描述，文字内容是“X”`` 拆成（清洁描述，可见文字）。

    不含该后缀时返回原描述和空的可见文字。
    """
    text = str(description or "").strip()
    if not text:
        return "", ""
    match = _EMOJI_VISIBLE_TEXT_RE.match(text)
    if not match:
        return text, ""
    clean_desc = (match.group(1) or "").strip()
    visible_text = (match.group(2) or "").strip()
    return clean_desc, visible_text


def _looks_like_structured_media_text(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if "<think" in lowered or "</think>" in lowered:
        return True
    if "```" in text:
        return True
    if lowered.startswith("json") and "{" in text:
        return True
    if any(
        token in text
        for token in (
            '"kind"',
            '"description"',
            '"emotion_tags"',
            '"detailed_description"',
            '"visible_text"',
            '{"detailed_description"',
            "输入数据：",
        )
    ):
        return True
    return any(
        phrase in text
        for phrase in (
            "用户给的例子",
            "现在重新分析",
            "需要提取信息",
            "只输出 JSON",
            "只输出JSON",
        )
    )


def _can_use_raw_media_description(value: str) -> bool:
    text = str(value or "").strip().strip("`")
    if not text:
        return False
    if text.startswith("{"):
        return False
    return not _looks_like_structured_media_text(text)


def _is_low_quality_rendered_media(
    rendered: RenderedMedia,
    *,
    summary_hint: str,
    resolved: ResolvedMedia,
) -> bool:
    if _looks_like_structured_media_text(rendered.description):
        return True
    if _looks_like_unusable_source_label(rendered.description):
        return True
    if _looks_like_structured_media_text(rendered.marker):
        return True
    if any(_looks_like_structured_media_text(tag) for tag in rendered.emotion_tags):
        return True
    if _is_generic_media_label(rendered.description):
        return True
    if (
        rendered.kind == "emoji"
        and not rendered.emotion_tags
        and _is_generic_media_label(rendered.marker)
    ):
        return True
    summary_label = _normalize_media_label(summary_hint)
    if (
        summary_label
        and summary_label == _normalize_media_label(rendered.description)
        and _is_generic_media_label(summary_hint)
    ):
        return True
    source_label = _normalize_media_label(resolved.source_name)
    return bool(
        source_label
        and source_label == _normalize_media_label(rendered.description)
        and _is_generic_media_label(resolved.source_name)
    )


def _build_fallback_render(
    resolved: ResolvedMedia,
    *,
    summary_hint: str = "",
    prefer_emoji: bool = False,
) -> RenderedMedia:
    if prefer_emoji:
        kind = "emoji"
    else:
        kind = _fallback_kind(
            summary_hint or resolved.source_name,
            width=resolved.width,
            height=resolved.height,
            segment_type=resolved.segment_type,
        )
    label = _safe_source_name(summary_hint or resolved.source_name)
    if kind == "emoji":
        emotion_tags = _normalize_emotion_tags(label)
        description = label or "一张聊天表情包"
    else:
        emotion_tags = ()
        # 文件名和传输摘要都不是视觉证据。视觉路由失败时明确标记未理解像素，
        # 避免回复模型根据来源标签猜测图片内容。
        description = "图片内容暂时无法识别"
    marker = _build_marker(kind, description, emotion_tags)
    return RenderedMedia(
        media_hash=resolved.media_hash,
        kind=kind,
        description=description,
        emotion_tags=emotion_tags,
        marker=marker,
        cached_path=resolved.cached_path,
    )


def _rendered_media_from_cache(cached: dict[str, Any], *, resolved: ResolvedMedia) -> RenderedMedia:
    kind = str(cached.get("kind", "") or "").strip() or "image"
    description = str(cached.get("description", "") or "").strip()
    emotion_tags = _normalize_emotion_tags(cached.get("emotion_tags"))
    marker = str(cached.get("marker", "") or "").strip() or _build_marker(
        kind, description, emotion_tags
    )
    face_id = str(cached.get("face_id", "") or "").strip()
    cultural_hint = str(cached.get("cultural_hint", "") or "").strip()
    return RenderedMedia(
        media_hash=resolved.media_hash,
        kind=kind,
        description=description,
        emotion_tags=emotion_tags,
        marker=marker,
        cached_path=resolved.cached_path,
        face_id=face_id,
        cultural_hint=cultural_hint,
    )


def _same_rendered_media(left: RenderedMedia, right: RenderedMedia) -> bool:
    return (
        left.kind == right.kind
        and left.face_id == right.face_id
        and left.description == right.description
        and tuple(left.emotion_tags) == tuple(right.emotion_tags)
        and left.marker == right.marker
    )
