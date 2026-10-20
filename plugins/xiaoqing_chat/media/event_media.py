"""把入站 OneBot 媒体解析为有界且隐私安全的聊天上下文。

远程 URL 统一经过安全 HTTP 边界，本地路径必须位于插件自有根目录；内容进入按哈希
寻址的收件箱前还会检查图像尺寸和帧数。事件级缓存避免单轮重复 I/O；持久化注册表
与缓存只保存描述和哈希，不保存来源凭据或任意外部路径。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import weakref
from pathlib import Path
from typing import Any, cast

from core.message import iter_message_segments

from ..chat_event import mention_text_segments
from ..media_registry import resolve_registered_media_items
from ..message_parts import build_text_message_parts, normalize_message_parts
from .emoji_library import collect_emoji_candidate
from .event_media_analysis import (
    analyze_media_with_llm,
    schedule_background_emoji_refine,
    should_refresh_cached_render,
)
from .loading import (
    inspect_image_payload_details,
    read_file_bounded,
    run_media_blocking,
    safe_source_name,
)
from .markers import (
    build_context_marker,
    build_fallback_render,
    build_marker,
    fallback_kind,
    is_low_quality_rendered_media,
    normalize_emotion_tags,
    segment_prefers_emoji,
    segment_summary_hint,
)
from .models import (
    SUPPORTED_MEDIA_TYPES,
    MediaPayloadTooLarge,
    RenderedMedia,
    ResolvedMedia,
)
from .qq_face import describe_face_segment
from .qq_face_catalog import record_face_observation
from .render_cache import (
    load_render_cache,
    render_cache_lock,
    rendered_media_from_cache,
    write_render_cache_entry,
)
from .settings import media_config, media_log
from .sources import hash_media_bytes, resolve_segment_media, validate_image_resource_limits

_MEDIA_RENDER_LOCKS_BY_LOOP: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


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
    media_log(context, runtime, step="media.resolve.ok", fields=fields)


async def _render_resolved_media(
    resolved: ResolvedMedia,
    *,
    context,
    runtime,
    prefer_emoji: bool,
    summary_hint: str = "",
) -> RenderedMedia:
    """同一事件循环内合并相同媒体的并发渲染，并复用持久缓存。"""

    loop     = asyncio.get_running_loop()
    locks    = _MEDIA_RENDER_LOCKS_BY_LOOP.setdefault(loop, weakref.WeakValueDictionary())
    lock_key = f"{Path(context.data_dir).resolve()}::{resolved.media_hash}"
    lock     = locks.setdefault(lock_key, asyncio.Lock())
    async with lock:
        return await _render_resolved_media_locked(
            resolved,
            context      = context,
            runtime      = runtime,
            prefer_emoji = prefer_emoji,
            summary_hint = summary_hint,
        )


def _reusable_cached_render(
    cached: dict[str, Any],
    resolved: ResolvedMedia,
    *,
    context,
    runtime,
    prefer_emoji: bool,
    summary_hint: str,
) -> RenderedMedia | None:
    """缓存仍然新鲜且不比本地兜底差时直接复用；需要刷新时返回 None。"""
    cached_rendered = rendered_media_from_cache(cached, resolved=resolved)
    source         = str(cached.get("analysis_source", "") or "")
    quality        = str(cached.get("analysis_quality", "") or "")
    prompt_version = int(cached.get("analysis_prompt_version", 0) or 0)
    refresh        = should_refresh_cached_render(
        cached_rendered,
        cached_source         = source,
        cached_quality        = quality,
        cached_prompt_version = prompt_version,
        fallback_rendered     = build_fallback_render(
            resolved, summary_hint=summary_hint, prefer_emoji=prefer_emoji
        ),
        summary_hint = summary_hint,
        resolved     = resolved,
        context      = context,
    )
    if not refresh:
        media_log(
            context,
            runtime,
            step   = "media.cache.hit",
            fields = {
                "media_hash": resolved.media_hash[:12],
                "analysis_source": source or "unknown",
                "analysis_quality": quality,
                "analysis_prompt_version": prompt_version,
                "marker": cached_rendered.marker,
            },
        )
        return cached_rendered
    media_log(
        context,
        runtime,
        step   = "media.cache.refresh",
        fields = {
            "media_hash": resolved.media_hash[:12],
            "cached_source": source or "unknown",
            "cached_quality": quality,
            "cached_prompt_version": prompt_version,
            "cached_marker": cached_rendered.marker,
            "summary_hint": summary_hint,
        },
    )
    return None


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

    cached = await run_media_blocking(
        _load_cached_render_entry,
        context.data_dir,
        resolved.media_hash,
    )

    if isinstance(cached, dict):
        reusable = _reusable_cached_render(
            cached,
            resolved,
            context      = context,
            runtime      = runtime,
            prefer_emoji = prefer_emoji,
            summary_hint = summary_hint,
        )
        if reusable is not None:
            return reusable

    try:
        rendered = await analyze_media_with_llm(
            resolved,
            context      = context,
            runtime      = runtime,
            prefer_emoji = prefer_emoji,
        )
    except Exception:
        rendered = None
    rendered_source = "fallback" if rendered is None else "llm"
    if rendered is None:
        rendered = build_fallback_render(
            resolved,
            summary_hint = summary_hint,
            prefer_emoji = prefer_emoji,
        )
        media_log(
            context,
            runtime,
            step   = "media.render.fallback",
            fields = {
                "media_hash": resolved.media_hash[:12],
                "segment_type": resolved.segment_type,
                "summary_hint": summary_hint,
                "marker": rendered.marker,
            },
            level="warning",
        )
    rendered_quality = (
        "generic"
        if is_low_quality_rendered_media(rendered, summary_hint=summary_hint, resolved=resolved)
        else "detailed"
    )

    await run_media_blocking(
        write_render_cache_entry,
        context.data_dir,
        resolved,
        rendered,
        source  = rendered_source,
        quality = rendered_quality,
    )
    if rendered_source == "llm" and rendered.kind == "emoji" and rendered_quality == "detailed":
        schedule_background_emoji_refine(
            rendered,
            resolved,
            context = context,
            runtime = runtime,
        )
    return rendered


def _load_cached_render_entry(data_dir: Path, media_hash: str) -> dict[str, Any] | None:
    with render_cache_lock(data_dir):
        cache  = load_render_cache(data_dir)
        items  = cache.setdefault("items", {})
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

    path      = Path(file_path)
    max_bytes = runtime.cfg.media.max_analyze_bytes
    try:
        resolved = await run_media_blocking(
            _resolved_local_media,
            path,
            max_bytes  = max_bytes,
            max_pixels = runtime.cfg.media.max_image_pixels,
            max_frames = runtime.cfg.media.max_animation_frames,
        )
    except (FileNotFoundError, MediaPayloadTooLarge, OSError, ValueError):
        return None
    return await _render_resolved_media(
        resolved,
        context      = context,
        runtime      = runtime,
        prefer_emoji = prefer_emoji,
        summary_hint = path.stem,
    )


def _resolved_local_media(
    path: Path,
    *,
    max_bytes: int,
    max_pixels: int,
    max_frames: int,
) -> ResolvedMedia:
    payload = read_file_bounded(path, max_bytes=max_bytes)
    info = inspect_image_payload_details(payload, fallback_suffix=path.suffix or ".png")
    validate_image_resource_limits(
        width       = info.width,
        height      = info.height,
        max_pixels  = max_pixels,
        max_frames  = max_frames,
        frame_count = info.frame_count,
    )
    return ResolvedMedia(
        media_hash   = hash_media_bytes(payload),
        segment_type = "image",
        source_name  = path.stem,
        mime_type    = info.mime_type,
        cached_path  = path,
        width        = info.width,
        height       = info.height,
        is_animated  = info.is_animated,
    )


def _render_summary_only_media(
    summary: str,
    *,
    segment_type: str,
    prefer_emoji: bool,
) -> RenderedMedia:
    """仅使用已清洗的适配器摘要构造非模型降级结果。"""

    cleaned = safe_source_name(summary)
    kind    = (
        "emoji"
        if prefer_emoji
        else fallback_kind(cleaned, width=0, height=0, segment_type=segment_type)
    )
    if kind == "emoji":
        emotion_tags = normalize_emotion_tags(cleaned)
        description  = cleaned or "一张表情包"
    else:
        emotion_tags = ()
        description  = cleaned or "图片内容读取失败"
    marker      = build_marker(kind, description, emotion_tags)
    summary_key = f"{segment_type}:{kind}:{summary or description}"
    return RenderedMedia(
        media_hash   = f"summary:{hashlib.sha1(summary_key.encode('utf-8')).hexdigest()}",
        kind         = kind,
        description  = description,
        emotion_tags = emotion_tags,
        marker       = marker,
        cached_path  = None,
    )


def _segment_failure_summary_hint(segment: dict[str, Any]) -> str:
    """保留显式语义摘要，但绝不把文件名当作图片内容。"""

    data = segment.get("data", {}) or {}
    for key in ("summary", "text"):
        value   = str(data.get(key, "") or "").strip()
        cleaned = safe_source_name(value)
        if cleaned and cleaned not in {"图片", "一张图片", "[图片]"}:
            return value
    return ""


def _render_face_segment(segment: dict[str, Any]) -> RenderedMedia:
    """用稳定 ID 表示内置 QQ 表情，不下载媒体。"""

    data                          = segment.get("data", {}) or {}
    face_id                       = str(data.get("id", "") or "").strip()
    label                         = describe_face_segment(segment)
    emotion_tags: tuple[str, ...] = ()
    if not label.startswith("id=") and "系统表情" not in label:
        emotion_tags = normalize_emotion_tags(label)
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
        media_hash   = media_hash,
        kind         = "qq_face",
        description  = label,
        emotion_tags = emotion_tags,
        marker       = f"[QQ表情：{label}]",
        cached_path  = None,
        face_id      = face_id,
    )


def _upgrade_rendered_media_from_registry(
    rendered_items: list[RenderedMedia],
) -> list[RenderedMedia]:
    """只有持久化元数据能提高渲染质量时才合并，绝不允许降级。"""

    if not rendered_items:
        return rendered_items
    from ..runtime_state import get_state as _state

    store = _state().media_store

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
                media_hash    = str(resolved.get("media_hash", "") or original.media_hash),
                kind          = str(resolved.get("kind", "") or original.kind),
                description   = str(resolved.get("description", "") or original.description),
                emotion_tags  = tags or original.emotion_tags,
                marker        = str(resolved.get("marker", "") or original.marker),
                cached_path   = original.cached_path,
                face_id       = str(resolved.get("face_id", "") or original.face_id),
                cultural_hint = original.cultural_hint,
            )
        )
    return upgraded


def _trim_ordered_text_segments(text_parts: list[str], clean_text: str) -> list[str]:
    """把清洗后的文本投影回有序片段，不重新引入原始文本。"""

    if not text_parts:
        return []

    raw_text = "".join(text_parts)
    target   = (clean_text or "").strip()
    if not raw_text:
        return text_parts

    raw_lstripped = raw_text.lstrip()
    leading_ws    = len(raw_text) - len(raw_lstripped)
    raw_core      = raw_lstripped.rstrip()

    if not target:
        remove_count = len(raw_text)
    elif raw_core.endswith(target):
        remove_count = leading_ws + max(0, len(raw_core) - len(target))
    else:
        return text_parts

    remaining           = remove_count
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

    segments = mention_text_segments(event)
    if not segments:
        return (clean_text or "").strip()

    has_media = any(
        str(segment.get("type", "") or "") in SUPPORTED_MEDIA_TYPES for segment in segments
    )
    if not has_media:
        return (clean_text or "").strip()

    text_parts = [
        str((segment.get("data", {}) or {}).get("text", ""))
        for segment in segments
        if str(segment.get("type", "") or "") == "text"
    ]
    trimmed_text_parts = _trim_ordered_text_segments(text_parts, clean_text)
    text_iter          = iter(trimmed_text_parts)
    media_iter         = iter(rendered_items)

    ordered_blocks: list[str] = []
    text_buffer               = ""
    for segment in segments:
        segment_type = str(segment.get("type", "") or "")
        if segment_type == "text":
            text_buffer += next(text_iter, "")
            continue
        if segment_type not in SUPPORTED_MEDIA_TYPES:
            continue
        flushed = text_buffer.strip()
        if flushed:
            ordered_blocks.append(flushed)
        text_buffer = ""
        rendered    = next(media_iter, None)
        if rendered is not None:
            context_marker = build_context_marker(rendered)
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
    face_id = rendered.face_id.strip()
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

    segments = mention_text_segments(event)
    if not segments:
        return cast(
            tuple[dict[str, Any], ...],
            build_text_message_parts((clean_text or "").strip()),
        )

    has_media = any(
        str(segment.get("type", "") or "") in SUPPORTED_MEDIA_TYPES for segment in segments
    )
    if not has_media:
        return cast(
            tuple[dict[str, Any], ...],
            build_text_message_parts((clean_text or "").strip()),
        )

    text_parts = [
        str((segment.get("data", {}) or {}).get("text", ""))
        for segment in segments
        if str(segment.get("type", "") or "") == "text"
    ]
    trimmed_text_parts = _trim_ordered_text_segments(text_parts, clean_text)
    text_iter          = iter(trimmed_text_parts)
    media_iter         = iter(rendered_items)

    ordered_parts: list[dict[str, Any]] = []
    text_buffer                         = ""
    for segment in segments:
        segment_type = str(segment.get("type", "") or "")
        if segment_type == "text":
            text_buffer += next(text_iter, "")
            continue
        if segment_type not in SUPPORTED_MEDIA_TYPES:
            continue
        flushed = text_buffer.strip()
        if flushed:
            ordered_parts.append({"kind": "text", "text": flushed})
        text_buffer = ""
        rendered    = next(media_iter, None)
        if rendered is not None:
            ordered_parts.append(_rendered_media_to_message_part(rendered))

    flushed = text_buffer.strip()
    if flushed:
        ordered_parts.append({"kind": "text", "text": flushed})

    if ordered_parts:
        return cast(tuple[dict[str, Any], ...], normalize_message_parts(ordered_parts))
    return cast(
        tuple[dict[str, Any], ...],
        build_text_message_parts((clean_text or "").strip()),
    )


def _render_face_with_observation(segment: dict[str, Any], *, context, runtime) -> RenderedMedia:
    rendered = _render_face_segment(segment)
    try:
        record_face_observation(
            context, face_id=(segment.get("data", {}) or {}).get("id"), label=rendered.description
        )
    except Exception as exc:
        # 内置表情观察索引属于附属能力，失败不能阻断当前消息渲染。
        media_log(
            context,
            runtime,
            step   = "media.face_observation.fail",
            fields = {"error_type": type(exc).__name__},
            level  = "warning",
        )
    return rendered


def _render_unresolvable_segment(
    segment: dict[str, Any], exc: Exception, log_fields: dict[str, Any], *, context, runtime
) -> RenderedMedia:
    """下载或解码失败时只保留摘要占位，让本轮仍知道对方发了什么类型的媒体。"""
    media_log(
        context,
        runtime,
        step   = "media.resolve.fail",
        fields = {**log_fields, "error_type": type(exc).__name__},
        level  = "warning",
    )
    rendered = _render_summary_only_media(
        _segment_failure_summary_hint(segment),
        segment_type = log_fields["segment_type"],
        prefer_emoji = log_fields["prefer_emoji"],
    )
    media_log(
        context,
        runtime,
        step   = "media.summary_only",
        fields = {key: log_fields[key] for key in log_fields if key != "prefer_emoji"}
        | {"marker": rendered.marker},
        level="warning",
    )
    return rendered


async def _collected_new_emoji(
    rendered: RenderedMedia, event: dict[str, Any], *, context, runtime
) -> bool:
    """把收到的表情收进候选库；返回是否为本库新收录的表情。"""
    if rendered.kind != "emoji" or rendered.cached_path is None:
        return False
    try:
        collected = await run_media_blocking(
            collect_emoji_candidate,
            context,
            runtime,
            rendered,
            source_path    = rendered.cached_path,
            source_chat_id = (
                f"g{event.get('group_id')}"
                if event.get("group_id") not in (None, "")
                else f"u{event.get('user_id')}"
            ),
            source_user_id=str(event.get("user_id") or ""),
        )
    except Exception:
        return False
    return collected is not None and collected[1]


async def render_event_media(event: dict[str, Any], *, context, runtime) -> list[RenderedMedia]:
    """每个事件只渲染一次，且不超过配置的媒体预算。

    结果在当前轮次缓存在事件上，并按稳定标识注册；触发的分析次数绝不超过
    ``max_media_per_message``。
    """

    cached_items = event.get("_xc_rendered_media_items")
    if isinstance(cached_items, list):
        return [item for item in cached_items if isinstance(item, RenderedMedia)]
    cfg = media_config(runtime)
    if not cfg.enable_inbound_media_context:
        return []

    rendered_items: list[RenderedMedia] = []
    new_emoji_markers: list[str]        = []
    max_media                           = max(0, cfg.max_media_per_message)
    for segment in iter_message_segments(event):
        segment_type = str(segment.get("type", "") or "")
        if segment_type not in SUPPORTED_MEDIA_TYPES:
            continue
        if max_media == 0 or len(rendered_items) >= max_media:
            break
        summary_hint = segment_summary_hint(segment)
        prefer_emoji = segment_prefers_emoji(segment)
        log_fields   = {
            **_event_media_log_fields(event),
            "segment_type": segment_type,
            "summary_hint": summary_hint,
            "prefer_emoji": prefer_emoji,
        }
        media_log(context, runtime, step="media.segment", fields=log_fields)
        if segment_type == "face":
            rendered_items.append(
                _render_face_with_observation(segment, context=context, runtime=runtime)
            )
            continue
        try:
            resolved = await resolve_segment_media(
                segment,
                context           = context,
                max_bytes         = cfg.max_analyze_bytes,
                max_pixels        = cfg.max_image_pixels,
                max_frames        = cfg.max_animation_frames,
                disk_quota_bytes  = cfg.inbox_disk_quota_bytes,
                cache_ttl_seconds = cfg.inbox_ttl_seconds,
                event             = event,
            )
        except Exception as exc:
            rendered_items.append(
                _render_unresolvable_segment(
                    segment, exc, log_fields, context=context, runtime=runtime
                )
            )
            continue
        if resolved is None:
            continue
        _log_resolved_media(context, runtime, resolved, event=event)
        rendered = await _render_resolved_media(
            resolved,
            context      = context,
            runtime      = runtime,
            prefer_emoji = prefer_emoji,
            summary_hint = summary_hint or resolved.source_name,
        )
        rendered_items.append(rendered)
        if await _collected_new_emoji(rendered, event, context=context, runtime=runtime):
            new_emoji_markers.append(rendered.marker)
    rendered_items                    = _upgrade_rendered_media_from_registry(rendered_items)
    event["_xc_rendered_media_items"] = rendered_items
    event["_xc_new_emoji_markers"]    = new_emoji_markers
    event["_xc_new_emoji_count"]      = len(new_emoji_markers)
    return rendered_items


async def build_effective_user_text(
    clean_text: str,
    event: dict[str, Any],
    *,
    context,
    runtime,
) -> str:
    """把文本与已解析媒体合成为本轮回复使用的统一消息表示。"""

    cached       = str(event.get("_xc_effective_user_text", "") or "").strip()
    cached_parts = normalize_message_parts(event.get("_xc_effective_user_parts"))
    if cached and cached_parts:
        return cached
    rendered_items = await render_event_media(event, context=context, runtime=runtime)
    effective = _compose_effective_user_text(
        clean_text     = clean_text,
        event          = event,
        rendered_items = rendered_items,
    )
    effective_parts = _compose_effective_user_parts(
        clean_text     = clean_text,
        event          = event,
        rendered_items = rendered_items,
    )
    event["_xc_effective_user_text"]  = effective
    event["_xc_effective_user_parts"] = effective_parts
    return effective
