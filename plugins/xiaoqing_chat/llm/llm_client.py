from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional, TypeVar

import aiohttp

from core.bounded_http import (
    BodyLimits,
    BoundedHttpError,
    HttpStatusError,
    JsonLimits,
    MimePolicy,
    aiohttp_request_bounded,
    parse_bounded_json,
)

from ..utils.json_parsing import normalize_response_content


class LLMError(RuntimeError):
    pass


_EXTRA_PAYLOAD_RESERVED_KEYS = frozenset({"model", "messages", "stream"})
_LLM_BODY_LIMITS = BodyLimits(
    max_wire_bytes=2 * 1024 * 1024,
    max_decoded_bytes=4 * 1024 * 1024,
)
_LLM_JSON_LIMITS = JsonLimits(max_bytes=_LLM_BODY_LIMITS.max_decoded_bytes)
_LLM_JSON_MIME = MimePolicy(
    exact=frozenset({"application/json"}),
    structured_suffixes=frozenset({"+json"}),
    allow_missing=True,
)


def _join_url(api_base: str, path: str) -> str:
    base = (api_base or "").rstrip("/")
    p = (path or "").lstrip("/")
    return f"{base}/{p}" if base else f"/{p}"


def extract_response_choice(data: dict[str, Any]) -> dict[str, Any]:
    choices = data.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return {}
    choice0 = choices[0] or {}
    return choice0 if isinstance(choice0, dict) else {}


def extract_response_message(data: dict[str, Any]) -> dict[str, Any]:
    choice0 = extract_response_choice(data)
    msg = choice0.get("message")
    if isinstance(msg, dict):
        return msg
    delta = choice0.get("delta")
    if isinstance(delta, dict):
        return delta
    return {}


def extract_response_content(data: dict[str, Any]) -> str:
    return normalize_response_content(data)


def extract_response_finish_reason(data: dict[str, Any]) -> str:
    choice0 = extract_response_choice(data)
    value = choice0.get("finish_reason")
    return str(value or "").strip()


def _is_retryable_status(status: int) -> bool:
    return status in (408, 409, 425, 429) or 500 <= status <= 599


def _retry_delay(retry_interval_seconds: float, attempt: int) -> float:
    base = max(0.0, float(retry_interval_seconds))
    if base <= 0.0:
        return 0.0
    return base * (2 ** max(0, attempt - 1))


def _merge_extra_payload(payload: dict[str, Any], extra_payload: Optional[dict[str, Any]]) -> None:
    if not extra_payload:
        return
    for key, value in extra_payload.items():
        if key in _EXTRA_PAYLOAD_RESERVED_KEYS or value is None:
            continue
        payload[key] = value


T = TypeVar("T")


def _candidate_paths(endpoint_path: str) -> list[str]:
    paths = [endpoint_path]
    if endpoint_path.rstrip("/") == "/v1/chat/completions":
        paths.append("/chat/completions")
    return paths


async def _call_with_fallback_paths(
    *,
    endpoint_path: str,
    invoke: Callable[[str], Awaitable[T]],
) -> tuple[T, str]:
    last_exc: Optional[BaseException] = None
    for path in _candidate_paths(endpoint_path):
        try:
            return await invoke(path), path
        except LLMError as exc:
            last_exc = exc
            if "http_404" not in str(exc):
                break
    raise last_exc or LLMError("llm_request_failed")


async def chat_completions_raw(
    *,
    session: aiohttp.ClientSession,
    api_base: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    proxy: str = "",
    endpoint_path: str = "/v1/chat/completions",
    tools: Optional[list[dict[str, Any]]] = None,
    tool_choice: Optional[Any] = None,
    extra_payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if not api_base or not api_key or not model:
        raise LLMError("LLM secrets missing: api_base/api_key/model")

    url = _join_url(api_base, endpoint_path)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    _merge_extra_payload(payload, extra_payload)
    payload["stream"] = False

    attempt = 0
    while True:
        attempt += 1
        try:
            request_kwargs: dict[str, Any] = {
                "json": payload,
                "timeout": aiohttp.ClientTimeout(total=timeout_seconds),
            }
            if proxy:
                request_kwargs["proxy"] = proxy

            try:
                response = await aiohttp_request_bounded(
                    session,
                    "POST",
                    url,
                    limits=_LLM_BODY_LIMITS,
                    mime_policy=_LLM_JSON_MIME,
                    headers=headers,
                    request_kwargs=request_kwargs,
                )
                data = parse_bounded_json(response, limits=_LLM_JSON_LIMITS)
            except HttpStatusError as exc:
                if exc.status == 413:
                    raise LLMError("request_too_large") from exc
                if _is_retryable_status(exc.status):
                    raise LLMError(f"retryable_http_{exc.status}") from exc
                raise LLMError(f"http_{exc.status}") from exc
            except BoundedHttpError as exc:
                raise LLMError(f"invalid_response_{type(exc).__name__}") from exc

            if not isinstance(data, dict):
                raise LLMError("invalid_response")
            msg = extract_response_message(data)
            content = msg.get("content")
            tool_calls = msg.get("tool_calls")
            if (not content or not str(content).strip()) and not tool_calls:
                raise LLMError("empty_response")
            return data
        except LLMError as exc:
            if str(exc) == "request_too_large":
                raise
            if attempt > max_retry + 1:
                raise
            await asyncio.sleep(_retry_delay(retry_interval_seconds, attempt))
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt > max_retry + 1:
                raise LLMError(f"transport_{type(exc).__name__}") from exc
            await asyncio.sleep(_retry_delay(retry_interval_seconds, attempt))


async def chat_completions(
    *,
    session: aiohttp.ClientSession,
    api_base: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    proxy: str = "",
    endpoint_path: str = "/v1/chat/completions",
    tools: Optional[list[dict[str, Any]]] = None,
    tool_choice: Optional[Any] = None,
    extra_payload: Optional[dict[str, Any]] = None,
) -> str:
    data = await chat_completions_raw(
        session=session,
        api_base=api_base,
        api_key=api_key,
        model=model,
        messages=messages,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        max_retry=max_retry,
        retry_interval_seconds=retry_interval_seconds,
        proxy=proxy,
        endpoint_path=endpoint_path,
        tools=tools,
        tool_choice=tool_choice,
        extra_payload=extra_payload,
    )
    return extract_response_content(data)


async def chat_completions_with_fallback_paths(
    *,
    session: aiohttp.ClientSession,
    api_base: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    proxy: str = "",
    endpoint_path: str = "/v1/chat/completions",
    tools: Optional[list[dict[str, Any]]] = None,
    tool_choice: Optional[Any] = None,
    extra_payload: Optional[dict[str, Any]] = None,
) -> tuple[str, str]:
    return await _call_with_fallback_paths(
        endpoint_path=endpoint_path,
        invoke=lambda path: chat_completions(
            session=session,
            api_base=api_base,
            api_key=api_key,
            model=model,
            messages=messages,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            max_retry=max_retry,
            retry_interval_seconds=retry_interval_seconds,
            proxy=proxy,
            endpoint_path=path,
            tools=tools,
            tool_choice=tool_choice,
            extra_payload=extra_payload,
        ),
    )


async def chat_completions_raw_with_fallback_paths(
    *,
    session: aiohttp.ClientSession,
    api_base: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    proxy: str = "",
    endpoint_path: str = "/v1/chat/completions",
    tools: Optional[list[dict[str, Any]]] = None,
    tool_choice: Optional[Any] = None,
    extra_payload: Optional[dict[str, Any]] = None,
) -> tuple[dict[str, Any], str]:
    return await _call_with_fallback_paths(
        endpoint_path=endpoint_path,
        invoke=lambda path: chat_completions_raw(
            session=session,
            api_base=api_base,
            api_key=api_key,
            model=model,
            messages=messages,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            max_retry=max_retry,
            retry_interval_seconds=retry_interval_seconds,
            proxy=proxy,
            endpoint_path=path,
            tools=tools,
            tool_choice=tool_choice,
            extra_payload=extra_payload,
        ),
    )
