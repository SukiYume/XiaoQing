"""Protocol-correct aiohttp doubles used with the production bounded transport."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

from core.bounded_http import BoundedHttpResponse


def bounded_json_response(payload: object, *, url: str) -> BoundedHttpResponse:
    """构造协议完整的 UTF-8 JSON 有界响应。"""

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


class _ByteStream:
    def __init__(self, body: bytes):
        self._body = body

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        for offset in range(0, len(self._body), max(1, size)):
            yield self._body[offset : offset + size]


async def _response_body(
    response: Any,
    *,
    default_media_type: str | None = None,
) -> tuple[bytes, str]:
    if hasattr(response, "_json_data"):
        return json.dumps(response._json_data).encode("utf-8"), "application/json"
    if callable(getattr(response, "json", None)) and not callable(getattr(response, "text", None)):
        return json.dumps(await response.json()).encode("utf-8"), "application/json"
    if callable(getattr(response, "text", None)):
        text = await response.text()
        media_type = default_media_type or (
            "application/xml" if text.lstrip().startswith("<?xml") else "text/plain"
        )
        return text.encode("utf-8"), media_type
    if callable(getattr(response, "read", None)):
        body = await response.read()
        return bytes(body), default_media_type or "application/octet-stream"
    raise TypeError("legacy response does not expose json(), text(), or read()")


class _BoundedResponseAdapter:
    def __init__(self, response: Any, *, url: str, body: bytes, media_type: str):
        self._response = response
        self.status = int(response.status)
        self.url = getattr(response, "url", url)
        source_headers = getattr(response, "headers", {})
        self.headers = dict(source_headers) if isinstance(source_headers, Mapping) else {}
        self.headers.setdefault("Content-Type", f"{media_type}; charset=utf-8")
        self.headers.setdefault("Content-Length", str(len(body)))
        self.charset = getattr(response, "charset", None) or "utf-8"
        self.content = _ByteStream(body)

    def close(self) -> None:
        close = getattr(self._response, "close", None)
        if callable(close):
            close()


class _RequestContextAdapter:
    def __init__(
        self,
        context_manager: Any,
        *,
        url: str,
        default_media_type: str | None,
    ):
        self._context_manager = context_manager
        self._url = url
        self._default_media_type = default_media_type

    async def __aenter__(self) -> _BoundedResponseAdapter:
        response = await self._context_manager.__aenter__()
        body, media_type = await _response_body(
            response,
            default_media_type=self._default_media_type,
        )
        return _BoundedResponseAdapter(
            response,
            url=self._url,
            body=body,
            media_type=media_type,
        )

    async def __aexit__(self, *args: Any) -> Any:
        return await self._context_manager.__aexit__(*args)


class LegacyAiohttpSessionAdapter:
    """Expose aiohttp's exact ``request`` surface over older test doubles."""

    def __init__(self, session: Any, *, default_media_type: str | None = None):
        self._session = session
        self._default_media_type = default_media_type
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def request(self, method: str, url: str, **kwargs: Any) -> _RequestContextAdapter:
        normalized = method.upper()
        self.requests.append((normalized, url, dict(kwargs)))
        if kwargs.pop("allow_redirects", None) is not False:
            raise AssertionError("production transport must disable automatic redirects")
        if kwargs.pop("auto_decompress", None) is not False:
            raise AssertionError("production transport must disable automatic decompression")
        operation = getattr(self._session, normalized.lower())
        return _RequestContextAdapter(
            operation(url, **kwargs),
            url=url,
            default_media_type=self._default_media_type,
        )


def wrap_legacy_aiohttp_session(session: Any | None) -> Any | None:
    if session is None or isinstance(session, LegacyAiohttpSessionAdapter):
        return session
    return LegacyAiohttpSessionAdapter(session)
