from __future__ import annotations

import asyncio
import base64
import io
import json
import re
from dataclasses import dataclass
from typing import Any

import aiohttp

from ..helper_utils import _llm_declared_extra_payload
from ..llm.llm_client import (
    LLMError,
    chat_completions_raw_with_fallback_paths,
    extract_response_content,
    extract_response_finish_reason,
)
from ..task_scheduler import _spawn_bg_task
from ..utils.json_parsing import parse_first_json_object, parse_first_json_object_with_status
from .event_media_common import (
    MediaAnalysisDraft,
    PreparedMediaForLLM,
    RenderedMedia,
    ResolvedMedia,
    _MEDIA_ANALYSIS_PROMPT_VERSION,
    _build_marker,
    _can_use_raw_media_description,
    _fallback_kind,
    _inspect_image_payload,
    _is_generic_media_label,
    _is_low_quality_rendered_media,
    _media_cfg,
    _media_log,
    _normalize_emotion_tags,
    _normalize_source_label,
    _render_animation_contact_sheet,
    _same_rendered_media,
    _safe_source_name,
    write_render_cache_entry,
)

_MEDIA_DETAIL_BASE_MAX_TOKENS = 360
_MEDIA_DETAIL_TRUNCATED_RETRY_MAX_TOKENS = 720


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
    return max(base * 4, 800 if base >= 200 else 480)


def _media_detail_base_max_tokens_for_reason(reason: str) -> int:
    normalized = str(reason or "").strip().lower()
    if normalized.endswith("length_truncated"):
        return _MEDIA_DETAIL_TRUNCATED_RETRY_MAX_TOKENS
    return _MEDIA_DETAIL_BASE_MAX_TOKENS


def _emoji_refine_timeout_seconds(runtime) -> float:
    cfg = _media_cfg(runtime)
    try:
        timeout = float(getattr(cfg, "emoji_refine_timeout_seconds", 2.0))
    except (TypeError, ValueError):
        return 2.0
    return max(0.0, timeout)


def _enable_emoji_refine_background(runtime) -> bool:
    cfg = _media_cfg(runtime)
    return bool(getattr(cfg, "enable_emoji_refine_background", True))


@dataclass(frozen=True)
class MediaLLMCallResult:
    text: str
    used_secrets: dict[str, Any]
    used_path: str
    finish_reason: str
    raw_chars: int


def _media_request_failure_reason(exc: Exception) -> str:
    if isinstance(exc, LLMError):
        text = str(exc).strip()
        return text.split(":", 1)[0] if text else "llm_error"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, aiohttp.ClientError):
        return type(exc).__name__
    return type(exc).__name__


def _is_media_request_failure(exc: Exception) -> bool:
    return isinstance(exc, (LLMError, asyncio.TimeoutError, TimeoutError, aiohttp.ClientError))


def _log_media_provider_fallback(
    *,
    context,
    runtime,
    from_provider: dict[str, Any],
    to_provider: dict[str, Any],
    max_tokens: int,
    reason: str,
) -> None:
    _media_log(
        context,
        runtime,
        step="media.analyze.provider_fallback",
        fields={
            "from_provider": from_provider.get("_provider_name", ""),
            "to_provider": to_provider.get("_provider_name", ""),
            "to_model": to_provider.get("model", ""),
            "to_max_tokens": _media_llm_max_tokens(to_provider, max_tokens),
            "reason": reason or "request_failed",
        },
        level="warning",
    )


def _build_media_provider_secrets(
    provider_name: str,
    provider_config: dict[str, Any],
    *,
    endpoint_path: str,
    scope: str,
) -> dict[str, Any]:
    extra_payload = _llm_declared_extra_payload(provider_config)
    return {
        "api_base": str(provider_config.get("api_base", "") or "").strip(),
        "api_key": str(provider_config.get("api_key", "") or "").strip(),
        "model": str(provider_config.get("model", "") or "").strip(),
        "endpoint_path": str(provider_config.get("endpoint_path", endpoint_path) or "").strip(),
        "proxy": str(provider_config.get("proxy", "") or "").strip(),
        "_extra_payload": extra_payload,
        "_provider_name": provider_name,
        "_provider_scope": scope,
        "_vision_enabled": True,
    }


def _explicit_media_llm_requested(context, runtime) -> bool:
    cfg = _media_cfg(runtime)
    if cfg is None:
        return False
    if str(getattr(cfg, "vision_provider", "") or "").strip():
        return True
    _, providers, default_name = _vision_plugin_secrets(context)
    return bool(providers and default_name)


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

    return provider_candidates or [empty]


def _resolve_media_llm_secrets(context, runtime) -> dict[str, Any]:
    candidates = _resolve_media_llm_secret_candidates(context, runtime)
    return candidates[0] if candidates else {}


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


def _prepare_media_for_llm(resolved: ResolvedMedia) -> PreparedMediaForLLM:
    payload = resolved.cached_path.read_bytes()
    source_mime = str(resolved.mime_type or _inspect_image_payload(payload, fallback_suffix=resolved.cached_path.suffix or ".png")[0]).split(";", 1)[0].strip().lower() or "image/png"
    if source_mime.startswith("image/") and source_mime not in {"image/svg+xml"}:
        try:
            if resolved.is_animated:
                contact_sheet_payload, frame_count = _render_animation_contact_sheet(payload)
                return PreparedMediaForLLM(
                    payload=contact_sheet_payload,
                    mime_type="image/png",
                    transcoded=True,
                    source_mime_type=source_mime,
                    is_animated=True,
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
    return PreparedMediaForLLM(
        payload=payload,
        mime_type=source_mime,
        transcoded=False,
        source_mime_type=source_mime,
        is_animated=resolved.is_animated,
        frame_strategy="original",
        frame_count=1,
    )


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
        return f'{desc}，文字内容是“{text}”'
    return f'文字内容是“{text}”'


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
    previous_reason = ""
    last_exc: Exception | None = None

    for index, secrets in enumerate(candidates):
        if index > 0 and previous_provider is not None:
            _log_media_provider_fallback(
                context=context,
                runtime=runtime,
                from_provider=previous_provider,
                to_provider=secrets,
                max_tokens=max_tokens,
                reason=previous_reason,
            )
        try:
            result = await _call_media_llm_once(
                context=context,
                runtime=runtime,
                secrets=secrets,
                messages=messages,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
            )
            return result.text, result.used_secrets
        except Exception as exc:
            setattr(exc, "_media_provider_name", secrets.get("_provider_name", ""))
            setattr(exc, "_media_provider_scope", secrets.get("_provider_scope", ""))
            setattr(exc, "_media_provider_model", secrets.get("model", ""))
            setattr(exc, "_media_provider_max_tokens", _media_llm_max_tokens(secrets, max_tokens))
            last_exc = exc
            if index + 1 < len(candidates) and _is_media_request_failure(exc):
                previous_provider = secrets
                previous_reason = _media_request_failure_reason(exc)
                continue
            raise

    if last_exc is not None:
        raise last_exc
    return "", candidates[0]


async def _call_media_llm_once(
    *,
    context,
    runtime,
    secrets: dict[str, Any],
    messages: list[dict[str, Any]],
    temperature: float,
    top_p: float,
    max_tokens: int,
) -> MediaLLMCallResult:
    cfg = _media_cfg(runtime)
    if cfg is None:
        return MediaLLMCallResult(text="", used_secrets={}, used_path="", finish_reason="", raw_chars=0)

    effective_max_tokens = _media_llm_max_tokens(secrets, max_tokens)
    used_secrets = dict(secrets)
    used_secrets["_effective_max_tokens"] = effective_max_tokens
    request_kwargs = {
        "session": context.http_session,
        "api_base": str(secrets.get("api_base", "") or ""),
        "api_key": str(secrets.get("api_key", "") or ""),
        "model": str(secrets.get("model", "") or ""),
        "messages": messages,
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": effective_max_tokens,
        "timeout_seconds": float(cfg.vision_timeout_seconds),
        "max_retry": int(cfg.vision_max_retry),
        "retry_interval_seconds": float(cfg.vision_retry_interval_seconds),
        "proxy": str(secrets.get("proxy", "") or ""),
        "endpoint_path": str(secrets.get("endpoint_path", "") or runtime.cfg.endpoint_path),
        "extra_payload": dict(secrets.get("_extra_payload") or {}),
    }
    response_data, used_path = await chat_completions_raw_with_fallback_paths(**request_kwargs)
    text = extract_response_content(response_data)
    return MediaLLMCallResult(
        text=text,
        used_secrets=used_secrets,
        used_path=str(used_path or ""),
        finish_reason=extract_response_finish_reason(response_data),
        raw_chars=len(text),
    )


def _parse_detail_analysis_output(
    output: str,
    *,
    resolved: ResolvedMedia,
    prefer_emoji: bool,
) -> MediaAnalysisDraft:
    data, parsed_json = parse_first_json_object_with_status(output)
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
        parsed_json=parsed_json,
    )


def _finalize_media_analysis(
    *,
    detail: MediaAnalysisDraft,
    refined: MediaAnalysisDraft | None,
    resolved: ResolvedMedia,
) -> tuple[RenderedMedia, bool, str]:
    kind = detail.kind
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
    rendered = RenderedMedia(
        media_hash=resolved.media_hash,
        kind=kind,
        description=description,
        emotion_tags=emotion_tags,
        marker=marker,
        cached_path=resolved.cached_path,
    )
    quality = "generic" if _is_low_quality_rendered_media(
        rendered,
        summary_hint=resolved.source_name,
        resolved=resolved,
    ) else "detailed"
    return rendered, used_summary_fallback, quality


def _semantic_retry_reason(
    *,
    detail: MediaAnalysisDraft,
    rendered: RenderedMedia,
    used_summary_fallback: bool,
    resolved: ResolvedMedia,
    finish_reason: str = "",
) -> str:
    if str(finish_reason or "").strip().lower() == "length":
        return "length_truncated"
    if not detail.parsed_json:
        return "invalid_json"
    if used_summary_fallback:
        return "summary_fallback"
    if not str(rendered.description or "").strip():
        return "empty_description"
    if _is_generic_media_label(rendered.description):
        return "generic_description"
    if _is_low_quality_rendered_media(rendered, summary_hint=resolved.source_name, resolved=resolved):
        return "low_quality_render"
    return ""


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
    data = parse_first_json_object(output) or {}
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


def _schedule_background_emoji_refine(
    rendered: RenderedMedia,
    resolved: ResolvedMedia,
    *,
    context,
    runtime,
) -> None:
    if rendered.kind != "emoji" or not _enable_emoji_refine_background(runtime):
        return
    timeout = _emoji_refine_timeout_seconds(runtime)
    if timeout <= 0:
        return

    detail = MediaAnalysisDraft(
        kind="emoji",
        description=rendered.description,
        visible_text="",
        emotion_tags=rendered.emotion_tags,
        raw_output="",
        parsed_json=True,
    )

    async def _run() -> None:
        try:
            refined, refined_provider = await asyncio.wait_for(
                _refine_emoji_analysis_with_llm(
                    detail,
                    resolved=resolved,
                    context=context,
                    runtime=runtime,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            _media_log(
                context,
                runtime,
                step="media.analyze.refine.background.timeout",
                fields={
                    "media_hash": resolved.media_hash[:12],
                    "timeout_seconds": f"{timeout:.3f}",
                },
                level="warning",
            )
            return
        except Exception as exc:
            _media_log(
                context,
                runtime,
                step="media.analyze.refine.background.fail",
                fields={
                    "media_hash": resolved.media_hash[:12],
                    "error": f"{type(exc).__name__}: {exc}",
                },
                level="warning",
            )
            return

        refined_rendered, used_summary_fallback, quality = _finalize_media_analysis(
            detail=detail,
            refined=refined,
            resolved=resolved,
        )
        semantic_reason = _semantic_retry_reason(
            detail=detail,
            rendered=refined_rendered,
            used_summary_fallback=used_summary_fallback,
            resolved=resolved,
        )
        if semantic_reason:
            _media_log(
                context,
                runtime,
                step="media.analyze.refine.background.skip",
                fields={
                    "media_hash": resolved.media_hash[:12],
                    "reason": semantic_reason,
                    "description": refined_rendered.description,
                },
                level="warning",
            )
            return

        write_render_cache_entry(
            context.data_dir,
            resolved,
            refined_rendered,
            source="llm",
            quality=quality,
            prompt_version=_MEDIA_ANALYSIS_PROMPT_VERSION,
        )
        _media_log(
            context,
            runtime,
            step="media.analyze.refine.background.ok",
            fields={
                "provider": refined_provider.get("_provider_name", ""),
                "model": refined_provider.get("model", ""),
                "max_tokens": refined_provider.get("_effective_max_tokens", ""),
                "media_hash": resolved.media_hash[:12],
                "description": refined_rendered.description,
                "emotion_tags": "，".join(refined_rendered.emotion_tags),
                "marker": refined_rendered.marker,
                "quality": quality,
            },
        )

    _media_log(
        context,
        runtime,
        step="media.analyze.refine.background.spawn",
        fields={"media_hash": resolved.media_hash[:12]},
    )
    _spawn_bg_task(context, _run(), name=f"media_refine:{resolved.media_hash[:12]}")


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
    candidates = _resolve_media_llm_secret_candidates(context, runtime)
    primary_secrets = candidates[0] if candidates else {}
    if not any(bool(secrets.get("_vision_enabled")) for secrets in candidates):
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
    complete_candidates = [
        secrets
        for secrets in candidates
        if all(str(secrets.get(field, "") or "").strip() for field in ("api_base", "api_key", "model"))
    ]
    if not complete_candidates:
        _media_log(
            context,
            runtime,
            step="media.analyze.skip",
            fields={
                "reason": "vision_secrets_incomplete",
                "provider": primary_secrets.get("_provider_name", ""),
                "provider_scope": primary_secrets.get("_provider_scope", ""),
                "has_api_base": bool(primary_secrets.get("api_base")),
                "has_api_key": bool(primary_secrets.get("api_key")),
                "has_model": bool(primary_secrets.get("model")),
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
        '格式: {"kind":"image|emoji","description":"...","visible_text":"...","emotion_tags":["..."]}。'
        "description 用简短中文概括图片或表情包内容，优先说出可参与聊天的重点，不要只描述界面、来源、时间或浏览量。"
        "visible_text 摘录图中最关键的清晰文字；如果是新闻截图、聊天截图、梗图或长段文字图，要保留事件主干/笑点/槽点，不必逐字全抄。"
        "如果它更像聊天表情包/梗图/贴纸，kind 填 emoji；普通照片、截图、插画填 image。"
        "emotion_tags 只放 0 到 4 个简短中文情绪或语气标签。"
        "不要输出泛化词，比如“图片”“表情包”“动画表情”“聊天表情包”。"
        "如果图里有清晰文字，description 和 visible_text 里至少要有一个包含这些文字的核心信息。"
    )
    if prefer_emoji:
        prompt += " 这张图来自表情包库，请优先按聊天表情包理解，并尽量提炼出适合聊天使用的情绪标签。"
    if prepared.is_animated and prepared.frame_strategy == "animation_contact_sheet" and prepared.frame_count > 1:
        prompt += (
            f" 这张图是从同一个动画表情里抽取的 {prepared.frame_count} 帧拼图，不是多个人物。"
            " 如果看到相似角色重复出现，要理解成同一角色在不同帧里的动作或表情变化。"
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

    semantic_retry_limit = 1
    previous_provider: dict[str, Any] | None = None
    previous_reason = ""

    for index, provider_secrets in enumerate(complete_candidates):
        if index > 0 and previous_provider is not None:
            _log_media_provider_fallback(
                context=context,
                runtime=runtime,
                from_provider=previous_provider,
                to_provider=provider_secrets,
                max_tokens=_media_detail_base_max_tokens_for_reason(previous_reason),
                reason=previous_reason,
            )

        last_semantic_reason = ""
        for semantic_attempt in range(1, semantic_retry_limit + 2):
            attempt_base_max_tokens = _media_detail_base_max_tokens_for_reason(
                last_semantic_reason or previous_reason
            )
            _media_log(
                context,
                runtime,
                step="media.analyze.start",
                fields={
                    "provider": provider_secrets.get("_provider_name", ""),
                    "provider_scope": provider_secrets.get("_provider_scope", ""),
                    "model": provider_secrets.get("model", ""),
                    "max_tokens": _media_llm_max_tokens(provider_secrets, attempt_base_max_tokens),
                    "media_hash": resolved.media_hash[:12],
                    "segment_type": resolved.segment_type,
                    "source_mime": prepared.source_mime_type,
                    "llm_mime": prepared.mime_type,
                    "transcoded": prepared.transcoded,
                    "animated": prepared.is_animated,
                    "frame_strategy": prepared.frame_strategy,
                    "frame_count": prepared.frame_count,
                    "prefer_emoji": prefer_emoji,
                    "semantic_attempt": semantic_attempt,
                    "semantic_retry_limit": semantic_retry_limit,
                },
            )

            try:
                llm_result = await _call_media_llm_once(
                    context=context,
                    runtime=runtime,
                    secrets=provider_secrets,
                    messages=messages,
                    temperature=0.2,
                    top_p=0.9,
                    max_tokens=attempt_base_max_tokens,
                )
            except Exception as exc:
                setattr(exc, "_media_provider_name", provider_secrets.get("_provider_name", ""))
                setattr(exc, "_media_provider_scope", provider_secrets.get("_provider_scope", ""))
                setattr(exc, "_media_provider_model", provider_secrets.get("model", ""))
                setattr(
                    exc,
                    "_media_provider_max_tokens",
                    _media_llm_max_tokens(provider_secrets, attempt_base_max_tokens),
                )
                if index + 1 < len(complete_candidates) and _is_media_request_failure(exc):
                    previous_provider = provider_secrets
                    previous_reason = _media_request_failure_reason(exc)
                    break
                _media_log(
                    context,
                    runtime,
                    step="media.analyze.fail",
                    fields={
                        "provider": getattr(exc, "_media_provider_name", provider_secrets.get("_provider_name", "")),
                        "model": getattr(exc, "_media_provider_model", provider_secrets.get("model", "")),
                        "media_hash": resolved.media_hash[:12],
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                    level="warning",
                )
                raise

            detail = _parse_detail_analysis_output(
                llm_result.text,
                resolved=resolved,
                prefer_emoji=prefer_emoji,
            )
            _media_log(
                context,
                runtime,
                step="media.analyze.detail.ok",
                fields={
                    "provider": llm_result.used_secrets.get("_provider_name", ""),
                    "model": llm_result.used_secrets.get("model", ""),
                    "max_tokens": llm_result.used_secrets.get("_effective_max_tokens", ""),
                    "used_path": llm_result.used_path,
                    "finish_reason": llm_result.finish_reason,
                    "raw_chars": llm_result.raw_chars,
                    "media_hash": resolved.media_hash[:12],
                    "kind": detail.kind,
                    "description": detail.description,
                    "visible_text": detail.visible_text,
                    "emotion_tags": "，".join(detail.emotion_tags),
                    "parsed_json": detail.parsed_json,
                    "raw_output": detail.raw_output,
                },
            )

            rendered, used_summary_fallback, quality = _finalize_media_analysis(
                detail=detail,
                refined=None,
                resolved=resolved,
            )
            semantic_reason = _semantic_retry_reason(
                detail=detail,
                rendered=rendered,
                used_summary_fallback=used_summary_fallback,
                resolved=resolved,
                finish_reason=llm_result.finish_reason,
            )
            if semantic_reason:
                last_semantic_reason = semantic_reason
                _media_log(
                    context,
                    runtime,
                    step="media.analyze.semantic.invalid",
                    fields={
                        "provider": llm_result.used_secrets.get("_provider_name", ""),
                        "model": llm_result.used_secrets.get("model", ""),
                        "media_hash": resolved.media_hash[:12],
                        "reason": semantic_reason,
                        "semantic_attempt": semantic_attempt,
                        "semantic_retry_limit": semantic_retry_limit,
                        "description": rendered.description,
                        "quality": quality,
                    },
                    level="warning",
                )
                if semantic_attempt <= semantic_retry_limit:
                    _media_log(
                        context,
                        runtime,
                        step="media.analyze.provider_retry",
                        fields={
                            "provider": llm_result.used_secrets.get("_provider_name", ""),
                            "model": llm_result.used_secrets.get("model", ""),
                            "media_hash": resolved.media_hash[:12],
                            "reason": semantic_reason,
                            "semantic_attempt": semantic_attempt,
                            "semantic_retry_limit": semantic_retry_limit,
                        },
                        level="warning",
                    )
                    continue
                if index + 1 < len(complete_candidates):
                    previous_provider = provider_secrets
                    previous_reason = f"semantic_{semantic_reason}"
                    break
                _media_log(
                    context,
                    runtime,
                    step="media.analyze.fail",
                    fields={
                        "provider": llm_result.used_secrets.get("_provider_name", ""),
                        "model": llm_result.used_secrets.get("model", ""),
                        "media_hash": resolved.media_hash[:12],
                        "error": f"semantic_validation_failed:{semantic_reason}",
                    },
                    level="warning",
                )
                return None

            _media_log(
                context,
                runtime,
                step="media.analyze.ok",
                fields={
                    "provider": llm_result.used_secrets.get("_provider_name", ""),
                    "model": llm_result.used_secrets.get("model", ""),
                    "max_tokens": llm_result.used_secrets.get("_effective_max_tokens", ""),
                    "used_path": llm_result.used_path,
                    "finish_reason": llm_result.finish_reason,
                    "raw_chars": llm_result.raw_chars,
                    "media_hash": resolved.media_hash[:12],
                    "kind": rendered.kind,
                    "description": rendered.description,
                    "detail_description": detail.description,
                    "visible_text": detail.visible_text,
                    "refined_description": "",
                    "used_summary_fallback": used_summary_fallback,
                    "marker": rendered.marker,
                    "quality": quality,
                },
            )
            return rendered

    return None
