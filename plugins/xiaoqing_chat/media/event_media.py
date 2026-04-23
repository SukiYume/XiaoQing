from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, unquote_to_bytes, urlparse

import aiohttp
from core.plugin_base import ensure_dir

from ..helper_utils import _iter_message_segments
from ..media_registry import resolve_registered_media_items
from ..message_parts import build_text_message_parts, normalize_message_parts
from .event_media_analysis import (
    _analyze_media_with_llm,
    _media_llm_max_tokens,
    _prepare_media_for_llm,
    _resolve_media_llm_secrets,
    _should_refresh_cached_render,
)
from .event_media_common import (
    RenderedMedia,
    ResolvedMedia,
    _DOWNLOAD_CHUNK_SIZE,
    _EMOJI_HINT_RE,
    _GENERIC_MEDIA_HINTS,
    _MEDIA_ANALYSIS_PROMPT_VERSION,
    _MEDIA_DOWNLOAD_TIMEOUT,
    _ONEBOT_HTTP_TIMEOUT,
    _SUPPORTED_IMAGE_SUFFIXES,
    _SUPPORTED_MEDIA_TYPES,
    _animation_sample_indexes,
    _build_context_marker,
    _build_fallback_render,
    _build_marker,
    _clean_media_hint,
    _fallback_kind,
    _figures_inbox_dir,
    _figures_root,
    _guess_mime_type,
    _inspect_image_payload,
    _is_generic_media_label,
    _is_low_quality_rendered_media,
    _load_render_cache,
    _looks_like_base64_source,
    _looks_like_data_url,
    _looks_like_structured_media_text,
    _looks_like_url,
    _media_cfg_value,
    _media_log,
    _media_root,
    _normalize_emotion_tags,
    _parse_file_uri,
    _render_cache_lock,
    _rendered_media_from_cache,
    _safe_source_name,
    _save_render_cache,
    _segment_prefers_emoji,
    _segment_summary_hint,
)
from .qq_face import describe_face_segment
from .qq_face_catalog import record_face_observation

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


async def _recover_media_bytes_if_needed(
    segment: dict[str, Any],
    *,
    event: dict[str, Any] | None,
    context,
    max_bytes: int,
) -> tuple[bytes, str, str] | None:
    if not _segment_prefers_emoji(segment):
        return None
    return await _recover_mface_media_via_onebot(
        segment,
        event=event,
        context=context,
        max_bytes=max_bytes,
    )


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
    summary_hint = _segment_summary_hint(segment)
    suffix_hint = _segment_suffix_hint(segment)
    last_error: Exception | None = None

    if not sources:
        last_error = FileNotFoundError("segment has no supported media source")

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

    cache_payload = {
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
        latest_items[resolved.media_hash] = cache_payload
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


def _render_summary_only_media(
    summary: str,
    *,
    segment_type: str,
    prefer_emoji: bool,
) -> RenderedMedia:
    cleaned = _safe_source_name(summary)
    kind = "emoji" if prefer_emoji else _fallback_kind(cleaned, width=0, height=0, segment_type=segment_type)
    if kind == "emoji":
        emotion_tags = _normalize_emotion_tags(cleaned)
        description = cleaned or "一张表情包"
    else:
        emotion_tags = tuple()
        description = cleaned or "一张图片"
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


def _render_face_segment(segment: dict[str, Any]) -> RenderedMedia:
    data = segment.get("data", {}) or {}
    face_id = str(data.get("id", "") or "").strip()
    label = describe_face_segment(segment)
    emotion_tags = tuple()
    if not label.startswith("id=") and "系统表情" not in label:
        emotion_tags = _normalize_emotion_tags(label)
    if face_id:
        media_hash = f"qq_face:{face_id}"
    else:
        media_hash = "qq_face:" + hashlib.sha1(
            json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    return RenderedMedia(
        media_hash=media_hash,
        kind="qq_face",
        description=label,
        emotion_tags=emotion_tags,
        marker=f"[QQ表情：{label}]",
        cached_path=None,
        face_id=face_id,
    )


def _upgrade_rendered_media_from_registry(rendered_items: list[RenderedMedia]) -> list[RenderedMedia]:
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
    for original, resolved in zip(rendered_items, resolved_refs):
        if not isinstance(resolved, dict):
            upgraded.append(original)
            continue
        tags = tuple(
            str(tag).strip()
            for tag in resolved.get("emotion_tags", [])
            if str(tag).strip()
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
            )
        )
    return upgraded


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


def _rendered_media_to_message_part(rendered: RenderedMedia) -> dict[str, Any]:
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
    segments = _iter_message_segments(event)
    if not segments:
        return build_text_message_parts((clean_text or "").strip())

    has_media = any(str(segment.get("type", "") or "") in _SUPPORTED_MEDIA_TYPES for segment in segments)
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
    cached_items = event.get("_xc_rendered_media_items")
    if isinstance(cached_items, list):
        return [item for item in cached_items if isinstance(item, RenderedMedia)]
    if not bool(_media_cfg_value(runtime, "enable_inbound_media_context", False)):
        return []

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
            rendered = _render_summary_only_media(
                summary_hint,
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
    rendered_items = _upgrade_rendered_media_from_registry(rendered_items)
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
