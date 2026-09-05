from __future__ import annotations

import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.bounded_http import ResponseFormatError, ResponseLimitError
from plugins.adnmb import adapi
from plugins.adnmb.adapi import AdnmbClient
from plugins.ads_paper.ads_client import ADSClient
from plugins.chime import main as chime
from plugins.github import main as github
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.settings_snapshot import with_settings_reader

ROOT = REPOSITORY_ROOT


class _ChunkContent:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks     = chunks
        self.iterations = 0

    async def iter_chunked(self, _size: int):
        self.iterations += 1
        for chunk in self.chunks:
            yield chunk


class _Response:
    def __init__(
        self,
        body: bytes = b"{}",
        *,
        status: int                = 200,
        content_type: str          = "application/json",
        content_length: int | None = None,
        url: str                   = "https://example.test/api",
    ) -> None:
        self.status  = status
        self.url     = url
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.content_length = content_length
        self.content        = _ChunkContent((body,))
        self.close_calls    = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def close(self) -> None:
        self.close_calls += 1


class _Session:
    def __init__(self, *responses: _Response) -> None:
        self.responses                                    = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        response     = self.responses.pop(0)
        response.url = url
        return response


def _context(tmp_path: Path, session: _Session, *, proxy: str = "") -> SimpleNamespace:
    return with_settings_reader(
        SimpleNamespace(
            http_session = session,
            logger       = logging.getLogger("test.cr221.fixed"),
            data_dir     = tmp_path,
            secrets      = {"plugins": {"github": {"proxy": proxy}}},
        )
    )


@pytest.mark.asyncio
async def test_ads_search_streams_bounded_json_and_preserves_timeout() -> None:
    response = _Response(
        b'{"response":{"docs":[{"bibcode":"2026Test"}]}}',
        url="https://api.adsabs.harvard.edu/v1/search/query",
    )
    session = _Session(response)

    result = await ADSClient("token", session).search_papers("frb")

    assert result == [{"bibcode": "2026Test"}]
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url == "https://api.adsabs.harvard.edu/v1/search/query"
    assert kwargs["allow_redirects"] is False
    assert kwargs["auto_decompress"] is False
    assert kwargs["timeout"].total == 30
    assert response.content.iterations == 1


@pytest.mark.asyncio
async def test_ads_bibtex_accepts_only_its_bounded_legacy_text_json() -> None:
    response = _Response(
        b'{"export":"  @article{bounded}  "}',
        content_type = "text/plain; charset=utf-8",
        url          = "https://api.adsabs.harvard.edu/v1/export/bibtex",
    )
    session = _Session(response)

    result = await ADSClient("token", session).get_bibtex("bibcode")

    assert result == "@article{bounded}"
    assert response.content.iterations == 1


@pytest.mark.asyncio
async def test_ads_rejects_wrong_mime_before_reading_body() -> None:
    response = _Response(
        b'{"response":{"docs":[]}}',
        content_type = "text/html",
        url          = "https://api.adsabs.harvard.edu/v1/search/query",
    )

    result = await ADSClient("token", _Session(response)).search_papers("frb")

    assert result == []
    assert response.content.iterations == 0


@pytest.mark.asyncio
async def test_ads_huge_error_body_is_closed_without_being_read() -> None:
    response = _Response(
        b"must-not-be-read",
        status         = 503,
        content_length = 1_000_000_000,
        url            = "https://api.adsabs.harvard.edu/v1/search/query",
    )

    result = await ADSClient("token", _Session(response)).search_papers("frb")

    assert result == []
    assert response.content.iterations == 0
    assert response.close_calls >= 1


@pytest.mark.asyncio
async def test_adnmb_get_uses_bounded_json_stream(tmp_path: Path) -> None:
    response = _Response(
        '[{"forums":[{"name":"综合版1","id":"4"}]}]'.encode(),
        url="https://www.nmbxd1.com/Api/getForumList",
    )
    session = _Session(response)
    client = AdnmbClient(session=session, cache_dir=tmp_path, uuid="uuid")

    result = await client.get_forum_list()

    assert result == {"综合版1": "4"}
    assert session.calls[0][0] == "GET"
    assert session.calls[0][2]["allow_redirects"] is False
    assert session.calls[0][2]["auto_decompress"] is False


@pytest.mark.asyncio
async def test_adnmb_rejects_wrong_mime_without_reading(tmp_path: Path) -> None:
    response = _Response(b"{}", content_type="text/html")
    client = AdnmbClient(session=_Session(response), cache_dir=tmp_path, uuid="uuid")

    with pytest.raises(ResponseFormatError):
        await client._get("forum_list")

    assert response.content.iterations == 0


@pytest.mark.asyncio
async def test_adnmb_image_fetch_is_restricted_to_https_cdn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_fetch(url: str, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return None

    monkeypatch.setattr(adapi, "fetch_public_bytes", fake_fetch)
    client = AdnmbClient(session=object(), cache_dir=tmp_path, uuid="uuid")

    assert await client.download_image("2026-07/test.jpg") is None
    assert captured["url"].startswith("https://image.nmb.best/image/")
    assert captured["allowed_hosts"] == {"image.nmb.best"}
    assert captured["allowed_schemes"] == {"https"}


@pytest.mark.asyncio
async def test_chime_reads_bounded_json(tmp_path: Path) -> None:
    response = _Response(
        b'{"FRB1":{"240101":{"timestamp":{"value":"new"}}}}',
        url=chime.CHIME_API_URL,
    )
    session = _Session(response)

    result = await chime.fetch_chime_repeaters(_context(tmp_path, session))

    assert result == {"FRB1": {"240101": {"timestamp": {"value": "new"}}}}
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url == chime.CHIME_API_URL
    assert kwargs["allow_redirects"] is False
    assert kwargs["auto_decompress"] is False
    assert kwargs["timeout"] == 30


@pytest.mark.asyncio
async def test_chime_rejects_chunked_body_over_limit(tmp_path: Path) -> None:
    response = _Response(
        b"{" + b" " * (4 * 1024 * 1024),
        url=chime.CHIME_API_URL,
    )

    result = await chime.fetch_chime_repeaters(_context(tmp_path, _Session(response)))

    assert result is None
    assert response.content.iterations == 1


@pytest.mark.asyncio
async def test_chime_huge_error_body_is_not_read(tmp_path: Path) -> None:
    response = _Response(
        b"must-not-be-read",
        status         = 500,
        content_length = 1_000_000_000,
        url            = chime.CHIME_API_URL,
    )

    result = await chime.fetch_chime_repeaters(_context(tmp_path, _Session(response)))

    assert result is None
    assert response.content.iterations == 0
    assert response.close_calls >= 1


@pytest.mark.asyncio
async def test_github_proxy_path_is_exact_origin_and_bounded(tmp_path: Path) -> None:
    html = b"""
    <article class="Box-row">
      <h2><a href="/owner/repo">owner/repo</a></h2>
      <p>bounded</p>
    </article>
    """
    response = _Response(
        html,
        content_type = "text/html; charset=utf-8",
        url          = "https://github.com/trending?since=daily",
    )
    session = _Session(response)
    context = _context(tmp_path, session, proxy="http://trusted-proxy.test:8080")

    result = await github._fetch_trending("daily", context)

    assert "owner/repo" in str(result)
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url == "https://github.com/trending?since=daily"
    assert kwargs["proxy"] == "http://trusted-proxy.test:8080"
    assert kwargs["timeout"] == 15
    assert kwargs["allow_redirects"] is False
    assert kwargs["auto_decompress"] is False


@pytest.mark.asyncio
async def test_github_proxy_rejects_wrong_mime_before_reading(tmp_path: Path) -> None:
    response = _Response(
        b"<html></html>",
        content_type = "application/octet-stream",
        url          = "https://github.com/trending?since=daily",
    )
    context = _context(
        tmp_path,
        _Session(response),
        proxy="http://trusted-proxy.test:8080",
    )

    result = await github._fetch_trending("daily", context)

    assert "XQ-PLUGIN-UNEXPECTED" in str(result)
    assert response.content.iterations == 0


@pytest.mark.asyncio
async def test_github_proxy_does_not_read_huge_error_body(tmp_path: Path) -> None:
    response = _Response(
        b"must-not-be-read",
        status         = 502,
        content_length = 1_000_000_000,
        url            = "https://github.com/trending?since=daily",
    )
    context = _context(
        tmp_path,
        _Session(response),
        proxy="http://trusted-proxy.test:8080",
    )

    result = await github._fetch_trending("daily", context)

    assert "HTTP 502" in str(result)
    assert response.content.iterations == 0
    assert response.close_calls >= 1


@pytest.mark.asyncio
async def test_github_proxy_rejects_cross_origin_redirect_before_request(
    tmp_path: Path,
) -> None:
    redirect = _Response(
        b"",
        status       = 302,
        content_type = "text/html",
        url          = "https://github.com/trending?since=daily",
    )
    redirect.headers["Location"] = "https://evil.example/internal"
    session                      = _Session(redirect)
    context                      = _context(
        tmp_path,
        session,
        proxy="http://trusted-proxy.test:8080",
    )

    result = await github._fetch_trending("daily", context)

    assert "XQ-PLUGIN-UNEXPECTED" in str(result)
    assert len(session.calls) == 1
    assert redirect.content.iterations == 0


@pytest.mark.parametrize(
    "relative_path",
    (
        "plugins/ads_paper/ads_client.py",
        "plugins/adnmb/adapi.py",
        "plugins/chime/main.py",
        "plugins/github/main.py",
    ),
)
def test_fixed_http_plugins_have_no_direct_response_body_reads(relative_path: str) -> None:
    source = (ROOT / relative_path).read_text(encoding="utf-8")
    direct_read = re.compile(r"await\s+[A-Za-z_]\w*\.(?:json|text|read)\s*\(")

    assert direct_read.search(source) is None


@pytest.mark.asyncio
async def test_adnmb_chunked_body_over_limit_is_rejected(tmp_path: Path) -> None:
    response = _Response(b"[" + b" " * (4 * 1024 * 1024))
    client = AdnmbClient(session=_Session(response), cache_dir=tmp_path, uuid="uuid")

    with pytest.raises(ResponseLimitError):
        await client._get("timeline")
