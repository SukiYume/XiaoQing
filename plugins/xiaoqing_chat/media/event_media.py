from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import mimetypes
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, unquote_to_bytes, urlparse

import aiohttp
from core.plugin_base import ensure_dir, load_json, write_json

from ..helper_utils import _iter_message_segments
from ..llm.llm_client import LLMError, chat_completions
from .qq_face import describe_face_segment
from .qq_face_catalog import record_face_observation

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
_MEDIA_ANALYSIS_PROMPT_VERSION = 3
_RENDER_CACHE_LOCKS: dict[str, threading.RLock] = {}
_RENDER_CACHE_LOCKS_GUARD = threading.Lock()
_MEDIA_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=10, sock_read=15)
_ONEBOT_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
_DOWNLOAD_CHUNK_SIZE = 64 * 1024


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


def _media_cfg(runtime) -> Any:
    return getattr(getattr(runtime, "cfg", None), "media", None)


def _media_cfg_value(runtime, field: str, default: Any) -> Any:
    cfg = _media_cfg(runtime)
    if cfg is None:
        return default
    return getattr(cfg, field, default)


def _media_log(context, runtime, *, step: str, fields: dict[str, Any] | None = None, level: str = "info") -> None:
    debug_cfg = getattr(getattr(runtime, "cfg", runtime), "debug", None)
    if debug_cfg is not None and not bool(getattr(debug_cfg, "log_steps", True)):
        return
    logger = getattr(context, "logger", None)
    if logger is None:
        return

    payload: dict[str, Any] = {"step": str(step)}
    if fields:
        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, Path):
                payload[str(key)] = str(value)
                continue
            text = str(value)
            payload[str(key)] = text if len(text) <= 240 else text[:239] + "…"
    try:
        log_fn = getattr(logger, level, None) or logger.info
        log_fn("xiaoqing_chat media=%s", json.dumps(payload, ensure_ascii=False))
    except Exception:
        return


def _media_root(data_dir: Path) -> Path:
    return data_dir / "media"


def _figures_root(context) -> Path:
    return Path(context.plugin_dir) / "figures"


def _figures_inbox_dir(context) -> Path:
    return _figures_root(context) / "inbox"


def _render_cache_path(data_dir: Path) -> Path:
    return _media_root(data_dir) / "render_cache.json"


def _load_render_cache(data_dir: Path) -> dict[str, Any]:
    payload = load_json(_render_cache_path(data_dir), default={"items": {}})
    items = payload.get("items")
    if not isinstance(items, dict):
        payload["items"] = {}
    return payload


def _save_render_cache(data_dir: Path, cache: dict[str, Any]) -> None:
    write_json(_render_cache_path(data_dir), cache)


def _render_cache_lock(data_dir: Path) -> threading.RLock:
    key = str(_render_cache_path(data_dir).resolve())
    with _RENDER_CACHE_LOCKS_GUARD:
        lock = _RENDER_CACHE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _RENDER_CACHE_LOCKS[key] = lock
        return lock


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
    return value.startswith("http://") or value.startswith("https://")


def _looks_like_base64_source(value: str) -> bool:
    return value.startswith("base64://")


def _looks_like_data_url(value: str) -> bool:
    return value.startswith("data:")


def _safe_source_name(value: str) -> str:
    normalized = _normalize_source_label(value)
    return normalized[:40]


def _normalize_source_label(value: str) -> str:
    if not value:
        return ""
    if _looks_like_base64_source(value) or _looks_like_data_url(value):
        return ""
    name = Path(value).stem if any(ch in value for ch in ("/", "\\")) else value
    name = re.sub(r"[_\-]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _guess_mime_type(path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(str(path))
    return mime_type or "image/png"


def _suffix_from_format(format_name: str) -> str:
    normalized = str(format_name or "").strip().upper()
    if not normalized:
        return ".png"
    mapping = {
        "JPEG": ".jpg",
        "JPG": ".jpg",
        "PNG": ".png",
        "GIF": ".gif",
        "WEBP": ".webp",
        "BMP": ".bmp",
    }
    return mapping.get(normalized, f".{normalized.lower()}")


def _inspect_image_payload(payload: bytes, *, fallback_suffix: str = ".png") -> tuple[str, str, int, int, bool]:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(payload)) as image:
            format_name = str(getattr(image, "format", "") or "").upper()
            mime_type = ""
            if format_name:
                mime_type = str(Image.MIME.get(format_name, "") or "").strip()
            width, height = image.size
            is_animated = bool(getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) > 1)
            suffix = _suffix_from_format(format_name) if format_name else fallback_suffix
            return mime_type or _guess_mime_type(Path(f"image{suffix}")), suffix or fallback_suffix, width, height, is_animated
    except Exception:
        mime_type = _guess_mime_type(Path(f"image{fallback_suffix or '.png'}"))
        return mime_type, fallback_suffix or ".png", 0, 0, False


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


def _clean_media_hint(value: Any) -> str:
    return str(value or "").strip()


def _segment_summary_hint(segment: dict[str, Any]) -> str:
    data = segment.get("data", {}) or {}
    generic = ""
    for key in ("summary", "text", "name", "key", "emoji_id"):
        value = _clean_media_hint(data.get(key))
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
    if not source_name and width and height and max(width, height) <= 512 and abs(width - height) <= 96:
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
    if rendered.kind != "emoji" or not marker.startswith("[表情包："):
        return marker

    description = rendered.description.strip()
    if not description or _is_generic_media_label(description):
        return marker

    label = "，".join(rendered.emotion_tags[:2]).strip()
    if not label:
        return marker

    normalized_label = _normalize_media_label(label)
    normalized_description = _normalize_media_label(description)
    if not normalized_description or normalized_description == normalized_label:
        return marker

    return f"[表情包：{label}；内容：{description}]"


def _normalize_media_label(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    text = re.sub(r"^(QQ表情|表情包|图片)\s*[：:]", "", text)
    text = re.sub(r"\s+", "", text)
    return text


def _is_generic_media_label(value: str) -> bool:
    normalized = _normalize_media_label(value)
    if not normalized:
        return True
    return normalized in _GENERIC_MEDIA_LABELS


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
    if any(
        phrase in text
        for phrase in (
            "用户给的例子",
            "现在重新分析",
            "需要提取信息",
            "只输出 JSON",
            "只输出JSON",
        )
    ):
        return True
    return False


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
    if _looks_like_structured_media_text(rendered.marker):
        return True
    if any(_looks_like_structured_media_text(tag) for tag in rendered.emotion_tags):
        return True
    if _is_generic_media_label(rendered.description):
        return True
    if rendered.kind == "emoji" and not rendered.emotion_tags and _is_generic_media_label(rendered.marker):
        return True
    summary_label = _normalize_media_label(summary_hint)
    if summary_label and summary_label == _normalize_media_label(rendered.description) and _is_generic_media_label(summary_hint):
        return True
    source_label = _normalize_media_label(resolved.source_name)
    if source_label and source_label == _normalize_media_label(rendered.description) and _is_generic_media_label(resolved.source_name):
        return True
    return False


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
        emotion_tags = tuple()
        description = label or "一张图片"
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
    marker = str(cached.get("marker", "") or "").strip() or _build_marker(kind, description, emotion_tags)
    return RenderedMedia(
        media_hash=resolved.media_hash,
        kind=kind,
        description=description,
        emotion_tags=emotion_tags,
        marker=marker,
        cached_path=resolved.cached_path,
    )


def _same_rendered_media(left: RenderedMedia, right: RenderedMedia) -> bool:
    return (
        left.kind == right.kind
        and left.description == right.description
        and tuple(left.emotion_tags) == tuple(right.emotion_tags)
        and left.marker == right.marker
    )


def _vision_plugin_secrets(context) -> tuple[dict[str, Any], dict[str, Any], str]:
    plugin_secrets = (getattr(context, "secrets", {}) or {}).get("plugins", {}).get("xiaoqing_chat", {}) or {}
    vision = plugin_secrets.get("vision") or {}
    providers = vision.get("providers") or {}
    default_name = str(vision.get("default", "") or "").strip()
    return plugin_secrets, providers, default_name


def _normalize_provider_list(value: Any) -> list[str]:
    if isinstance(value, str):
        item = value.strip()
        return [item] if item else []
    if isinstance(value, (list, tuple)):
        items: list[str] = []
        for raw in value:
            item = str(raw or "").strip()
            if item and item not in items:
                items.append(item)
        return items
    return []


def _media_llm_max_tokens(secrets: dict[str, Any], base_max_tokens: int) -> int:
    base = max(1, int(base_max_tokens))
    model = str(secrets.get("model", "") or "").strip().lower()
    if "thinking" not in model:
        return base
    # Thinking models may emit hidden reasoning before the final JSON.
    # Give them a wider budget so they do not truncate before producing usable output.
    return max(base * 4, 800 if base >= 200 else 480)


def _build_media_provider_secrets(
    provider_name: str,
    provider_config: dict[str, Any],
    *,
    endpoint_path: str,
    scope: str,
) -> dict[str, Any]:
    return {
        "api_base": str(provider_config.get("api_base", "") or "").strip(),
        "api_key": str(provider_config.get("api_key", "") or "").strip(),
        "model": str(provider_config.get("model", "") or "").strip(),
        "endpoint_path": str(provider_config.get("endpoint_path", endpoint_path) or "").strip(),
        "proxy": str(provider_config.get("proxy", "") or "").strip(),
        "_provider_name": provider_name,
        "_provider_scope": scope,
        "_vision_enabled": True,
    }


def _legacy_direct_vision_overrides(cfg) -> dict[str, str]:
    return {
        "api_base": str(getattr(cfg, "vision_api_base", "") or "").strip(),
        "api_key": str(getattr(cfg, "vision_api_key", "") or "").strip(),
        "model": str(getattr(cfg, "vision_model", "") or "").strip(),
        "endpoint_path": str(getattr(cfg, "vision_endpoint_path", "") or "").strip(),
        "proxy": str(getattr(cfg, "vision_proxy", "") or "").strip(),
    }


def _explicit_media_llm_requested(context, runtime) -> bool:
    cfg = _media_cfg(runtime)
    if cfg is None:
        return False
    if str(getattr(cfg, "vision_provider", "") or "").strip():
        return True
    if any(_legacy_direct_vision_overrides(cfg).values()):
        return True
    _, providers, default_name = _vision_plugin_secrets(context)
    return bool(providers and default_name)


def _has_media_llm_capability(context, runtime) -> bool:
    if not _explicit_media_llm_requested(context, runtime):
        return False
    for secrets in _resolve_media_llm_secret_candidates(context, runtime):
        if all(str(secrets.get(field, "") or "").strip() for field in ("api_base", "api_key", "model")):
            return True
    return False


def _looks_like_source_placeholder(
    rendered: RenderedMedia,
    *,
    summary_hint: str,
    resolved: ResolvedMedia,
) -> bool:
    if rendered.kind != "image":
        return False
    full_source_label = _normalize_source_label(summary_hint or resolved.source_name)
    if not full_source_label:
        return False
    return (
        rendered.description == full_source_label
        and rendered.marker == _build_marker(rendered.kind, rendered.description, rendered.emotion_tags)
    )


def _should_refresh_cached_render(
    cached_rendered: RenderedMedia,
    *,
    cached_source: str,
    cached_quality: str,
    cached_prompt_version: int,
    fallback_rendered: RenderedMedia,
    summary_hint: str,
    resolved: ResolvedMedia,
    context,
    runtime,
) -> bool:
    if not _has_media_llm_capability(context, runtime):
        return False

    normalized_source = str(cached_source or "").strip().lower()
    if normalized_source == "llm":
        # Generic LLM labels are not materially better than fallback summary markers.
        # Keep retrying them on subsequent hits instead of treating them as stable cache.
        if cached_quality == "generic" or _is_low_quality_rendered_media(
            cached_rendered,
            summary_hint=summary_hint,
            resolved=resolved,
        ):
            return True
        if cached_prompt_version < _MEDIA_ANALYSIS_PROMPT_VERSION:
            return True
        return False
    if normalized_source == "fallback":
        return True
    return _same_rendered_media(cached_rendered, fallback_rendered) or _looks_like_source_placeholder(
        cached_rendered,
        summary_hint=summary_hint,
        resolved=resolved,
    )


async def _download_url_bytes(url: str, *, context, max_bytes: int) -> tuple[bytes, str]:
    session = getattr(context, "http_session", None)
    if session is None or not hasattr(session, "get"):
        raise FileNotFoundError(f"HTTP session unavailable for {url}")

    async with session.get(url, timeout=_MEDIA_DOWNLOAD_TIMEOUT) as resp:
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        headers = getattr(resp, "headers", {}) or {}
        content_type = str(headers.get("Content-Type", "") or "")
        content_length = headers.get("Content-Length")
        if max_bytes > 0 and content_length:
            try:
                if int(content_length) > max_bytes:
                    raise ValueError(f"media too large: {content_length} bytes")
            except ValueError:
                pass

        stream = getattr(getattr(resp, "content", None), "iter_chunked", None)
        if callable(stream):
            chunks: list[bytes] = []
            total = 0
            async for chunk in stream(_DOWNLOAD_CHUNK_SIZE):
                total += len(chunk)
                if max_bytes > 0 and total > max_bytes:
                    raise ValueError(f"media too large: {total} bytes")
                chunks.append(chunk)
            data = b"".join(chunks)
        else:
            data = await resp.read()
            if max_bytes > 0 and len(data) > max_bytes:
                raise ValueError(f"media too large: {len(data)} bytes")
    return data, content_type


def _onebot_http_base(context) -> str:
    return str((getattr(context, "config", {}) or {}).get("onebot_http_base", "") or "").strip().rstrip("/")


def _onebot_headers(context) -> dict[str, str]:
    token = str((getattr(context, "secrets", {}) or {}).get("onebot_token", "") or "").strip()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


async def _onebot_api_post(context, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    base = _onebot_http_base(context)
    session = getattr(context, "http_session", None)
    if not base or session is None or not hasattr(session, "post"):
        return {}

    url = f"{base}/{action.lstrip('/')}"
    async with session.post(
        url,
        json=payload,
        headers=_onebot_headers(context),
        timeout=_ONEBOT_HTTP_TIMEOUT,
    ) as resp:
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        data = await resp.json(content_type=None)
    return data if isinstance(data, dict) else {}


def _message_segments_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data", {}) or {}
    message = data.get("message")
    if isinstance(message, list):
        return [seg for seg in message if isinstance(seg, dict)]
    return []


def _segment_identity_key(segment: dict[str, Any]) -> tuple[str, str, str]:
    data = segment.get("data", {}) or {}
    return (
        str(data.get("emoji_id", "") or "").strip(),
        str(data.get("key", "") or "").strip(),
        str(data.get("summary", "") or "").strip(),
    )


def _matching_segments_from_message(
    original_segment: dict[str, Any],
    message_segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    original_key = _segment_identity_key(original_segment)
    matches: list[dict[str, Any]] = []
    fallbacks: list[dict[str, Any]] = []

    for seg in message_segments:
        seg_type = str(seg.get("type", "") or "")
        if seg_type not in _SUPPORTED_MEDIA_TYPES:
            continue
        if _segment_identity_key(seg) == original_key and any(original_key):
            matches.append(seg)
            continue
        if _segment_prefers_emoji(seg):
            fallbacks.append(seg)

    return matches or fallbacks


def _get_image_request_candidates(
    segment: dict[str, Any],
    *,
    allow_inference: bool,
) -> list[dict[str, str]]:
    data = segment.get("data", {}) or {}
    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _push(key: str, value: Any) -> None:
        cleaned = str(value or "").strip()
        if not cleaned:
            return
        token = (key, cleaned)
        if token in seen:
            return
        seen.add(token)
        candidates.append({key: cleaned})

    _push("file_id", data.get("file_id"))
    _push("file", data.get("file"))
    if allow_inference:
        _push("file_id", data.get("emoji_id"))
        _push("file", data.get("key"))

    return candidates


async def _resolve_onebot_image_result(
    data: dict[str, Any],
    *,
    summary_hint: str,
    context,
    max_bytes: int,
) -> tuple[bytes, str, str] | None:
    synthetic_data: dict[str, Any] = {
        "name": str(data.get("file_name", "") or summary_hint or "").strip(),
        "summary": summary_hint,
    }
    file_value = str(data.get("file", "") or "").strip()
    url_value = str(data.get("url", "") or "").strip()
    base64_value = str(data.get("base64", "") or "").strip()
    if file_value:
        synthetic_data["file"] = file_value
    elif base64_value:
        synthetic_data["file"] = f"base64://{base64_value}"
    elif url_value:
        synthetic_data["url"] = url_value
    else:
        return None

    synthetic_segment = {"type": "image", "data": synthetic_data}
    try:
        return await _resolve_media_bytes(
            synthetic_segment,
            context=context,
            max_bytes=max_bytes,
            event=None,
        )
    except Exception:
        return None


async def _recover_mface_media_via_onebot(
    segment: dict[str, Any],
    *,
    event: dict[str, Any] | None,
    context,
    max_bytes: int,
) -> tuple[bytes, str, str] | None:
    if event is None:
        return None

    message_id = event.get("message_id")
    summary_hint = _segment_summary_hint(segment)
    candidate_requests: list[dict[str, str]] = []
    seen_requests: set[tuple[str, str]] = set()

    def _extend_requests(items: list[dict[str, str]]) -> None:
        for item in items:
            if len(item) != 1:
                continue
            key, value = next(iter(item.items()))
            token = (key, value)
            if token in seen_requests:
                continue
            seen_requests.add(token)
            candidate_requests.append(item)

    if message_id is not None:
        try:
            detail_payload = await _onebot_api_post(context, "get_msg", {"message_id": message_id})
        except Exception:
            detail_payload = {}
        detail_segments = _message_segments_from_payload(detail_payload)
        _extend_requests(
            [
                item
                for detail_segment in _matching_segments_from_message(segment, detail_segments)
                for item in _get_image_request_candidates(detail_segment, allow_inference=False)
            ]
        )

    _extend_requests(_get_image_request_candidates(segment, allow_inference=True))

    for params in candidate_requests:
        try:
            image_payload = await _onebot_api_post(context, "get_image", params)
        except Exception:
            continue
        image_data = image_payload.get("data")
        if not isinstance(image_data, dict):
            continue
        resolved = await _resolve_onebot_image_result(
            image_data,
            summary_hint=summary_hint,
            context=context,
            max_bytes=max_bytes,
        )
        if resolved is not None:
            return resolved

    return None


def _resolve_media_source_path(value: str) -> Path | None:
    if not value:
        return None
    if _looks_like_base64_source(value) or _looks_like_data_url(value) or _looks_like_url(value):
        return None
    if value.startswith("file://"):
        return _parse_file_uri(value)
    path = Path(value)
    if path.exists():
        return path
    return None


def _decode_base64_payload(payload: str) -> bytes:
    normalized = re.sub(r"\s+", "", payload or "")
    if not normalized:
        raise ValueError("empty base64 payload")
    try:
        return base64.b64decode(normalized, validate=False)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid base64 payload") from exc


def _decode_data_url(value: str) -> tuple[bytes, str]:
    header, sep, payload = value.partition(",")
    if not sep:
        raise ValueError("invalid data url")

    meta = header[5:]
    mime_type = "image/png"
    is_base64 = False
    for part in meta.split(";"):
        token = part.strip()
        if not token:
            continue
        if token.lower() == "base64":
            is_base64 = True
            continue
        if "/" in token:
            mime_type = token

    if is_base64:
        return _decode_base64_payload(payload), mime_type
    return unquote_to_bytes(payload), mime_type


def _guess_suffix_from_mime(mime_type: str) -> str:
    normalized = str(mime_type or "").split(";", 1)[0].strip().lower()
    if not normalized:
        return ".png"
    suffix = mimetypes.guess_extension(normalized)
    if suffix == ".jpe":
        return ".jpg"
    return suffix or ".png"


def _segment_suffix_hint(segment: dict[str, Any]) -> str:
    data = segment.get("data", {}) or {}
    for key in ("name", "path", "file", "url"):
        value = str(data.get(key, "") or "").strip()
        if not value or _looks_like_base64_source(value) or _looks_like_data_url(value):
            continue
        if value.startswith("file://"):
            parsed = _parse_file_uri(value)
            if parsed is not None and parsed.suffix:
                return parsed.suffix
            continue
        suffix = Path(urlparse(value).path or value).suffix
        if suffix:
            return suffix
    return ".png"


def _resolve_media_sources(segment: dict[str, Any]) -> list[tuple[str, str]]:
    data = segment.get("data", {}) or {}
    key_order = {"path": 0, "file": 1, "url": 2}
    sources: list[tuple[str, str]] = []
    seen: set[str] = set()
    for key in ("path", "file", "url"):
        value = str(data.get(key, "") or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        sources.append((key, value))

    def _priority(item: tuple[str, str]) -> tuple[int, int]:
        key, value = item
        if _resolve_media_source_path(value) is not None:
            return 0, key_order.get(key, 9)
        if _looks_like_base64_source(value) or _looks_like_data_url(value):
            return 1, key_order.get(key, 9)
        if _looks_like_url(value):
            return 2, key_order.get(key, 9)
        return 3, key_order.get(key, 9)

    return sorted(sources, key=_priority)


def _hash_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _cached_media_path(context, media_hash: str, suffix: str) -> Path:
    suffix = suffix.lower() if suffix else ".bin"
    if suffix not in _SUPPORTED_IMAGE_SUFFIXES:
        suffix = ".png"
    return _figures_inbox_dir(context) / f"{media_hash}{suffix}"


async def _resolve_media_bytes(
    segment: dict[str, Any],
    *,
    context,
    max_bytes: int,
    event: dict[str, Any] | None = None,
) -> tuple[bytes, str, str]:
    sources = _resolve_media_sources(segment)
    if not sources:
        if _segment_prefers_emoji(segment):
            recovered = await _recover_mface_media_via_onebot(
                segment,
                event=event,
                context=context,
                max_bytes=max_bytes,
            )
            if recovered is not None:
                return recovered
        raise FileNotFoundError("segment has no supported media source")

    summary_hint = _segment_summary_hint(segment)
    suffix_hint = _segment_suffix_hint(segment)
    last_error: Exception | None = None

    for source_key, source_value in sources:
        source_name = _safe_source_name(summary_hint or source_value)
        try:
            source_path = _resolve_media_source_path(source_value)
            if source_path is not None:
                payload = source_path.read_bytes()
                if max_bytes > 0 and len(payload) > max_bytes:
                    raise ValueError(f"media too large: {len(payload)} bytes")
                return payload, source_name or source_path.stem, source_path.suffix or suffix_hint

            if _looks_like_base64_source(source_value):
                payload = _decode_base64_payload(source_value[len("base64://") :])
                if max_bytes > 0 and len(payload) > max_bytes:
                    raise ValueError(f"media too large: {len(payload)} bytes")
                return payload, source_name or "image", suffix_hint

            if _looks_like_data_url(source_value):
                payload, mime_type = _decode_data_url(source_value)
                if max_bytes > 0 and len(payload) > max_bytes:
                    raise ValueError(f"media too large: {len(payload)} bytes")
                return payload, source_name or "image", _guess_suffix_from_mime(mime_type)

            if _looks_like_url(source_value):
                payload, content_type = await _download_url_bytes(source_value, context=context, max_bytes=max_bytes)
                suffix = Path(urlparse(source_value).path).suffix
                if not suffix:
                    suffix = _guess_suffix_from_mime(content_type)
                return payload, source_name or Path(urlparse(source_value).path).stem, suffix

            raise FileNotFoundError(f"unsupported media source: {source_key}")
        except Exception as exc:
            last_error = exc
            continue

    if last_error is not None:
        if _segment_prefers_emoji(segment):
            recovered = await _recover_mface_media_via_onebot(
                segment,
                event=event,
                context=context,
                max_bytes=max_bytes,
            )
            if recovered is not None:
                return recovered
        raise last_error
    raise FileNotFoundError("segment has no supported media source")


def _event_media_log_fields(event: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(event, dict):
        return {}
    return {
        "message_id": event.get("message_id"),
        "group_id": event.get("group_id"),
        "user_id": event.get("user_id"),
    }


def _log_resolved_media(context, runtime, resolved: ResolvedMedia, *, event: dict[str, Any] | None = None) -> None:
    fields = _event_media_log_fields(event)
    fields.update(
        {
            "media_hash": resolved.media_hash[:12],
            "segment_type": resolved.segment_type,
            "source_name": resolved.source_name,
            "mime_type": resolved.mime_type,
            "animated": resolved.is_animated,
            "width": resolved.width,
            "height": resolved.height,
            "cached_path": resolved.cached_path,
        }
    )
    _media_log(context, runtime, step="media.resolve.ok", fields=fields)


async def _resolve_segment_media(
    segment: dict[str, Any],
    *,
    context,
    max_bytes: int,
    event: dict[str, Any] | None = None,
) -> ResolvedMedia | None:
    payload, source_name, suffix = await _resolve_media_bytes(
        segment,
        context=context,
        max_bytes=max_bytes,
        event=event,
    )
    mime_type, suffix, width, height, is_animated = _inspect_image_payload(payload, fallback_suffix=suffix)
    media_hash = _hash_bytes(payload)
    cached_path = _cached_media_path(context, media_hash, suffix)
    ensure_dir(cached_path.parent)
    if not cached_path.exists():
        cached_path.write_bytes(payload)
    resolved = ResolvedMedia(
        media_hash=media_hash,
        segment_type=str(segment.get("type", "") or ""),
        source_name=source_name,
        mime_type=mime_type,
        cached_path=cached_path,
        width=width,
        height=height,
        is_animated=is_animated,
    )
    return resolved


def _prepare_media_for_llm(resolved: ResolvedMedia) -> PreparedMediaForLLM:
    payload = resolved.cached_path.read_bytes()
    source_mime = str(resolved.mime_type or "").strip() or "image/png"
    if source_mime in {"image/png", "image/jpeg", "image/jpg"} and not resolved.is_animated:
        return PreparedMediaForLLM(
            payload=payload,
            mime_type=source_mime,
            transcoded=False,
            source_mime_type=source_mime,
            is_animated=resolved.is_animated,
            frame_strategy="original",
            frame_count=1,
        )

    try:
        if resolved.is_animated:
            prepared_payload, frame_count = _render_animation_contact_sheet(payload)
            return PreparedMediaForLLM(
                payload=prepared_payload,
                mime_type="image/png",
                transcoded=True,
                source_mime_type=source_mime,
                is_animated=resolved.is_animated,
                frame_strategy="animation_contact_sheet",
                frame_count=frame_count,
            )

        from PIL import Image

        with Image.open(io.BytesIO(payload)) as image:
            if getattr(image, "mode", "") not in {"RGB", "RGBA"}:
                image = image.convert("RGBA")
            elif image.mode == "P":
                image = image.convert("RGBA")
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
        return PreparedMediaForLLM(
            payload=buffer.getvalue(),
            mime_type="image/png",
            transcoded=True,
            source_mime_type=source_mime,
            is_animated=resolved.is_animated,
            frame_strategy="single_frame_png",
            frame_count=1,
        )
    except Exception:
        return PreparedMediaForLLM(
            payload=payload,
            mime_type=source_mime,
            transcoded=False,
            source_mime_type=source_mime,
            is_animated=resolved.is_animated,
            frame_strategy="original_fallback",
            frame_count=1,
        )


def _extract_json_object(text: str) -> dict[str, Any]:
    if not text:
        return {}
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        raw = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _extract_first_text_value(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(data.get(key, "") or "").strip()
        if value:
            return value
    return ""


def _merge_visible_text(description: str, visible_text: str) -> str:
    desc = str(description or "").strip()
    text = re.sub(r"\s+", " ", str(visible_text or "").strip())
    if not text:
        return desc
    normalized_desc = re.sub(r"\s+", "", desc)
    normalized_text = re.sub(r"\s+", "", text)
    if normalized_text and normalized_text in normalized_desc:
        return desc
    if desc:
        return f'{desc}，配字是“{text}”'
    return f'配字是“{text}”'


async def _call_media_llm(
    *,
    context,
    runtime,
    messages: list[dict[str, Any]],
    temperature: float,
    top_p: float,
    max_tokens: int,
) -> tuple[str, dict[str, Any]]:
    cfg = _media_cfg(runtime)
    if cfg is None:
        return "", {}

    candidates = _resolve_media_llm_secret_candidates(context, runtime)
    if not candidates:
        return "", {}

    previous_provider: dict[str, Any] | None = None
    last_exc: Exception | None = None

    for index, secrets in enumerate(candidates):
        effective_max_tokens = _media_llm_max_tokens(secrets, max_tokens)
        if index > 0 and previous_provider is not None:
            _media_log(
                context,
                runtime,
                step="media.analyze.provider_fallback",
                fields={
                    "from_provider": previous_provider.get("_provider_name", ""),
                    "to_provider": secrets.get("_provider_name", ""),
                    "to_model": secrets.get("model", ""),
                    "to_max_tokens": effective_max_tokens,
                    "reason": "retryable_http_429",
                },
                level="warning",
            )
        try:
            output = await chat_completions(
                session=context.http_session,
                api_base=str(secrets.get("api_base", "") or ""),
                api_key=str(secrets.get("api_key", "") or ""),
                model=str(secrets.get("model", "") or ""),
                messages=messages,
                temperature=temperature,
                top_p=top_p,
                max_tokens=effective_max_tokens,
                timeout_seconds=float(cfg.vision_timeout_seconds),
                max_retry=int(cfg.vision_max_retry),
                retry_interval_seconds=float(cfg.vision_retry_interval_seconds),
                proxy=str(secrets.get("proxy", "") or ""),
                endpoint_path=str(secrets.get("endpoint_path", "") or runtime.cfg.endpoint_path),
            )
            used_secrets = dict(secrets)
            used_secrets["_effective_max_tokens"] = effective_max_tokens
            return output, used_secrets
        except Exception as exc:
            setattr(exc, "_media_provider_name", secrets.get("_provider_name", ""))
            setattr(exc, "_media_provider_scope", secrets.get("_provider_scope", ""))
            setattr(exc, "_media_provider_model", secrets.get("model", ""))
            setattr(exc, "_media_provider_max_tokens", effective_max_tokens)
            last_exc = exc
            if (
                index + 1 < len(candidates)
                and isinstance(exc, LLMError)
                and str(exc).startswith("retryable_http_429:")
            ):
                previous_provider = secrets
                continue
            raise

    if last_exc is not None:
        raise last_exc
    return "", candidates[0]


def _parse_detail_analysis_output(
    output: str,
    *,
    resolved: ResolvedMedia,
    prefer_emoji: bool,
) -> MediaAnalysisDraft:
    data = _extract_json_object(output)
    kind = str(data.get("kind", "") or "").strip().lower()
    if kind not in {"image", "emoji"}:
        kind = _fallback_kind(
            resolved.source_name,
            width=resolved.width,
            height=resolved.height,
            segment_type=resolved.segment_type,
        )
    if prefer_emoji:
        kind = "emoji"

    description = _extract_first_text_value(
        data,
        "detailed_description",
        "detail_description",
        "detail",
        "description",
        "summary",
    )
    visible_text = _extract_first_text_value(
        data,
        "visible_text",
        "ocr_text",
        "text",
        "caption_text",
    )
    if not description:
        raw_output = str(output or "").strip().strip("`")
        if _can_use_raw_media_description(raw_output):
            description = raw_output
    description = _merge_visible_text(description, visible_text)
    emotion_tags = _normalize_emotion_tags(
        data.get("emotion_tags") or data.get("emotions") or data.get("tone_tags")
    )
    if kind == "emoji" and not emotion_tags:
        emotion_tags = _normalize_emotion_tags(description or visible_text)
    return MediaAnalysisDraft(
        kind=kind,
        description=description.strip(),
        visible_text=visible_text.strip(),
        emotion_tags=emotion_tags,
        raw_output=str(output or "").strip(),
    )


async def _refine_emoji_analysis_with_llm(
    draft: MediaAnalysisDraft,
    *,
    resolved: ResolvedMedia,
    context,
    runtime,
) -> tuple[MediaAnalysisDraft, dict[str, Any]]:
    prompt = (
        "你要把表情包的详细描述压缩成适合聊天使用的短标签 JSON。"
        "只输出 JSON，不要额外解释。"
        '格式: {"description":"...","emotion_tags":["..."]}。'
        "description 用简短中文概括主体、动作、表情和可见文字，优先保留梗图里最有辨识度的内容。"
        "不要输出泛化词，比如“图片”“表情包”“动画表情”“聊天表情包”。"
        "emotion_tags 放 1 到 4 个适合聊天使用的情绪或语气标签。"
    )
    detail_block = {
        "detailed_description": draft.description,
        "visible_text": draft.visible_text,
        "emotion_tags": list(draft.emotion_tags),
        "source_hint": resolved.source_name,
    }
    messages = [
        {"role": "system", "content": "你是表情包标签提炼器，只输出 JSON。"},
        {
            "role": "user",
            "content": prompt + "\n输入数据：" + json.dumps(detail_block, ensure_ascii=False),
        },
    ]
    output, used_secrets = await _call_media_llm(
        context=context,
        runtime=runtime,
        messages=messages,
        temperature=0.2,
        top_p=0.9,
        max_tokens=120,
    )
    data = _extract_json_object(output)
    description = _extract_first_text_value(data, "description", "label", "summary")
    if not description:
        raw_output = str(output or "").strip().strip("`")
        if _can_use_raw_media_description(raw_output):
            description = raw_output
    description = description.strip()
    emotion_tags = _normalize_emotion_tags(data.get("emotion_tags") or data.get("emotions"))
    if not emotion_tags:
        emotion_tags = draft.emotion_tags
    return (
        MediaAnalysisDraft(
            kind="emoji",
            description=description,
            visible_text=draft.visible_text,
            emotion_tags=emotion_tags,
            raw_output=str(output or "").strip(),
        ),
        used_secrets,
    )


def _resolve_media_llm_secret_candidates(context, runtime) -> list[dict[str, Any]]:
    cfg = _media_cfg(runtime)
    if cfg is None:
        return [{
            "api_base": "",
            "api_key": "",
            "model": "",
            "endpoint_path": "",
            "proxy": "",
            "_provider_name": "",
            "_provider_scope": "none",
            "_vision_enabled": False,
        }]

    plugin_secrets, vision_providers, vision_default = _vision_plugin_secrets(context)
    vision_cfg = plugin_secrets.get("vision") or {}
    chat_providers = plugin_secrets.get("providers") or {}
    provider_name = str(getattr(cfg, "vision_provider", "") or "").strip()
    endpoint_path = str(getattr(runtime.cfg, "endpoint_path", "") or "")

    empty: dict[str, Any] = {
        "api_base": "",
        "api_key": "",
        "model": "",
        "endpoint_path": endpoint_path,
        "proxy": "",
        "_provider_name": "",
        "_provider_scope": "none",
        "_vision_enabled": False,
    }

    provider_candidates: list[dict[str, Any]] = []
    provider_names: list[str] = []
    root_fallbacks = _normalize_provider_list(vision_cfg.get("fallbacks"))

    if provider_name and provider_name in vision_providers:
        provider_names.append(provider_name)
        provider_names.extend(_normalize_provider_list((vision_providers.get(provider_name) or {}).get("fallbacks")))
        provider_names.extend(root_fallbacks)
    elif not provider_name and vision_default and vision_default in vision_providers:
        provider_names.append(vision_default)
        provider_names.extend(_normalize_provider_list((vision_providers.get(vision_default) or {}).get("fallbacks")))
        provider_names.extend(root_fallbacks)
    elif provider_name and provider_name in chat_providers:
        provider_candidates.append(
            _build_media_provider_secrets(
                provider_name,
                chat_providers.get(provider_name) or {},
                endpoint_path=endpoint_path,
                scope="chat_provider",
            )
        )

    seen: set[str] = set()
    for idx, name in enumerate(provider_names):
        if not name or name in seen or name not in vision_providers:
            continue
        seen.add(name)
        scope = "vision" if provider_name else "vision_default"
        if idx > 0:
            scope = "vision_fallback"
        provider_candidates.append(
            _build_media_provider_secrets(
                name,
                vision_providers.get(name) or {},
                endpoint_path=endpoint_path,
                scope=scope,
            )
        )

    overrides = _legacy_direct_vision_overrides(cfg)
    if any(overrides.values()):
        secrets = provider_candidates[0] if provider_candidates else dict(empty)
        secrets["_provider_name"] = secrets.get("_provider_name", "") or "legacy_direct"
        secrets["_provider_scope"] = "legacy_direct"
        secrets["_vision_enabled"] = True
        for key, value in overrides.items():
            if key == "api_key" and not value:
                secrets[key] = ""
                continue
            if value:
                secrets[key] = value
        return [secrets]

    return provider_candidates or [empty]


def _resolve_media_llm_secrets(context, runtime) -> dict[str, Any]:
    candidates = _resolve_media_llm_secret_candidates(context, runtime)
    return candidates[0] if candidates else {}


async def _analyze_media_with_llm(
    resolved: ResolvedMedia,
    *,
    context,
    runtime,
    prefer_emoji: bool,
) -> RenderedMedia | None:
    cfg = _media_cfg(runtime)
    if cfg is None:
        return None
    secrets = _resolve_media_llm_secrets(context, runtime)
    if not bool(secrets.get("_vision_enabled")):
        _media_log(
            context,
            runtime,
            step="media.analyze.skip",
            fields={
                "reason": "vision_not_configured",
                "media_hash": resolved.media_hash[:12],
                "segment_type": resolved.segment_type,
            },
        )
        return None
    if not secrets.get("api_base") or not secrets.get("api_key") or not secrets.get("model"):
        _media_log(
            context,
            runtime,
            step="media.analyze.skip",
            fields={
                "reason": "vision_secrets_incomplete",
                "provider": secrets.get("_provider_name", ""),
                "provider_scope": secrets.get("_provider_scope", ""),
                "has_api_base": bool(secrets.get("api_base")),
                "has_api_key": bool(secrets.get("api_key")),
                "has_model": bool(secrets.get("model")),
                "media_hash": resolved.media_hash[:12],
            },
            level="warning",
        )
        return None

    payload = resolved.cached_path.read_bytes()
    if cfg.max_analyze_bytes > 0 and len(payload) > int(cfg.max_analyze_bytes):
        _media_log(
            context,
            runtime,
            step="media.analyze.skip",
            fields={
                "reason": "media_too_large",
                "bytes": len(payload),
                "limit": int(cfg.max_analyze_bytes),
                "media_hash": resolved.media_hash[:12],
            },
            level="warning",
        )
        return None

    prepared = _prepare_media_for_llm(resolved)
    image_b64 = base64.b64encode(prepared.payload).decode("ascii")
    prompt = (
        "请把这张聊天图片分析成 JSON。"
        "只输出 JSON，不要额外解释。"
        '格式: {"kind":"image|emoji","description":"...","emotion_tags":["..."]}。'
        "description 用简短中文描述图片或表情包内容，要尽量说出主体、动作/表情、画面里的可见文字。"
        "如果它更像聊天表情包/梗图/贴纸，kind 填 emoji；普通照片、截图、插画填 image。"
        "emotion_tags 只放 0 到 4 个简短中文情绪或语气标签。"
        "不要输出泛化词，比如“图片”“表情包”“动画表情”“聊天表情包”。"
        "如果图里有清晰文字，description 尽量把文字内容带上。"
    )
    if prefer_emoji:
        prompt += " 这张图来自表情包库，请优先按聊天表情包理解，并尽量提炼出适合聊天使用的情绪标签。"
    if prepared.is_animated and prepared.frame_strategy == "animation_contact_sheet" and prepared.frame_count > 1:
        prompt += (
            f" 这张图是从同一个动画表情里抽取的 {prepared.frame_count} 帧拼图，不是多个人物。"
            " 如果看到相似角色重复出现，要理解成同一角色在不同帧里的动作或表情变化。"
        )

    _media_log(
        context,
        runtime,
        step="media.analyze.start",
        fields={
            "provider": secrets.get("_provider_name", ""),
            "provider_scope": secrets.get("_provider_scope", ""),
            "model": secrets.get("model", ""),
            "max_tokens": _media_llm_max_tokens(secrets, 200),
            "media_hash": resolved.media_hash[:12],
            "segment_type": resolved.segment_type,
            "source_mime": prepared.source_mime_type,
            "llm_mime": prepared.mime_type,
            "transcoded": prepared.transcoded,
            "animated": prepared.is_animated,
            "frame_strategy": prepared.frame_strategy,
            "frame_count": prepared.frame_count,
            "prefer_emoji": prefer_emoji,
        },
    )

    messages = [
        {"role": "system", "content": "你是聊天图片解析器，只输出 JSON。"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{prepared.mime_type};base64,{image_b64}"},
                },
            ],
        },
    ]

    try:
        detail_output, used_secrets = await _call_media_llm(
            context=context,
            runtime=runtime,
            messages=messages,
            temperature=0.2,
            top_p=0.9,
            max_tokens=200,
        )
    except Exception as exc:
        _media_log(
            context,
            runtime,
            step="media.analyze.fail",
            fields={
                "provider": getattr(exc, "_media_provider_name", secrets.get("_provider_name", "")),
                "model": getattr(exc, "_media_provider_model", secrets.get("model", "")),
                "media_hash": resolved.media_hash[:12],
                "error": f"{type(exc).__name__}: {exc}",
            },
            level="warning",
        )
        raise

    detail = _parse_detail_analysis_output(
        detail_output,
        resolved=resolved,
        prefer_emoji=prefer_emoji,
    )
    _media_log(
        context,
        runtime,
        step="media.analyze.detail.ok",
        fields={
            "provider": used_secrets.get("_provider_name", ""),
            "model": used_secrets.get("model", ""),
            "max_tokens": used_secrets.get("_effective_max_tokens", ""),
            "media_hash": resolved.media_hash[:12],
            "kind": detail.kind,
            "description": detail.description,
            "visible_text": detail.visible_text,
            "emotion_tags": "，".join(detail.emotion_tags),
            "raw_output": detail.raw_output,
        },
    )

    kind = detail.kind
    refined: MediaAnalysisDraft | None = None
    refined_provider: dict[str, Any] = used_secrets
    if kind == "emoji":
        try:
            refined, refined_provider = await _refine_emoji_analysis_with_llm(
                detail,
                resolved=resolved,
                context=context,
                runtime=runtime,
            )
            _media_log(
                context,
                runtime,
                step="media.analyze.refine.ok",
                fields={
                    "provider": refined_provider.get("_provider_name", ""),
                    "model": refined_provider.get("model", ""),
                    "max_tokens": refined_provider.get("_effective_max_tokens", ""),
                    "media_hash": resolved.media_hash[:12],
                    "description": refined.description,
                    "emotion_tags": "，".join(refined.emotion_tags),
                    "raw_output": refined.raw_output,
                },
            )
        except Exception as exc:
            _media_log(
                context,
                runtime,
                step="media.analyze.refine.fail",
                fields={
                    "provider": getattr(exc, "_media_provider_name", used_secrets.get("_provider_name", "")),
                    "model": getattr(exc, "_media_provider_model", used_secrets.get("model", "")),
                    "max_tokens": getattr(exc, "_media_provider_max_tokens", used_secrets.get("_effective_max_tokens", "")),
                    "media_hash": resolved.media_hash[:12],
                    "error": f"{type(exc).__name__}: {exc}",
                },
                level="warning",
            )

    used_summary_fallback = False
    if kind == "emoji":
        if refined and refined.description and not _is_generic_media_label(refined.description):
            description = refined.description
        elif detail.description and not _is_generic_media_label(detail.description):
            description = detail.description
        elif refined and refined.description:
            description = refined.description
        elif detail.description:
            description = detail.description
        else:
            description = _safe_source_name(resolved.source_name) or "一张表情包"
            used_summary_fallback = True
        emotion_tags = refined.emotion_tags if refined and refined.emotion_tags else detail.emotion_tags
        if not emotion_tags:
            emotion_tags = _normalize_emotion_tags(description or detail.visible_text)
    else:
        description = detail.description
        if not description:
            description = _safe_source_name(resolved.source_name) or "一张图片"
            used_summary_fallback = True
        emotion_tags = tuple()

    marker = _build_marker(kind, description, emotion_tags)
    quality = "generic" if _is_low_quality_rendered_media(
        RenderedMedia(
            media_hash=resolved.media_hash,
            kind=kind,
            description=description,
            emotion_tags=emotion_tags,
            marker=marker,
            cached_path=resolved.cached_path,
        ),
        summary_hint=resolved.source_name,
        resolved=resolved,
    ) else "detailed"
    _media_log(
        context,
        runtime,
        step="media.analyze.ok",
        fields={
            "provider": secrets.get("_provider_name", ""),
            "model": secrets.get("model", ""),
            "max_tokens": used_secrets.get("_effective_max_tokens", ""),
            "media_hash": resolved.media_hash[:12],
            "kind": kind,
            "description": description,
            "detail_description": detail.description,
            "visible_text": detail.visible_text,
            "refined_description": refined.description if refined else "",
            "used_summary_fallback": used_summary_fallback,
            "marker": marker,
            "quality": quality,
        },
    )
    return RenderedMedia(
        media_hash=resolved.media_hash,
        kind=kind,
        description=description,
        emotion_tags=emotion_tags,
        marker=marker,
        cached_path=resolved.cached_path,
    )


async def _render_resolved_media(
    resolved: ResolvedMedia,
    *,
    context,
    runtime,
    prefer_emoji: bool,
    summary_hint: str = "",
) -> RenderedMedia:
    with _render_cache_lock(context.data_dir):
        cache = _load_render_cache(context.data_dir)
        items = cache.setdefault("items", {})
        cached = items.get(resolved.media_hash)
    if isinstance(cached, dict):
        cached_rendered = _rendered_media_from_cache(cached, resolved=resolved)
        fallback_rendered = _build_fallback_render(
            resolved,
            summary_hint=summary_hint,
            prefer_emoji=prefer_emoji,
        )
        if not _should_refresh_cached_render(
            cached_rendered,
            cached_source=str(cached.get("analysis_source", "") or ""),
            cached_quality=str(cached.get("analysis_quality", "") or ""),
            cached_prompt_version=int(cached.get("analysis_prompt_version", 0) or 0),
            fallback_rendered=fallback_rendered,
            summary_hint=summary_hint,
            resolved=resolved,
            context=context,
            runtime=runtime,
        ):
            _media_log(
                context,
                runtime,
                step="media.cache.hit",
                fields={
                    "media_hash": resolved.media_hash[:12],
                    "analysis_source": str(cached.get("analysis_source", "") or "unknown"),
                    "analysis_quality": str(cached.get("analysis_quality", "") or ""),
                    "analysis_prompt_version": int(cached.get("analysis_prompt_version", 0) or 0),
                    "marker": cached_rendered.marker,
                },
            )
            return cached_rendered
        _media_log(
            context,
            runtime,
            step="media.cache.refresh",
            fields={
                "media_hash": resolved.media_hash[:12],
                "cached_source": str(cached.get("analysis_source", "") or "unknown"),
                "cached_quality": str(cached.get("analysis_quality", "") or ""),
                "cached_prompt_version": int(cached.get("analysis_prompt_version", 0) or 0),
                "cached_marker": cached_rendered.marker,
                "summary_hint": summary_hint,
            },
        )

    rendered = None
    rendered_source = "fallback"
    try:
        rendered = await _analyze_media_with_llm(
            resolved,
            context=context,
            runtime=runtime,
            prefer_emoji=prefer_emoji,
        )
    except Exception:
        rendered = None

    if rendered is None:
        rendered = _build_fallback_render(
            resolved,
            summary_hint=summary_hint,
            prefer_emoji=prefer_emoji,
        )
        _media_log(
            context,
            runtime,
            step="media.render.fallback",
            fields={
                "media_hash": resolved.media_hash[:12],
                "segment_type": resolved.segment_type,
                "summary_hint": summary_hint,
                "marker": rendered.marker,
            },
            level="warning",
        )
    else:
        rendered_source = "llm"

    rendered_quality = "detailed"
    if _is_low_quality_rendered_media(
        rendered,
        summary_hint=summary_hint,
        resolved=resolved,
    ):
        rendered_quality = "generic"

    items[resolved.media_hash] = {
        "kind": rendered.kind,
        "description": rendered.description,
        "emotion_tags": list(rendered.emotion_tags),
        "marker": rendered.marker,
        "analysis_source": rendered_source,
        "analysis_quality": rendered_quality,
        "analysis_prompt_version": _MEDIA_ANALYSIS_PROMPT_VERSION if rendered_source == "llm" else 0,
    }
    with _render_cache_lock(context.data_dir):
        latest_cache = _load_render_cache(context.data_dir)
        latest_items = latest_cache.setdefault("items", {})
        latest_items[resolved.media_hash] = {
            "kind": rendered.kind,
            "description": rendered.description,
            "emotion_tags": list(rendered.emotion_tags),
            "marker": rendered.marker,
            "analysis_source": rendered_source,
            "analysis_quality": rendered_quality,
            "analysis_prompt_version": _MEDIA_ANALYSIS_PROMPT_VERSION if rendered_source == "llm" else 0,
        }
        _save_render_cache(context.data_dir, latest_cache)
    return rendered


async def render_local_media_file(
    file_path: Path | str,
    *,
    context,
    runtime,
    prefer_emoji: bool = False,
) -> RenderedMedia | None:
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        return None
    payload = path.read_bytes()
    max_bytes = int(_media_cfg_value(runtime, "max_analyze_bytes", 4 * 1024 * 1024))
    if max_bytes > 0 and len(payload) > max_bytes:
        return None
    mime_type, _, width, height, is_animated = _inspect_image_payload(payload, fallback_suffix=path.suffix or ".png")
    resolved = ResolvedMedia(
        media_hash=_hash_bytes(payload),
        segment_type="image",
        source_name=path.stem,
        mime_type=mime_type,
        cached_path=path,
        width=width,
        height=height,
        is_animated=is_animated,
    )
    return await _render_resolved_media(
        resolved,
        context=context,
        runtime=runtime,
        prefer_emoji=prefer_emoji,
        summary_hint=path.stem,
    )


def _render_summary_only_emoji(summary: str) -> RenderedMedia:
    cleaned = _safe_source_name(summary)
    emotion_tags = _normalize_emotion_tags(cleaned)
    description = cleaned or "一张表情包"
    marker = _build_marker("emoji", description, emotion_tags)
    return RenderedMedia(
        media_hash=f"summary:{hashlib.sha1(description.encode('utf-8')).hexdigest()}",
        kind="emoji",
        description=description,
        emotion_tags=emotion_tags,
        marker=marker,
        cached_path=None,
    )


def _render_face_segment(segment: dict[str, Any]) -> RenderedMedia:
    data = segment.get("data", {}) or {}
    label = describe_face_segment(segment)
    emotion_tags = tuple()
    if not label.startswith("id=") and "系统表情" not in label:
        emotion_tags = _normalize_emotion_tags(label)
    media_hash = hashlib.sha1(
        json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return RenderedMedia(
        media_hash=f"face:{media_hash}",
        kind="emoji",
        description=label,
        emotion_tags=emotion_tags,
        marker=f"[QQ表情：{label}]",
        cached_path=None,
    )


def _trim_ordered_text_segments(text_parts: list[str], clean_text: str) -> list[str]:
    if not text_parts:
        return []

    raw_text = "".join(text_parts)
    target = (clean_text or "").strip()
    if not raw_text:
        return text_parts

    raw_lstripped = raw_text.lstrip()
    leading_ws = len(raw_text) - len(raw_lstripped)
    raw_core = raw_lstripped.rstrip()

    if not target:
        remove_count = len(raw_text)
    elif raw_core.endswith(target):
        remove_count = leading_ws + max(0, len(raw_core) - len(target))
    else:
        return text_parts

    remaining = remove_count
    adjusted: list[str] = []
    for part in text_parts:
        if remaining <= 0:
            adjusted.append(part)
            continue
        if remaining >= len(part):
            adjusted.append("")
            remaining -= len(part)
            continue
        adjusted.append(part[remaining:])
        remaining = 0
    return adjusted


def _compose_effective_user_text(
    *,
    clean_text: str,
    event: dict[str, Any],
    rendered_items: list[RenderedMedia],
) -> str:
    segments = _iter_message_segments(event)
    if not segments:
        return (clean_text or "").strip()

    has_media = any(str(segment.get("type", "") or "") in _SUPPORTED_MEDIA_TYPES for segment in segments)
    if not has_media:
        return (clean_text or "").strip()

    text_parts = [
        str((segment.get("data", {}) or {}).get("text", ""))
        for segment in segments
        if str(segment.get("type", "") or "") == "text"
    ]
    trimmed_text_parts = _trim_ordered_text_segments(text_parts, clean_text)
    text_iter = iter(trimmed_text_parts)
    media_iter = iter(rendered_items)

    ordered_blocks: list[str] = []
    text_buffer = ""
    for segment in segments:
        segment_type = str(segment.get("type", "") or "")
        if segment_type == "text":
            text_buffer += next(text_iter, "")
            continue
        if segment_type not in _SUPPORTED_MEDIA_TYPES:
            continue
        flushed = text_buffer.strip()
        if flushed:
            ordered_blocks.append(flushed)
        text_buffer = ""
        rendered = next(media_iter, None)
        if rendered is not None:
            context_marker = _build_context_marker(rendered)
            if context_marker.strip():
                ordered_blocks.append(context_marker.strip())

    flushed = text_buffer.strip()
    if flushed:
        ordered_blocks.append(flushed)

    if ordered_blocks:
        return "\n".join(ordered_blocks).strip()
    return (clean_text or "").strip()


async def render_event_media(event: dict[str, Any], *, context, runtime) -> list[RenderedMedia]:
    cached_items = event.get("_xc_rendered_media_items")
    if isinstance(cached_items, list):
        return [item for item in cached_items if isinstance(item, RenderedMedia)]
    if not bool(_media_cfg_value(runtime, "enable_inbound_media_context", False)):
        return []

    max_items = max(0, int(_media_cfg_value(runtime, "max_media_per_message", 3)))
    rendered_items: list[RenderedMedia] = []
    new_emoji_markers: list[str] = []
    for segment in _iter_message_segments(event):
        segment_type = str(segment.get("type", "") or "")
        if segment_type not in _SUPPORTED_MEDIA_TYPES:
            continue
        summary_hint = _segment_summary_hint(segment)
        prefer_emoji = _segment_prefers_emoji(segment)
        _media_log(
            context,
            runtime,
            step="media.segment",
            fields={
                **_event_media_log_fields(event),
                "segment_type": segment_type,
                "summary_hint": summary_hint,
                "prefer_emoji": prefer_emoji,
            },
        )
        if segment_type == "face":
            rendered_face = _render_face_segment(segment)
            rendered_items.append(rendered_face)
            try:
                face_data = segment.get("data", {}) or {}
                record_face_observation(
                    context,
                    face_id=face_data.get("id"),
                    label=rendered_face.description,
                )
            except Exception:
                pass
            if max_items > 0 and len(rendered_items) >= max_items:
                break
            continue

        try:
            resolved = await _resolve_segment_media(
                segment,
                context=context,
                max_bytes=int(_media_cfg_value(runtime, "max_analyze_bytes", 4 * 1024 * 1024)),
                event=event,
            )
        except Exception as exc:
            _media_log(
                context,
                runtime,
                step="media.resolve.fail",
                fields={
                    **_event_media_log_fields(event),
                    "segment_type": segment_type,
                    "summary_hint": summary_hint,
                    "prefer_emoji": prefer_emoji,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                level="warning",
            )
            if prefer_emoji and summary_hint:
                rendered = _render_summary_only_emoji(summary_hint)
                _media_log(
                    context,
                    runtime,
                    step="media.summary_only",
                    fields={
                        **_event_media_log_fields(event),
                        "segment_type": segment_type,
                        "summary_hint": summary_hint,
                        "marker": rendered.marker,
                    },
                    level="warning",
                )
                rendered_items.append(rendered)
                if max_items > 0 and len(rendered_items) >= max_items:
                    break
            continue
        if resolved is None:
            continue
        _log_resolved_media(context, runtime, resolved, event=event)

        rendered = await _render_resolved_media(
            resolved,
            context=context,
            runtime=runtime,
            prefer_emoji=prefer_emoji,
            summary_hint=summary_hint or resolved.source_name,
        )
        rendered_items.append(rendered)
        if rendered.kind == "emoji" and rendered.cached_path is not None:
            try:
                from .emoji_library import collect_emoji_candidate

                collected = collect_emoji_candidate(
                    context,
                    runtime,
                    rendered,
                    source_path=rendered.cached_path,
                )
            except Exception:
                collected = None
            if collected is not None:
                _, is_new = collected
                if is_new:
                    new_emoji_markers.append(rendered.marker)
        if max_items > 0 and len(rendered_items) >= max_items:
            break

    event["_xc_rendered_media_items"] = rendered_items
    event["_xc_new_emoji_markers"] = new_emoji_markers
    event["_xc_new_emoji_count"] = len(new_emoji_markers)
    return rendered_items


async def render_event_media_text(event: dict[str, Any], *, context, runtime) -> str:
    cached = str(event.get("_xc_rendered_media_text", "") or "").strip()
    if cached:
        return cached
    rendered = await render_event_media(event, context=context, runtime=runtime)
    markers = [item.marker for item in rendered if item.marker.strip()]
    text = "\n".join(markers).strip()
    event["_xc_rendered_media_text"] = text
    return text


async def build_effective_user_text(
    clean_text: str,
    event: dict[str, Any],
    *,
    context,
    runtime,
) -> str:
    cached = str(event.get("_xc_effective_user_text", "") or "").strip()
    if cached:
        return cached
    rendered_items = await render_event_media(event, context=context, runtime=runtime)
    effective = _compose_effective_user_text(
        clean_text=clean_text,
        event=event,
        rendered_items=rendered_items,
    )
    event["_xc_effective_user_text"] = effective
    return effective
