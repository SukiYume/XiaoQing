"""实现生产有界 HTTP 传输契约的轻量测试替身。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

from core.bounded_http import BoundedHttpResponse


def bounded_json_response(payload: object, *, url: str) -> BoundedHttpResponse:
    """构造已经完成有界读取的 UTF-8 JSON 响应。"""

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return BoundedHttpResponse(
        url=url,
        status=200,
        body=body,
        media_type="application/json",
        charset="utf-8",
        headers={"Content-Type": "application/json"},
        wire_bytes=len(body),
        decoded_bytes=len(body),
    )


class AiohttpBodyStream:
    """按 aiohttp 的分块协议暴露固定响应体。"""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        for offset in range(0, len(self._body), max(1, size)):
            yield self._body[offset : offset + size]


class AiohttpResponse:
    """提供有界传输读取所需的最小 aiohttp 响应表面。"""

    def __init__(
        self,
        body: bytes,
        *,
        media_type: str,
        status: int = 200,
        url: str = "https://example.invalid/",
        charset: str | None = "utf-8",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status = status
        self.url = url
        self.closed = False
        self.content = AiohttpBodyStream(body)
        self.content_length = len(body)
        self.headers = dict(headers or {})
        content_type = media_type
        if charset is not None:
            content_type = f"{content_type}; charset={charset}"
        self.headers.setdefault("Content-Type", content_type)
        self.headers.setdefault("Content-Length", str(len(body)))

    async def __aenter__(self) -> AiohttpResponse:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def aiohttp_json_response(
    payload: object,
    *,
    status: int = 200,
    media_type: str = "application/json",
    url: str = "https://example.invalid/",
    headers: Mapping[str, str] | None = None,
) -> AiohttpResponse:
    """构造尚待生产有界传输读取的 JSON 响应。"""

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return AiohttpResponse(
        body,
        status=status,
        media_type=media_type,
        url=url,
        headers=headers,
    )


def aiohttp_text_response(
    text: str,
    *,
    status: int = 200,
    media_type: str = "text/plain",
    url: str = "https://example.invalid/",
    headers: Mapping[str, str] | None = None,
) -> AiohttpResponse:
    """构造尚待生产有界传输读取的 UTF-8 文本响应。"""

    return AiohttpResponse(
        text.encode("utf-8"),
        status=status,
        media_type=media_type,
        url=url,
        headers=headers,
    )


class QueuedAiohttpSession:
    """记录标准 ``request`` 调用，并按顺序返回预置响应。"""

    def __init__(self, *responses: AiohttpResponse) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def queue(self, *responses: AiohttpResponse) -> None:
        self.responses.extend(responses)

    def request(self, method: str, url: str, **kwargs: Any) -> AiohttpResponse:
        normalized_method = method.upper()
        self.requests.append((normalized_method, url, dict(kwargs)))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        response = self.responses.pop(0)
        if response.url == "https://example.invalid/":
            response.url = url
        return response
