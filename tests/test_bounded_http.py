from __future__ import annotations

import gzip
from dataclasses import dataclass, field
from typing import Any

import pytest
from multidict import CIMultiDict

from core import bounded_http
from core.bounded_http import (
    BodyLimits,
    HttpStatusError,
    JsonLimits,
    MimePolicy,
    RedirectPolicy,
    RedirectPolicyError,
    ResponseFormatError,
    ResponseLimitError,
    ResponseTransportError,
    XmlLimits,
    aiohttp_request_bounded,
    decode_limited_chunks,
    parse_bounded_json,
    read_aiohttp_response,
    read_requests_response,
    requests_request_bounded,
    validate_bounded_xml,
)


class AsyncContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.requested_chunk_sizes: list[int] = []

    async def iter_chunked(self, size: int):
        self.requested_chunk_sizes.append(size)
        for chunk in self.chunks:
            yield chunk


class FailingAsyncContent:
    async def iter_chunked(self, _size: int):
        yield b"part"
        raise OSError("raw async canary detail")


class NeverReadAsyncContent:
    async def iter_chunked(self, _size: int):
        raise AssertionError("invalid headers must be rejected before body read")
        yield b""  # pragma: no cover


@dataclass
class AsyncResponse:
    status: int = 200
    url: str = "https://example.test/data"
    headers: dict[str, str] = field(default_factory=lambda: {"Content-Type": "application/json"})
    chunks: list[bytes] = field(default_factory=lambda: [b"{}"])
    content_length: int | None = None
    closed: bool = False

    def __post_init__(self) -> None:
        self.content = AsyncContent(self.chunks)

    def close(self) -> None:
        self.closed = True


class AsyncRequestContext:
    def __init__(self, response: AsyncResponse) -> None:
        self.response = response

    async def __aenter__(self) -> AsyncResponse:
        return self.response

    async def __aexit__(self, *_args: Any) -> None:
        self.response.close()


class AsyncSession:
    def __init__(self, responses: list[AsyncResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> AsyncRequestContext:
        self.calls.append((method, url, kwargs))
        return AsyncRequestContext(self.responses.pop(0))


class RawStream:
    def __init__(self, chunks: list[bytes], *, fail_if_read: bool = False) -> None:
        self.chunks = chunks
        self.fail_if_read = fail_if_read
        self.decode_content: bool | None = None
        self.calls: list[tuple[int, bool]] = []

    def stream(self, size: int, *, decode_content: bool):
        self.calls.append((size, decode_content))
        if self.fail_if_read:
            raise AssertionError("the response body must not be read")
        yield from self.chunks


class FailingRawStream:
    decode_content: bool | None = None

    def stream(self, _size: int, *, decode_content: bool):
        assert decode_content is False
        yield b"part"
        raise OSError("raw sync canary detail")


class SlowDripRawStream:
    decode_content: bool | None = None

    def __init__(self, clock: list[float]) -> None:
        self.clock = clock

    def stream(self, _size: int, *, decode_content: bool):
        assert decode_content is False
        yield b"first"
        self.clock[0] = 4.0
        yield b"second"


@dataclass
class SyncResponse:
    status_code: int = 200
    url: str = "https://example.test/data"
    headers: dict[str, str] = field(default_factory=lambda: {"Content-Type": "application/json"})
    chunks: list[bytes] = field(default_factory=lambda: [b"{}"])
    fail_if_read: bool = False
    closed: bool = False

    def __post_init__(self) -> None:
        self.raw = RawStream(self.chunks, fail_if_read=self.fail_if_read)

    def close(self) -> None:
        self.closed = True


class SyncSession:
    def __init__(self, responses: list[SyncResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> SyncResponse:
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


SMALL_LIMITS = BodyLimits(
    max_wire_bytes=128,
    max_decoded_bytes=256,
    max_decompression_ratio=20,
    ratio_grace_bytes=64,
    chunk_bytes=17,
)


@pytest.mark.parametrize(
    "field",
    [
        "max_wire_bytes",
        "max_decoded_bytes",
        "max_decompression_ratio",
        "ratio_grace_bytes",
        "chunk_bytes",
    ],
)
def test_body_limits_require_positive_values(field: str) -> None:
    values = {
        "max_wire_bytes": 1,
        "max_decoded_bytes": 1,
        "max_decompression_ratio": 1,
        "ratio_grace_bytes": 1,
        "chunk_bytes": 1,
    }
    values[field] = 0

    with pytest.raises(ValueError, match=field):
        BodyLimits(**values)


def test_identity_body_accepts_exact_boundary_and_rejects_next_byte() -> None:
    limits = BodyLimits(
        max_wire_bytes=4,
        max_decoded_bytes=4,
        max_decompression_ratio=1,
        ratio_grace_bytes=1,
        chunk_bytes=2,
    )

    assert decode_limited_chunks([b"ab", b"cd"], encoding="identity", limits=limits) == (
        b"abcd",
        4,
    )
    with pytest.raises(ResponseLimitError, match="wire body"):
        decode_limited_chunks([b"abcd", b"e"], encoding="identity", limits=limits)


def test_false_small_content_length_cannot_bypass_stream_limit() -> None:
    response = SyncResponse(
        headers={"Content-Type": "application/json", "Content-Length": "1"},
        chunks=[b"x" * 129],
    )

    with pytest.raises(ResponseLimitError, match="wire body"):
        read_requests_response(response, limits=SMALL_LIMITS)


def test_declared_oversize_is_rejected_before_reading() -> None:
    response = SyncResponse(
        headers={"Content-Type": "application/json", "Content-Length": "129"},
        fail_if_read=True,
    )

    with pytest.raises(ResponseLimitError, match="declared response body"):
        read_requests_response(response, limits=SMALL_LIMITS)
    assert response.raw.calls == []


@pytest.mark.asyncio
async def test_async_and_sync_gzip_readers_share_wire_decoded_contract() -> None:
    payload = b'{"value":"bounded"}'
    compressed = gzip.compress(payload, mtime=0)
    headers = {
        "Content-Type": "application/problem+json; charset=utf-8",
        "Content-Encoding": "gzip",
    }
    async_response = AsyncResponse(headers=headers, chunks=[compressed[:4], compressed[4:]])
    sync_response = SyncResponse(headers=headers, chunks=[compressed[:3], compressed[3:]])

    async_result = await read_aiohttp_response(
        async_response,
        limits=SMALL_LIMITS,
        mime_policy=bounded_http.JSON_MIME_POLICY,
    )
    sync_result = read_requests_response(
        sync_response,
        limits=SMALL_LIMITS,
        mime_policy=bounded_http.JSON_MIME_POLICY,
    )

    assert async_result.body == sync_result.body == payload
    assert async_result.wire_bytes == sync_result.wire_bytes == len(compressed)
    assert async_result.decoded_bytes == sync_result.decoded_bytes == len(payload)
    assert async_result.media_type == "application/problem+json"
    assert async_result.charset == "utf-8"
    assert async_response.content.requested_chunk_sizes == [17]
    assert sync_response.raw.calls == [(17, False)]
    assert sync_response.raw.decode_content is False


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        (gzip.compress(b"x" * 200, mtime=0), "decoded response body"),
        (gzip.compress(b"x" * 100, mtime=0)[:-2], "truncated compressed response"),
        (gzip.compress(b"ok", mtime=0) + b"trailing", "trailing data"),
        (
            gzip.compress(b"first", mtime=0) + gzip.compress(b"second", mtime=0),
            "trailing data",
        ),
    ],
    ids=("decoded-limit", "truncated-stream", "trailing-bytes", "concatenated-stream"),
)
def test_compressed_body_limits_and_integrity(wire: bytes, expected: str) -> None:
    limits = BodyLimits(
        max_wire_bytes=512,
        max_decoded_bytes=128,
        max_decompression_ratio=100,
        ratio_grace_bytes=128,
        chunk_bytes=64,
    )

    with pytest.raises((ResponseLimitError, ResponseFormatError), match=expected):
        decode_limited_chunks([wire], encoding="gzip", limits=limits)


def test_decompression_ratio_is_bounded_during_decode() -> None:
    wire = gzip.compress(b"x" * 512, mtime=0)
    limits = BodyLimits(
        max_wire_bytes=512,
        max_decoded_bytes=1_024,
        max_decompression_ratio=2,
        ratio_grace_bytes=8,
        chunk_bytes=64,
    )

    with pytest.raises(ResponseLimitError, match="decoded response body|decompression ratio"):
        decode_limited_chunks([wire], encoding="gzip", limits=limits)


@pytest.mark.parametrize("encoding", ["br", "gzip, deflate"])
def test_unknown_or_stacked_content_encoding_is_rejected(encoding: str) -> None:
    response = SyncResponse(headers={"Content-Encoding": encoding}, fail_if_read=True)

    with pytest.raises(ResponseFormatError, match="encoding"):
        read_requests_response(response, limits=SMALL_LIMITS)
    assert response.raw.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        CIMultiDict(
            [
                ("Content-Type", "application/json"),
                ("Content-Type", "text/html"),
            ]
        ),
        CIMultiDict(
            [
                ("Content-Encoding", "identity"),
                ("Content-Encoding", "gzip"),
            ]
        ),
        CIMultiDict(
            [
                ("Content-Length", "2"),
                ("Content-Length", "3"),
            ]
        ),
    ],
)
async def test_duplicate_security_headers_are_rejected_before_body_read(headers) -> None:
    response = AsyncResponse(headers=headers)
    response.content = NeverReadAsyncContent()

    with pytest.raises(ResponseFormatError, match="duplicate"):
        await read_aiohttp_response(response, limits=SMALL_LIMITS)


@pytest.mark.parametrize(
    ("content_type", "accepted"),
    [
        ("application/json", True),
        ("application/activity+json", True),
        ("text/json", False),
        (None, False),
        ("not-a-mime", False),
    ],
)
def test_mime_policy_is_checked_before_body_read(
    content_type: str | None,
    accepted: bool,
) -> None:
    headers = {} if content_type is None else {"Content-Type": content_type}
    response = SyncResponse(headers=headers, chunks=[b"{}"], fail_if_read=not accepted)

    if accepted:
        assert (
            read_requests_response(
                response,
                limits=SMALL_LIMITS,
                mime_policy=bounded_http.JSON_MIME_POLICY,
            ).body
            == b"{}"
        )
    else:
        with pytest.raises(ResponseFormatError, match="Content-Type|content type"):
            read_requests_response(
                response,
                limits=SMALL_LIMITS,
                mime_policy=bounded_http.JSON_MIME_POLICY,
            )
        assert response.raw.calls == []


def test_mime_policy_validates_configuration() -> None:
    with pytest.raises(ValueError, match="MIME"):
        MimePolicy(exact=frozenset({"json"}))
    with pytest.raises(ValueError, match="prefix"):
        MimePolicy(type_prefixes=frozenset({"image"}))
    with pytest.raises(ValueError, match="suffix"):
        MimePolicy(structured_suffixes=frozenset({"json"}))


def test_sync_wrapper_never_reads_error_body_and_always_closes() -> None:
    response = SyncResponse(status_code=500, chunks=[b"secret" * 1_000_000], fail_if_read=True)
    session = SyncSession([response])

    with pytest.raises(HttpStatusError) as caught:
        requests_request_bounded(
            "GET",
            "https://example.test/data",
            limits=SMALL_LIMITS,
            session=session,
        )

    assert caught.value.status == 500
    assert response.raw.calls == []
    assert response.closed is True


def test_sync_midstream_transport_failure_is_normalized_and_closed() -> None:
    response = SyncResponse(headers={"Content-Type": "application/json"})
    response.raw = FailingRawStream()
    session = SyncSession([response])

    with pytest.raises(ResponseTransportError, match="response body transport failed") as caught:
        requests_request_bounded(
            "GET",
            "https://example.test/data",
            limits=SMALL_LIMITS,
            session=session,
        )

    assert "canary" not in str(caught.value)
    assert response.closed is True


def test_sync_slow_drip_is_stopped_by_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(bounded_http.time, "monotonic", lambda: clock[0])
    response = SyncResponse(headers={"Content-Type": "application/json"})
    response.raw = SlowDripRawStream(clock)

    with pytest.raises(ResponseTransportError, match="total timeout"):
        requests_request_bounded(
            "GET",
            "https://example.test/data",
            limits=SMALL_LIMITS,
            session=SyncSession([response]),
            request_kwargs={"timeout": 3},
        )

    assert response.closed is True


@pytest.mark.asyncio
async def test_async_midstream_transport_failure_is_normalized_and_closed() -> None:
    response = AsyncResponse(headers={"Content-Type": "application/json"})
    response.content = FailingAsyncContent()
    session = AsyncSession([response])

    with pytest.raises(ResponseTransportError, match="response body transport failed") as caught:
        await aiohttp_request_bounded(
            session,
            "GET",
            "https://example.test/data",
            limits=SMALL_LIMITS,
        )

    assert "canary" not in str(caught.value)
    assert response.closed is True


@pytest.mark.asyncio
async def test_async_wrapper_forces_transport_guards() -> None:
    response = AsyncResponse()
    session = AsyncSession([response])

    result = await aiohttp_request_bounded(
        session,
        "get",
        "https://example.test/data",
        limits=SMALL_LIMITS,
        mime_policy=bounded_http.JSON_MIME_POLICY,
        request_kwargs={"timeout": 3},
    )

    assert result.body == b"{}"
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("GET", "https://example.test/data")
    assert kwargs["allow_redirects"] is False
    assert kwargs["auto_decompress"] is False
    assert kwargs["headers"]["Accept-Encoding"] == "gzip, deflate"
    assert kwargs["timeout"] == 3


def test_sync_wrapper_forces_transport_guards_and_raw_wire_mode() -> None:
    response = SyncResponse()
    session = SyncSession([response])

    result = requests_request_bounded(
        "get",
        "https://example.test/data",
        limits=SMALL_LIMITS,
        session=session,
        request_kwargs={"timeout": 3},
    )

    assert result.body == b"{}"
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("GET", "https://example.test/data")
    assert kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True
    assert kwargs["headers"]["Accept-Encoding"] == "gzip, deflate"
    assert kwargs["timeout"] == 3
    assert response.raw.calls == [(17, False)]
    assert response.closed is True


@pytest.mark.parametrize(
    ("wrapper", "reserved"),
    [
        ("async", "auto_decompress"),
        ("async", "allow_redirects"),
        ("sync", "stream"),
        ("sync", "headers"),
    ],
)
@pytest.mark.asyncio
async def test_transport_guards_cannot_be_overridden(wrapper: str, reserved: str) -> None:
    if wrapper == "async":
        with pytest.raises(ValueError, match="transport guards"):
            await aiohttp_request_bounded(
                AsyncSession([]),
                "GET",
                "https://example.test/data",
                limits=SMALL_LIMITS,
                request_kwargs={reserved: True},
            )
    else:
        with pytest.raises(ValueError, match="transport guards"):
            requests_request_bounded(
                "GET",
                "https://example.test/data",
                limits=SMALL_LIMITS,
                session=SyncSession([]),
                request_kwargs={reserved: True},
            )


def test_relative_same_origin_redirect_is_explicit_and_bounded() -> None:
    redirect = SyncResponse(
        status_code=302,
        url="https://example.test/start",
        headers={"Location": "/final"},
        fail_if_read=True,
    )
    final = SyncResponse(url="https://example.test/final")
    session = SyncSession([redirect, final])

    result = requests_request_bounded(
        "GET",
        "https://example.test/start",
        limits=SMALL_LIMITS,
        redirect_policy=RedirectPolicy(max_hops=1),
        session=session,
    )

    assert result.url == "https://example.test/final"
    assert [call[1] for call in session.calls] == [
        "https://example.test/start",
        "https://example.test/final",
    ]
    assert redirect.raw.calls == []
    assert redirect.closed and final.closed


@pytest.mark.parametrize(
    ("location", "message"),
    [
        ("https://other.test/final", "cross-origin"),
        ("http://example.test/final", "downgrade"),
        ("file:///etc/passwd", "scheme|absolute HTTP"),
    ],
)
def test_redirect_policy_rejects_cross_origin_downgrade_and_non_http(
    location: str,
    message: str,
) -> None:
    redirect = SyncResponse(
        status_code=302,
        url="https://example.test/start",
        headers={"Location": location},
        fail_if_read=True,
    )

    with pytest.raises(RedirectPolicyError, match=message):
        requests_request_bounded(
            "GET",
            "https://example.test/start",
            limits=SMALL_LIMITS,
            redirect_policy=RedirectPolicy(max_hops=1),
            session=SyncSession([redirect]),
        )
    assert redirect.closed


def test_cross_origin_redirect_strips_credentials_when_explicitly_allowed() -> None:
    redirect = SyncResponse(
        status_code=307,
        url="https://example.test/start",
        headers={"Location": "https://cdn.test/final"},
        fail_if_read=True,
    )
    final = SyncResponse(url="https://cdn.test/final")
    session = SyncSession([redirect, final])

    requests_request_bounded(
        "GET",
        "https://example.test/start",
        limits=SMALL_LIMITS,
        redirect_policy=RedirectPolicy(max_hops=1, same_origin_only=False),
        headers={
            "Author" + "ization": "Bearer canary",
            "Coo" + "kie": "secret=canary",
            "X-Request-ID": "safe",
        },
        session=session,
    )

    redirected_headers = session.calls[1][2]["headers"]
    assert "Authorization" not in redirected_headers
    assert "Cookie" not in redirected_headers
    assert redirected_headers["X-Request-ID"] == "safe"


def test_post_redirect_is_rejected_without_rewriting_method() -> None:
    redirect = SyncResponse(
        status_code=303,
        headers={"Location": "/final"},
        fail_if_read=True,
    )
    with pytest.raises(RedirectPolicyError, match="method"):
        requests_request_bounded(
            "POST",
            "https://example.test/start",
            limits=SMALL_LIMITS,
            redirect_policy=RedirectPolicy(max_hops=1),
            session=SyncSession([redirect]),
        )


def test_redirect_hop_limit_is_enforced() -> None:
    redirect = SyncResponse(
        status_code=302,
        headers={"Location": "/again"},
        fail_if_read=True,
    )
    with pytest.raises(RedirectPolicyError, match="too many"):
        requests_request_bounded(
            "GET",
            "https://example.test/start",
            limits=SMALL_LIMITS,
            session=SyncSession([redirect]),
        )


def test_json_valid_payload_and_utf8_bom() -> None:
    assert parse_bounded_json(b'\xef\xbb\xbf{"ok":true,"items":[1,null]}') == {
        "ok": True,
        "items": [1, None],
    }


@pytest.mark.parametrize(
    ("payload", "limits", "message"),
    [
        (b"[[[0]]]", JsonLimits(max_depth=2), "deeply nested"),
        (b"[1,2,3]", JsonLimits(max_nodes=3), "too many nodes"),
        (b'{"key":"value"}', JsonLimits(max_string_chars=5), "strings are too large"),
        (b"123456", JsonLimits(max_number_chars=5), "number token"),
        (b'"abcdef"', JsonLimits(max_bytes=5), "body is too large"),
    ],
)
def test_json_resource_limits(payload: bytes, limits: JsonLimits, message: str) -> None:
    with pytest.raises(ResponseLimitError, match=message):
        parse_bounded_json(payload, limits=limits)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b'{"key":1,"key":2}', "duplicate"),
        (b'{"value":NaN}', "non-finite"),
        (b'{"value":1e9999}', "non-finite"),
        (b'{"value":-1e9999}', "non-finite"),
        (b'{"open":', "invalid|unterminated"),
        (b'"\\x"', "escape"),
        (b'"\xff"', "UTF-8"),
    ],
)
def test_json_invalid_inputs_fail_closed(payload: bytes, message: str) -> None:
    with pytest.raises(ResponseFormatError, match=message):
        parse_bounded_json(payload)


def test_xml_valid_document_is_returned_unchanged() -> None:
    payload = b'<?xml version="1.0"?><feed><item id="1">safe</item></feed>'
    assert validate_bounded_xml(payload) == payload


@pytest.mark.parametrize(
    ("payload", "limits", "message"),
    [
        (b"<a><b><c/></b></a>", XmlLimits(max_depth=2), "deeply nested"),
        (b"<a><b/><c/></a>", XmlLimits(max_nodes=2), "too many nodes"),
        (b'<a x="1" y="2"/>', XmlLimits(max_attributes=1), "too many attributes"),
        (b'<a long="value"/>', XmlLimits(max_attribute_chars=5), "attributes are too large"),
        (b"<longname/>", XmlLimits(max_name_chars=4), "name is too large"),
        (b"<a>abcdef</a>", XmlLimits(max_text_chars=5), "text is too large"),
        (b"<a><!--one--><!--two--></a>", XmlLimits(max_nodes=2), "too many nodes"),
        (b"<a><?one x?><?two y?></a>", XmlLimits(max_nodes=2), "too many nodes"),
        (b"<a><!--abcdef--></a>", XmlLimits(max_text_chars=5), "text is too large"),
        (
            b"<a><?longname value?></a>",
            XmlLimits(max_name_chars=4),
            "instruction name is too large",
        ),
        (b"<a/>", XmlLimits(max_bytes=3), "body is too large"),
    ],
)
def test_xml_resource_limits(payload: bytes, limits: XmlLimits, message: str) -> None:
    with pytest.raises(ResponseLimitError, match=message):
        validate_bounded_xml(payload, limits=limits)


@pytest.mark.parametrize(
    "payload",
    [
        b'<!DOCTYPE a [<!ENTITY x "boom">]><a>&x;</a>',
        b'<!DOCTYPE a SYSTEM "https://example.test/external.dtd"><a/>',
    ],
)
def test_xml_dtd_and_entities_are_rejected(payload: bytes) -> None:
    with pytest.raises(ResponseFormatError, match="DTD|entities|declarations"):
        validate_bounded_xml(payload)


def test_xml_malformed_and_empty_documents_are_rejected() -> None:
    with pytest.raises(ResponseFormatError, match="invalid XML"):
        validate_bounded_xml(b"<a>")
    with pytest.raises(ResponseFormatError, match="invalid XML"):
        validate_bounded_xml(b"")


def test_xml_fails_closed_for_unsafe_expat_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bounded_http.expat, "version_info", (2, 7, 1))

    with pytest.raises(ResponseFormatError, match="parser version"):
        validate_bounded_xml(b"<a/>")
