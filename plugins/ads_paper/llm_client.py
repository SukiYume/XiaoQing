import logging

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

_ADS_LLM_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=10, sock_read=45)
_ADS_LLM_BODY_LIMITS = BodyLimits(
    max_wire_bytes=2 * 1024 * 1024,
    max_decoded_bytes=4 * 1024 * 1024,
)
_ADS_LLM_JSON_LIMITS = JsonLimits(max_bytes=_ADS_LLM_BODY_LIMITS.max_decoded_bytes)
_ADS_LLM_JSON_MIME = MimePolicy(
    exact=frozenset({"application/json"}),
    structured_suffixes=frozenset({"+json"}),
    allow_missing=True,
)


async def generate_summary(
    session: aiohttp.ClientSession,
    api_base: str,
    api_key: str,
    model: str,
    title: str,
    abstract: str
) -> str:
    url = f"{api_base.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    prompt = f"""请用中文总结以下论文的要点，包括：
1. 研究背景和动机
2. 主要方法和创新点
3. 关键结果和结论
4. 研究意义

论文标题: {title}

摘要:
{abstract}

请用简洁清晰的语言总结，不超过300字。"""

    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 500
    }

    try:
        response = await aiohttp_request_bounded(
            session,
            "POST",
            url,
            limits=_ADS_LLM_BODY_LIMITS,
            mime_policy=_ADS_LLM_JSON_MIME,
            headers=headers,
            request_kwargs={"json": payload, "timeout": _ADS_LLM_TIMEOUT},
        )
    except HttpStatusError as exc:
        raise RuntimeError(f"LLM API error: {exc.status}") from exc

    data = parse_bounded_json(response, limits=_ADS_LLM_JSON_LIMITS)
    if not isinstance(data, dict):
        raise RuntimeError("Invalid response from LLM")
    choices = data.get("choices", [])
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RuntimeError("Empty response from LLM")
    message = choices[0].get("message", {})
    if not isinstance(message, dict):
        raise RuntimeError("Invalid response from LLM")
    content = message.get("content", "")
    return content.strip() if isinstance(content, str) else ""
