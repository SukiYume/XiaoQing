"""Bounded response transport and structured payload parsing.

This module limits response resources; it does *not* decide whether a target
URL is trusted.  Chat-controlled URLs still require :mod:`core.safe_http` for
DNS pinning and SSRF protection.  Fixed or administrator-configured providers
can use these readers without losing private-host, proxy, or non-standard-port
support.

本模块只负责“响应有界”，不负责判断目标地址是否可信。核心不变量是同时限制线上的
压缩字节、解压后字节和解压倍率；重定向必须逐跳验证且跨源时剥离敏感请求头；结构化
数据在交给业务代码前还要独立通过 JSON/XML 的深度、节点数与标量大小检查。
"""

from __future__ import annotations

import json
import math
import re
import time
import zlib
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, cast
from urllib.parse import urljoin, urlsplit
from xml.parsers import expat

_REDIRECT_STATUSES        = frozenset({301, 302, 303, 307, 308})
_DEFAULT_SUCCESS_STATUSES = frozenset({200})
_RESERVED_AIOHTTP_KWARGS  = frozenset(
    {"allow_redirects", "auto_decompress", "headers", "method", "url"}
)
_RESERVED_REQUESTS_KWARGS   = frozenset({"allow_redirects", "stream", "headers", "method", "url"})
_SENSITIVE_REDIRECT_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})
_TOKEN                      = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_CHARSET                    = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_JSON_DELIMITERS            = frozenset(" \t\r\n,]}:")
_HEX                        = frozenset("0123456789abcdefABCDEF")


class BoundedHttpError(RuntimeError):
    """Base class for fail-closed response-boundary errors."""


class ResponseLimitError(BoundedHttpError):
    """A wire, decoded, ratio, or structured payload limit was exceeded."""


class ResponseFormatError(BoundedHttpError):
    """A response encoding, MIME type, or structured payload was invalid."""


class ResponseTransportError(BoundedHttpError):
    """A response body stream failed before a complete bounded body arrived."""


class HttpStatusError(BoundedHttpError):
    """A response status was outside the caller's explicit success set."""

    def __init__(self, status: int) -> None:
        self.status = int(status)
        super().__init__(f"unexpected HTTP status {self.status}")


class RedirectPolicyError(BoundedHttpError):
    """A redirect violated the explicit method, hop, scheme, or origin policy."""


@dataclass(frozen=True, slots=True)
class BodyLimits:
    max_wire_bytes: int          = 2 * 1024 * 1024
    max_decoded_bytes: int       = 4 * 1024 * 1024
    max_decompression_ratio: int = 20
    ratio_grace_bytes: int       = 64 * 1024
    chunk_bytes: int             = 64 * 1024

    def __post_init__(self) -> None:
        for name in (
            "max_wire_bytes",
            "max_decoded_bytes",
            "max_decompression_ratio",
            "ratio_grace_bytes",
            "chunk_bytes",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class MimePolicy:
    exact: frozenset[str]               = frozenset()
    type_prefixes: frozenset[str]       = frozenset()
    structured_suffixes: frozenset[str] = frozenset()
    allow_missing: bool                 = False

    def __post_init__(self) -> None:
        for media_type in self.exact:
            _validate_media_type_pattern(media_type, prefix=False)
        for prefix in self.type_prefixes:
            _validate_media_type_pattern(prefix, prefix=True)
        for suffix in self.structured_suffixes:
            if not suffix.startswith("+") or not _TOKEN.fullmatch(suffix[1:]):
                raise ValueError("invalid structured MIME suffix")

    def accepts(self, media_type: str | None) -> bool:
        if media_type is None:
            return self.allow_missing
        normalized = media_type.casefold()
        if normalized in {value.casefold() for value in self.exact}:
            return True
        if any(normalized.startswith(prefix.casefold()) for prefix in self.type_prefixes):
            return True
        return any(
            normalized.startswith("application/") and normalized.endswith(suffix.casefold())
            for suffix in self.structured_suffixes
        )


@dataclass(frozen=True, slots=True)
class RedirectPolicy:
    max_hops: int                          = 0
    allowed_schemes: frozenset[str]        = frozenset({"https"})
    allowed_origins: frozenset[str] | None = None
    same_origin_only: bool                 = True
    allow_https_upgrade_same_host: bool    = False

    def __post_init__(self) -> None:
        if self.max_hops < 0:
            raise ValueError("max_hops must not be negative")
        if not self.allowed_schemes:
            raise ValueError("allowed_schemes must not be empty")
        normalized_schemes = frozenset(value.casefold() for value in self.allowed_schemes)
        if not normalized_schemes <= {"http", "https"}:
            raise ValueError("redirect schemes must be http or https")
        if self.allowed_origins is not None:
            for origin in self.allowed_origins:
                _origin_from_url(origin)


@dataclass(frozen=True, slots=True)
class JsonLimits:
    max_bytes: int        = 4 * 1024 * 1024
    max_depth: int        = 32
    max_nodes: int        = 20_000
    max_string_chars: int = 256_000
    max_number_chars: int = 4_096

    def __post_init__(self) -> None:
        for name in (
            "max_bytes",
            "max_depth",
            "max_nodes",
            "max_string_chars",
            "max_number_chars",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class XmlLimits:
    max_bytes: int           = 8 * 1024 * 1024
    max_depth: int           = 64
    max_nodes: int           = 50_000
    max_attributes: int      = 100_000
    max_attribute_chars: int = 1_000_000
    max_name_chars: int      = 512
    max_text_chars: int      = 1_000_000

    def __post_init__(self) -> None:
        for name in (
            "max_bytes",
            "max_depth",
            "max_nodes",
            "max_attributes",
            "max_attribute_chars",
            "max_name_chars",
            "max_text_chars",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class BoundedHttpResponse:
    url: str
    status: int
    body: bytes
    media_type: str | None
    charset: str | None
    headers: Mapping[str, str]
    wire_bytes: int
    decoded_bytes: int


def _validate_media_type_pattern(value: str, *, prefix: bool) -> None:
    normalized = value.casefold()
    if prefix:
        if not normalized.endswith("/"):
            raise ValueError("MIME type prefix must end with '/'")
        major = normalized[:-1]
        if not _TOKEN.fullmatch(major):
            raise ValueError("invalid MIME type prefix")
        return
    parts = normalized.split("/", 1)
    if len(parts) != 2 or not all(_TOKEN.fullmatch(part) for part in parts):
        raise ValueError("invalid MIME type")


JSON_MIME_POLICY = MimePolicy(
    exact               = frozenset({"application/json"}),
    structured_suffixes = frozenset({"+json"}),
)
HTML_MIME_POLICY = MimePolicy(
    exact=frozenset({"text/html", "application/xhtml+xml"}),
)
XML_MIME_POLICY = MimePolicy(
    exact=frozenset(
        {
            "application/xml",
            "text/xml",
            "application/atom+xml",
            "application/rss+xml",
        }
    ),
    structured_suffixes=frozenset({"+xml"}),
)
NO_REDIRECTS = RedirectPolicy()


def _single_header(headers: Mapping[str, Any], name: str) -> Any:
    getall = getattr(headers, "getall", None)
    if callable(getall):
        try:
            values = list(getall(name, []))
        except KeyError:
            values = []
        if len(values) > 1:
            raise ResponseFormatError(f"duplicate {name} header")
        return values[0] if values else None

    values = [value for key, value in headers.items() if str(key).casefold() == name.casefold()]
    if len(values) > 1:
        raise ResponseFormatError(f"duplicate {name} header")
    return values[0] if values else None


def _parse_content_type(headers: Mapping[str, Any]) -> tuple[str | None, str | None]:
    raw_value = _single_header(headers, "Content-Type")
    if raw_value in (None, ""):
        return None, None
    if not isinstance(raw_value, str) or len(raw_value) > 512:
        raise ResponseFormatError("invalid Content-Type header")
    parts      = [part.strip() for part in raw_value.split(";")]
    media_type = parts[0].casefold()
    try:
        _validate_media_type_pattern(media_type, prefix=False)
    except ValueError as exc:
        raise ResponseFormatError("invalid Content-Type header") from exc
    charset: str | None = None
    for parameter in parts[1:]:
        if "=" not in parameter:
            continue
        name, value = parameter.split("=", 1)
        if name.strip().casefold() != "charset":
            continue
        candidate = value.strip().strip('"').strip("'")
        if not _CHARSET.fullmatch(candidate):
            raise ResponseFormatError("invalid response charset")
        charset = candidate
        break
    return media_type, charset


def _content_length(response: Any) -> int | None:
    headers = getattr(response, "headers", {})
    raw     = _single_header(headers, "Content-Length")
    value   = raw if raw not in (None, "") else getattr(response, "content_length", None)
    if value is None:
        return None
    try:
        length = int(value)
    except (TypeError, ValueError) as exc:
        raise ResponseFormatError("invalid Content-Length header") from exc
    if length < 0:
        raise ResponseFormatError("invalid Content-Length header")
    return length


def _content_encoding(headers: Mapping[str, Any]) -> str:
    raw = _single_header(headers, "Content-Encoding") or "identity"
    if not isinstance(raw, str) or "," in raw:
        raise ResponseFormatError("multiple content encodings are not supported")
    encoding = raw.strip().casefold() or "identity"
    if encoding == "x-gzip":
        encoding = "gzip"
    if encoding not in {"identity", "gzip", "deflate"}:
        raise ResponseFormatError("unsupported content encoding")
    return encoding


class _BoundedDecoder:
    def __init__(
        self,
        *,
        encoding: str,
        limits: BodyLimits,
        declared_wire_bytes: int | None,
    ) -> None:
        if declared_wire_bytes is not None and declared_wire_bytes > limits.max_wire_bytes:
            raise ResponseLimitError("declared response body is too large")
        if (
            encoding == "identity"
            and declared_wire_bytes is not None
            and declared_wire_bytes > limits.max_decoded_bytes
        ):
            raise ResponseLimitError("declared decoded body is too large")
        self._limits     = limits
        self._wire_bytes = 0
        self._decoded    = bytearray()
        if encoding == "identity":
            self._decompressor: zlib._Decompress | None = None
        elif encoding == "gzip":
            self._decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
        elif encoding == "deflate":
            self._decompressor = zlib.decompressobj()
        else:  # pragma: no cover - validated by _content_encoding
            raise ResponseFormatError("unsupported content encoding")

    @property
    def wire_bytes(self) -> int:
        return self._wire_bytes

    def feed(self, chunk: bytes | bytearray | memoryview) -> None:
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise ResponseFormatError("response stream yielded a non-bytes chunk")
        payload = bytes(chunk)
        if not payload:
            return
        self._wire_bytes += len(payload)
        if self._wire_bytes > self._limits.max_wire_bytes:
            raise ResponseLimitError("response wire body is too large")

        if self._decompressor is None:
            self._append(payload)
            return
        try:
            # 每次只允许解压“剩余预算 + 1”；多出的一个字节用于可靠识别越界，
            # 避免先完整解压恶意压缩包、再检查大小所造成的内存峰值。
            output = self._decompressor.decompress(
                payload,
                self._remaining_output_budget() + 1,
            )
        except zlib.error as exc:
            raise ResponseFormatError("invalid compressed response") from exc
        self._append(output)
        if self._decompressor.unconsumed_tail:
            raise ResponseLimitError("decoded response body is too large")

    def finish(self) -> bytes:
        if self._decompressor is not None:
            try:
                output = self._decompressor.flush(self._remaining_output_budget() + 1)
            except zlib.error as exc:
                raise ResponseFormatError("invalid compressed response") from exc
            self._append(output)
            if not self._decompressor.eof:
                raise ResponseFormatError("truncated compressed response")
            if self._decompressor.unused_data or self._decompressor.unconsumed_tail:
                raise ResponseFormatError("compressed response has trailing data")
        return bytes(self._decoded)

    def _append(self, output: bytes | bytearray | memoryview) -> None:
        if not output:
            return
        if len(self._decoded) + len(output) > self._limits.max_decoded_bytes:
            raise ResponseLimitError("decoded response body is too large")
        self._decoded.extend(output)
        permitted_by_ratio = max(
            self._limits.ratio_grace_bytes,
            self._wire_bytes * self._limits.max_decompression_ratio,
        )
        if len(self._decoded) > permitted_by_ratio:
            raise ResponseLimitError("response decompression ratio is too large")

    def _remaining_output_budget(self) -> int:
        absolute_remaining = self._limits.max_decoded_bytes - len(self._decoded)
        ratio_total        = max(
            self._limits.ratio_grace_bytes,
            self._wire_bytes * self._limits.max_decompression_ratio,
        )
        ratio_remaining = ratio_total - len(self._decoded)
        remaining       = min(absolute_remaining, ratio_remaining)
        if remaining < 0:
            raise ResponseLimitError("decoded response body is too large")
        return remaining


def decode_limited_chunks(
    chunks: Iterable[bytes | bytearray | memoryview],
    *,
    encoding: str,
    limits: BodyLimits,
    declared_wire_bytes: int | None  = None,
    deadline_monotonic: float | None = None,
) -> tuple[bytes, int]:
    """Decode a synchronous wire-chunk iterable with shared hard limits."""

    decoder = _BoundedDecoder(
        encoding            = encoding,
        limits              = limits,
        declared_wire_bytes = declared_wire_bytes,
    )
    iterator = iter(chunks)
    while True:
        _ensure_before_deadline(deadline_monotonic)
        try:
            chunk = next(iterator)
        except StopIteration:
            _ensure_before_deadline(deadline_monotonic)
            break
        _ensure_before_deadline(deadline_monotonic)
        decoder.feed(chunk)
    return decoder.finish(), decoder.wire_bytes


def _ensure_before_deadline(deadline_monotonic: float | None) -> None:
    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
        raise ResponseTransportError("response total timeout exceeded")


async def read_aiohttp_response(
    response: Any,
    *,
    limits: BodyLimits,
    mime_policy: MimePolicy | None = None,
) -> BoundedHttpResponse:
    """Read an aiohttp response created with ``auto_decompress=False``."""

    headers = getattr(response, "headers", {})
    media_type, charset = _parse_content_type(headers)
    if mime_policy is not None and not mime_policy.accepts(media_type):
        raise ResponseFormatError("response content type is not allowed")
    encoding = _content_encoding(headers)
    decoder  = _BoundedDecoder(
        encoding            = encoding,
        limits              = limits,
        declared_wire_bytes = _content_length(response),
    )
    content  = getattr(response, "content", None)
    iterator = getattr(content, "iter_chunked", None)
    if not callable(iterator):
        raise ResponseFormatError("aiohttp response has no bounded byte stream")
    try:
        async for chunk in iterator(limits.chunk_bytes):
            decoder.feed(chunk)
    except BoundedHttpError:
        raise
    except Exception as exc:
        raise ResponseTransportError("response body transport failed") from exc
    body = decoder.finish()
    return _build_response(
        response,
        body       = body,
        media_type = media_type,
        charset    = charset,
        wire_bytes = decoder.wire_bytes,
    )


def read_requests_response(
    response: Any,
    *,
    limits: BodyLimits,
    mime_policy: MimePolicy | None   = None,
    deadline_monotonic: float | None = None,
) -> BoundedHttpResponse:
    """Read a streamed requests response without transparent decompression."""

    headers = getattr(response, "headers", {})
    media_type, charset = _parse_content_type(headers)
    if mime_policy is not None and not mime_policy.accepts(media_type):
        raise ResponseFormatError("response content type is not allowed")
    encoding = _content_encoding(headers)
    raw      = getattr(response, "raw", None)
    stream   = getattr(raw, "stream", None)
    if not callable(stream):
        raise ResponseFormatError("requests response has no bounded raw stream")
    try:
        cast(Any, raw).decode_content = False
    except (AttributeError, TypeError):
        pass
    try:
        body, wire_bytes = decode_limited_chunks(
            stream(limits.chunk_bytes, decode_content=False),
            encoding            = encoding,
            limits              = limits,
            declared_wire_bytes = _content_length(response),
            deadline_monotonic  = deadline_monotonic,
        )
    except BoundedHttpError:
        raise
    except Exception as exc:
        raise ResponseTransportError("response body transport failed") from exc
    return _build_response(
        response,
        body       = body,
        media_type = media_type,
        charset    = charset,
        wire_bytes = wire_bytes,
    )


def _build_response(
    response: Any,
    *,
    body: bytes,
    media_type: str | None,
    charset: str | None,
    wire_bytes: int,
) -> BoundedHttpResponse:
    response_url = getattr(response, "url", "")
    headers      = {
        str(key): str(value) for key, value in dict(getattr(response, "headers", {})).items()
    }
    return BoundedHttpResponse(
        url           = str(response_url),
        status        = int(getattr(response, "status", getattr(response, "status_code", 0))),
        body          = body,
        media_type    = media_type,
        charset       = charset,
        headers       = MappingProxyType(headers),
        wire_bytes    = wire_bytes,
        decoded_bytes = len(body),
    )


def _origin_from_url(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        port   = parsed.port
    except (TypeError, ValueError) as exc:
        raise RedirectPolicyError("invalid request URL") from exc
    scheme = parsed.scheme.casefold()
    host   = (parsed.hostname or "").rstrip(".").casefold()
    if scheme not in {"http", "https"} or not host:
        raise RedirectPolicyError("request URL must be absolute HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise RedirectPolicyError("request URL userinfo is not allowed")
    return scheme, host, port or (443 if scheme == "https" else 80)


def _redirect_target(
    *,
    current_url: str,
    location: str,
    policy: RedirectPolicy,
) -> tuple[str, bool]:
    target_url     = urljoin(current_url, location)
    current_origin = _origin_from_url(current_url)
    target_origin  = _origin_from_url(target_url)
    if current_origin[0] == "https" and target_origin[0] != "https":
        raise RedirectPolicyError("HTTPS redirect downgrade is not allowed")
    if target_origin[0] not in {scheme.casefold() for scheme in policy.allowed_schemes}:
        raise RedirectPolicyError("redirect scheme is not allowed")
    same_origin        = target_origin == current_origin
    upgraded_same_host = (
        policy.allow_https_upgrade_same_host
        and current_origin[0] == "http"
        and target_origin[0] == "https"
        and current_origin[1] == target_origin[1]
    )
    if policy.same_origin_only and not (same_origin or upgraded_same_host):
        raise RedirectPolicyError("cross-origin redirect is not allowed")
    if policy.allowed_origins is not None:
        allowed = {_origin_from_url(origin) for origin in policy.allowed_origins}
        if target_origin not in allowed:
            raise RedirectPolicyError("redirect origin is not allowed")
    return target_url, same_origin


def _request_headers(
    headers: Mapping[str, str] | None,
    *,
    accept_encoding: str,
) -> dict[str, str]:
    if accept_encoding not in {"identity", "gzip, deflate"}:
        raise ValueError("accept_encoding must be identity or gzip, deflate")
    result = {str(key): str(value) for key, value in (headers or {}).items()}
    for key in tuple(result):
        if key.casefold() == "accept-encoding":
            del result[key]
    result["Accept-Encoding"] = accept_encoding
    return result


def _without_sensitive_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.casefold() not in _SENSITIVE_REDIRECT_HEADERS
    }


async def aiohttp_request_bounded(
    session: Any,
    method: str,
    url: str,
    *,
    limits: BodyLimits,
    mime_policy: MimePolicy | None           = None,
    redirect_policy: RedirectPolicy          = NO_REDIRECTS,
    success_statuses: Collection[int]        = _DEFAULT_SUCCESS_STATUSES,
    headers: Mapping[str, str] | None        = None,
    request_kwargs: Mapping[str, Any] | None = None,
    accept_encoding: str                     = "gzip, deflate",
) -> BoundedHttpResponse:
    """Issue a bounded aiohttp request with explicit manual redirects."""

    normalized_method = method.upper()
    _origin_from_url(url)
    current_url = url
    current_headers = _request_headers(headers, accept_encoding=accept_encoding)
    base_kwargs = dict(request_kwargs or {})
    forbidden   = _RESERVED_AIOHTTP_KWARGS & base_kwargs.keys()
    if forbidden:
        raise ValueError("bounded aiohttp request options cannot override transport guards")

    for hop in range(redirect_policy.max_hops + 1):
        kwargs = dict(base_kwargs)
        if hop:
            kwargs.pop("params", None)
        request = session.request(
            normalized_method,
            current_url,
            headers         = current_headers,
            allow_redirects = False,
            auto_decompress = False,
            **kwargs,
        )
        async with request as response:
            status = int(getattr(response, "status", 0))
            if status in _REDIRECT_STATUSES:
                if normalized_method not in {"GET", "HEAD"}:
                    _close_response(response)
                    raise RedirectPolicyError("redirects are disabled for this HTTP method")
                if hop >= redirect_policy.max_hops:
                    _close_response(response)
                    raise RedirectPolicyError("too many redirects")
                location = getattr(response, "headers", {}).get("Location")
                if not location:
                    _close_response(response)
                    raise RedirectPolicyError("redirect has no Location header")
                next_url, same_origin = _redirect_target(
                    current_url = str(getattr(response, "url", current_url)),
                    location    = location,
                    policy      = redirect_policy,
                )
                # Authorization/Cookie 只能留在原始源；显式允许的跨源跳转也不能继承。
                if not same_origin:
                    current_headers = _without_sensitive_headers(current_headers)
                current_url = next_url
                _close_response(response)
                continue
            if status not in success_statuses:
                _close_response(response)
                raise HttpStatusError(status)
            return await read_aiohttp_response(
                response,
                limits      = limits,
                mime_policy = mime_policy,
            )
    raise RedirectPolicyError("too many redirects")


def requests_request_bounded(
    method: str,
    url: str,
    *,
    limits: BodyLimits,
    mime_policy: MimePolicy | None           = None,
    redirect_policy: RedirectPolicy          = NO_REDIRECTS,
    success_statuses: Collection[int]        = _DEFAULT_SUCCESS_STATUSES,
    headers: Mapping[str, str] | None        = None,
    request_kwargs: Mapping[str, Any] | None = None,
    session: Any                             = None,
    accept_encoding: str                     = "gzip, deflate",
    total_timeout_seconds: float | None      = None,
) -> BoundedHttpResponse:
    """Issue a bounded requests request with explicit manual redirects."""

    normalized_method = method.upper()
    _origin_from_url(url)
    current_url = url
    current_headers = _request_headers(headers, accept_encoding=accept_encoding)
    base_kwargs = dict(request_kwargs or {})
    forbidden   = _RESERVED_REQUESTS_KWARGS & base_kwargs.keys()
    if forbidden:
        raise ValueError("bounded requests options cannot override transport guards")
    base_kwargs.setdefault("timeout", 30.0)
    deadline_monotonic = time.monotonic() + _requests_total_timeout(
        base_kwargs,
        explicit_seconds=total_timeout_seconds,
    )
    if session is None:
        # ``requests`` belongs to the optional plugin dependency set.  Keep
        # aiohttp-only core/safe_http imports usable in a minimal installation.
        import requests

        request_fn = requests.request
    else:
        request_fn = session.request

    for hop in range(redirect_policy.max_hops + 1):
        remaining_seconds = _remaining_before_deadline(deadline_monotonic)
        kwargs            = dict(base_kwargs)
        kwargs["timeout"] = _requests_deadline_timeout(
            base_kwargs["timeout"],
            remaining_seconds=remaining_seconds,
        )
        if hop:
            kwargs.pop("params", None)
        response = request_fn(
            normalized_method,
            current_url,
            headers         = current_headers,
            allow_redirects = False,
            stream          = True,
            **kwargs,
        )
        try:
            _ensure_before_deadline(deadline_monotonic)
            status = int(getattr(response, "status_code", 0))
            if status in _REDIRECT_STATUSES:
                if normalized_method not in {"GET", "HEAD"}:
                    raise RedirectPolicyError("redirects are disabled for this HTTP method")
                if hop >= redirect_policy.max_hops:
                    raise RedirectPolicyError("too many redirects")
                location = getattr(response, "headers", {}).get("Location")
                if not location:
                    raise RedirectPolicyError("redirect has no Location header")
                next_url, same_origin = _redirect_target(
                    current_url = str(getattr(response, "url", current_url)),
                    location    = location,
                    policy      = redirect_policy,
                )
                if not same_origin:
                    current_headers = _without_sensitive_headers(current_headers)
                current_url = next_url
                continue
            if status not in success_statuses:
                raise HttpStatusError(status)
            return read_requests_response(
                response,
                limits             = limits,
                mime_policy        = mime_policy,
                deadline_monotonic = deadline_monotonic,
            )
        finally:
            _close_response(response)
    raise RedirectPolicyError("too many redirects")


def _remaining_before_deadline(deadline_monotonic: float) -> float:
    """Return the positive budget shared by redirects and response reading."""

    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise ResponseTransportError("response total timeout exceeded")
    return remaining


def _requests_deadline_timeout(configured: Any, *, remaining_seconds: float) -> Any:
    """Build a fresh urllib3 timeout capped by the shared request deadline.

    ``requests`` otherwise gives every redirect the original timeout again.
    urllib3's ``total`` timeout also subtracts connection time from the first
    response read; the explicit deadline checks between streamed chunks cover
    slow-drip responses across subsequent reads.
    """

    from requests.adapters import TimeoutSauce

    if isinstance(configured, (tuple, list)) and len(configured) == 2:
        connect, read = configured
    else:
        connect = read = configured

    def capped(value: Any) -> float:
        if value is None:
            return remaining_seconds
        if isinstance(value, bool):
            raise ValueError("request timeout must be a positive finite number")
        try:
            seconds = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("request timeout must be a positive finite number") from exc
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("request timeout must be a positive finite number")
        return min(seconds, remaining_seconds)

    return TimeoutSauce(
        total   = remaining_seconds,
        connect = capped(connect),
        read    = capped(read),
    )


def _requests_total_timeout(
    request_kwargs: Mapping[str, Any],
    *,
    explicit_seconds: float | None,
) -> float:
    value: Any = explicit_seconds
    if value is None:
        value = request_kwargs.get("timeout", 30.0)
        if isinstance(value, (tuple, list)) and len(value) == 2:
            value = float(value[0]) + float(value[1])
    if isinstance(value, bool):
        raise ValueError("total timeout must be a positive finite number")
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("total timeout must be a positive finite number") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("total timeout must be a positive finite number")
    return seconds


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _body_bytes(source: BoundedHttpResponse | bytes | bytearray | memoryview) -> bytes:
    if isinstance(source, BoundedHttpResponse):
        return source.body
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source)
    raise TypeError("structured response source must be bounded bytes")


def parse_bounded_json(
    source: BoundedHttpResponse | bytes | bytearray | memoryview,
    *,
    limits: JsonLimits = JsonLimits(),
) -> Any:
    """Preflight and parse JSON with depth, node, and scalar limits.

    The raw-text preflight, ``json.loads`` and parsed-tree validation are three
    intentional passes over untrusted input.  They enforce different limits
    before recursion, during syntax decoding and after materialization; do not
    replace them with a provider-specific fast path at this security boundary.
    """

    body = _body_bytes(source)
    if len(body) > limits.max_bytes:
        raise ResponseLimitError("JSON response body is too large")
    try:
        text = body.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise ResponseFormatError("JSON response is not valid UTF-8") from exc
    _preflight_json(text, limits)

    def reject_constant(_value: str) -> None:
        raise ResponseFormatError("non-finite JSON number is not allowed")

    def parse_finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ResponseFormatError("non-finite JSON number is not allowed")
        return parsed

    def object_pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ResponseFormatError("duplicate JSON object key")
            result[key] = value
        return result

    try:
        value = json.loads(
            text,
            parse_constant    = reject_constant,
            parse_float       = parse_finite_float,
            object_pairs_hook = object_pairs,
        )
    except BoundedHttpError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ResponseFormatError("invalid JSON response") from exc
    _validate_json_value(value, limits)
    return value


def _preflight_json(text: str, limits: JsonLimits) -> None:
    stack: list[str] = []
    nodes            = 0
    string_chars     = 0
    index            = 0
    size             = len(text)

    def add_node() -> None:
        nonlocal nodes
        nodes += 1
        if nodes > limits.max_nodes:
            raise ResponseLimitError("JSON response has too many nodes")

    while index < size:
        char = text[index]
        if char in " \t\r\n,:":
            index += 1
            continue
        if char in "{[":
            stack.append("}" if char == "{" else "]")
            if len(stack) > limits.max_depth:
                raise ResponseLimitError("JSON response is too deeply nested")
            add_node()
            index += 1
            continue
        if char in "}]":
            if not stack or stack.pop() != char:
                raise ResponseFormatError("invalid JSON nesting")
            index += 1
            continue
        if char == '"':
            add_node()
            index += 1
            while index < size:
                current = text[index]
                if current == '"':
                    index += 1
                    break
                if ord(current) < 0x20:
                    raise ResponseFormatError("invalid control character in JSON string")
                if current == "\\":
                    index += 1
                    if index >= size:
                        raise ResponseFormatError("invalid JSON escape")
                    escaped = text[index]
                    if escaped == "u":
                        digits = text[index + 1 : index + 5]
                        if len(digits) != 4 or any(digit not in _HEX for digit in digits):
                            raise ResponseFormatError("invalid JSON unicode escape")
                        index += 4
                    elif escaped not in '"\\/bfnrt':
                        raise ResponseFormatError("invalid JSON escape")
                string_chars += 1
                if string_chars > limits.max_string_chars:
                    raise ResponseLimitError("JSON response strings are too large")
                index += 1
            else:
                raise ResponseFormatError("unterminated JSON string")
            continue

        start = index
        while index < size and text[index] not in _JSON_DELIMITERS:
            index += 1
        token = text[start:index]
        if token in {"true", "false", "null"}:
            add_node()
            continue
        if token in {"NaN", "Infinity", "-Infinity"}:
            raise ResponseFormatError("non-finite JSON number is not allowed")
        if not token or token[0] not in "-0123456789":
            raise ResponseFormatError("invalid JSON token")
        if len(token) > limits.max_number_chars:
            raise ResponseLimitError("JSON number token is too large")
        add_node()
    if stack:
        raise ResponseFormatError("unterminated JSON container")


def _validate_json_value(value: Any, limits: JsonLimits) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    nodes                        = 0
    string_chars                 = 0
    while stack:
        current, depth = stack.pop()
        if depth > limits.max_depth:
            raise ResponseLimitError("JSON response is too deeply nested")
        nodes += 1
        if nodes > limits.max_nodes:
            raise ResponseLimitError("JSON response has too many nodes")
        if isinstance(current, dict):
            for key, child in current.items():
                nodes += 1
                if nodes > limits.max_nodes:
                    raise ResponseLimitError("JSON response has too many nodes")
                string_chars += len(key)
                stack.append((child, depth + 1))
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
        elif isinstance(current, str):
            string_chars += len(current)
        elif isinstance(current, float) and not math.isfinite(current):
            raise ResponseFormatError("non-finite JSON number is not allowed")
        elif current is not None and not isinstance(current, (bool, int, float)):
            raise ResponseFormatError("JSON response contains an invalid value")
        if string_chars > limits.max_string_chars:
            raise ResponseLimitError("JSON response strings are too large")


def validate_bounded_xml(
    source: BoundedHttpResponse | bytes | bytearray | memoryview,
    *,
    limits: XmlLimits = XmlLimits(),
) -> bytes:
    """Reject entity-bearing or structurally excessive XML before consumers parse it."""

    body = _body_bytes(source)
    if len(body) > limits.max_bytes:
        raise ResponseLimitError("XML response body is too large")
    folded = body.lower()
    if b"<!doctype" in folded or b"<!entity" in folded:
        raise ResponseFormatError("XML DTD and entity declarations are not allowed")

    if tuple(expat.version_info) < (2, 7, 2):
        raise ResponseFormatError("XML parser version is not safe for external input")
    parser          = expat.ParserCreate()
    depth           = 0
    nodes           = 0
    attributes      = 0
    attribute_chars = 0
    text_chars      = 0

    def account_node() -> None:
        nonlocal nodes
        nodes += 1
        if nodes > limits.max_nodes:
            raise ResponseLimitError("XML response has too many nodes")

    def start_element(_name: str, attrs: Mapping[str, str]) -> None:
        nonlocal depth, attributes, attribute_chars
        depth += 1
        account_node()
        attributes += len(attrs)
        if len(_name) > limits.max_name_chars:
            raise ResponseLimitError("XML element name is too large")
        for name, value in attrs.items():
            if len(name) > limits.max_name_chars:
                raise ResponseLimitError("XML attribute name is too large")
            attribute_chars += len(name) + len(value)
        if depth > limits.max_depth:
            raise ResponseLimitError("XML response is too deeply nested")
        if attributes > limits.max_attributes:
            raise ResponseLimitError("XML response has too many attributes")
        if attribute_chars > limits.max_attribute_chars:
            raise ResponseLimitError("XML response attributes are too large")

    def end_element(_name: str) -> None:
        nonlocal depth
        depth -= 1

    def character_data(value: str) -> None:
        nonlocal text_chars
        text_chars += len(value)
        if text_chars > limits.max_text_chars:
            raise ResponseLimitError("XML response text is too large")

    def comment(value: str) -> None:
        account_node()
        character_data(value)

    def processing_instruction(target: str, data: str) -> None:
        if len(target) > limits.max_name_chars:
            raise ResponseLimitError("XML processing instruction name is too large")
        account_node()
        character_data(data)

    def reject_declaration(*_args: Any) -> None:
        raise ResponseFormatError("XML declarations with entities are not allowed")

    def reject_external_entity(*_args: Any) -> int:
        raise ResponseFormatError("external XML entities are not allowed")

    parser.StartElementHandler          = start_element
    parser.EndElementHandler            = end_element
    parser.CharacterDataHandler         = character_data
    parser.CommentHandler               = comment
    parser.ProcessingInstructionHandler = processing_instruction
    parser.StartDoctypeDeclHandler      = reject_declaration
    parser.EntityDeclHandler            = reject_declaration
    parser.ExternalEntityRefHandler     = reject_external_entity
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.Parse(body, True)
    except BoundedHttpError:
        raise
    except expat.ExpatError as exc:
        raise ResponseFormatError("invalid XML response") from exc
    if depth != 0 or nodes == 0:
        raise ResponseFormatError("invalid XML response")
    return body


__all__ = [
    "BodyLimits",
    "BoundedHttpError",
    "BoundedHttpResponse",
    "HTML_MIME_POLICY",
    "HttpStatusError",
    "JSON_MIME_POLICY",
    "JsonLimits",
    "MimePolicy",
    "NO_REDIRECTS",
    "RedirectPolicy",
    "RedirectPolicyError",
    "ResponseFormatError",
    "ResponseLimitError",
    "ResponseTransportError",
    "XML_MIME_POLICY",
    "XmlLimits",
    "aiohttp_request_bounded",
    "decode_limited_chunks",
    "parse_bounded_json",
    "read_aiohttp_response",
    "read_requests_response",
    "requests_request_bounded",
    "validate_bounded_xml",
]
