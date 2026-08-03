"""xiaoqing_chat 到 core AI capability 的薄适配层。

插件内部仍然可以针对主回复、规划、记忆和视觉任务设置不同的温度与 token 预算，
但 provider 凭据、模型链和跨模型 fallback 全部由 core route 统一处理。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from core.ai import AICompletionResult, AIConfigError
from core.interfaces import AICapability


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
    route: str = "chat",
    required_modalities: tuple[str, ...] = ("text",),
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    timeout_seconds: float | None = None,
    total_timeout_seconds: float | None = None,
    max_retry: int | None = None,
    retry_interval_seconds: float | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    extra_payload: Mapping[str, Any] | None = None,
) -> AICompletionResult:
    """调用一个统一 route，并保留 raw response 供工具调用和复杂解析使用。"""

    service = _service(secrets)
    return await service.complete(
        _route(secrets, route),
        messages,
        required_modalities=required_modalities,
        pinned_model=_pinned_model(secrets),
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        total_timeout_seconds=total_timeout_seconds,
        max_retry=max_retry,
        retry_interval_seconds=retry_interval_seconds,
        tools=tools,
        tool_choice=tool_choice,
        extra_payload=extra_payload,
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

    # ``model`` 只供旧的测试/日志钩子读取；真实模型由 route 决定，插件不能覆盖。
    kwargs.pop("model", None)
    result = await complete_raw(**kwargs)
    return result.response, result.profile


__all__ = [
    "chat_completions",
    "chat_completions_raw_with_fallback_paths",
    "chat_completions_with_fallback_paths",
    "complete_raw",
]
