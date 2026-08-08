"""聊天媒体的视觉分析流水线。

媒体字节和服务商输出都视为不可信：解码前限制读取量，凭据绝不进入渲染或缓存记录，
只有通过语义校验的描述才会提交。服务商降级和语义重试均有序且有界，避免单条消息
无限消耗延迟或 token 预算。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..llm.llm_client import (
    LLMError,
    chat_completions_raw_with_fallback_paths,
    extract_response_content,
    extract_response_finish_reason,
)
from ..media_registry import _quality_score
from ..task_scheduler import _spawn_bg_task
from ..utils.json_parsing import parse_first_json_object, parse_first_json_object_with_status
from .event_media_common import (
    _MEDIA_ANALYSIS_PROMPT_VERSION,
    MediaAnalysisDraft,
    MediaPayloadTooLarge,
    PreparedMediaForLLM,
    RenderedMedia,
    ResolvedMedia,
    _build_marker,
    _can_use_raw_media_description,
    _fallback_kind,
    _inspect_image_payload_details,
    _is_generic_media_label,
    _is_low_quality_rendered_media,
    _media_cfg,
    _media_log,
    _normalize_emotion_tags,
    _normalize_source_label,
    _read_file_bounded,
    _render_animation_contact_sheet,
    _run_media_blocking,
    _safe_source_name,
    _same_rendered_media,
    write_render_cache_entry,
)

_MEDIA_DETAIL_BASE_MAX_TOKENS = 360
_MEDIA_DETAIL_TRUNCATED_RETRY_MAX_TOKENS = 720
_MEDIA_SEMANTIC_RETRY_LIMIT = 1
_VISION_REFUSAL_RE = re.compile(
    r"(?:无法|不能|没法|未能|暂时无法)(?:查看|读取|识别|访问|打开|看到)|"
    r"(?:看不到|没看到|图片缺失|图像缺失|未提供图片|没有提供图片|图片打不开)"
)


def _media_llm_max_tokens(secrets: Mapping[str, Any], base_max_tokens: int) -> int:
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
    return max(0.0, float(cfg.emoji_refine_timeout_seconds))


def _enable_emoji_refine_background(runtime) -> bool:
    return bool(_media_cfg(runtime).enable_emoji_refine_background)


@dataclass(frozen=True)
class MediaLLMCallResult:
    """清洗后的结果，以及审计日志所需的服务商元数据。"""

    text: str
    used_secrets: dict[str, Any]
    used_path: str
    finish_reason: str
    raw_chars: int


@dataclass(frozen=True)
class _PreparedMediaAnalysisRequest:
    prepared: PreparedMediaForLLM
    messages: list[dict[str, Any]]


@dataclass(frozen=True)
class _MediaAnalysisAttempt:
    llm_result: MediaLLMCallResult
    detail: MediaAnalysisDraft
    rendered: RenderedMedia
    used_summary_fallback: bool
    quality: str


@dataclass(frozen=True)
class _MediaProviderOutcome:
    rendered: RenderedMedia | None = None
    fallback_reason: str = ""


def _media_request_failure_reason(exc: Exception) -> str:
    if isinstance(exc, LLMError):
        text = str(exc).strip()
        return text.split(":", 1)[0] if text else "llm_error"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    return type(exc).__name__


def _is_media_request_failure(exc: Exception) -> bool:
    return isinstance(exc, (LLMError, asyncio.TimeoutError, TimeoutError))


def _log_media_provider_fallback(
    *,
    context,
    runtime,
    from_provider: Mapping[str, Any],
    to_provider: Mapping[str, Any],
    max_tokens: int,
    reason: str,
) -> None:
    """审计服务商切换，但不记录凭据或媒体字节。"""

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


def _explicit_media_llm_requested(context) -> bool:
    return bool(_resolve_media_llm_secret_candidates(context)[0].get("_vision_enabled"))


def _resolve_media_llm_secret_candidates(context) -> list[dict[str, Any]]:
    """从 core route 解析只含公开元数据的有序视觉模型链。"""

    empty: dict[str, Any] = {
        "model": "",
        "_ai": None,
        "_route": "vision",
        "_pinned_model": None,
        "_provider_name": "",
        "_provider_scope": "none",
        "_vision_enabled": False,
    }
    capabilities = getattr(context, "capabilities", None)
    ai_service = getattr(capabilities, "ai", None)
    if ai_service is None:
        return [empty]
    try:
        models = ai_service.list_models(
            "vision",
            required_modalities=("text", "image"),
        )
    except Exception:
        return [empty]
    candidates = [
        {
            "model": item.model,
            "_ai": ai_service,
            "_route": "vision",
            "_pinned_model": item.name,
            "_profile": item.name,
            "_provider_name": item.name,
            "_backend_provider": item.provider,
            "_provider_scope": "vision_route" if index == 0 else "vision_fallback",
            "_vision_enabled": True,
        }
        for index, item in enumerate(models)
    ]
    return candidates or [empty]


def _has_media_llm_capability(context) -> bool:
    return _explicit_media_llm_requested(context)


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
    return bool(
        rendered.description == full_source_label
        and rendered.marker
        == _build_marker(rendered.kind, rendered.description, rendered.emotion_tags)
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
) -> bool:
    """判断缓存条目是否早于当前提示词或质量约束。"""

    if not _has_media_llm_capability(context):
        return False

    normalized_source = str(cached_source or "").strip().lower()
    if normalized_source == "llm":
        if cached_quality == "generic" or _is_low_quality_rendered_media(
            cached_rendered,
            summary_hint=summary_hint,
            resolved=resolved,
        ):
            return True
        return cached_prompt_version < _MEDIA_ANALYSIS_PROMPT_VERSION
    if normalized_source == "fallback":
        return True
    return _same_rendered_media(
        cached_rendered, fallback_rendered
    ) or _looks_like_source_placeholder(
        cached_rendered,
        summary_hint=summary_hint,
        resolved=resolved,
    )


def _prepare_media_for_llm(
    resolved: ResolvedMedia,
    payload: bytes | None = None,
) -> PreparedMediaForLLM:
    """把有界字节规范化为服务商安全的静态图或联系表。

    动画采样在保留时间顺序的同时限制帧数和图像尺寸；原始及转换后的 MIME 元数据
    仍保留供审计使用。
    """

    if payload is None:
        payload = resolved.cached_path.read_bytes()
    source_mime = (
        str(
            resolved.mime_type
            or _inspect_image_payload_details(
                payload, fallback_suffix=resolved.cached_path.suffix or ".png"
            ).mime_type
        )
        .split(";", 1)[0]
        .strip()
        .lower()
        or "image/png"
    )
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
        except Exception as exc:
            raise ValueError("image decode or transcode failed") from exc
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
        return f"{desc}，文字内容是“{text}”"
    return f"文字内容是“{text}”"


async def _call_media_llm(
    *,
    context,
    runtime,
    messages: list[dict[str, Any]],
    temperature: float,
    top_p: float,
    max_tokens: int,
) -> tuple[str, dict[str, Any]]:
    """调用统一视觉 route；传输错误的有序 fallback 由 core 完成。"""

    candidates = _resolve_media_llm_secret_candidates(context)
    if not candidates:
        return "", {}

    route_context = dict(candidates[0])
    route_context["_pinned_model"] = None
    result = await _call_media_llm_once(
        runtime=runtime,
        secrets=route_context,
        messages=messages,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
    )
    return result.text, result.used_secrets


async def _call_media_llm_once(
    *,
    runtime,
    secrets: Mapping[str, Any],
    messages: list[dict[str, Any]],
    temperature: float,
    top_p: float,
    max_tokens: int,
) -> MediaLLMCallResult:
    """发起一次有界视觉请求，只保留可安全脱敏的元数据。"""

    cfg = _media_cfg(runtime)
    effective_max_tokens = _media_llm_max_tokens(secrets, max_tokens)
    used_secrets = dict(secrets)
    used_secrets["_effective_max_tokens"] = effective_max_tokens
    response_data, used_profile = await chat_completions_raw_with_fallback_paths(
        secrets=secrets,
        route="vision",
        required_modalities=("text", "image"),
        model=str(secrets.get("model", "") or ""),
        messages=messages,
        temperature=temperature,
        top_p=top_p,
        max_tokens=effective_max_tokens,
        timeout_seconds=float(cfg.vision_timeout_seconds),
        max_retry=int(cfg.vision_max_retry),
        retry_interval_seconds=float(cfg.vision_retry_interval_seconds),
    )
    text = extract_response_content(response_data)
    try:
        models = secrets["_ai"].list_models(
            "vision",
            required_modalities=("text", "image"),
        )
        matched = next((item for item in models if item.name == used_profile), None)
    except Exception:
        matched = None
    if matched is not None:
        used_secrets.update(
            {
                "model": matched.model,
                "_profile": matched.name,
                "_provider_name": matched.name,
                "_backend_provider": matched.provider,
            }
        )
    return MediaLLMCallResult(
        text=text,
        used_secrets=used_secrets,
        used_path=str(used_profile or ""),
        finish_reason=extract_response_finish_reason(response_data),
        raw_chars=len(text),
    )


def _parse_detail_analysis_output(
    output: str,
    *,
    resolved: ResolvedMedia,
    prefer_emoji: bool,
) -> MediaAnalysisDraft:
    """规范化不可信服务商文本，不把畸形 JSON 当作有效事实。"""

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
    """构造确定性渲染结果，并报告是否使用了来源降级。"""

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
        emotion_tags = (
            refined.emotion_tags if refined and refined.emotion_tags else detail.emotion_tags
        )
        if not emotion_tags:
            emotion_tags = _normalize_emotion_tags(description or detail.visible_text)
    else:
        description = detail.description
        if not description:
            description = _safe_source_name(resolved.source_name) or "一张图片"
            used_summary_fallback = True
        emotion_tags = ()

    marker = _build_marker(kind, description, emotion_tags)
    rendered = RenderedMedia(
        media_hash=resolved.media_hash,
        kind=kind,
        description=description,
        emotion_tags=emotion_tags,
        marker=marker,
        cached_path=resolved.cached_path,
    )
    quality = (
        "generic"
        if _is_low_quality_rendered_media(
            rendered,
            summary_hint=resolved.source_name,
            resolved=resolved,
        )
        else "detailed"
    )
    return rendered, used_summary_fallback, quality


def _semantic_retry_reason(
    *,
    detail: MediaAnalysisDraft,
    rendered: RenderedMedia,
    used_summary_fallback: bool,
    resolved: ResolvedMedia,
    finish_reason: str = "",
) -> str:
    """返回稳定的重试代码，优先反映截断和解析完整性问题。"""

    if str(finish_reason or "").strip().lower() == "length":
        return "length_truncated"
    if not detail.parsed_json:
        return "invalid_json"
    if used_summary_fallback:
        return "summary_fallback"
    if not str(rendered.description or "").strip():
        return "empty_description"
    if _VISION_REFUSAL_RE.search(str(rendered.description or "")):
        return "vision_refusal"
    if _is_generic_media_label(rendered.description):
        return "generic_description"
    if _is_low_quality_rendered_media(
        rendered, summary_hint=resolved.source_name, resolved=resolved
    ):
        return "low_quality_render"
    return ""


async def _refine_emoji_analysis_with_llm(
    draft: MediaAnalysisDraft,
    *,
    resolved: ResolvedMedia,
    context,
    runtime,
) -> tuple[MediaAnalysisDraft, dict[str, Any]]:
    """压缩已验证的表情草稿，同时保留可见文字。"""

    prompt = (
        "你要把表情包的详细描述压缩成适合聊天使用的短标签 JSON。"
        "只输出 JSON，不要额外解释。"
        '格式: {"description":"...","emotion_tags":["..."]}。'
        "description 用简短中文保留最有辨识度且能从输入直接支持的主体、动作、表情或可见文字。"
        "不能只复述媒介类型，也不能补写输入中没有的梗义、评价或意图。"
        "emotion_tags 放 1 到 4 个能由可见表情或动作支持的情绪、语气标签；不确定就少写。"
    )
    detail_block = {
        "detailed_description": draft.description,
        "visible_text": draft.visible_text,
        "emotion_tags": list(draft.emotion_tags),
        "source_hint": resolved.source_name,
    }
    messages: list[dict[str, Any]] = [
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
    """在媒体哈希隔离下异步优化已经提交的表情分析。

    当前轮次始终以前台输出为准；延迟结果只能在质量提升时更新同一哈希的缓存条目。
    """

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
                    "error_type": type(exc).__name__,
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
                    "reason_code": semantic_reason,
                    "description": refined_rendered.description,
                },
                level="warning",
            )
            return

        refined_quality = _quality_score(
            {
                "kind": refined_rendered.kind,
                "description": refined_rendered.description,
                "marker": refined_rendered.marker,
                "emotion_tags": refined_rendered.emotion_tags,
                "file_path": str(refined_rendered.cached_path or ""),
            }
        )
        committed_quality = _quality_score(
            {
                "kind": rendered.kind,
                "description": rendered.description,
                "marker": rendered.marker,
                "emotion_tags": rendered.emotion_tags,
                "file_path": str(rendered.cached_path or ""),
            }
        )
        if refined_quality <= committed_quality + 0.05:
            _media_log(
                context,
                runtime,
                step="media.analyze.refine.background.skip",
                fields={
                    "media_hash": resolved.media_hash[:12],
                    "reason_code": "quality_not_improved",
                    "quality": quality,
                },
            )
            return

        await _run_media_blocking(
            write_render_cache_entry,
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


def _resolve_media_analysis_candidates(
    resolved: ResolvedMedia,
    *,
    context,
    runtime,
) -> tuple[Any, list[dict[str, Any]]] | None:
    """验证媒体配置，并按优先级返回不含凭据的模型 profile。"""

    cfg = _media_cfg(runtime)
    candidates = _resolve_media_llm_secret_candidates(context)
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

    complete_candidates = [item for item in candidates if item.get("_ai") is not None]
    if complete_candidates:
        return cfg, complete_candidates

    _media_log(
        context,
        runtime,
        step="media.analyze.skip",
        fields={
            "reason": "vision_route_unavailable",
            "provider": primary_secrets.get("_provider_name", ""),
            "provider_scope": primary_secrets.get("_provider_scope", ""),
            "media_hash": resolved.media_hash[:12],
        },
        level="warning",
    )
    return None


def _build_media_analysis_prompt(
    prepared: PreparedMediaForLLM,
    *,
    prefer_emoji: bool,
) -> str:
    """构造稳定的视觉提示词，并包含来源特定指引。"""

    prompt = (
        "请把这张聊天图片分析成 JSON。"
        "只输出 JSON，不要额外解释。"
        '格式: {"kind":"image|emoji","description":"...","visible_text":"...","emotion_tags":["..."]}。'
        "description 只记录画面中可以直接观察或清晰读取的信息，并区分主体、动作、构图和文字；"
        "不要把审美评价、幽默效果、文化含义、人物动机或聊天意图写成可见事实。"
        "无法确认某种解释时，保留客观细节而不是猜测。"
        "visible_text 摘录最关键的清晰文字；文字较多时保留理解内容所需的主干，不虚构缺失部分。"
        "kind 根据图片在聊天中的实际形式选择 image 或 emoji，不根据你是否理解其含义来选择。"
        "emotion_tags 只放 0 到 4 个能由可见表情、姿态或文字语气直接支持的简短标签。"
        "description 必须包含有辨识度的画面内容，不能只复述媒介类型或文件格式。"
        "如果图里有清晰文字，description 和 visible_text 里至少要有一个包含这些文字的核心信息。"
    )
    if prefer_emoji:
        prompt += (
            " 这张图来自表情包库，请优先按聊天表情包理解，并尽量提炼出适合聊天使用的情绪标签。"
        )
    if (
        prepared.is_animated
        and prepared.frame_strategy == "animation_contact_sheet"
        and prepared.frame_count > 1
    ):
        prompt += (
            f" 这张图是从同一个动画表情里抽取的 {prepared.frame_count} 帧拼图，不是多个人物。"
            " 如果看到相似角色重复出现，要理解成同一角色在不同帧里的动作或表情变化。"
        )
    return prompt


async def _prepare_media_analysis_request(
    resolved: ResolvedMedia,
    *,
    context,
    runtime,
    cfg,
    prefer_emoji: bool,
) -> _PreparedMediaAnalysisRequest | None:
    """在调用服务商前加载、限制、规范化并编码媒体。"""

    prepared, image_b64, payload_size = await _run_media_blocking(
        _load_and_prepare_media_for_llm,
        resolved,
        max_bytes=int(cfg.max_analyze_bytes),
    )
    if prepared is None:
        _media_log(
            context,
            runtime,
            step="media.analyze.skip",
            fields={
                "reason": "media_too_large",
                "bytes": payload_size,
                "limit": int(cfg.max_analyze_bytes),
                "media_hash": resolved.media_hash[:12],
            },
            level="warning",
        )
        return None

    prompt = _build_media_analysis_prompt(prepared, prefer_emoji=prefer_emoji)
    messages: list[dict[str, Any]] = [
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
    return _PreparedMediaAnalysisRequest(prepared=prepared, messages=messages)


def _log_media_analysis_start(
    *,
    context,
    runtime,
    resolved: ResolvedMedia,
    prepared: PreparedMediaForLLM,
    provider_secrets: Mapping[str, Any],
    prefer_emoji: bool,
    semantic_attempt: int,
    attempt_base_max_tokens: int,
) -> None:
    """记录有界请求元数据，同时排除载荷和凭据。"""

    _media_log(
        context,
        runtime,
        step="media.analyze.start",
        fields={
            "provider": provider_secrets.get("_provider_name", ""),
            "provider_scope": provider_secrets.get("_provider_scope", ""),
            "model": provider_secrets.get("model", ""),
            "max_tokens": _media_llm_max_tokens(
                provider_secrets,
                attempt_base_max_tokens,
            ),
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
            "semantic_retry_limit": _MEDIA_SEMANTIC_RETRY_LIMIT,
        },
    )


def _log_media_analysis_exception(
    exc: Exception,
    *,
    context,
    runtime,
    resolved: ResolvedMedia,
    provider_secrets: Mapping[str, Any],
) -> None:
    """记录清洗后的失败标识，绝不记录异常正文或服务商密钥。"""

    _media_log(
        context,
        runtime,
        step="media.analyze.fail",
        fields={
            "provider": provider_secrets.get("_provider_name", ""),
            "model": provider_secrets.get("model", ""),
            "media_hash": resolved.media_hash[:12],
            "error_type": type(exc).__name__,
        },
        level="warning",
    )


async def _run_media_analysis_attempt(
    *,
    context,
    runtime,
    resolved: ResolvedMedia,
    request: _PreparedMediaAnalysisRequest,
    provider_secrets: Mapping[str, Any],
    prefer_emoji: bool,
    semantic_attempt: int,
    attempt_base_max_tokens: int,
) -> _MediaAnalysisAttempt:
    """调用一个服务商一次，并规范化其原始响应。"""

    _log_media_analysis_start(
        context=context,
        runtime=runtime,
        resolved=resolved,
        prepared=request.prepared,
        provider_secrets=provider_secrets,
        prefer_emoji=prefer_emoji,
        semantic_attempt=semantic_attempt,
        attempt_base_max_tokens=attempt_base_max_tokens,
    )
    llm_result = await _call_media_llm_once(
        runtime=runtime,
        secrets=provider_secrets,
        messages=request.messages,
        temperature=0.2,
        top_p=0.9,
        max_tokens=attempt_base_max_tokens,
    )
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
    return _MediaAnalysisAttempt(
        llm_result=llm_result,
        detail=detail,
        rendered=rendered,
        used_summary_fallback=used_summary_fallback,
        quality=quality,
    )


def _log_invalid_media_analysis(
    attempt: _MediaAnalysisAttempt,
    *,
    semantic_reason: str,
    semantic_attempt: int,
    will_retry: bool,
    context,
    runtime,
    resolved: ResolvedMedia,
) -> None:
    """记录语义拒绝，以及有界重试是否继续。"""

    fields = {
        "provider": attempt.llm_result.used_secrets.get("_provider_name", ""),
        "model": attempt.llm_result.used_secrets.get("model", ""),
        "media_hash": resolved.media_hash[:12],
        "reason_code": semantic_reason,
        "semantic_attempt": semantic_attempt,
        "semantic_retry_limit": _MEDIA_SEMANTIC_RETRY_LIMIT,
        "description": attempt.rendered.description,
        "quality": attempt.quality,
    }
    _media_log(
        context,
        runtime,
        step="media.analyze.semantic.invalid",
        fields=fields,
        level="warning",
    )
    if will_retry:
        _media_log(
            context,
            runtime,
            step="media.analyze.provider_retry",
            fields={
                key: value for key, value in fields.items() if key not in {"description", "quality"}
            },
            level="warning",
        )


def _log_terminal_semantic_failure(
    attempt: _MediaAnalysisAttempt,
    *,
    semantic_reason: str,
    context,
    runtime,
    resolved: ResolvedMedia,
) -> None:
    """记录语义重试耗尽，但不持久化原始输出。"""

    _media_log(
        context,
        runtime,
        step="media.analyze.fail",
        fields={
            "provider": attempt.llm_result.used_secrets.get("_provider_name", ""),
            "model": attempt.llm_result.used_secrets.get("model", ""),
            "media_hash": resolved.media_hash[:12],
            "error_type": "semantic_validation_failed",
            "reason_code": semantic_reason,
        },
        level="warning",
    )


async def _commit_media_analysis_attempt(
    attempt: _MediaAnalysisAttempt,
    *,
    context,
    runtime,
    resolved: ResolvedMedia,
) -> RenderedMedia:
    """应用可选增强、写入成功审计并返回结果。"""

    rendered = attempt.rendered
    cultural_hint = ""
    if _should_extract_cultural_hint(rendered, attempt.detail, runtime):
        cultural_hint = await _extract_cultural_hint(
            rendered=rendered,
            detail=attempt.detail,
            resolved=resolved,
            context=context,
            runtime=runtime,
        )
        if cultural_hint:
            rendered = RenderedMedia(
                media_hash=rendered.media_hash,
                kind=rendered.kind,
                description=rendered.description,
                emotion_tags=rendered.emotion_tags,
                marker=rendered.marker,
                cached_path=rendered.cached_path,
                face_id=rendered.face_id,
                cultural_hint=cultural_hint,
            )

    llm_result = attempt.llm_result
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
            "detail_description": attempt.detail.description,
            "visible_text": attempt.detail.visible_text,
            "refined_description": "",
            "used_summary_fallback": attempt.used_summary_fallback,
            "marker": rendered.marker,
            "cultural_hint": cultural_hint,
            "quality": attempt.quality,
        },
    )
    return rendered


async def _analyze_media_with_provider(
    *,
    context,
    runtime,
    resolved: ResolvedMedia,
    request: _PreparedMediaAnalysisRequest,
    provider_secrets: Mapping[str, Any],
    prefer_emoji: bool,
    previous_reason: str,
    has_fallback_provider: bool,
) -> _MediaProviderOutcome:
    """对单个服务商执行有界语义重试，并决定是否降级。"""

    last_semantic_reason = ""
    for semantic_attempt in range(1, _MEDIA_SEMANTIC_RETRY_LIMIT + 2):
        attempt_base_max_tokens = _media_detail_base_max_tokens_for_reason(
            last_semantic_reason or previous_reason
        )
        try:
            attempt = await _run_media_analysis_attempt(
                context=context,
                runtime=runtime,
                resolved=resolved,
                request=request,
                provider_secrets=provider_secrets,
                prefer_emoji=prefer_emoji,
                semantic_attempt=semantic_attempt,
                attempt_base_max_tokens=attempt_base_max_tokens,
            )
        except Exception as exc:
            if has_fallback_provider and _is_media_request_failure(exc):
                return _MediaProviderOutcome(fallback_reason=_media_request_failure_reason(exc))
            _log_media_analysis_exception(
                exc,
                context=context,
                runtime=runtime,
                resolved=resolved,
                provider_secrets=provider_secrets,
            )
            raise

        semantic_reason = _semantic_retry_reason(
            detail=attempt.detail,
            rendered=attempt.rendered,
            used_summary_fallback=attempt.used_summary_fallback,
            resolved=resolved,
            finish_reason=attempt.llm_result.finish_reason,
        )
        if not semantic_reason:
            rendered = await _commit_media_analysis_attempt(
                attempt,
                context=context,
                runtime=runtime,
                resolved=resolved,
            )
            return _MediaProviderOutcome(rendered=rendered)

        last_semantic_reason = semantic_reason
        will_retry = semantic_attempt <= _MEDIA_SEMANTIC_RETRY_LIMIT
        _log_invalid_media_analysis(
            attempt,
            semantic_reason=semantic_reason,
            semantic_attempt=semantic_attempt,
            will_retry=will_retry,
            context=context,
            runtime=runtime,
            resolved=resolved,
        )
        if will_retry:
            continue
        if has_fallback_provider:
            return _MediaProviderOutcome(fallback_reason=f"semantic_{semantic_reason}")

        _log_terminal_semantic_failure(
            attempt,
            semantic_reason=semantic_reason,
            context=context,
            runtime=runtime,
            resolved=resolved,
        )
        return _MediaProviderOutcome()

    return _MediaProviderOutcome()


async def _analyze_media_with_llm(
    resolved: ResolvedMedia,
    *,
    context,
    runtime,
    prefer_emoji: bool,
) -> RenderedMedia | None:
    """通过有界重试和服务商降级分析一个已准备的媒体项。"""

    candidate_setup = _resolve_media_analysis_candidates(
        resolved,
        context=context,
        runtime=runtime,
    )
    if candidate_setup is None:
        return None
    cfg, complete_candidates = candidate_setup

    request = await _prepare_media_analysis_request(
        resolved,
        context=context,
        runtime=runtime,
        cfg=cfg,
        prefer_emoji=prefer_emoji,
    )
    if request is None:
        return None

    previous_provider: dict[str, Any] | None = None
    previous_reason = ""
    for index, provider_secrets in enumerate(complete_candidates):
        if previous_provider is not None:
            _log_media_provider_fallback(
                context=context,
                runtime=runtime,
                from_provider=previous_provider,
                to_provider=provider_secrets,
                max_tokens=_media_detail_base_max_tokens_for_reason(previous_reason),
                reason=previous_reason,
            )

        outcome = await _analyze_media_with_provider(
            context=context,
            runtime=runtime,
            resolved=resolved,
            request=request,
            provider_secrets=provider_secrets,
            prefer_emoji=prefer_emoji,
            previous_reason=previous_reason,
            has_fallback_provider=index + 1 < len(complete_candidates),
        )
        if outcome.rendered is not None:
            return outcome.rendered
        if not outcome.fallback_reason:
            return None
        previous_provider = provider_secrets
        previous_reason = outcome.fallback_reason

    return None


def _load_and_prepare_media_for_llm(
    resolved: ResolvedMedia,
    *,
    max_bytes: int,
) -> tuple[PreparedMediaForLLM | None, str, int]:
    """只读取一次，执行字节上限后再解码、转码和编码。"""

    try:
        payload = _read_file_bounded(resolved.cached_path, max_bytes=max_bytes)
    except MediaPayloadTooLarge as exc:
        return None, "", exc.size
    prepared = _prepare_media_for_llm(resolved, payload)
    image_b64 = base64.b64encode(prepared.payload).decode("ascii")
    return prepared, image_b64, len(payload)


def _should_extract_cultural_hint(
    rendered: RenderedMedia,
    detail: MediaAnalysisDraft,
    runtime,
) -> bool:
    cfg = _media_cfg(runtime)
    if not cfg.enable_meme_cultural_hint:
        return False
    if rendered.kind != "emoji":
        return False
    visible_text = str(getattr(detail, "visible_text", "") or "").strip()
    description = str(rendered.description or "").strip()
    return bool(visible_text) or "“" in description or '"' in description


async def _extract_cultural_hint(
    *,
    rendered: RenderedMedia,
    detail: MediaAnalysisDraft,
    resolved: ResolvedMedia,
    context,
    runtime,
) -> str:
    """在独立超时和输出上限内请求可选的简短梗背景。"""

    cfg = _media_cfg(runtime)
    timeout = max(1.0, cfg.meme_cultural_hint_timeout_seconds)

    visible_text = str(getattr(detail, "visible_text", "") or "").strip()
    description = str(rendered.description or "").strip()

    payload = {
        "description": description,
        "visible_text": visible_text,
        "emotion_tags": list(rendered.emotion_tags),
    }
    prompt = (
        "下面是一张聊天媒体的客观描述和可见文字。"
        "只有这些证据足以唯一识别一个稳定的文化引用时，才用一句不超过 24 字的中文说明其来源或常见用法。"
        "证据不足、存在多个可能解释或只是普通画面时，hint 必须为空；不要根据相似印象补全。"
        '只输出 JSON：{"hint":"..."}。\n输入：' + json.dumps(payload, ensure_ascii=False)
    )
    messages = [
        {"role": "system", "content": "你是中文网络梗解读器，只输出 JSON。"},
        {"role": "user", "content": prompt},
    ]

    try:
        output, used_secrets = await asyncio.wait_for(
            _call_media_llm(
                context=context,
                runtime=runtime,
                messages=messages,
                temperature=0.2,
                top_p=0.9,
                max_tokens=80,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        _media_log(
            context,
            runtime,
            step="media.cultural_hint.timeout",
            fields={
                "media_hash": resolved.media_hash[:12],
                "timeout_seconds": f"{timeout:.3f}",
            },
            level="warning",
        )
        return ""
    except Exception as exc:
        _media_log(
            context,
            runtime,
            step="media.cultural_hint.fail",
            fields={
                "media_hash": resolved.media_hash[:12],
                "error_type": type(exc).__name__,
            },
            level="warning",
        )
        return ""

    data = parse_first_json_object(output) or {}
    hint = str(data.get("hint", "") or "").strip()
    if not hint:
        return ""
    if len(hint) > 60:
        hint = hint[:60].rstrip() + "…"
    refusal_tokens = ("不知道", "不确定", "无法", "无内容", "无梗", "需要更多")
    if any(token in hint for token in refusal_tokens):
        return ""
    _media_log(
        context,
        runtime,
        step="media.cultural_hint.ok",
        fields={
            "provider": used_secrets.get("_provider_name", ""),
            "model": used_secrets.get("model", ""),
            "media_hash": resolved.media_hash[:12],
            "hint": hint,
        },
    )
    return hint
