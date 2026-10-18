"""xiaoqing_chat 到 core AI capability 的薄适配层。

插件内部仍然可以针对主回复、规划、记忆和视觉任务设置不同的温度与 token 预算，
但 provider 凭据、模型链和跨模型 fallback 全部由 core route 统一处理。本模块同时
提供 OpenAI 兼容响应的文本与结束原因提取。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from core.ai import AICompletionResult, AIConfigError
from core.ai import AIError as LLMError
from core.interfaces import AICapability

from ..utils.json_parsing import normalize_response_content


def _service(secrets: Mapping[str, Any]) -> AICapability:
    service = secrets.get("_ai")
    if service is None or not callable(getattr(service, "complete", None)):
        raise AIConfigError("xiaoqing_chat AI capability is unavailable")
    # ``secrets`` 是插件边界上的动态映射；运行时校验后再恢复 core 的结构化协议类型。
    return cast(AICapability, service)


def _route(secrets: Mapping[str, Any], default: str) -> str:
    value = str(secrets.get("_route") or default).strip()
    if not value:
        raise AIConfigError("xiaoqing_chat AI route is unavailable")
    return value


def _pinned_model(secrets: Mapping[str, Any]) -> str | None:
    value = secrets.get("_pinned_model")
    return str(value).strip() if isinstance(value, str) and value.strip() else None


async def complete_raw(
    *,
    secrets: Mapping[str, Any],
    messages: list[dict[str, Any]],
    route: str                              = "chat",
    required_modalities: tuple[str, ...]    = ("text",),
    temperature: float | None               = None,
    top_p: float | None                     = None,
    max_tokens: int | None                  = None,
    timeout_seconds: float | None           = None,
    total_timeout_seconds: float | None     = None,
    max_retry: int | None                   = None,
    retry_interval_seconds: float | None    = None,
    tools: list[dict[str, Any]] | None      = None,
    tool_choice: Any                        = None,
    extra_payload: Mapping[str, Any] | None = None,
) -> AICompletionResult:
    """调用一个统一 route，并保留 raw response 供工具调用和复杂解析使用。"""

    service = _service(secrets)
    return await service.complete(
        _route(secrets, route),
        messages,
        required_modalities    = required_modalities,
        pinned_model           = _pinned_model(secrets),
        temperature            = temperature,
        top_p                  = top_p,
        max_tokens             = max_tokens,
        timeout_seconds        = timeout_seconds,
        total_timeout_seconds  = total_timeout_seconds,
        max_retry              = max_retry,
        retry_interval_seconds = retry_interval_seconds,
        tools                  = tools,
        tool_choice            = tool_choice,
        extra_payload          = extra_payload,
    )


async def chat_completions(
    **kwargs: Any,
) -> str:
    """返回文本内容。"""

    result: AICompletionResult = await complete_raw(**kwargs)
    return result.content


async def chat_completions_with_fallback_paths(
    **kwargs: Any,
) -> tuple[str, str]:
    """返回文本与实际命中的模型 profile。"""

    result: AICompletionResult = await complete_raw(**kwargs)
    return result.content, result.profile


async def chat_completions_raw_with_fallback_paths(
    **kwargs: Any,
) -> tuple[dict[str, Any], str]:
    """返回原始响应及实际命中的模型 profile。"""

    result = await complete_raw(**kwargs)
    return result.response, result.profile


def extract_response_content(data: dict[str, Any]) -> str:
    """提取并规范化第一段助手文本。"""

    content = normalize_response_content(data)
    return content if isinstance(content, str) else ""


def extract_response_finish_reason(data: dict[str, Any]) -> str:
    """提取第一项 choice 的结束原因，畸形响应或缺失时返回空串。"""

    choices = data.get("choices") or []
    choice  = choices[0] if isinstance(choices, list) and choices else None
    return str(choice.get("finish_reason") or "").strip() if isinstance(choice, dict) else ""


__all__ = [
    "LLMError",
    "chat_completions",
    "chat_completions_raw_with_fallback_paths",
    "chat_completions_with_fallback_paths",
    "complete_raw",
    "extract_response_content",
    "extract_response_finish_reason",
]
