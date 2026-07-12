from __future__ import annotations

import ast
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from core import bounded_http
from core.bounded_http import (
    BoundedHttpResponse,
    ResponseFormatError,
    ResponseLimitError,
    ResponseTransportError,
)
from plugins.astro_tools import obj

ROOT = Path(__file__).resolve().parents[2]
_COLUMN_NAMES = ("ra", "dec", "otype", "V", "sp_type")
_VALID_ROW = (10.6847083, 41.26875, "Galaxy", 3.44, "SA(s)b")


class _RawBody:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks
        self.stream_calls = 0
        self.decode_content: bool | None = None

    def stream(self, _chunk_size: int, *, decode_content: bool) -> Iterator[bytes]:
        self.stream_calls += 1
        assert decode_content is False
        yield from self.chunks


class _ClockedRawBody(_RawBody):
    def __init__(self, chunks: tuple[bytes, ...], clock: list[float]) -> None:
        super().__init__(chunks)
        self.clock = clock

    def stream(self, _chunk_size: int, *, decode_content: bool) -> Iterator[bytes]:
        self.stream_calls += 1
        assert decode_content is False
        for chunk in self.chunks:
            self.clock[0] += 7.25
            yield chunk


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        content_type: str = "application/json; charset=utf-8",
        content_length: int | None = None,
    ) -> None:
        self.status_code = 200
        self.url = obj.SIMBAD_TAP_SYNC_URL
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.raw = _RawBody((body,))
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _table_payload(
    *,
    names: tuple[str, ...] = _COLUMN_NAMES,
    rows: tuple[tuple[object, ...], ...] = (_VALID_ROW,),
) -> dict[str, object]:
    return {
        "metadata": [{"name": name} for name in names],
        "data": [list(row) for row in rows],
    }


def _encoded_payload(**kwargs: Any) -> bytes:
    return json.dumps(_table_payload(**kwargs), separators=(",", ":")).encode()


def test_simbad_tap_post_is_streamed_bounded_and_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Response(_encoded_payload())
    captured: dict[str, Any] = {}

    def request(method: str, url: str, **kwargs: Any) -> _Response:
        captured.update({"method": method, "url": url, **kwargs})
        return response

    monkeypatch.setattr(obj, "_build_simbad_query", lambda _name: "SELECT TOP 1 ra")
    monkeypatch.setattr(requests, "request", request)

    result = obj._query_simbad_object("M31")

    assert result == obj.SimbadRow(
        ra_deg=10.6847083,
        dec_deg=41.26875,
        otype="Galaxy",
        v_magnitude=3.44,
        sp_type="SA(s)b",
    )
    assert captured["method"] == "POST"
    assert captured["url"] == obj.SIMBAD_TAP_SYNC_URL
    assert captured["stream"] is True
    assert captured["allow_redirects"] is False
    assert captured["timeout"] == (
        obj.SIMBAD_CONNECT_TIMEOUT_SECONDS,
        obj.SIMBAD_REQUEST_TIMEOUT_SECONDS,
    )
    assert captured["data"] == {
        "REQUEST": "doQuery",
        "LANG": "ADQL",
        "FORMAT": "json",
        "MAXREC": "1",
        "QUERY": "SELECT TOP 1 ra",
    }
    assert captured["headers"]["Accept"] == "application/json"
    assert captured["headers"]["Accept-Encoding"] == "gzip, deflate"
    assert response.raw.stream_calls == 1
    assert response.closed is True


def test_simbad_passes_total_deadline_shorter_than_outer_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _encoded_payload()
    captured: dict[str, Any] = {}

    def bounded_request(*_args: Any, **kwargs: Any) -> BoundedHttpResponse:
        captured.update(kwargs)
        return BoundedHttpResponse(
            url=obj.SIMBAD_TAP_SYNC_URL,
            status=200,
            body=body,
            media_type="application/json",
            charset="utf-8",
            headers={"Content-Type": "application/json"},
            wire_bytes=len(body),
            decoded_bytes=len(body),
        )

    monkeypatch.setattr(obj, "_build_simbad_query", lambda _name: "SELECT TOP 1 ra")
    monkeypatch.setattr(obj, "requests_request_bounded", bounded_request)

    assert obj._query_simbad_object("M31") is not None
    assert captured["total_timeout_seconds"] == 14
    assert (
        captured["total_timeout_seconds"]
        < obj.SIMBAD_TOTAL_TIMEOUT_SECONDS
    )


def test_simbad_slow_drip_hits_transport_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    response = _Response(b"")
    response.raw = _ClockedRawBody((b"{", b"}"), clock)
    monkeypatch.setattr(obj, "_build_simbad_query", lambda _name: "SELECT TOP 1 ra")
    monkeypatch.setattr(requests, "request", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(bounded_http.time, "monotonic", lambda: clock[0])

    with pytest.raises(ResponseTransportError, match="total timeout"):
        obj._query_simbad_object("M31")

    assert clock[0] == 14.5
    assert response.closed is True


def test_simbad_rejects_false_content_length_wire_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Response(
        b"{" + b"x" * obj._SIMBAD_BODY_LIMITS.max_wire_bytes,
        content_length=2,
    )
    monkeypatch.setattr(obj, "_build_simbad_query", lambda _name: "SELECT TOP 1 ra")
    monkeypatch.setattr(requests, "request", lambda *_args, **_kwargs: response)

    with pytest.raises(ResponseLimitError):
        obj._query_simbad_object("M31")

    assert response.raw.stream_calls == 1
    assert response.closed is True


def test_simbad_rejects_wrong_mime_before_reading_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Response(b"SIMBAD_MIME_BODY_CANARY", content_type="text/html")
    monkeypatch.setattr(obj, "_build_simbad_query", lambda _name: "SELECT TOP 1 ra")
    monkeypatch.setattr(requests, "request", lambda *_args, **_kwargs: response)

    with pytest.raises(ResponseFormatError):
        obj._query_simbad_object("M31")

    assert response.raw.stream_calls == 0
    assert response.closed is True


def test_simbad_json_depth_is_bounded_before_shape_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deep_value: object = None
    for _ in range(obj._SIMBAD_JSON_LIMITS.max_depth + 1):
        deep_value = [deep_value]
    response = _Response(json.dumps(deep_value).encode())
    monkeypatch.setattr(obj, "_build_simbad_query", lambda _name: "SELECT TOP 1 ra")
    monkeypatch.setattr(requests, "request", lambda *_args, **_kwargs: response)

    with pytest.raises(ResponseLimitError):
        obj._query_simbad_object("M31")


@pytest.mark.parametrize(
    ("payload", "error_match"),
    [
        ({}, "JSON table"),
        (_table_payload(names=("ra", "dec", "otype", "V")), "required columns"),
        (
            _table_payload(names=("ra", "RA", "otype", "V", "sp_type")),
            "duplicate",
        ),
        (
            _table_payload(rows=((10.0, 20.0, "Star", 1.0),)),
            "row width",
        ),
        (
            _table_payload(rows=(_VALID_ROW, _VALID_ROW)),
            "MAXREC",
        ),
        (
            _table_payload(names=("ra", "dec", "bad-name", "V", "sp_type")),
            "column name",
        ),
    ],
)
def test_simbad_rejects_malformed_metadata_and_row_shapes(
    payload: object,
    error_match: str,
) -> None:
    with pytest.raises(ResponseFormatError, match=error_match):
        obj._validate_simbad_payload(payload)


@pytest.mark.parametrize(
    ("row", "error_match"),
    [
        ((360.0, 20.0, "Star", 1.0, "G2V"), "RA"),
        ((10.0, -91.0, "Star", 1.0, "G2V"), "Dec"),
        ((True, 20.0, "Star", 1.0, "G2V"), "RA"),
        ((10.0, 20.0, "Star", "bright", "G2V"), "V"),
        ((10.0, 20.0, "x" * 129, 1.0, "G2V"), "otype"),
        ((10.0, 20.0, "Star", 1.0, "x" * 129), "sp_type"),
    ],
)
def test_simbad_rejects_invalid_coordinate_and_scalar_values(
    row: tuple[object, ...],
    error_match: str,
) -> None:
    with pytest.raises(ResponseFormatError, match=error_match):
        obj._validate_simbad_payload(_table_payload(rows=(row,)))


def test_simbad_empty_data_is_a_clean_not_found_result() -> None:
    assert obj._validate_simbad_payload(_table_payload(rows=())) is None


def test_simbad_adql_builder_limits_rows_and_escapes_quotes() -> None:
    query = obj._build_simbad_query("O'Brien's Star")

    assert "SELECT TOP 1" in query.upper()
    assert "O''Brien''s Star" in query


@pytest.mark.asyncio
async def test_simbad_transport_error_is_public_and_does_not_log_raw_details(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_endpoint = "https://private.example.invalid/secret-path"
    raw_secret = "SIMBAD_RAW_ERROR_SECRET"

    def fail(_name: str) -> None:
        raise requests.ConnectTimeout(f"request to {raw_endpoint} api_key={raw_secret}")

    logger = logging.getLogger("test.cr221.simbad")
    context = SimpleNamespace(
        logger=logger,
        request_id="simbad-request",
        secrets={"plugins": {"astro_tools": {"api_key": raw_secret}}},
    )
    monkeypatch.setattr(obj, "_query_simbad_object", fail)

    with caplog.at_level(logging.ERROR, logger=logger.name):
        result = await obj.handle_obj("M31", context)

    assert "XQ-PLUGIN-UNEXPECTED" in result
    assert raw_endpoint not in result
    assert raw_secret not in result
    assert raw_endpoint not in caplog.text
    assert raw_secret not in caplog.text


def test_simbad_source_keeps_astroquery_offline_and_bounded() -> None:
    path = ROOT / "plugins" / "astro_tools" / "obj.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    query_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "query_object"
    ]

    assert len(query_calls) == 1
    keywords = {keyword.arg: keyword.value for keyword in query_calls[0].keywords}
    assert isinstance(keywords.get("get_query_payload"), ast.Constant)
    assert keywords["get_query_payload"].value is True
    assert "requests_request_bounded" in source
    assert obj.SIMBAD_TAP_SYNC_URL in source
    assert ".json(" not in source
    assert ".content" not in source
    assert ".text" not in source
