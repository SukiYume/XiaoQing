"""Small, fail-closed HTTP client for untrusted URLs.

Plugins must use this module when a chat message controls the request target.
It validates every DNS result before connecting and pins the validated address
in an aiohttp resolver, which prevents a hostname from being rebound between
validation and the TCP connection.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from urllib.parse import SplitResult, urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from core.bounded_http import (
    HTML_MIME_POLICY,
    BodyLimits,
    BoundedHttpError,
    read_aiohttp_response,
)

ALLOWED_SCHEMES = {"http", "https"}
DEFAULT_PORTS = {"http": 80, "https": 443}
MAX_REDIRECTS = 3
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_DECOMPRESSION_RATIO = 20
MAX_COMPRESSED_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 10
_CROSS_ORIGIN_HEADER_ALLOWLIST = frozenset(
    {"accept", "accept-encoding", "accept-language", "range", "user-agent"}
)


class UnsafeUrlError(ValueError):
    """Raised when a URL is not an allowed public Internet target."""


class SafeHttpError(RuntimeError):
    """Raised when a remote response exceeds the safe client limits."""


@dataclass(frozen=True)
class SafeHttpResponse:
    """The bounded, decoded body returned by :func:`fetch_public_html`."""

    url: str
    status: int
    body: bytes
    charset: str | None
    headers: Mapping[str, str]


def _is_public_ip(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


def _looks_like_noncanonical_ipv4(host: str) -> bool:
    """Reject integer, octal and hexadecimal IPv4 spellings before DNS.

    Different resolvers disagree on forms such as ``2130706433`` and
    ``0x7f000001``.  Treating them as hostnames could turn a textual check into
    a loopback connection on one platform, so only canonical dotted-decimal
    literals are accepted as IPv4 addresses.
    """

    if re.fullmatch(r"(?:0x[0-9a-f]+|\d+)", host, flags=re.IGNORECASE):
        return True
    if "." not in host:
        return False
    parts = host.split(".")
    if len(parts) != 4:
        return False
    if all(re.fullmatch(r"(?:0x[0-9a-f]+|\d+)", part, flags=re.IGNORECASE) for part in parts):
        try:
            canonical = str(ipaddress.IPv4Address(host))
        except ipaddress.AddressValueError:
            return True
        return canonical != host
    return False


def validate_public_url(url: str) -> SplitResult:
    """Validate URL syntax and literal targets without performing DNS."""

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise UnsafeUrlError("invalid URL") from exc

    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").rstrip(".").lower()
    if scheme not in ALLOWED_SCHEMES or not host:
        raise UnsafeUrlError("only absolute http(s) URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("URL userinfo is not allowed")
    if port not in (None, DEFAULT_PORTS[scheme]):
        raise UnsafeUrlError("non-standard URL port is not allowed")
    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafeUrlError("local hostname is not allowed")
    if _looks_like_noncanonical_ipv4(host):
        raise UnsafeUrlError("noncanonical IPv4 literal is not allowed")

    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        return parsed
    if not literal_ip.is_global:
        raise UnsafeUrlError("non-public IP address is not allowed")
    return parsed


async def resolve_public_host(host: str, port: int) -> tuple[str, ...]:
    """Resolve every A/AAAA address and reject the whole result if one is private."""

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
            family=socket.AF_UNSPEC,
        )
    except socket.gaierror as exc:
        raise UnsafeUrlError("hostname could not be resolved") from exc

    addresses = tuple(dict.fromkeys(info[4][0] for info in infos))
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise UnsafeUrlError("hostname has a non-public DNS result")
    return addresses


async def validate_public_fetch_target(url: str) -> tuple[SplitResult, tuple[str, ...]]:
    """Validate URL and DNS records before an untrusted resource is used."""

    parsed = validate_public_url(url)
    addresses = await resolve_public_host(
        parsed.hostname or "", parsed.port or DEFAULT_PORTS[parsed.scheme.lower()]
    )
    return parsed, addresses


class _PinnedResolver(AbstractResolver):
    """aiohttp resolver which can return only the addresses already validated."""

    def __init__(self, hostname: str, addresses: tuple[str, ...]) -> None:
        self._hostname = hostname.rstrip(".").lower()
        self._addresses = addresses

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_UNSPEC,
    ) -> list[dict[str, object]]:
        if host.rstrip(".").lower() != self._hostname:
            raise OSError("resolver hostname mismatch")

        records: list[dict[str, object]] = []
        for address in self._addresses:
            ip = ipaddress.ip_address(address)
            address_family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
            if family not in (socket.AF_UNSPEC, address_family):
                continue
            records.append(
                {
                    "hostname": host,
                    "host": address,
                    "port": port,
                    "family": address_family,
                    "proto": 0,
                    "flags": 0,
                }
            )
        if not records:
            raise OSError("no validated address matches requested family")
        return records

    async def close(self) -> None:
        return None


def _response_charset(response: aiohttp.ClientResponse) -> str | None:
    try:
        return response.charset
    except (LookupError, ValueError):
        return None


def _url_origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise UnsafeUrlError("invalid redirect URL") from exc
    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").rstrip(".").casefold()
    if scheme not in DEFAULT_PORTS or not host:
        raise UnsafeUrlError("invalid redirect URL")
    return scheme, host, port or DEFAULT_PORTS[scheme]


def _headers_after_redirect(
    headers: Mapping[str, str],
    *,
    current_url: str,
    next_url: str,
) -> dict[str, str]:
    if _url_origin(current_url) == _url_origin(next_url):
        return dict(headers)
    return {
        key: value
        for key, value in headers.items()
        if key.casefold() in _CROSS_ORIGIN_HEADER_ALLOWLIST
    }


def _public_request_headers(
    headers: Mapping[str, str] | None,
    *,
    binary: bool,
) -> dict[str, str]:
    result = {str(key): str(value) for key, value in (headers or {}).items()}
    requested_encoding = ""
    for key in tuple(result):
        if key.casefold() == "accept-encoding":
            requested_encoding = result.pop(key).strip().casefold()
    if binary or requested_encoding == "identity":
        result["Accept-Encoding"] = "identity"
    else:
        result["Accept-Encoding"] = "gzip, deflate"
    return result


async def _read_limited_body(response: aiohttp.ClientResponse) -> bytes:
    """Read a response with compressed and decompressed size limits."""
    limits = BodyLimits(
        max_wire_bytes=MAX_COMPRESSED_BYTES,
        max_decoded_bytes=MAX_RESPONSE_BYTES,
        max_decompression_ratio=MAX_DECOMPRESSION_RATIO,
        ratio_grace_bytes=64 * 1024,
        chunk_bytes=64 * 1024,
    )
    try:
        return (await read_aiohttp_response(response, limits=limits)).body
    except BoundedHttpError as exc:
        message = str(exc).replace(
            "decoded response body is too large",
            "decompressed response is too large",
        )
        raise SafeHttpError(message) from exc


async def fetch_public_html(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    allowed_hosts: Collection[str] | None = None,
) -> SafeHttpResponse | None:
    """Fetch one public HTML document with DNS pinning and checked redirects."""

    current_url = url
    current_headers = _public_request_headers(headers, binary=False)
    timeout = aiohttp.ClientTimeout(total=timeout_seconds, sock_read=min(5, timeout_seconds))
    for _ in range(MAX_REDIRECTS + 1):
        parsed, addresses = await validate_public_fetch_target(current_url)
        if allowed_hosts is not None and (parsed.hostname or "").rstrip(".").lower() not in {
            host.rstrip(".").lower() for host in allowed_hosts
        }:
            raise UnsafeUrlError("URL host is not allowed")
        resolver = _PinnedResolver(parsed.hostname or "", addresses)
        connector = aiohttp.TCPConnector(
            resolver=resolver,
            use_dns_cache=False,
            force_close=True,
        )
        try:
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                auto_decompress=False,
                trust_env=False,
            ) as session:
                async with session.get(
                    current_url,
                    headers=current_headers,
                    allow_redirects=False,
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            return None
                        next_url = urljoin(current_url, location)
                        current_headers = _headers_after_redirect(
                            current_headers,
                            current_url=current_url,
                            next_url=next_url,
                        )
                        current_url = next_url
                        continue
                    if response.status != 200:
                        return None
                    content_type = (
                        response.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
                    )
                    if not HTML_MIME_POLICY.accepts(content_type or None):
                        return None
                    body = await _read_limited_body(response)
                    return SafeHttpResponse(
                        url=current_url,
                        status=response.status,
                        body=body,
                        charset=_response_charset(response),
                        headers=response.headers,
                    )
        finally:
            await resolver.close()
            await connector.close()

    raise UnsafeUrlError("too many redirects")


async def _read_identity_body_limited(
    response: aiohttp.ClientResponse,
    *,
    max_bytes: int,
) -> bytes:
    """Read an untrusted binary response without transparent decompression."""
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in {"", "identity"}:
        raise SafeHttpError("encoded binary responses are not allowed")
    try:
        return (
            await read_aiohttp_response(
                response,
                limits=BodyLimits(
                    max_wire_bytes=max_bytes,
                    max_decoded_bytes=max_bytes,
                    max_decompression_ratio=1,
                    ratio_grace_bytes=1,
                    chunk_bytes=64 * 1024,
                ),
            )
        ).body
    except BoundedHttpError as exc:
        raise SafeHttpError("response is too large") from exc


async def fetch_public_bytes(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
    allowed_content_type_prefixes: tuple[str, ...] = ("image/",),
    allowed_content_types: Collection[str] | None = None,
    allowed_hosts: Collection[str] | None = None,
    allowed_schemes: Collection[str] | None = None,
) -> SafeHttpResponse | None:
    """Fetch bounded bytes from a public URL with DNS pinning on every hop."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    current_url = url
    current_headers = _public_request_headers(headers, binary=True)
    timeout = aiohttp.ClientTimeout(total=timeout_seconds, sock_read=min(5, timeout_seconds))
    for _ in range(MAX_REDIRECTS + 1):
        parsed, addresses = await validate_public_fetch_target(current_url)
        if allowed_schemes is not None and parsed.scheme.lower() not in {
            scheme.lower() for scheme in allowed_schemes
        }:
            raise UnsafeUrlError("URL scheme is not allowed")
        if allowed_hosts is not None and (parsed.hostname or "").rstrip(".").lower() not in {
            host.rstrip(".").lower() for host in allowed_hosts
        }:
            raise UnsafeUrlError("URL host is not allowed")
        resolver = _PinnedResolver(parsed.hostname or "", addresses)
        connector = aiohttp.TCPConnector(
            resolver=resolver,
            use_dns_cache=False,
            force_close=True,
        )
        try:
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                auto_decompress=False,
                trust_env=False,
            ) as session:
                async with session.get(
                    current_url,
                    headers=current_headers,
                    allow_redirects=False,
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            return None
                        next_url = urljoin(current_url, location)
                        current_headers = _headers_after_redirect(
                            current_headers,
                            current_url=current_url,
                            next_url=next_url,
                        )
                        current_url = next_url
                        continue
                    if response.status != 200:
                        return None
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                    if allowed_content_types is not None and content_type not in {
                        value.strip().casefold() for value in allowed_content_types
                    }:
                        raise SafeHttpError("response content type is not allowed")
                    if allowed_content_type_prefixes and not any(
                        content_type.startswith(prefix.lower())
                        for prefix in allowed_content_type_prefixes
                    ):
                        raise SafeHttpError("response content type is not allowed")
                    body = await _read_identity_body_limited(response, max_bytes=max_bytes)
                    return SafeHttpResponse(
                        url=current_url,
                        status=response.status,
                        body=body,
                        charset=None,
                        headers=response.headers,
                    )
        finally:
            await resolver.close()
            await connector.close()
    raise UnsafeUrlError("too many redirects")
