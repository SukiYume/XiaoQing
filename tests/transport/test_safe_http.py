# 验证不可信 URL 的地址解析、重定向和内网访问限制。
"""Fail-closed HTTP client for untrusted URLs."""

from __future__ import annotations

import asyncio
import gzip
import socket

import pytest

from core import safe_http


@pytest.mark.parametrize(
    "proxy",
    [
        "http://proxy.example",
        "HTTPS://proxy.example:8443",
        "http://proxy.example/",
        "http://user:pass@127.0.0.1:7890",
        "http://[::1]:8080",
        "http://proxy.example:1",
        "http://proxy.example:65535",
    ],
)
def test_validated_proxy_url_returns_accepted_values_unchanged(proxy: str) -> None:
    assert safe_http.validated_proxy_url(proxy) == proxy


@pytest.mark.parametrize(
    "proxy",
    [
        None,
        123,
        b"http://proxy.example",
        "",
        " ",
        "proxy.example:8080",
        "ftp://proxy.example",
        "socks5://proxy.example",
        "http://",
        "http:///path",
        "http://proxy.example/path",
        "http://proxy.example?key=value",
        "http://proxy.example#fragment",
        "http://proxy.example:0",
        "http://proxy.example:65536",
        "http://proxy.example:bad",
        "http://[::1",
        "http://proxy.example\x00",
        "http://proxy.example\n",
        "http://proxy.example\x7f",
        " http://proxy.example",
        "http://proxy.example ",
        "h" * (safe_http.MAX_PROXY_URL_CHARS + 1),
    ],
)
def test_validated_proxy_url_rejects_malformed_values(proxy: object) -> None:
    with pytest.raises(ValueError):
        safe_http.validated_proxy_url(proxy)


def test_validated_proxy_url_allows_a_path_only_when_asked() -> None:
    proxy = "https://proxy.example:8443/relay/v1"

    assert safe_http.validated_proxy_url(proxy, allow_path=True) == proxy
    with pytest.raises(ValueError):
        safe_http.validated_proxy_url(proxy)
    for rejected in ("http://proxy.example/a?q=1", "http://proxy.example/a#f"):
        with pytest.raises(ValueError):
            safe_http.validated_proxy_url(rejected, allow_path=True)


def test_split_https_url_on_hosts_accepts_only_plain_https_on_listed_hosts() -> None:
    hosts = frozenset({"img.example"})

    accepted = safe_http.split_https_url_on_hosts("https://IMG.example.:443/a.jpg?x=1", hosts)
    assert accepted is not None
    assert (accepted.path, accepted.query) == ("/a.jpg", "x=1")
    for rejected in (
        "http://img.example/a.jpg",
        "https://img.example.evil.test/a.jpg",
        "https://user@img.example/a.jpg",
        "https://user:pw@img.example/a.jpg",
        "https://img.example:8443/a.jpg",
        "https://img.example:bad/a.jpg",
        "//img.example/a.jpg",
        "",
    ):
        assert safe_http.split_https_url_on_hosts(rejected, hosts) is None, rejected


@pytest.mark.parametrize(
    "proxy",
    [
        "ftp://user:hunter2@proxy.example",
        "http://user:hunter2@proxy.example:bad",
        "http://user:hunter2@proxy.example:70000",
        "http://user:hunter2@proxy.example/path",
        "http://user:hunter2@proxy.example\x00",
    ],
)
def test_validated_proxy_url_errors_never_expose_the_credentials(proxy: str) -> None:
    with pytest.raises(ValueError) as caught:
        safe_http.validated_proxy_url(proxy)

    error = caught.value
    assert "hunter2" not in str(error)
    assert error.__cause__ is None
    assert error.__suppress_context__ or error.__context__ is None


class _FakeDns:
    """DNS 边界：替换运行中事件循环的 `getaddrinfo`，产品的地址校验照常运行。

    没有单独指定的主机名都解析到 `default`（默认是一个公网地址）。
    """

    def __init__(
        self,
        monkeypatch,
        answers: dict[str, list[str]] | None = None,
        *,
        default: tuple[str, ...] = ("8.8.8.8",),
        delay: float             = 0.0,
        block: bool              = False,
    ) -> None:
        self.answers            = dict(answers or {})
        self.default            = default
        self.delay              = delay
        self.block              = block
        self.lookups: list[str] = []
        self.cancelled          = False
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", self._getaddrinfo)

    async def _getaddrinfo(self, host, _port, *, type, family):
        self.lookups.append(host)
        if self.block:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self.delay:
            await asyncio.sleep(self.delay)
        addresses = self.answers.get(host, self.default)
        return [
            (socket.AF_INET6 if ":" in address else socket.AF_INET, type, 0, "", (address, 0))
            for address in addresses
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
    _FakeDns(monkeypatch, default=("8.8.8.8", "127.0.0.1"))
    with pytest.raises(safe_http.UnsafeUrlError, match="non-public DNS"):
        await safe_http.resolve_public_host("rebind.example", 443)


@pytest.mark.asyncio
async def test_transparent_proxy_fake_dns_requires_explicit_opt_in(monkeypatch):
    fake_addresses = ["198.18.0.68", "fdfe:dcba:9876::30"]
    dns = _FakeDns(monkeypatch, default=tuple(fake_addresses))

    with pytest.raises(safe_http.UnsafeUrlError, match="non-public DNS"):
        await safe_http.resolve_public_host("apod.nasa.gov", 443)

    assert await safe_http.resolve_public_host(
        "apod.nasa.gov",
        443,
        allow_transparent_proxy_fake_dns=True,
    ) == tuple(fake_addresses)

    dns.default = ("198.18.0.68", "127.0.0.1")
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

    _FakeDns(monkeypatch)
    monkeypatch.setattr(
        safe_http.aiohttp,
        "ClientSession",
        lambda **kwargs: _Session(responses, requested_urls, **kwargs),
    )

    with pytest.raises(safe_http.UnsafeUrlError, match="non-public IP address"):
        await safe_http.fetch_public_html("https://example.com/redirect")
    assert requested_urls == ["https://example.com/redirect"]


@pytest.mark.asyncio
async def test_fake_dns_opt_in_does_not_follow_cross_host_redirect(monkeypatch):
    responses = [
        _Response(302, headers={"Location": "https://cdn.nasa.gov/apod.html"}),
        _Response(200, chunks=[b"<html/>\n"]),
    ]
    requested_urls: list[str] = []

    # 起始主机解析到透明代理的假 DNS 地址，跳转目标解析到公网地址。
    dns = _FakeDns(monkeypatch, {"apod.nasa.gov": ["198.18.0.68"]})
    monkeypatch.setattr(
        safe_http.aiohttp,
        "ClientSession",
        lambda **kwargs: _Session(responses, requested_urls, **kwargs),
    )
    fetch_kwargs = {
        "allowed_hosts"                   : {"apod.nasa.gov", "cdn.nasa.gov"},
        "allow_transparent_proxy_fake_dns": True,
    }

    response = await safe_http.fetch_public_html(
        "https://apod.nasa.gov/apod/astropix.html", **fetch_kwargs
    )

    assert response is not None
    assert dns.lookups == ["apod.nasa.gov", "cdn.nasa.gov"]
    assert requested_urls == [
        "https://apod.nasa.gov/apod/astropix.html",
        "https://cdn.nasa.gov/apod.html",
    ]

    # 假 DNS 的许可只属于起始主机：跳转到别的主机后，它解析到假 DNS 地址就被拒。
    dns.answers["cdn.nasa.gov"] = ["198.18.0.68"]
    responses[:]                = [
        _Response(302, headers={"Location": "https://cdn.nasa.gov/apod.html"}),
        _Response(200, chunks=[b"<html/>\n"]),
    ]
    requested_urls.clear()
    with pytest.raises(safe_http.UnsafeUrlError, match="non-public DNS"):
        await safe_http.fetch_public_html(
            "https://apod.nasa.gov/apod/astropix.html", **fetch_kwargs
        )
    assert requested_urls == ["https://apod.nasa.gov/apod/astropix.html"]


@pytest.mark.asyncio
async def test_fake_dns_opt_in_rejects_same_host_https_downgrade(monkeypatch):
    responses = [_Response(302, headers={"Location": "http://apod.nasa.gov/plain"})]
    requested_urls: list[str] = []

    _FakeDns(monkeypatch, {"apod.nasa.gov": ["198.18.0.68"]})
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

    _FakeDns(monkeypatch, default=("93.184.216.34",))
    monkeypatch.setattr(
        safe_http.aiohttp,
        "ClientSession",
        lambda **kwargs: _Session(responses, requested_urls, **kwargs),
    )

    with pytest.raises(safe_http.UnsafeUrlError, match="invalid redirect URL"):
        await safe_http.fetch_public_html("https://example.com/start")
    assert requested_urls == ["https://example.com/start"]


@pytest.mark.parametrize(
    "redirect_target",
    [
        "http://127.0.0.1/image.png",
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

    _FakeDns(monkeypatch)
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

    _FakeDns(monkeypatch)
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

    _FakeDns(monkeypatch)
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

    _FakeDns(monkeypatch)
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

    _FakeDns(monkeypatch)

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

    _FakeDns(monkeypatch)
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
    requested_urls : list[str]  = []
    session_options: list[dict] = []

    _FakeDns(monkeypatch)

    def session_factory(**kwargs):
        session_options.append(kwargs)
        return _Session(responses, requested_urls, **kwargs)

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
    dns = _FakeDns(monkeypatch, block=True)
    fetcher = getattr(safe_http, fetcher_name)

    with pytest.raises(asyncio.TimeoutError):
        await fetcher("https://example.com/resource", timeout_seconds=0.01)

    assert dns.cancelled is True


@pytest.mark.asyncio
async def test_redirects_share_one_total_deadline(monkeypatch):
    responses = [
        _Response(302, headers={"Location": "/next"}),
        _Response(200, chunks=[b"<html/>"]),
    ]
    requested_urls: list[str] = []

    dns = _FakeDns(monkeypatch, delay=0.12)
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

    assert len(dns.lookups) == 2
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

    _FakeDns(monkeypatch, default=("93.184.216.34",))
    response = _Response(
        200,
        headers = {"Content-Type": "text/htmlx"},
        chunks  = [b"<html>spoof</html>"],
    )
    response.content          = NeverReadContent()
    requested_urls: list[str] = []
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
    requested_urls   : list[str]            = []
    requested_headers: list[dict[str, str]] = []

    _FakeDns(monkeypatch, default=("93.184.216.34",))

    class CapturingSession(_Session):
        def get(self, url, **kwargs):
            requested_headers.append(dict(kwargs["headers"]))
            return super().get(url, **kwargs)

    monkeypatch.setattr(
        safe_http.aiohttp,
        "ClientSession",
        lambda **kwargs: CapturingSession(responses, requested_urls, **kwargs),
    )

    result = await safe_http.fetch_public_html(
        "https://example.com/start",
        headers={
            "Author" + "ization": "Bearer cross-origin-canary",
            "Coo" + "kie"       : "session=cross-origin-canary",
            "X-API-Key"         : "cross-origin-canary",
            "User-Agent"        : "safe-agent",
            "Accept"            : "text/html",
        },
        allowed_hosts={"example.com", "cdn.example.com"},
    )

    assert result is not None and result.body == b"<html/>"
    assert requested_headers[0]["Authorization"] == "Bearer cross-origin-canary"
    assert requested_headers[1] == {
        "User-Agent"     : "safe-agent",
        "Accept"         : "text/html",
        "Accept-Encoding": "gzip, deflate",
    }
