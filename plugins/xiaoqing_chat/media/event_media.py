"""把入站 OneBot 媒体解析为有界且隐私安全的聊天上下文。

远程 URL 统一经过安全 HTTP 边界，本地路径必须位于插件自有根目录；内容进入按哈希
寻址的收件箱前还会检查图像尺寸和帧数。事件级缓存避免单轮重复 I/O；持久化注册表
与缓存只保存描述和哈希，不保存来源凭据或任意外部路径。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import mimetypes
import re
import time
import weakref
from pathlib import Path
from typing import Any
from urllib.parse import unquote_to_bytes, urlparse

from core.atomic_store import keyed_path_lock
from core.message import iter_message_segments
from core.plugin_base import atomic_write_bytes, ensure_dir
from core.safe_http import SafeHttpError, fetch_public_bytes

from ..media_registry import resolve_registered_media_items
from ..message_parts import build_text_message_parts, normalize_message_parts
from .event_media_analysis import (
    _analyze_media_with_llm,
    _schedule_background_emoji_refine,
    _should_refresh_cached_render,
)
from .event_media_common import (
    _MEDIA_DOWNLOAD_TIMEOUT,
    _SUPPORTED_IMAGE_SUFFIXES,
    _SUPPORTED_MEDIA_TYPES,
    MediaPayloadTooLarge,
    RenderedMedia,
    ResolvedMedia,
    _build_context_marker,
    _build_fallback_render,
    _build_marker,
    _fallback_kind,
    _figures_inbox_dir,
    _inspect_image_payload_details,
    _is_low_quality_rendered_media,
    _load_render_cache,
    _looks_like_base64_source,
    _looks_like_data_url,
    _looks_like_url,
    _media_cfg,
    _media_log,
    _normalize_emotion_tags,
    _parse_file_uri,
    _read_file_bounded,
    _render_cache_lock,
    _rendered_media_from_cache,
    _run_media_blocking,
    _safe_source_name,
    _segment_prefers_emoji,
    _segment_summary_hint,
    write_render_cache_entry,
)
from .qq_face import describe_face_segment
from .qq_face_catalog import record_face_observation

_MEDIA_RENDER_LOCKS_BY_LOOP: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


async def _download_url_bytes(url: str, *, max_bytes: int) -> tuple[bytes, str]:
    """通过 SSRF 防护下载公开图片，并限制响应字节数。"""
    try:
        response = await fetch_public_bytes(
            url,
            timeout_seconds=float(_MEDIA_DOWNLOAD_TIMEOUT.total),
            max_bytes=max(1, int(max_bytes)),
            allowed_content_type_prefixes=("image/", "application/octet-stream"),
        )
    except SafeHttpError as exc:
        raise ValueError(str(exc)) from exc
    if response is None:
        raise FileNotFoundError("media download failed")
    return response.body, str(response.headers.get("Content-Type", "") or "")


def _message_segments_from_data(data: dict[str, Any]) -> list[dict[str, Any]]:
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
    """构造去重后的适配器标识符，不把标识符当作路径。"""

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
    """让适配器结果经过常规字节上限和来源策略门控后再解码。"""

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
    """只通过显式 OneBot 媒体能力恢复 mface。

    适配器授权由该能力负责；候选响应仍受 ``max_bytes`` 约束，且绝不视为可信本地路径。
    """

    if event is None:
        return None

    capabilities = getattr(context, "capabilities", None)
    onebot_media = getattr(capabilities, "onebot_media", None)
    if onebot_media is None:
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
            detail_data = await onebot_media.get_message(message_id)
        except Exception:
            detail_data = {}
        detail_segments = _message_segments_from_data(detail_data)
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
            image_data = await onebot_media.get_image(
                file_id=params.get("file_id"),
                file=params.get("file"),
            )
        except Exception:
            continue
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


async def _recover_media_bytes_if_needed(
    segment: dict[str, Any],
    *,
    event: dict[str, Any] | None,
    context,
    max_bytes: int,
) -> tuple[bytes, str, str] | None:
    """只对缺少可用字节的表情类片段启用适配器恢复。"""

    if not _segment_prefers_emoji(segment):
        return None
    return await _recover_mface_media_via_onebot(
        segment,
        event=event,
        context=context,
        max_bytes=max_bytes,
    )


def _is_allowed_local_media_path(path: Path, context: Any | None) -> bool:
    """只允许解析到插件自有数据或代码根目录下的本地路径。"""

    if context is None:
        return False
    allowed_roots: list[Path] = []
    for attr in ("data_dir", "plugin_dir"):
        root = getattr(context, attr, None)
        if not root:
            continue
        try:
            allowed_roots.append(Path(root).resolve())
        except OSError:
            continue
    if not allowed_roots:
        return False
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(resolved == root or root in resolved.parents for root in allowed_roots)


def _resolve_media_source_path(value: str, *, context: Any | None = None) -> Path | None:
    if not value:
        return None
    if _looks_like_base64_source(value) or _looks_like_data_url(value) or _looks_like_url(value):
        return None
    path: Path | None
    if value.startswith("file://"):
        path = _parse_file_uri(value)
    else:
        path = Path(value)
    if path is None:
        return None
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    return resolved if _is_allowed_local_media_path(resolved, context) else None


def _decode_base64_payload(payload: str, *, max_bytes: int = 0) -> bytes:
    normalized = re.sub(r"\s+", "", payload or "")
    if not normalized:
        raise ValueError("empty base64 payload")
    limit = int(max_bytes)
    if limit > 0 and len(normalized) > ((limit + 2) // 3) * 4 + 4:
        raise MediaPayloadTooLarge((len(normalized) * 3) // 4, limit)
    try:
        decoded = base64.b64decode(normalized, validate=False)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid base64 payload") from exc
    if limit > 0 and len(decoded) > limit:
        raise MediaPayloadTooLarge(len(decoded), limit)
    return decoded


def _decode_data_url(value: str, *, max_bytes: int = 0) -> tuple[bytes, str]:
    """解码 data URL，并在展开前后都执行字节上限。"""

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
        return _decode_base64_payload(payload, max_bytes=max_bytes), mime_type
    limit = int(max_bytes)
    if limit > 0 and len(payload) > limit * 3:
        raise MediaPayloadTooLarge((len(payload) + 2) // 3, limit)
    decoded = unquote_to_bytes(payload)
    if limit > 0 and len(decoded) > limit:
        raise MediaPayloadTooLarge(len(decoded), limit)
    return decoded, mime_type


def _guess_suffix_from_mime(mime_type: str) -> str:
    normalized = str(mime_type or "").split(";", 1)[0].strip().lower()
    if not normalized:
        return ".png"
    suffix = mimetypes.guess_extension(normalized)
    if suffix == ".jpe":
        return ".jpg"
    return suffix or ".png"


def _segment_suffix_hint(segment: dict[str, Any]) -> str:
    """只推断文件后缀，绝不以该提示授权媒体来源。"""

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


def _resolve_media_sources(
    segment: dict[str, Any],
    *,
    context: Any | None = None,
) -> list[tuple[str, str, Path | None]]:
    """按信任顺序返回去重来源，并排除越界本地路径。"""

    data = segment.get("data", {}) or {}
    key_order = {"path": 0, "file": 1, "url": 2}
    sources: list[tuple[str, str, Path | None]] = []
    seen: set[str] = set()
    for key in ("path", "file", "url"):
        value = str(data.get(key, "") or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        sources.append((key, value, _resolve_media_source_path(value, context=context)))

    def _priority(item: tuple[str, str, Path | None]) -> tuple[int, int]:
        key, value, source_path = item
        if source_path is not None:
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
    """依次尝试有界来源，并保留最后一个类型化错误供降级处理。"""

    sources = await _run_media_blocking(_resolve_media_sources, segment, context=context)
    summary_hint = _segment_summary_hint(segment)
    suffix_hint = _segment_suffix_hint(segment)
    last_error: Exception | None = None

    if not sources:
        last_error = FileNotFoundError("segment has no supported media source")

    for source_key, source_value, source_path in sources:
        embedded = _looks_like_base64_source(source_value) or _looks_like_data_url(source_value)
        source_name = _safe_source_name(summary_hint or ("image" if embedded else source_value))
        try:
            if source_path is not None:
                payload = await _run_media_blocking(
                    _read_file_bounded,
                    source_path,
                    max_bytes=max_bytes,
                )
                return payload, source_name or source_path.stem, source_path.suffix or suffix_hint

            if _looks_like_base64_source(source_value):
                payload = await _run_media_blocking(
                    _decode_base64_payload,
                    source_value[len("base64://") :],
                    max_bytes=max_bytes,
                )
                return payload, source_name or "image", suffix_hint

            if _looks_like_data_url(source_value):
                payload, mime_type = await _run_media_blocking(
                    _decode_data_url,
                    source_value,
                    max_bytes=max_bytes,
                )
                return payload, source_name or "image", _guess_suffix_from_mime(mime_type)

            if _looks_like_url(source_value):
                payload, content_type = await _download_url_bytes(source_value, max_bytes=max_bytes)
                suffix = Path(urlparse(source_value).path).suffix
                if not suffix:
                    suffix = _guess_suffix_from_mime(content_type)
                return payload, source_name or Path(urlparse(source_value).path).stem, suffix

            raise FileNotFoundError(f"unsupported media source: {source_key}")
        except Exception as exc:
            last_error = exc
            continue

    recovered = await _recover_media_bytes_if_needed(
        segment,
        event=event,
        context=context,
        max_bytes=max_bytes,
    )
    if recovered is not None:
        return recovered
    if last_error is not None:
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


def _log_resolved_media(
    context, runtime, resolved: ResolvedMedia, *, event: dict[str, Any] | None = None
) -> None:
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
    max_pixels: int = 16_000_000,
    max_frames: int = 120,
    disk_quota_bytes: int = 256 * 1024 * 1024,
    cache_ttl_seconds: float = 7 * 86400.0,
    event: dict[str, Any] | None = None,
) -> ResolvedMedia | None:
    """解析单个片段，仅在全部资源限制通过后才落盘。"""

    payload, source_name, suffix = await _resolve_media_bytes(
        segment,
        context=context,
        max_bytes=max_bytes,
        event=event,
    )
    return await _run_media_blocking(
        _materialize_resolved_media,
        payload,
        segment_type=str(segment.get("type", "") or ""),
        source_name=source_name,
        suffix=suffix,
        context=context,
        max_pixels=max_pixels,
        max_frames=max_frames,
        disk_quota_bytes=disk_quota_bytes,
        cache_ttl_seconds=cache_ttl_seconds,
    )


def _materialize_resolved_media(
    payload: bytes,
    *,
    segment_type: str,
    source_name: str,
    suffix: str,
    context,
    max_pixels: int,
    max_frames: int,
    disk_quota_bytes: int,
    cache_ttl_seconds: float,
) -> ResolvedMedia:
    """验证字节并原子写入按内容寻址的媒体收件箱。

    清理仅限收件箱根目录，并在原子写入前运行，使容量统计包含即将写入的对象；已有
    哈希则直接复用。
    """

    info = _inspect_image_payload_details(payload, fallback_suffix=suffix)
    _validate_image_resource_limits(
        payload,
        width=info.width,
        height=info.height,
        max_pixels=max_pixels,
        max_frames=max_frames,
        frame_count=info.frame_count,
    )
    media_hash = _hash_bytes(payload)
    cached_path = _cached_media_path(context, media_hash, info.suffix)
    ensure_dir(cached_path.parent)
    with keyed_path_lock(cached_path.parent):
        already_cached = cached_path.exists()
        _prune_media_inbox(
            cached_path.parent,
            quota_bytes=disk_quota_bytes,
            ttl_seconds=cache_ttl_seconds,
            incoming_bytes=0 if already_cached else len(payload),
            protected_path=cached_path if already_cached else None,
        )
        if not already_cached:
            atomic_write_bytes(cached_path, payload)
    resolved = ResolvedMedia(
        media_hash=media_hash,
        segment_type=segment_type,
        source_name=source_name,
        mime_type=info.mime_type,
        cached_path=cached_path,
        width=info.width,
        height=info.height,
        is_animated=info.is_animated,
    )
    return resolved


def _validate_image_resource_limits(
    payload: bytes,
    *,
    width: int,
    height: int,
    max_pixels: int,
    max_frames: int,
    frame_count: int | None = None,
) -> None:
    """按解码像素数和动画帧数拒绝解压炸弹。"""

    del payload  # 探测器已经验证完整编码流；此处只检查它返回的资源元数据。
    if width <= 0 or height <= 0:
        raise ValueError("image dimensions unavailable")
    if max_pixels > 0 and width * height > max_pixels:
        raise ValueError("image pixel limit exceeded")
    if frame_count is None or frame_count <= 0:
        raise ValueError("image frame count unavailable")
    if max_frames > 0 and frame_count > max_frames:
        raise ValueError("image frame limit exceeded")


def _prune_media_inbox(
    root: Path,
    *,
    quota_bytes: int,
    ttl_seconds: float,
    incoming_bytes: int,
    protected_path: Path | None = None,
) -> None:
    """只在专用收件箱内先按 TTL、再按最旧优先执行容量淘汰。"""

    now = time.time()
    files: list[tuple[float, int, Path]] = []
    for path in root.glob("*"):
        try:
            if not path.is_file():
                continue
            stat = path.stat()
            is_protected = protected_path is not None and path == protected_path
            if not is_protected and ttl_seconds > 0 and now - stat.st_mtime > ttl_seconds:
                path.unlink(missing_ok=True)
                continue
            files.append((stat.st_mtime, stat.st_size, path))
        except OSError:
            continue
    if quota_bytes <= 0:
        return
    total = sum(size for _mtime, size, _path in files)
    for _mtime, size, path in sorted(files):
        if total + incoming_bytes <= quota_bytes:
            break
        if protected_path is not None and path == protected_path:
            continue
        try:
            path.unlink(missing_ok=True)
            total -= size
        except OSError:
            continue
    if total + incoming_bytes > quota_bytes:
        raise ValueError("media cache quota exceeded")


async def _render_resolved_media(
    resolved: ResolvedMedia,
    *,
    context,
    runtime,
    prefer_emoji: bool,
    summary_hint: str = "",
) -> RenderedMedia:
    """Single-flight identical media within one event loop, then use the render cache."""

    loop = asyncio.get_running_loop()
    locks = _MEDIA_RENDER_LOCKS_BY_LOOP.setdefault(loop, weakref.WeakValueDictionary())
    lock_key = f"{Path(context.data_dir).resolve()}::{resolved.media_hash}"
    lock = locks.setdefault(lock_key, asyncio.Lock())
    async with lock:
        return await _render_resolved_media_locked(
            resolved,
            context=context,
            runtime=runtime,
            prefer_emoji=prefer_emoji,
            summary_hint=summary_hint,
        )


async def _render_resolved_media_locked(
    resolved: ResolvedMedia,
    *,
    context,
    runtime,
    prefer_emoji: bool,
    summary_hint: str = "",
) -> RenderedMedia:
    """在缓存与视觉分析之间选择，同时禁止质量倒退。

    有效的新缓存优先；过期或泛化条目可以刷新，但模型失败时会确定性降级，后台优化
    也始终绑定媒体哈希。
    """

    cached = await _run_media_blocking(
        _load_cached_render_entry,
        context.data_dir,
        resolved.media_hash,
    )

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

    await _run_media_blocking(
        write_render_cache_entry,
        context.data_dir,
        resolved,
        rendered,
        source=rendered_source,
        quality=rendered_quality,
    )
    if rendered_source == "llm" and rendered.kind == "emoji" and rendered_quality == "detailed":
        _schedule_background_emoji_refine(
            rendered,
            resolved,
            context=context,
            runtime=runtime,
        )
    return rendered


def _load_cached_render_entry(data_dir: Path, media_hash: str) -> dict[str, Any] | None:
    with _render_cache_lock(data_dir):
        cache = _load_render_cache(data_dir)
        items = cache.setdefault("items", {})
        cached = items.get(media_hash)
        return dict(cached) if isinstance(cached, dict) else None


async def render_local_media_file(
    file_path: Path | str,
    *,
    context,
    runtime,
    prefer_emoji: bool = False,
) -> RenderedMedia | None:
    """按相同字节、像素和帧数预算渲染调用方提供的可信文件。"""

    path = Path(file_path)
    max_bytes = runtime.cfg.media.max_analyze_bytes
    try:
        resolved = await _run_media_blocking(
            _resolved_local_media,
            path,
            max_bytes=max_bytes,
            max_pixels=runtime.cfg.media.max_image_pixels,
            max_frames=runtime.cfg.media.max_animation_frames,
        )
    except (FileNotFoundError, MediaPayloadTooLarge, OSError, ValueError):
        return None
    return await _render_resolved_media(
        resolved,
        context=context,
        runtime=runtime,
        prefer_emoji=prefer_emoji,
        summary_hint=path.stem,
    )


def _resolved_local_media(
    path: Path,
    *,
    max_bytes: int,
    max_pixels: int,
    max_frames: int,
) -> ResolvedMedia:
    payload = _read_file_bounded(path, max_bytes=max_bytes)
    info = _inspect_image_payload_details(payload, fallback_suffix=path.suffix or ".png")
    _validate_image_resource_limits(
        payload,
        width=info.width,
        height=info.height,
        max_pixels=max_pixels,
        max_frames=max_frames,
        frame_count=info.frame_count,
    )
    return ResolvedMedia(
        media_hash=_hash_bytes(payload),
        segment_type="image",
        source_name=path.stem,
        mime_type=info.mime_type,
        cached_path=path,
        width=info.width,
        height=info.height,
        is_animated=info.is_animated,
    )


def _render_summary_only_media(
    summary: str,
    *,
    segment_type: str,
    prefer_emoji: bool,
) -> RenderedMedia:
    """仅使用已清洗的适配器摘要构造非模型降级结果。"""

    cleaned = _safe_source_name(summary)
    kind = (
        "emoji"
        if prefer_emoji
        else _fallback_kind(cleaned, width=0, height=0, segment_type=segment_type)
    )
    if kind == "emoji":
        emotion_tags = _normalize_emotion_tags(cleaned)
        description = cleaned or "一张表情包"
    else:
        emotion_tags = ()
        description = cleaned or "图片内容读取失败"
    marker = _build_marker(kind, description, emotion_tags)
    summary_key = f"{segment_type}:{kind}:{summary or description}"
    return RenderedMedia(
        media_hash=f"summary:{hashlib.sha1(summary_key.encode('utf-8')).hexdigest()}",
        kind=kind,
        description=description,
        emotion_tags=emotion_tags,
        marker=marker,
        cached_path=None,
    )


def _segment_failure_summary_hint(segment: dict[str, Any]) -> str:
    """Keep explicit semantic summaries, but never treat a filename as image content."""

    data = segment.get("data", {}) or {}
    for key in ("summary", "text"):
        value = str(data.get(key, "") or "").strip()
        cleaned = _safe_source_name(value)
        if cleaned and cleaned not in {"图片", "一张图片", "[图片]"}:
            return value
    return ""


def _render_face_segment(segment: dict[str, Any]) -> RenderedMedia:
    """用稳定 ID 表示内置 QQ 表情，不下载媒体。"""

    data = segment.get("data", {}) or {}
    face_id = str(data.get("id", "") or "").strip()
    label = describe_face_segment(segment)
    emotion_tags = ()
    if not label.startswith("id=") and "系统表情" not in label:
        emotion_tags = _normalize_emotion_tags(label)
    if face_id:
        media_hash = f"qq_face:{face_id}"
    else:
        media_hash = (
            "qq_face:"
            + hashlib.sha1(
                json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
        )
    return RenderedMedia(
        media_hash=media_hash,
        kind="qq_face",
        description=label,
        emotion_tags=emotion_tags,
        marker=f"[QQ表情：{label}]",
        cached_path=None,
        face_id=face_id,
    )


def _upgrade_rendered_media_from_registry(
    rendered_items: list[RenderedMedia],
) -> list[RenderedMedia]:
    """只有持久化元数据能提高渲染质量时才合并，绝不允许降级。"""

    if not rendered_items:
        return rendered_items
    try:
        from ..runtime_state import get_state as _state
    except Exception:
        return rendered_items

    store = getattr(_state(), "media_store", None)
    if store is None:
        return rendered_items

    refs: list[dict[str, Any]] = []
    for item in rendered_items:
        ref: dict[str, Any] = {
            "kind": str(item.kind or ""),
            "media_hash": str(item.media_hash or ""),
            "face_id": str(item.face_id or ""),
            "marker": str(item.marker or ""),
            "description": str(item.description or ""),
            "emotion_tags": [str(tag) for tag in item.emotion_tags if str(tag).strip()],
        }
        if item.cached_path is not None:
            ref["file_path"] = str(item.cached_path)
        refs.append(ref)

    resolved_refs = resolve_registered_media_items(refs, store=store)
    if not resolved_refs:
        return rendered_items

    upgraded: list[RenderedMedia] = []
    for original, resolved in zip(rendered_items, resolved_refs, strict=False):
        if not isinstance(resolved, dict):
            upgraded.append(original)
            continue
        tags = tuple(
            str(tag).strip() for tag in resolved.get("emotion_tags", []) if str(tag).strip()
        )
        upgraded.append(
            RenderedMedia(
                media_hash=str(resolved.get("media_hash", "") or original.media_hash),
                kind=str(resolved.get("kind", "") or original.kind),
                description=str(resolved.get("description", "") or original.description),
                emotion_tags=tags or original.emotion_tags,
                marker=str(resolved.get("marker", "") or original.marker),
                cached_path=original.cached_path,
                face_id=str(resolved.get("face_id", "") or original.face_id),
                cultural_hint=original.cultural_hint,
            )
        )
    return upgraded


def _trim_ordered_text_segments(text_parts: list[str], clean_text: str) -> list[str]:
    """把清洗后的文本投影回有序片段，不重新引入原始文本。"""

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
    """按原始消息顺序交错组合清洗文本与渲染标记。"""

    segments = list(iter_message_segments(event))
    if not segments:
        return (clean_text or "").strip()

    has_media = any(
        str(segment.get("type", "") or "") in _SUPPORTED_MEDIA_TYPES for segment in segments
    )
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


def _rendered_media_to_message_part(rendered: RenderedMedia) -> dict[str, Any]:
    """只序列化模型安全的媒体元数据，排除来源字节和凭据。"""

    payload: dict[str, Any] = {
        "kind": str(rendered.kind or "").strip(),
        "media_hash": str(rendered.media_hash or "").strip(),
        "marker": str(rendered.marker or "").strip(),
    }
    description = str(rendered.description or "").strip()
    if description:
        payload["description"] = description
        if payload["kind"] == "qq_face":
            payload["label"] = description
    emotion_tags = [str(tag).strip() for tag in rendered.emotion_tags if str(tag).strip()]
    if emotion_tags:
        payload["emotion_tags"] = emotion_tags
    if rendered.cached_path is not None:
        payload["file_path"] = str(rendered.cached_path)
    face_id = str(getattr(rendered, "face_id", "") or "").strip()
    if face_id:
        payload["face_id"] = face_id
    return payload


def _compose_effective_user_parts(
    *,
    clean_text: str,
    event: dict[str, Any],
    rendered_items: list[RenderedMedia],
) -> tuple[dict[str, Any], ...]:
    """在保留清洗后片段顺序的同时构造结构化模型输入。"""

    segments = list(iter_message_segments(event))
    if not segments:
        return build_text_message_parts((clean_text or "").strip())

    has_media = any(
        str(segment.get("type", "") or "") in _SUPPORTED_MEDIA_TYPES for segment in segments
    )
    if not has_media:
        return build_text_message_parts((clean_text or "").strip())

    text_parts = [
        str((segment.get("data", {}) or {}).get("text", ""))
        for segment in segments
        if str(segment.get("type", "") or "") == "text"
    ]
    trimmed_text_parts = _trim_ordered_text_segments(text_parts, clean_text)
    text_iter = iter(trimmed_text_parts)
    media_iter = iter(rendered_items)

    ordered_parts: list[dict[str, Any]] = []
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
            ordered_parts.append({"kind": "text", "text": flushed})
        text_buffer = ""
        rendered = next(media_iter, None)
        if rendered is not None:
            ordered_parts.append(_rendered_media_to_message_part(rendered))

    flushed = text_buffer.strip()
    if flushed:
        ordered_parts.append({"kind": "text", "text": flushed})

    if ordered_parts:
        return normalize_message_parts(ordered_parts)
    return build_text_message_parts((clean_text or "").strip())


async def render_event_media(event: dict[str, Any], *, context, runtime) -> list[RenderedMedia]:
    """每个事件只渲染一次，且不超过配置的媒体预算。

    结果在当前轮次缓存在事件上，并按稳定标识注册；触发的分析次数绝不超过
    ``max_media_per_message``。
    """

    cached_items = event.get("_xc_rendered_media_items")
    if isinstance(cached_items, list):
        return [item for item in cached_items if isinstance(item, RenderedMedia)]
    cfg = _media_cfg(runtime)
    if not cfg.enable_inbound_media_context:
        return []

    rendered_items: list[RenderedMedia] = []
    new_emoji_markers: list[str] = []
    max_media = max(0, cfg.max_media_per_message)
    for segment in iter_message_segments(event):
        segment_type = str(segment.get("type", "") or "")
        if segment_type not in _SUPPORTED_MEDIA_TYPES:
            continue
        if max_media == 0 or len(rendered_items) >= max_media:
            break
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
            continue

        try:
            resolved = await _resolve_segment_media(
                segment,
                context=context,
                max_bytes=cfg.max_analyze_bytes,
                max_pixels=cfg.max_image_pixels,
                max_frames=cfg.max_animation_frames,
                disk_quota_bytes=cfg.inbox_disk_quota_bytes,
                cache_ttl_seconds=cfg.inbox_ttl_seconds,
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
                    "error_type": type(exc).__name__,
                },
                level="warning",
            )
            rendered = _render_summary_only_media(
                _segment_failure_summary_hint(segment),
                segment_type=segment_type,
                prefer_emoji=prefer_emoji,
            )
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

                collected = await _run_media_blocking(
                    collect_emoji_candidate,
                    context,
                    runtime,
                    rendered,
                    source_path=rendered.cached_path,
                    source_chat_id=(
                        f"g{event.get('group_id')}"
                        if event.get("group_id") not in (None, "")
                        else f"u{event.get('user_id')}"
                    ),
                    source_user_id=str(event.get("user_id") or ""),
                )
            except Exception:
                collected = None
            if collected is not None:
                _, is_new = collected
                if is_new:
                    new_emoji_markers.append(rendered.marker)
    rendered_items = _upgrade_rendered_media_from_registry(rendered_items)
    event["_xc_rendered_media_items"] = rendered_items
    event["_xc_new_emoji_markers"] = new_emoji_markers
    event["_xc_new_emoji_count"] = len(new_emoji_markers)
    return rendered_items


async def build_effective_user_text(
    clean_text: str,
    event: dict[str, Any],
    *,
    context,
    runtime,
) -> str:
    """把文本与已解析媒体合成为本轮回复使用的统一消息表示。"""

    cached = str(event.get("_xc_effective_user_text", "") or "").strip()
    cached_parts = normalize_message_parts(event.get("_xc_effective_user_parts"))
    if cached and cached_parts:
        return cached
    rendered_items = await render_event_media(event, context=context, runtime=runtime)
    effective = _compose_effective_user_text(
        clean_text=clean_text,
        event=event,
        rendered_items=rendered_items,
    )
    effective_parts = _compose_effective_user_parts(
        clean_text=clean_text,
        event=event,
        rendered_items=rendered_items,
    )
    event["_xc_effective_user_text"] = effective
    event["_xc_effective_user_parts"] = effective_parts
    return effective
