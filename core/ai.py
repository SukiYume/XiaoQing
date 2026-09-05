"""统一的 LLM/VLM 配置解析、路由与有界请求。

模型连接信息由 core 持有。插件只通过声明过的 route 发起调用，既不需要复制
provider 凭据，也无法读取其他插件或其他 route 的密钥。route 中的模型名按顺序
组成调用链：第一个是主模型，其余只在明确允许的故障类型下依次降级。
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from .bounded_http import (
    BodyLimits,
    BoundedHttpError,
    HttpStatusError,
    JsonLimits,
    MimePolicy,
    aiohttp_request_bounded,
    parse_bounded_json,
)
from .config import materialize_snapshot_value

logger = logging.getLogger(__name__)

_NAME_RE               = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_RESERVED_PAYLOAD_KEYS = frozenset(
    {
        "model",
        "messages",
        "stream",
        "temperature",
        "top_p",
        "max_tokens",
        "tools",
        "tool_choice",
    }
)
_SUPPORTED_MODALITIES = frozenset({"text", "image", "audio"})
_DEFAULT_FALLBACK_ON  = frozenset(
    {
        "transport",
        "timeout",
        "rate_limit",
        "server_error",
        "model_unavailable",
        "invalid_response",
        "empty_response",
    }
)
_SUPPORTED_FALLBACK_ON = _DEFAULT_FALLBACK_ON | frozenset(
    {
        "request_timeout",
        "conflict",
    }
)
_AI_BODY_LIMITS = BodyLimits(
    max_wire_bytes    = 2 * 1024 * 1024,
    max_decoded_bytes = 4 * 1024 * 1024,
)
_AI_JSON_LIMITS = JsonLimits(max_bytes=_AI_BODY_LIMITS.max_decoded_bytes)
_AI_JSON_MIME = MimePolicy(
    exact               = frozenset({"application/json"}),
    structured_suffixes = frozenset({"+json"}),
    allow_missing       = True,
)


class AIError(RuntimeError):
    """统一 AI 层的稳定公开异常基类。"""


class AIConfigError(AIError):
    """AI registry、route 或凭据配置无效。"""


class AIRequestError(AIError):
    """远程模型请求失败；异常文本不包含 URL、密钥或响应正文。"""

    def __init__(self, category: str, *, status: int | None = None) -> None:
        self.category = str(category)
        self.status   = int(status) if status is not None else None
        super().__init__(f"ai_{self.category}")


@dataclass(frozen=True, slots=True)
class AIModelInfo:
    """可安全暴露给插件和管理命令的模型元数据。"""

    name: str
    provider: str
    model: str
    modalities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AICompletionResult:
    """一次成功补全的响应及其脱敏路由元数据。"""

    response: dict[str, Any]
    profile: str
    provider: str
    model: str
    finish_reason: str
    attempts: int

    @property
    def content(self) -> str:
        """返回常见 Chat Completions 文本内容；复杂内容仍可读取 response。"""

        return _message_text(_response_message(self.response))


@dataclass(frozen=True, slots=True)
class _AIModelTarget:
    name: str
    provider: str
    model: str
    api_base: str
    api_key: str
    endpoint_path: str
    proxy: str
    modalities: tuple[str, ...]
    request_defaults: dict[str, Any]

    @property
    def url(self) -> str:
        return f"{self.api_base.rstrip('/')}/{self.endpoint_path.lstrip('/')}"


@dataclass(frozen=True, slots=True)
class _AIRoute:
    plugin: str
    name: str
    model_names: tuple[str, ...]
    temperature: float | None
    top_p: float | None
    max_tokens: int | None
    timeout_seconds: float
    total_timeout_seconds: float
    max_retry: int
    retry_interval_seconds: float
    fallback_on: frozenset[str]
    request_defaults: dict[str, Any]


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AIConfigError(f"{label} must be an object")
    return value


def _named_mapping(value: Any, label: str) -> Mapping[str, Any]:
    mapping = _mapping(value, label)
    for name in mapping:
        if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
            raise AIConfigError(f"{label} contains an invalid name")
    return mapping


def _required_string(mapping: Mapping[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AIConfigError(f"{label}.{key} must be a non-empty string")
    return value.strip()


def _optional_string(
    mapping: Mapping[str, Any],
    key: str,
    *,
    default: str = "",
    label: str,
) -> str:
    value = mapping.get(key, default)
    if not isinstance(value, str):
        raise AIConfigError(f"{label}.{key} must be a string")
    return value.strip()


def _bounded_float(
    value: Any,
    *,
    label: str,
    default: float | None,
    minimum: float,
    maximum: float,
) -> float | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AIConfigError(f"{label} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or not minimum <= normalized <= maximum:
        raise AIConfigError(f"{label} is outside the supported range")
    return normalized


def _bounded_int(
    value: Any,
    *,
    label: str,
    default: int | None,
    minimum: int,
    maximum: int,
) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise AIConfigError(f"{label} must be an integer")
    if not minimum <= value <= maximum:
        raise AIConfigError(f"{label} is outside the supported range")
    return value


def _payload_mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    mapping                 = _mapping(value, label)
    payload: dict[str, Any] = {}
    for key, item in mapping.items():
        if not isinstance(key, str) or not key or key in _RESERVED_PAYLOAD_KEYS:
            raise AIConfigError(f"{label} contains a reserved or invalid key")
        payload[key] = materialize_snapshot_value(item)
    return payload


def _modalities(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise AIConfigError(f"{label} must be a non-empty list")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in _SUPPORTED_MODALITIES:
            raise AIConfigError(f"{label} contains an unsupported modality")
        if item not in normalized:
            normalized.append(item)
    return tuple(normalized)


def _model_names(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise AIConfigError(f"{label} must be a non-empty list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or _NAME_RE.fullmatch(item) is None:
            raise AIConfigError(f"{label} contains an invalid model profile")
        if item in result:
            raise AIConfigError(f"{label} contains a duplicate model profile")
        result.append(item)
    return tuple(result)


def _fallback_categories(value: Any, label: str) -> frozenset[str]:
    if value is None:
        return _DEFAULT_FALLBACK_ON
    if not isinstance(value, (list, tuple)):
        raise AIConfigError(f"{label} must be a list")
    normalized = frozenset(str(item) for item in value)
    if not normalized <= _SUPPORTED_FALLBACK_ON:
        raise AIConfigError(f"{label} contains an unsupported category")
    return normalized


def _absolute_http_base(value: str, label: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise AIConfigError(f"{label} must be an absolute HTTP(S) URL")
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise AIConfigError(f"{label} must not contain credentials, query, or fragment")
    return value.rstrip("/")


def _proxy_url(value: str, label: str) -> str:
    if not value:
        return ""
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise AIConfigError(f"{label} must be an absolute HTTP(S) proxy URL")
    return value


def _route_config(
    config: Mapping[str, Any],
    plugin_name: str,
    route_name: str,
) -> _AIRoute:
    plugins       = _mapping(config.get("plugins", {}), "config.plugins")
    plugin_config = _mapping(plugins.get(plugin_name, {}), f"config.plugins.{plugin_name}")
    ai_config     = _mapping(plugin_config.get("ai", {}), f"config.plugins.{plugin_name}.ai")
    routes        = _named_mapping(
        ai_config.get("routes", {}),
        f"config.plugins.{plugin_name}.ai.routes",
    )
    if route_name not in routes:
        raise AIConfigError(f"AI route is not configured: {plugin_name}.{route_name}")
    label   = f"config.plugins.{plugin_name}.ai.routes.{route_name}"
    route   = _mapping(routes[route_name], label)
    timeout = _bounded_float(
        route.get("timeout_seconds"),
        label   = f"{label}.timeout_seconds",
        default = 30.0,
        minimum = 0.1,
        maximum = 600.0,
    )
    assert timeout is not None
    total_timeout = _bounded_float(
        route.get("total_timeout_seconds"),
        label   = f"{label}.total_timeout_seconds",
        default = max(timeout, 60.0),
        minimum = 0.1,
        maximum = 1800.0,
    )
    assert total_timeout is not None
    max_retry = _bounded_int(
        route.get("max_retry"),
        label   = f"{label}.max_retry",
        default = 1,
        minimum = 0,
        maximum = 10,
    )
    assert max_retry is not None
    retry_interval = _bounded_float(
        route.get("retry_interval_seconds"),
        label   = f"{label}.retry_interval_seconds",
        default = 0.5,
        minimum = 0.0,
        maximum = 60.0,
    )
    assert retry_interval is not None
    return _AIRoute(
        plugin      = plugin_name,
        name        = route_name,
        model_names = _model_names(route.get("models"), f"{label}.models"),
        temperature = _bounded_float(
            route.get("temperature"),
            label   = f"{label}.temperature",
            default = None,
            minimum = 0.0,
            maximum = 2.0,
        ),
        top_p=_bounded_float(
            route.get("top_p"),
            label   = f"{label}.top_p",
            default = None,
            minimum = 0.0,
            maximum = 1.0,
        ),
        max_tokens=_bounded_int(
            route.get("max_tokens"),
            label   = f"{label}.max_tokens",
            default = None,
            minimum = 1,
            maximum = 1_000_000,
        ),
        timeout_seconds=timeout,
        total_timeout_seconds=total_timeout,
        max_retry=max_retry,
        retry_interval_seconds=retry_interval,
        fallback_on=_fallback_categories(route.get("fallback_on"), f"{label}.fallback_on"),
        request_defaults=_payload_mapping(
            route.get("request_defaults"),
            f"{label}.request_defaults",
        ),
    )


def _model_targets(
    config: Mapping[str, Any],
    secrets: Mapping[str, Any],
    route: _AIRoute,
    *,
    pinned_model: str | None,
    required_modalities: tuple[str, ...],
) -> tuple[_AIModelTarget, ...]:
    public_ai        = _mapping(config.get("ai", {}), "config.ai")
    providers        = _named_mapping(public_ai.get("providers", {}), "config.ai.providers")
    models           = _named_mapping(public_ai.get("models", {}), "config.ai.models")
    secret_ai        = _mapping(secrets.get("ai", {}), "secrets.ai")
    secret_providers = _named_mapping(
        secret_ai.get("providers", {}),
        "secrets.ai.providers",
    )

    names = route.model_names
    if pinned_model is not None:
        if not isinstance(pinned_model, str) or pinned_model not in names:
            raise AIConfigError("pinned model is not allowed by this route")
        names = (pinned_model,)

    required = set(required_modalities)
    if not required or not required <= _SUPPORTED_MODALITIES:
        raise AIConfigError("required_modalities contains an unsupported value")

    targets: list[_AIModelTarget] = []
    for profile_name in names:
        label         = f"config.ai.models.{profile_name}"
        model_config  = _mapping(models.get(profile_name), label)
        provider_name = _required_string(model_config, "provider", label)
        if _NAME_RE.fullmatch(provider_name) is None:
            raise AIConfigError(f"{label}.provider is invalid")
        provider_label  = f"config.ai.providers.{provider_name}"
        provider_config = _mapping(providers.get(provider_name), provider_label)
        provider_secret = _mapping(
            secret_providers.get(provider_name),
            f"secrets.ai.providers.{provider_name}",
        )
        model_modalities = _modalities(model_config.get("modalities"), f"{label}.modalities")
        if not required <= set(model_modalities):
            raise AIConfigError(
                f"model profile {profile_name} does not support the requested modalities"
            )
        endpoint_path = _optional_string(
            provider_config,
            "endpoint_path",
            default = "/chat/completions",
            label   = provider_label,
        )
        if not endpoint_path.startswith("/") or "?" in endpoint_path or "#" in endpoint_path:
            raise AIConfigError(f"{provider_label}.endpoint_path must be an absolute URL path")
        targets.append(
            _AIModelTarget(
                name     = profile_name,
                provider = provider_name,
                model    = _required_string(model_config, "model", label),
                api_base = _absolute_http_base(
                    _required_string(provider_config, "api_base", provider_label),
                    f"{provider_label}.api_base",
                ),
                api_key=_required_string(
                    provider_secret,
                    "api_key",
                    f"secrets.ai.providers.{provider_name}",
                ),
                endpoint_path = endpoint_path,
                proxy         = _proxy_url(
                    _optional_string(
                        provider_config,
                        "proxy",
                        label=provider_label,
                    ),
                    f"{provider_label}.proxy",
                ),
                modalities       = model_modalities,
                request_defaults = _payload_mapping(
                    model_config.get("request_defaults"),
                    f"{label}.request_defaults",
                ),
            )
        )
    return tuple(targets)


def list_configured_models(
    *,
    config: Mapping[str, Any],
    secrets: Mapping[str, Any],
    plugin_name: str,
    route_name: str,
    required_modalities: tuple[str, ...] = ("text",),
) -> tuple[AIModelInfo, ...]:
    """解析并验证 route，返回不含凭据的有序模型列表。"""

    route   = _route_config(config, plugin_name, route_name)
    targets = _model_targets(
        config,
        secrets,
        route,
        pinned_model        = None,
        required_modalities = required_modalities,
    )
    return tuple(
        AIModelInfo(
            name       = target.name,
            provider   = target.provider,
            model      = target.model,
            modalities = target.modalities,
        )
        for target in targets
    )


def _response_message(data: Mapping[str, Any]) -> Mapping[str, Any]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        return {}
    message = choices[0].get("message")
    return message if isinstance(message, Mapping) else {}


def _finish_reason(data: Mapping[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        return ""
    value = choices[0].get("finish_reason")
    return str(value or "").strip()


def _message_text(message: Mapping[str, Any]) -> str:
    """Normalize exactly the text that :class:`AICompletionResult` exposes."""

    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        parts = [
            text
            for item in content
            if isinstance(item, Mapping)
            and isinstance(text := item.get("text"), str)
            and text.strip()
        ]
        return "\n".join(parts).strip()
    return ""


def _has_tool_call(message: Mapping[str, Any]) -> bool:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return False
    for call in tool_calls:
        if not isinstance(call, Mapping):
            continue
        function = call.get("function")
        if isinstance(function, Mapping):
            name = function.get("name")
            if isinstance(name, str) and name.strip():
                return True
        if any(isinstance(call.get(key), str) and str(call[key]).strip() for key in ("id", "type")):
            return True
    return False


def _has_completion(data: Mapping[str, Any]) -> bool:
    message = _response_message(data)
    return bool(_message_text(message)) or _has_tool_call(message)


def _status_error(status: int) -> AIRequestError:
    if status == 404:
        return AIRequestError("model_unavailable", status=status)
    if status == 408:
        return AIRequestError("request_timeout", status=status)
    if status == 409:
        return AIRequestError("conflict", status=status)
    if status in {425, 429}:
        return AIRequestError("rate_limit", status=status)
    if 500 <= status <= 599:
        return AIRequestError("server_error", status=status)
    if status in {401, 403}:
        return AIRequestError("authentication", status=status)
    if status == 413:
        return AIRequestError("request_too_large", status=status)
    return AIRequestError("invalid_request", status=status)


def _retryable(category: str) -> bool:
    return category in {
        "transport",
        "timeout",
        "request_timeout",
        "conflict",
        "rate_limit",
        "server_error",
        "invalid_response",
        "empty_response",
    }


def _retry_delay(base_seconds: float, retry_index: int) -> float:
    # mypy 对 ``int ** int`` 的返回类型保守为 Any；显式收口为 API 承诺的秒数浮点值。
    return float(max(0.0, base_seconds) * (2 ** max(0, retry_index - 1)))


def _request_payload(
    target: _AIModelTarget,
    route: _AIRoute,
    messages: list[dict[str, Any]],
    *,
    temperature: float | None,
    top_p: float | None,
    max_tokens: int | None,
    tools: list[dict[str, Any]] | None,
    tool_choice: Any,
    extra_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": target.model,
        "messages": materialize_snapshot_value(messages),
        "stream": False,
    }
    payload.update(target.request_defaults)
    payload.update(route.request_defaults)
    effective_temperature = route.temperature if temperature is None else temperature
    effective_top_p       = route.top_p if top_p is None else top_p
    effective_max_tokens  = route.max_tokens if max_tokens is None else max_tokens
    if effective_temperature is not None:
        payload["temperature"] = effective_temperature
    if effective_top_p is not None:
        payload["top_p"] = effective_top_p
    if effective_max_tokens is not None:
        payload["max_tokens"] = effective_max_tokens
    if tools:
        payload["tools"] = materialize_snapshot_value(tools)
    if tool_choice is not None:
        payload["tool_choice"] = materialize_snapshot_value(tool_choice)
    if extra_payload is not None:
        payload.update(_payload_mapping(extra_payload, "AI request extra_payload"))
    return payload


async def _request_target(
    *,
    session: aiohttp.ClientSession,
    target: _AIModelTarget,
    route: _AIRoute,
    messages: list[dict[str, Any]],
    temperature: float | None,
    top_p: float | None,
    max_tokens: int | None,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    tools: list[dict[str, Any]] | None,
    tool_choice: Any,
    extra_payload: Mapping[str, Any] | None,
    attempt_counter: list[int],
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {target.api_key}",
        "Content-Type": "application/json",
    }
    payload = _request_payload(
        target,
        route,
        messages,
        temperature   = temperature,
        top_p         = top_p,
        max_tokens    = max_tokens,
        tools         = tools,
        tool_choice   = tool_choice,
        extra_payload = extra_payload,
    )
    for retry_index in range(max_retry + 1):
        attempt_counter[0] += 1
        request_kwargs: dict[str, Any] = {
            "json": payload,
            "timeout": aiohttp.ClientTimeout(total=timeout_seconds),
        }
        if target.proxy:
            request_kwargs["proxy"] = target.proxy
        try:
            response = await aiohttp_request_bounded(
                session,
                "POST",
                target.url,
                limits         = _AI_BODY_LIMITS,
                mime_policy    = _AI_JSON_MIME,
                headers        = headers,
                request_kwargs = request_kwargs,
            )
            data = parse_bounded_json(response, limits=_AI_JSON_LIMITS)
            if not isinstance(data, dict):
                raise AIRequestError("invalid_response")
            if not _has_completion(data):
                raise AIRequestError("empty_response")
            return data
        except HttpStatusError as exc:
            error = _status_error(exc.status)
        except AIRequestError as exc:
            error = exc
        except TimeoutError:
            error = AIRequestError("timeout")
        except aiohttp.ClientError:
            error = AIRequestError("transport")
        except BoundedHttpError:
            error = AIRequestError("invalid_response")

        if retry_index >= max_retry or not _retryable(error.category):
            raise error
        delay = _retry_delay(retry_interval_seconds, retry_index + 1)
        if delay:
            await asyncio.sleep(delay)
    raise AssertionError("unreachable AI retry loop")


async def _complete_route(
    *,
    session: aiohttp.ClientSession,
    route: _AIRoute,
    targets: tuple[_AIModelTarget, ...],
    messages: list[dict[str, Any]],
    temperature: float | None,
    top_p: float | None,
    max_tokens: int | None,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    tools: list[dict[str, Any]] | None,
    tool_choice: Any,
    extra_payload: Mapping[str, Any] | None,
) -> AICompletionResult:
    last_error: AIRequestError | None = None
    attempt_counter                   = [0]
    for index, target in enumerate(targets):
        try:
            response = await _request_target(
                session                = session,
                target                 = target,
                route                  = route,
                messages               = messages,
                temperature            = temperature,
                top_p                  = top_p,
                max_tokens             = max_tokens,
                timeout_seconds        = timeout_seconds,
                max_retry              = max_retry,
                retry_interval_seconds = retry_interval_seconds,
                tools                  = tools,
                tool_choice            = tool_choice,
                extra_payload          = extra_payload,
                attempt_counter        = attempt_counter,
            )
            return AICompletionResult(
                response      = response,
                profile       = target.name,
                provider      = target.provider,
                model         = str(response.get("model") or target.model),
                finish_reason = _finish_reason(response),
                attempts      = attempt_counter[0],
            )
        except AIRequestError as exc:
            last_error   = exc
            has_fallback = index + 1 < len(targets)
            if not has_fallback or exc.category not in route.fallback_on:
                raise
            logger.warning(
                "AI route fallback plugin=%s route=%s from_profile=%s to_profile=%s reason=%s",
                route.plugin,
                route.name,
                target.name,
                targets[index + 1].name,
                exc.category,
            )
    raise last_error or AIRequestError("route_failed")


async def complete_configured_route(
    *,
    session: aiohttp.ClientSession,
    config: Mapping[str, Any],
    secrets: Mapping[str, Any],
    plugin_name: str,
    route_name: str,
    messages: list[dict[str, Any]],
    required_modalities: tuple[str, ...]    = ("text",),
    pinned_model: str | None                = None,
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
    """按插件 route 完成一次有界调用，并在允许的错误上按序降级。"""

    if not isinstance(messages, list) or not messages:
        raise AIConfigError("messages must be a non-empty list")
    if any(not isinstance(message, dict) for message in messages):
        raise AIConfigError("each message must be an object")
    route   = _route_config(config, plugin_name, route_name)
    targets = _model_targets(
        config,
        secrets,
        route,
        pinned_model        = pinned_model,
        required_modalities = required_modalities,
    )
    effective_timeout = _bounded_float(
        timeout_seconds,
        label   = "AI request timeout_seconds",
        default = route.timeout_seconds,
        minimum = 0.1,
        maximum = 600.0,
    )
    effective_total_timeout = _bounded_float(
        total_timeout_seconds,
        label   = "AI request total_timeout_seconds",
        default = route.total_timeout_seconds,
        minimum = 0.1,
        maximum = 1800.0,
    )
    effective_retry = _bounded_int(
        max_retry,
        label   = "AI request max_retry",
        default = route.max_retry,
        minimum = 0,
        maximum = 10,
    )
    effective_retry_interval = _bounded_float(
        retry_interval_seconds,
        label   = "AI request retry_interval_seconds",
        default = route.retry_interval_seconds,
        minimum = 0.0,
        maximum = 60.0,
    )
    effective_temperature = _bounded_float(
        temperature,
        label   = "AI request temperature",
        default = None,
        minimum = 0.0,
        maximum = 2.0,
    )
    effective_top_p = _bounded_float(
        top_p,
        label   = "AI request top_p",
        default = None,
        minimum = 0.0,
        maximum = 1.0,
    )
    effective_max_tokens = _bounded_int(
        max_tokens,
        label   = "AI request max_tokens",
        default = None,
        minimum = 1,
        maximum = 1_000_000,
    )
    assert effective_timeout is not None
    assert effective_total_timeout is not None
    assert effective_retry is not None
    assert effective_retry_interval is not None
    try:
        return await asyncio.wait_for(
            _complete_route(
                session                = session,
                route                  = route,
                targets                = targets,
                messages               = messages,
                temperature            = effective_temperature,
                top_p                  = effective_top_p,
                max_tokens             = effective_max_tokens,
                timeout_seconds        = effective_timeout,
                max_retry              = effective_retry,
                retry_interval_seconds = effective_retry_interval,
                tools                  = tools,
                tool_choice            = tool_choice,
                extra_payload          = extra_payload,
            ),
            timeout=effective_total_timeout,
        )
    except TimeoutError as exc:
        raise AIRequestError("route_timeout") from exc


__all__ = [
    "AICompletionResult",
    "AIConfigError",
    "AIError",
    "AIModelInfo",
    "AIRequestError",
    "complete_configured_route",
    "list_configured_models",
]
