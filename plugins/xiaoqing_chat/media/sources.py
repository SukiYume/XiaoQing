"""获取入站媒体字节：下载、OneBot 补取、data URL/base64 解码、本地路径授权、落盘与资源上限。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote_to_bytes, urlparse

from core.atomic_store import keyed_path_lock
from core.plugin_base import atomic_write_bytes, ensure_dir, file_uri_to_path
from core.safe_http import SafeHttpError, fetch_public_bytes

from .loading import (
    inspect_image_payload_details,
    looks_like_base64_source,
    looks_like_data_url,
    looks_like_url,
    read_file_bounded,
    run_media_blocking,
    safe_source_name,
)
from .markers import (
    segment_prefers_emoji,
    segment_summary_hint,
)
from .models import (
    SUPPORTED_IMAGE_SUFFIXES,
    SUPPORTED_MEDIA_TYPES,
    MediaPayloadTooLarge,
    ResolvedMedia,
)
from .settings import MEDIA_DOWNLOAD_TIMEOUT, figures_inbox_dir

_QQ_MEDIA_FAKE_DNS_HOSTS = frozenset({"multimedia.nt.qq.com.cn"})


async def _download_url_bytes(url: str, *, max_bytes: int) -> tuple[bytes, str]:
    """通过 SSRF 防护下载公开图片，并限制响应字节数。"""
    parsed   = urlparse(url)
    fake_dns = (
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").rstrip(".").casefold() in _QQ_MEDIA_FAKE_DNS_HOSTS
    )
    try:
        response = await fetch_public_bytes(
            url,
            timeout_seconds                  = float(MEDIA_DOWNLOAD_TIMEOUT.total or 20),
            max_bytes                        = max(1, int(max_bytes)),
            allowed_content_type_prefixes    = ("image/", "application/octet-stream"),
            allowed_hosts                    = _QQ_MEDIA_FAKE_DNS_HOSTS if fake_dns else None,
            allowed_schemes                  = {"https"} if fake_dns else None,
            allow_transparent_proxy_fake_dns = fake_dns,
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
    original_key   = _segment_identity_key(original_segment)
    original_data  = original_segment.get("data", {}) or {}
    media_segments = [seg for seg in message_segments if seg.get("type") in SUPPORTED_MEDIA_TYPES]
    direct_matches = [
        seg
        for seg in media_segments
        if any(
            original_data.get(key)
            and original_data.get(key) == (seg.get("data", {}) or {}).get(key)
            for key in ("file_id", "file", "url", "emoji_id", "key")
        )
    ]
    if direct_matches:
        return direct_matches
    if not segment_prefers_emoji(original_segment):
        return media_segments if len(media_segments) == 1 else []
    matches  : list[dict[str, Any]] = []
    fallbacks: list[dict[str, Any]] = []

    for seg in message_segments:
        seg_type = str(seg.get("type", "") or "")
        if seg_type not in SUPPORTED_MEDIA_TYPES:
            continue
        if _segment_identity_key(seg) == original_key and any(original_key):
            matches.append(seg)
            continue
        if segment_prefers_emoji(seg):
            fallbacks.append(seg)

    return matches or fallbacks


def _get_image_request_candidates(
    segment: dict[str, Any],
    *,
    allow_inference: bool,
) -> list[dict[str, str]]:
    """构造去重后的适配器标识符，不把标识符当作路径。"""

    data                             = segment.get("data", {}) or {}
    candidates: list[dict[str, str]] = []
    seen      : set[tuple[str, str]] = set()

    def _push(key: str, value: Any) -> None:
        cleaned = str(value or "").strip()
        if not cleaned:
            return
        if (
            looks_like_url(cleaned)
            or looks_like_data_url(cleaned)
            or looks_like_base64_source(cleaned)
            or any(char in cleaned for char in ("/", "\\", ":", "\n", "\r"))
        ):
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
        "name"   : str(data.get("file_name", "") or summary_hint or "").strip(),
        "summary": summary_hint,
    }
    file_value   = str(data.get("file", "") or "").strip()
    url_value    = str(data.get("url", "") or "").strip()
    base64_value = str(data.get("base64", "") or "").strip()
    if base64_value:
        synthetic_data["file"] = f"base64://{base64_value}"
    elif file_value:
        synthetic_data["file"] = file_value
    if url_value:
        synthetic_data["url"] = url_value
    if not any(key in synthetic_data for key in ("file", "url")):
        return None

    synthetic_segment = {"type": "image", "data": synthetic_data}
    try:
        return await _resolve_media_bytes(
            synthetic_segment,
            context   = context,
            max_bytes = max_bytes,
            event     = None,
        )
    except Exception:
        return None


async def _recover_media_via_onebot(
    segment: dict[str, Any],
    *,
    event: dict[str, Any] | None,
    context,
    max_bytes: int,
) -> tuple[bytes, str, str] | None:
    """通过显式 OneBot 媒体能力恢复普通图片和表情的可用来源。

    适配器授权由该能力负责；候选响应仍受 ``max_bytes`` 约束，且绝不视为可信本地路径。
    """

    if event is None:
        return None

    onebot_media = context.capabilities.onebot_media
    if onebot_media is None:
        return None

    message_id                               = event.get("message_id")
    summary_hint                             = segment_summary_hint(segment)
    candidate_requests: list[dict[str, str]] = []
    seen_requests     : set[tuple[str, str]] = set()

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
        detail_segments  = _message_segments_from_data(detail_data)
        matched_segments = _matching_segments_from_message(segment, detail_segments)
        for detail_segment in matched_segments:
            try:
                return await _resolve_media_bytes(
                    detail_segment, context=context, max_bytes=max_bytes, event=None
                )
            except Exception:
                continue
        _extend_requests(
            [
                item
                for detail_segment in matched_segments
                for item in _get_image_request_candidates(detail_segment, allow_inference=False)
            ]
        )

    _extend_requests(_get_image_request_candidates(segment, allow_inference=True))

    for params in candidate_requests:
        try:
            image_data = await onebot_media.get_image(
                file_id = params.get("file_id"),
                file    = params.get("file"),
            )
        except Exception:
            continue
        if not isinstance(image_data, dict):
            continue
        resolved = await _resolve_onebot_image_result(
            image_data,
            summary_hint = summary_hint,
            context      = context,
            max_bytes    = max_bytes,
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
    """为缺少可用字节的图片和表情启用适配器恢复。"""

    if segment.get("type") not in SUPPORTED_MEDIA_TYPES:
        return None
    return await _recover_media_via_onebot(
        segment,
        event     = event,
        context   = context,
        max_bytes = max_bytes,
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
    if looks_like_base64_source(value) or looks_like_data_url(value) or looks_like_url(value):
        return None
    path: Path | None = file_uri_to_path(value) if value.startswith("file://") else Path(value)
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

    meta      = header[5:]
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
        if not value or looks_like_base64_source(value) or looks_like_data_url(value):
            continue
        if value.startswith("file://"):
            parsed = file_uri_to_path(value)
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

    data                                        = segment.get("data", {}) or {}
    key_order                                   = {"path": 0, "file": 1, "url": 2}
    sources: list[tuple[str, str, Path | None]] = []
    seen   : set[str]                           = set()
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
        if looks_like_base64_source(value) or looks_like_data_url(value):
            return 1, key_order.get(key, 9)
        if looks_like_url(value):
            return 2, key_order.get(key, 9)
        return 3, key_order.get(key, 9)

    return sorted(sources, key=_priority)


def hash_media_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _cached_media_path(context, media_hash: str, suffix: str) -> Path:
    suffix = suffix.lower() if suffix else ".bin"
    if suffix not in SUPPORTED_IMAGE_SUFFIXES:
        suffix = ".png"
    return figures_inbox_dir(context) / f"{media_hash}{suffix}"


async def _resolve_media_bytes(
    segment: dict[str, Any],
    *,
    context,
    max_bytes: int,
    event: dict[str, Any] | None = None,
) -> tuple[bytes, str, str]:
    """依次尝试有界来源，并保留最后一个类型化错误供降级处理。"""

    sources = await run_media_blocking(_resolve_media_sources, segment, context=context)
    summary_hint                 = segment_summary_hint(segment)
    suffix_hint                  = _segment_suffix_hint(segment)
    last_error: Exception | None = None

    if not sources:
        last_error = FileNotFoundError("segment has no supported media source")

    for source_key, source_value, source_path in sources:
        embedded    = looks_like_base64_source(source_value) or looks_like_data_url(source_value)
        source_name = safe_source_name(summary_hint or ("image" if embedded else source_value))
        try:
            if source_path is not None:
                payload = await run_media_blocking(
                    read_file_bounded,
                    source_path,
                    max_bytes=max_bytes,
                )
                return payload, source_name or source_path.stem, source_path.suffix or suffix_hint

            if looks_like_base64_source(source_value):
                payload = await run_media_blocking(
                    _decode_base64_payload,
                    source_value[len("base64://") :],
                    max_bytes=max_bytes,
                )
                return payload, source_name or "image", suffix_hint

            if looks_like_data_url(source_value):
                payload, mime_type = await run_media_blocking(
                    _decode_data_url,
                    source_value,
                    max_bytes=max_bytes,
                )
                return payload, source_name or "image", _guess_suffix_from_mime(mime_type)

            if looks_like_url(source_value):
                payload, content_type = await _download_url_bytes(source_value, max_bytes=max_bytes)
                suffix = Path(urlparse(source_value).path).suffix
                if not suffix:
                    suffix = _guess_suffix_from_mime(content_type)
                return payload, source_name or Path(urlparse(source_value).path).stem, suffix

            if last_error is None:
                last_error = FileNotFoundError(f"unsupported media source: {source_key}")
        except Exception as exc:
            last_error = exc
            continue

    recovered = await _recover_media_bytes_if_needed(
        segment,
        event     = event,
        context   = context,
        max_bytes = max_bytes,
    )
    if recovered is not None:
        return recovered
    if last_error is not None:
        raise last_error
    raise FileNotFoundError("segment has no supported media source")


async def resolve_segment_media(
    segment: dict[str, Any],
    *,
    context,
    max_bytes: int,
    max_pixels: int              = 16_000_000,
    max_frames: int              = 120,
    disk_quota_bytes: int        = 256 * 1024 * 1024,
    cache_ttl_seconds: float     = 7 * 86400.0,
    event: dict[str, Any] | None = None,
) -> ResolvedMedia | None:
    """解析单个片段，仅在全部资源限制通过后才落盘。"""

    payload, source_name, suffix = await _resolve_media_bytes(
        segment,
        context   = context,
        max_bytes = max_bytes,
        event     = event,
    )
    materialized = await run_media_blocking(
        _materialize_resolved_media,
        payload,
        segment_type      = str(segment.get("type", "") or ""),
        source_name       = source_name,
        suffix            = suffix,
        context           = context,
        max_pixels        = max_pixels,
        max_frames        = max_frames,
        disk_quota_bytes  = disk_quota_bytes,
        cache_ttl_seconds = cache_ttl_seconds,
    )
    return materialized if isinstance(materialized, ResolvedMedia) else None


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

    # 当前配置直接交给唯一的解码校验边界：降低预算时在完整解码前拒绝，提高上限后也能处理。
    info = inspect_image_payload_details(
        payload,
        fallback_suffix = suffix,
        max_pixels      = max_pixels,
        max_frames      = max_frames,
    )
    validate_image_resource_limits(
        width       = info.width,
        height      = info.height,
        max_pixels  = max_pixels,
        max_frames  = max_frames,
        frame_count = info.frame_count,
    )
    media_hash  = hash_media_bytes(payload)
    cached_path = _cached_media_path(context, media_hash, info.suffix)
    ensure_dir(cached_path.parent)
    with keyed_path_lock(cached_path.parent):
        already_cached = cached_path.exists()
        _prune_media_inbox(
            cached_path.parent,
            quota_bytes    = disk_quota_bytes,
            ttl_seconds    = cache_ttl_seconds,
            incoming_bytes = 0 if already_cached else len(payload),
            protected_path = cached_path if already_cached else None,
        )
        if not already_cached:
            atomic_write_bytes(cached_path, payload)
    return ResolvedMedia(
        media_hash   = media_hash,
        segment_type = segment_type,
        source_name  = source_name,
        mime_type    = info.mime_type,
        cached_path  = cached_path,
        width        = info.width,
        height       = info.height,
        is_animated  = info.is_animated,
    )


def validate_image_resource_limits(
    *,
    width: int,
    height: int,
    max_pixels: int,
    max_frames: int,
    frame_count: int | None = None,
) -> None:
    """按解码像素数和动画帧数拒绝解压炸弹。"""

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

    now                                  = time.time()
    files: list[tuple[float, int, Path]] = []
    for path in root.glob("*"):
        try:
            if not path.is_file():
                continue
            stat         = path.stat()
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
