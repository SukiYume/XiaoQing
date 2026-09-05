# 验证不可信 URL 的地址解析、重定向和内网访问限制。
"""Regression tests for the fail-closed untrusted URL HTTP client."""

from __future__ import annotations

import asyncio
import gzip
import socket
from urllib.parse import urlsplit

import pytest

from core import safe_http


def test_redact_url_for_log_removes_credentials_query_and_fragment() -> None:
    redacted = safe_http.redact_url_for_log(
        "https://user:password@example.com:8443/path?token=secret#private"
    )

    assert redacted == "https://example.com:8443/path?<redacted>#<redacted>"
    assert "user" not in redacted
    assert "password" not in redacted
    assert "secret" not in redacted
    assert "private" not in redacted


class _FakeLoop:
    def __init__(self, addresses: list[str]) -> None:
        self.addresses = addresses

    async def getaddrinfo(self, _host, _port, *, type, family):
        return [
            (socket.AF_INET6 if ":" in address else socket.AF_INET, type, 0, "", (address, 0))
            for address in self.addresses
        ]


class _Content:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self._chunks:
            yield chunk


class _Response:
    def __init__(self, status: int, *, headers=None, chunks=None) -> None:
        self.status         = status
        self.headers        = headers or {"Content-Type": "text/html"}
        self.content        = _Content(chunks or [])
        self.content_length = sum(len(chunk) for chunk in (chunks or []))
        self.charset        = "utf-8"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _Session:
    def __init__(self, responses, requested_urls, **kwargs) -> None:
        self._responses      = responses
        self._requested_urls = requested_urls
        self.kwargs          = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def get(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        self._requested_urls.append(url)
        return self._responses.pop(0)


def test_rejects_noncanonical_and_private_url_literals():
    for url in (
        "http://2130706433/",
        "http://0x7f000001/",
        "http://0177.0.0.1/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://example.com:8080/",
    ):
        with pytest.raises(safe_http.UnsafeUrlError):
            safe_http.validate_public_url(url)

    assert safe_http.validate_public_url("https://example.com/path").hostname == "example.com"


@pytest.mark.asyncio
async def test_rejects_dns_result_when_any_address_is_not_public(monkeypatch):
    monkeypatch.setattr(
        safe_http.asyncio, "get_running_loop", lambda: _FakeLoop(["8.8.8.8", "127.0.0.1"])
    )
    with pytest.raises(safe_http.UnsafeUrlError, match="non-public DNS"):
        await safe_http.resolve_public_host("rebind.example", 443)


@pytest.mark.asyncio
async def test_transparent_proxy_fake_dns_requires_explicit_opt_in(monkeypatch):
    fake_addresses = ["198.18.0.68", "fdfe:dcba:9876::30"]
    monkeypatch.setattr(
        safe_http.asyncio,
        "get_running_loop",
        lambda: _FakeLoop(fake_addresses),
    )

    with pytest.raises(safe_http.UnsafeUrlError, match="non-public DNS"):
        await safe_http.resolve_public_host("apod.nasa.gov", 443)

    assert await safe_http.resolve_public_host(
        "apod.nasa.gov",
        443,
        allow_transparent_proxy_fake_dns=True,
    ) == tuple(fake_addresses)

    monkeypatch.setattr(
        safe_http.asyncio,
        "get_running_loop",
        lambda: _FakeLoop(["198.18.0.68", "127.0.0.1"]),
    )
    with pytest.raises(safe_http.UnsafeUrlError, match="non-public DNS"):
        await safe_http.resolve_public_host(
            "apod.nasa.gov",
            443,
            allow_transparent_proxy_fake_dns=True,
        )


@pytest.mark.asyncio
async def test_pinned_resolver_only_returns_prevalidated_addresses():
    resolver = safe_http._PinnedResolver("example.com", ("8.8.8.8", "2001:4860:4860::8888"))
    records  = await resolver.resolve("example.com", 443)
    assert {record["host"] for record in records} == {"8.8.8.8", "2001:4860:4860::8888"}
    with pytest.raises(OSError, match="hostname mismatch"):
        await resolver.resolve("rebound.example", 443)


@pytest.mark.asyncio
async def test_each_redirect_target_is_validated_before_a_connection(monkeypatch):
    responses = [
        _Response(302, headers={"Location": "http://127.0.0.1/internal"}),
    ]
    requested_urls: list[str] = []

    async def validate(url):
        if "127.0.0.1" in url:
            raise safe_http.UnsafeUrlError("non-public redirect")
        return urlsplit(url), ("8.8.8.8",)

    monkeypatch.setattr(safe_http, "validate_public_fetch_target", validate)
    monkeypatch.setattr(
        safe_http.aiohttp,
        "ClientSession",
        lambda **kwargs: _Session(responses, requested_urls, **kwargs),
    )

    with pytest.raises(safe_http.UnsafeUrlError, match="non-public redirect"):
        await safe_http.fetch_public_html("https://example.com/redirect")
    assert requested_urls == ["https://example.com/redirect"]


@pytest.mark.asyncio
async def test_fake_dns_opt_in_does_not_follow_cross_host_redirect(monkeypatch):
    responses = [
        _Response(302, headers={"Location": "https://cdn.nasa.gov/apod.html"}),
        _Response(200, chunks=[b"<html/>\n"]),
    ]
    requested_urls: list[str]  = []
    fake_dns_flags: list[bool] = []

    async def validate(url, *, allow_transparent_proxy_fake_dns=False):
        fake_dns_flags.append(allow_transparent_proxy_fake_dns)
        return urlsplit(url), ("198.18.0.68" if allow_transparent_proxy_fake_dns else "8.8.8.8",)

    monkeypatch.setattr(safe_http, "validate_public_fetch_target", validate)
    monkeypatch.setattr(
        safe_http.aiohttp,
        "ClientSession",
        lambda **kwargs: _Session(responses, requested_urls, **kwargs),
    )

    response = await safe_http.fetch_public_html(
        "https://apod.nasa.gov/apod/astropix.html",
        allowed_hosts                    = {"apod.nasa.gov", "cdn.nasa.gov"},
        allow_transparent_proxy_fake_dns = True,
    )

    assert response is not None
    assert fake_dns_flags == [True, False]
    assert requested_urls == [
        "https://apod.nasa.gov/apod/astropix.html",
        "https://cdn.nasa.gov/apod.html",
    ]


@pytest.mark.asyncio
async def test_fake_dns_opt_in_rejects_same_host_https_downgrade(monkeypatch):
    responses = [_Response(302, headers={"Location": "http://apod.nasa.gov/plain"})]
    requested_urls: list[str] = []

    async def validate(url, *, allow_transparent_proxy_fake_dns=False):
        assert allow_transparent_proxy_fake_dns is True
        return urlsplit(url), ("198.18.0.68",)

    monkeypatch.setattr(safe_http, "validate_public_fetch_target", validate)
    monkeypatch.setattr(
        safe_http.aiohttp,
        "ClientSession",
        lambda **kwargs: _Session(responses, requested_urls, **kwargs),
    )

    with pytest.raises(safe_http.UnsafeUrlError, match="requires HTTPS"):
        await safe_http.fetch_public_html(
            "https://apod.nasa.gov/apod/astropix.html",
            allowed_hosts                    = {"apod.nasa.gov"},
            allow_transparent_proxy_fake_dns = True,
        )
    assert requested_urls == ["https://apod.nasa.gov/apod/astropix.html"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fetcher",
    [safe_http.fetch_public_html, safe_http.fetch_public_bytes],
)
async def test_fake_dns_opt_in_requires_an_explicit_host_allowlist(fetcher):
    with pytest.raises(ValueError, match="explicit host allowlist"):
        await fetcher(
            "https://apod.nasa.gov/apod/astropix.html",
            allow_transparent_proxy_fake_dns=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fetcher",
    [safe_http.fetch_public_html, safe_http.fetch_public_bytes],
)
async def test_fake_dns_opt_in_requires_https(fetcher):
    with pytest.raises(safe_http.UnsafeUrlError, match="requires HTTPS"):
        await fetcher(
            "http://apod.nasa.gov/apod/astropix.html",
            allowed_hosts                    = {"apod.nasa.gov"},
            allow_transparent_proxy_fake_dns = True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["file:///etc/passwd", "https://example.com:bad/"])
async def test_malformed_redirect_has_stable_error_before_second_request(
    monkeypatch,
    location,
):
    responses = [_Response(302, headers={"Location": location})]
    requested_urls: list[str] = []

    async def validate(url):
        return safe_http.validate_public_url(url), ("93.184.216.34",)

    monkeypatch.setattr(safe_http, "validate_public_fetch_target", validate)
    monkeypatch.setattr(
        safe_http.aiohttp,
        "ClientSession",
        lambda **kwargs: _Session(responses, requested_urls, **kwargs),
    )

    with pytest.raises(safe_http.UnsafeUrlError, match="invalid redirect URL"):
        await safe_http.fetch_public_html("https://example.com/start")
    assert requested_urls == ["https://example.com/start"]


@pytest.mark.asyncio
async def test_binary_fetch_revalidates_private_redirect(monkeypatch):
    responses = [_Response(302, headers={"Location": "http://127.0.0.1/image.png"})]
    requested_urls: list[str] = []

    async def validate(url):
        if "127.0.0.1" in url:
            raise safe_http.UnsafeUrlError("non-public redirect")
        return urlsplit(url), ("8.8.8.8",)

    monkeypatch.setattr(safe_http, "validate_public_fetch_target", validate)
    monkeypatch.setattr(
        safe_http.aiohttp,
        "ClientSession",
        lambda **kwargs: _Session(responses, requested_urls, **kwargs),
    )

    with pytest.raises(safe_http.UnsafeUrlError, match="non-public redirect"):
        await safe_http.fetch_public_bytes("https://example.com/image.png")
    assert requested_urls == ["https://example.com/image.png"]


@pytest.mark.parametrize(
    "redirect_target",
    [
        "http://10.0.0.1/image.png",
        "http://172.16.0.1/image.png",
        "http://192.168.1.1/image.png",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/image.png",
        "http://[fd00::1]/image.png",
        "http://[fe80::1]/image.png",
    ],
)
@pytest.mark.asyncio
async def test_binary_fetch_rejects_private_redirect_before_next_request(
    monkeypatch,
    redirect_target,
):
    responses = [_Response(302, headers={"Location": redirect_target})]
    requested_urls: list[str] = []

    async def validate(url):
        return safe_http.validate_public_url(url), ("8.8.8.8",)

    monkeypatch.setattr(safe_http, "validate_public_fetch_target", validate)
    monkeypatch.setattr(
        safe_http.aiohttp,
        "ClientSession",
        lambda **kwargs: _Session(responses, requested_urls, **kwargs),
    )

    with pytest.raises(safe_http.UnsafeUrlError):
        await safe_http.fetch_public_bytes("https://pbs.twimg.com/start")
    assert requested_urls == ["https://pbs.twimg.com/start"]


@pytest.mark.asyncio
async def test_binary_fetch_allows_only_declared_redirect_hosts(monkeypatch):
    responses = [
        _Response(302, headers={"Location": "https://ton.twitter.com/final.png"}),
        _Response(200, headers={"Content-Type": "image/png"}, chunks=[b"png"]),
    ]
    requested_urls: list[str] = []

    async def validate(url):
        return urlsplit(url), ("8.8.8.8",)

    monkeypatch.setattr(safe_http, "validate_public_fetch_target", validate)
    monkeypatch.setattr(
        safe_http.aiohttp,
        "ClientSession",
        lambda **kwargs: _Session(responses, requested_urls, **kwargs),
    )

    response = await safe_http.fetch_public_bytes(
        "https://pbs.twimg.com/start",
        allowed_hosts   = {"pbs.twimg.com", "ton.twitter.com"},
        allowed_schemes = {"https"},
    )

    assert response is not None and response.body == b"png"
    assert requested_urls == [
        "https://pbs.twimg.com/start",
        "https://ton.twitter.com/final.png",
    ]


@pytest.mark.parametrize(
    ("redirect_target", "match"),
    [
        ("https://public.example/final.png", "host"),
        ("http://pbs.twimg.com/final.png", "scheme"),
    ],
)
@pytest.mark.asyncio
async def test_binary_fetch_rejects_host_or_https_downgrade_before_next_request(
    monkeypatch,
    redirect_target,
    match,
):
    responses = [_Response(302, headers={"Location": redirect_target})]
    requested_urls: list[str] = []

    async def validate(url):
        return urlsplit(url), ("8.8.8.8",)

    monkeypatch.setattr(safe_http, "validate_public_fetch_target", validate)
    monkeypatch.setattr(
        safe_http.aiohttp,
        "ClientSession",
        lambda **kwargs: _Session(responses, requested_urls, **kwargs),
    )

    with pytest.raises(safe_http.UnsafeUrlError, match=match):
        await safe_http.fetch_public_bytes(
            "https://pbs.twimg.com/start",
            allowed_hosts   = {"pbs.twimg.com", "ton.twitter.com"},
            allowed_schemes = {"https"},
        )
    assert requested_urls == ["https://pbs.twimg.com/start"]


@pytest.mark.asyncio
async def test_binary_fetch_rejects_missing_and_excessive_redirects(monkeypatch):
    requested_urls: list[str] = []

    async def validate(url):
        return urlsplit(url), ("8.8.8.8",)

    monkeypatch.setattr(safe_http, "validate_public_fetch_target", validate)
    responses = [_Response(302, headers={})]
    monkeypatch.setattr(
        safe_http.aiohttp,
        "ClientSession",
        lambda **kwargs: _Session(responses, requested_urls, **kwargs),
    )
    assert await safe_http.fetch_public_bytes("https://example.com/start") is None

    responses.extend(
        _Response(302, headers={"Location": "/again"}) for _ in range(safe_http.MAX_REDIRECTS + 1)
    )
    with pytest.raises(safe_http.UnsafeUrlError, match="too many redirects"):
        await safe_http.fetch_public_bytes("https://example.com/start")


@pytest.mark.asyncio
async def test_binary_fetch_enforces_mime_encoding_and_actual_byte_limit(monkeypatch):
    requested_urls: list[str] = []

    async def validate(url):
        return urlsplit(url), ("8.8.8.8",)

    monkeypatch.setattr(safe_http, "validate_public_fetch_target", validate)

    responses = [_Response(200, headers={"Content-Type": "text/plain"}, chunks=[b"x"])]
    monkeypatch.setattr(
        safe_http.aiohttp,
        "ClientSession",
        lambda **kwargs: _Session(responses, requested_urls, **kwargs),
    )
    with pytest.raises(safe_http.SafeHttpError, match="content type"):
        await safe_http.fetch_public_bytes("https://example.com/image")

    encoded = _Response(
        200,
        headers = {"Content-Type": "image/png", "Content-Encoding": "gzip"},
        chunks  = [b"compressed"],
    )
    responses.append(encoded)
    with pytest.raises(safe_http.SafeHttpError, match="encoded binary"):
        await safe_http.fetch_public_bytes("https://example.com/image")

    oversized = _Response(200, headers={"Content-Type": "image/png"}, chunks=[b"1234"])
    oversized.content_length = 1
    responses.append(oversized)
    with pytest.raises(safe_http.SafeHttpError, match="too large"):
        await safe_http.fetch_public_bytes("https://example.com/image", max_bytes=3)


@pytest.mark.asyncio
async def test_binary_fetch_exact_mime_rejects_spoof_before_body_read(monkeypatch):
    class NeverReadContent:
        def iter_chunked(self, _size):
            raise AssertionError("invalid exact MIME body must not be read")

    response = _Response(200, headers={"Content-Type": "image/jpeg-malformed"})
    response.content          = NeverReadContent()
    requested_urls: list[str] = []

    async def validate(url):
        return urlsplit(url), ("8.8.8.8",)

    monkeypatch.setattr(safe_http, "validate_public_fetch_target", validate)
    monkeypatch.setattr(
        safe_http.aiohttp,
        "ClientSession",
        lambda **kwargs: _Session([response], requested_urls, **kwargs),
    )

    with pytest.raises(safe_http.SafeHttpError, match="content type"):
        await safe_http.fetch_public_bytes(
            "https://example.com/image",
            allowed_content_types={"image/jpeg"},
        )
    assert requested_urls == ["https://example.com/image"]


@pytest.mark.asyncio
async def test_redirects_are_manual_and_timeout_is_bounded(monkeypatch):
    responses = [
        _Response(307, headers={"Location": "/next"}),
        _Response(200, chunks=[b"<html><title>ok</title></html>"]),
    ]
    requested_urls: list[str]   = []
    session_options: list[dict] = []

    async def validate(url):
        return urlsplit(url), ("8.8.8.8",)

    def session_factory(**kwargs):
        session_options.append(kwargs)
        return _Session(responses, requested_urls, **kwargs)

    monkeypatch.setattr(safe_http, "validate_public_fetch_target", validate)
    monkeypatch.setattr(safe_http.aiohttp, "ClientSession", session_factory)

    response = await safe_http.fetch_public_html("https://example.com/start", timeout_seconds=4)
    assert response is not None and response.body.startswith(b"<html")
    assert requested_urls == ["https://example.com/start", "https://example.com/next"]
    assert all(
        options["auto_decompress"] is False and options["trust_env"] is False
        for options in session_options
    )
    assert all(
        options["timeout"].total == 4 and options["timeout"].sock_read == 4
        for options in session_options
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fetcher_name", ["fetch_public_html", "fetch_public_bytes"])
async def test_total_timeout_covers_dns_validation(monkeypatch, fetcher_name):
    cancelled = False

    async def blocked_dns(_url):
        nonlocal cancelled
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled = True
            raise

    monkeypatch.setattr(safe_http, "validate_public_fetch_target", blocked_dns)
    fetcher = getattr(safe_http, fetcher_name)

    with pytest.raises(asyncio.TimeoutError):
        await fetcher("https://example.com/resource", timeout_seconds=0.01)

    assert cancelled is True


@pytest.mark.asyncio
async def test_redirects_share_one_total_deadline(monkeypatch):
    responses = [
        _Response(302, headers={"Location": "/next"}),
        _Response(200, chunks=[b"<html/>"]),
    ]
    requested_urls: list[str] = []
    validation_count          = 0

    async def slow_validate(url):
        nonlocal validation_count
        validation_count += 1
        await asyncio.sleep(0.12)
        return urlsplit(url), ("8.8.8.8",)

    monkeypatch.setattr(safe_http, "validate_public_fetch_target", slow_validate)
    monkeypatch.setattr(
        safe_http.aiohttp,
        "ClientSession",
        lambda **kwargs: _Session(responses, requested_urls, **kwargs),
    )

    with pytest.raises(asyncio.TimeoutError):
        await safe_http.fetch_public_html(
            "https://example.com/start",
            timeout_seconds=0.2,
        )

    assert validation_count == 2
    assert requested_urls == ["https://example.com/start"]


@pytest.mark.asyncio
async def test_rejects_compressed_response_bomb():
    payload    = b"x" * (safe_http.MAX_RESPONSE_BYTES + 1)
    compressed = gzip.compress(payload)
    response   = _Response(
        200,
        headers = {"Content-Encoding": "gzip", "Content-Type": "text/html"},
        chunks  = [compressed],
    )
    with pytest.raises(
        safe_http.SafeHttpError,
        match="decompressed response is too large|decompression ratio",
    ):
        await safe_http._read_limited_body(response)


@pytest.mark.asyncio
async def test_rejects_excessive_decompression_ratio_before_response_limit():
    compressed = gzip.compress(b"x" * (128 * 1024))
    response   = _Response(
        200,
        headers = {"Content-Encoding": "gzip", "Content-Type": "text/html"},
        chunks  = [compressed],
    )
    with pytest.raises(safe_http.SafeHttpError, match="decompression ratio"):
        await safe_http._read_limited_body(response)


@pytest.mark.asyncio
async def test_public_html_rejects_spoofed_html_mime(monkeypatch):
    class NeverReadContent:
        def iter_chunked(self, _size):
            raise AssertionError("spoofed MIME body must not be read")

    async def validate(url):
        return safe_http.validate_public_url(url), ("93.184.216.34",)

    response = _Response(
        200,
        headers = {"Content-Type": "text/htmlx"},
        chunks  = [b"<html>spoof</html>"],
    )
    response.content          = NeverReadContent()
    requested_urls: list[str] = []
    monkeypatch.setattr(safe_http, "validate_public_fetch_target", validate)
    monkeypatch.setattr(
        safe_http.aiohttp,
        "ClientSession",
        lambda **kwargs: _Session([response], requested_urls, **kwargs),
    )

    assert await safe_http.fetch_public_html("https://example.com/") is None
    assert requested_urls == ["https://example.com/"]


@pytest.mark.asyncio
async def test_cross_origin_redirect_strips_caller_secrets(monkeypatch):
    responses = [
        _Response(302, headers={"Location": "https://cdn.example.com/final"}),
        _Response(200, headers={"Content-Type": "text/html"}, chunks=[b"<html/>"]),
    ]
    requested_urls: list[str]               = []
    requested_headers: list[dict[str, str]] = []

    async def validate(url):
        return safe_http.validate_public_url(url), ("93.184.216.34",)

    class CapturingSession(_Session):
        def get(self, url, **kwargs):
            requested_headers.append(dict(kwargs["headers"]))
            return super().get(url, **kwargs)

    monkeypatch.setattr(safe_http, "validate_public_fetch_target", validate)
    monkeypatch.setattr(
        safe_http.aiohttp,
        "ClientSession",
        lambda **kwargs: CapturingSession(responses, requested_urls, **kwargs),
    )

    result = await safe_http.fetch_public_html(
        "https://example.com/start",
        headers={
            "Author" + "ization": "Bearer cross-origin-canary",
            "Coo" + "kie": "session=cross-origin-canary",
            "X-API-Key": "cross-origin-canary",
            "User-Agent": "safe-agent",
            "Accept": "text/html",
        },
        allowed_hosts={"example.com", "cdn.example.com"},
    )

    assert result is not None and result.body == b"<html/>"
    assert requested_headers[0]["Authorization"] == "Bearer cross-origin-canary"
    assert requested_headers[1] == {
        "User-Agent": "safe-agent",
        "Accept": "text/html",
        "Accept-Encoding": "gzip, deflate",
    }
