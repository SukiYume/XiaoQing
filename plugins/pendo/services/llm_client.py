import asyncio
import logging
from typing import Any

import aiohttp

from core.bounded_http import (
    BodyLimits,
    HttpStatusError,
    JsonLimits,
    MimePolicy,
    aiohttp_request_bounded,
    parse_bounded_json,
)

logger = logging.getLogger(__name__)

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


async def chat_completions_with_fallback_paths(
    session: aiohttp.ClientSession | None,
    api_base: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    endpoint_path: str = "/chat/completions",
    thinking: dict[str, Any] | None = None,
    temperature: float = 0.7,
    max_tokens: int = 1000,
    timeout_seconds: int = 30,
    max_retry: int = 2,
    retry_interval_seconds: int = 1,
    proxy: str | None = None,
) -> tuple[str | None, str]:
    """
    通用聊天补全逻辑，包含简单的重试机制
    """
    path = endpoint_path if endpoint_path.startswith("/") else f"/{endpoint_path}"
    url = f"{api_base.rstrip('/')}{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens
    }
    if thinking is not None:
        payload["thinking"] = thinking

    local_session = None
    if session is None:
        local_session = aiohttp.ClientSession()
        session = local_session

    try:
        for attempt in range(max_retry + 1):
            try:
                request_kwargs: dict[str, Any] = {
                    "json": payload,
                    "timeout": aiohttp.ClientTimeout(total=timeout_seconds),
                }
                if proxy:
                    request_kwargs["proxy"] = proxy
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
                if not isinstance(data, dict):
                    raise ValueError("LLM response must be a JSON object")
                choices = data.get("choices", [])
                choice = choices[0] if isinstance(choices, list) and choices else {}
                message = choice.get("message", {}) if isinstance(choice, dict) else {}
                content = message.get("content", "") if isinstance(message, dict) else ""
                return (content if isinstance(content, str) else ""), "main_path"
            except HttpStatusError as exc:
                logger.warning(
                    "LLM API Error (Attempt %s): status=%s",
                    attempt + 1,
                    exc.status,
                )
            except Exception as exc:
                logger.warning(
                    "LLM request failed attempt=%s error_type=%s",
                    attempt + 1,
                    type(exc).__name__,
                )

            if attempt < max_retry:
                await asyncio.sleep(retry_interval_seconds)

        return None, "error"
    finally:
        if local_session:
            await local_session.close()
