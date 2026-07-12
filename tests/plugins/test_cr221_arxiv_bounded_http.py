from __future__ import annotations

import ast
import importlib
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import requests

from core.bounded_http import (
    BodyLimits,
    BoundedHttpResponse,
    HttpStatusError,
)

_ARXIV_TODAY = "plugins.arxiv_filter.arxiv_today"
_STEP2 = "plugins.arxiv_filter.train_model.data_prep.step2_fetch_all_astro_ph"
_STEP3 = "plugins.arxiv_filter.train_model.data_prep.step3_build_dataset"
_XML_ENTITY_CANARY = b"""<?xml version="1.0"?>
<!DOCTYPE feed [<!ENTITY secret "CR221_ENTITY_SECRET">]>
<feed xmlns="http://www.w3.org/2005/Atom"><title>&secret;</title></feed>
"""
_VALID_ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:totalResults>1</opensearch:totalResults>
  <entry>
    <id>https://arxiv.org/abs/2607.00001v1</id>
    <title>Bounded transport</title>
    <summary>Safe Atom payload.</summary>
  </entry>
</feed>
"""
_VALID_HTML = b"""<!doctype html>
<html><body>
<h3>Showing new listings for Saturday, 11 July 2026</h3>
<dl>
  <dt><a href="/abs/2607.00001">arXiv:2607.00001</a></dt>
  <dd>
    <div class="list-title">Title: Bounded transport</div>
    <p class="mathjax">Safe bounded HTML payload.</p>
  </dd>
</dl>
</body></html>
"""


class _AttrDict(dict[str, Any]):
    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


@pytest.fixture
def load_with_feedparser(monkeypatch: pytest.MonkeyPatch):
    loaded: list[str] = []

    def load(module_name: str, parse: Callable[[bytes], Any]) -> ModuleType:
        fake_feedparser = ModuleType("feedparser")
        fake_feedparser.parse = parse  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "feedparser", fake_feedparser)
        sys.modules.pop(module_name, None)
        loaded.append(module_name)
        return importlib.import_module(module_name)

    yield load

    for module_name in loaded:
        sys.modules.pop(module_name, None)


class _RawBody:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.stream_calls = 0
        self.decode_content: bool | None = None

    def stream(self, _chunk_size: int, *, decode_content: bool) -> Iterator[bytes]:
        self.stream_calls += 1
        assert decode_content is False
        yield from self._chunks


class _RequestsResponse:
    def __init__(self, *, body: bytes, content_type: str) -> None:
        self.status_code = 200
        self.url = "https://arxiv.org/list/astro-ph/new"
        self.headers = {"Content-Type": content_type}
        self.raw = _RawBody([body])
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _bounded_response(
    body: bytes, content_type: str = "application/atom+xml"
) -> BoundedHttpResponse:
    return BoundedHttpResponse(
        url="https://export.arxiv.org/api/query",
        status=200,
        body=body,
        media_type=content_type,
        charset="UTF-8",
        headers={"Content-Type": content_type},
        wire_bytes=len(body),
        decoded_bytes=len(body),
    )


def test_runtime_html_uses_streamed_wrapper_and_rejects_wire_overflow(
    monkeypatch: pytest.MonkeyPatch,
    load_with_feedparser,
) -> None:
    module = load_with_feedparser(_ARXIV_TODAY, lambda _body: None)
    monkeypatch.delenv("ARXIV_PROXY", raising=False)
    monkeypatch.setattr(
        module,
        "_HTML_BODY_LIMITS",
        BodyLimits(
            max_wire_bytes=8,
            max_decoded_bytes=8,
            max_decompression_ratio=20,
            ratio_grace_bytes=8,
            chunk_bytes=4,
        ),
    )
    response = _RequestsResponse(body=b"123456789", content_type="text/html; charset=UTF-8")
    request_options: dict[str, Any] = {}

    def request(_method: str, _url: str, **kwargs: Any) -> _RequestsResponse:
        request_options.update(kwargs)
        return response

    monkeypatch.setattr(requests, "request", request)

    result = module._fetch_arxiv_page(
        "https://arxiv.org/list/astro-ph/new",
        {"arxiv": {"timeout": 7, "use_ssl_verify": True}},
    )

    assert result is None
    assert response.closed is True
    assert response.raw.stream_calls == 1
    assert request_options["stream"] is True
    assert request_options["allow_redirects"] is False
    assert request_options["headers"]["Accept-Encoding"] == "gzip, deflate"
    assert request_options["timeout"] == 7
    assert request_options["proxies"] is None
    assert request_options["verify"] is True


def test_runtime_html_rejects_wrong_mime_before_reading_body(
    monkeypatch: pytest.MonkeyPatch,
    load_with_feedparser,
) -> None:
    module = load_with_feedparser(_ARXIV_TODAY, lambda _body: None)
    response = _RequestsResponse(body=b"CR221_MIME_BODY_CANARY", content_type="application/json")
    monkeypatch.setattr(requests, "request", lambda *_args, **_kwargs: response)

    result = module._fetch_arxiv_page(
        "https://arxiv.org/list/astro-ph/new",
        {"arxiv": {"timeout": 7, "use_ssl_verify": True}},
    )

    assert result is None
    assert response.closed is True
    assert response.raw.stream_calls == 0


def test_runtime_html_success_preserves_article_and_date_behavior(
    monkeypatch: pytest.MonkeyPatch,
    load_with_feedparser,
) -> None:
    module = load_with_feedparser(_ARXIV_TODAY, lambda _body: None)
    monkeypatch.setattr(module, "load_plugin_config", lambda: {"arxiv": {}})
    monkeypatch.setattr(
        module,
        "requests_request_bounded",
        lambda *_args, **_kwargs: _bounded_response(_VALID_HTML, "text/html"),
    )

    frame = module.get_today_arxiv("https://arxiv.org/list/astro-ph/new")

    assert frame.to_dict(orient="records") == [
        {
            "arXiv ID": "2607.00001",
            "Title": "Bounded transport",
            "Abstract": "Safe bounded HTML payload.",
        }
    ]
    assert module.check_arxiv_update_date("https://arxiv.org/list/astro-ph/new") == "2026-07-11"


def test_runtime_atom_rejects_entity_before_feedparser(
    monkeypatch: pytest.MonkeyPatch,
    load_with_feedparser,
) -> None:
    parsed: list[bytes] = []
    module = load_with_feedparser(_ARXIV_TODAY, lambda body: parsed.append(body))
    calls: list[dict[str, Any]] = []
    monkeypatch.delenv("ARXIV_PROXY", raising=False)
    monkeypatch.setattr(
        module,
        "load_plugin_config",
        lambda: {
            "arxiv": {
                "proxy": "http://127.0.0.1:8080",
                "timeout": 19,
                "use_ssl_verify": False,
            }
        },
    )

    def fetch(*_args: Any, **kwargs: Any) -> BoundedHttpResponse:
        calls.append(kwargs)
        return _bounded_response(_XML_ENTITY_CANARY)

    monkeypatch.setattr(module, "requests_request_bounded", fetch)

    result = module.get_today_arxiv_api(days=1)

    assert result.empty
    assert parsed == []
    request_kwargs = calls[0]["request_kwargs"]
    assert request_kwargs["params"]["max_results"] == 1000
    assert request_kwargs["proxies"] == {
        "http": "http://127.0.0.1:8080",
        "https": "http://127.0.0.1:8080",
    }
    assert request_kwargs["verify"] is False
    assert request_kwargs["timeout"] == 19


def test_step2_preserves_paging_and_validates_xml_before_feedparser(
    monkeypatch: pytest.MonkeyPatch,
    load_with_feedparser,
) -> None:
    parsed: list[bytes] = []

    def parse(body: bytes) -> Any:
        parsed.append(body)
        return _AttrDict(
            feed=_AttrDict(opensearch_totalresults="1"),
            entries=[
                _AttrDict(
                    id="https://arxiv.org/abs/2607.00001v1",
                    title="  Bounded   transport ",
                    summary=" Safe   Atom payload. ",
                )
            ],
        )

    module = load_with_feedparser(_STEP2, parse)
    calls: list[dict[str, Any]] = []

    def fetch(*_args: Any, **kwargs: Any) -> BoundedHttpResponse:
        calls.append(kwargs)
        return _bounded_response(_VALID_ATOM)

    monkeypatch.setattr(module, "requests_request_bounded", fetch)
    monkeypatch.setattr(module, "DELAY", 0)

    result = module.fetch_month("202607010000", "202607312359")

    assert result.completed is True
    assert result.next_offset == 1
    assert result.total_results == 1
    assert result.papers == [
        {
            "arxiv_id": "2607.00001",
            "title": "Bounded transport",
            "abstract": "Safe Atom payload.",
        }
    ]
    assert parsed == [_VALID_ATOM]
    assert calls[0]["request_kwargs"]["params"]["start"] == 0
    assert calls[0]["request_kwargs"]["params"]["max_results"] == module.MAX_RESULTS
    assert calls[0]["request_kwargs"]["timeout"] == 120


def test_step2_entity_payload_never_reaches_feedparser(
    monkeypatch: pytest.MonkeyPatch,
    load_with_feedparser,
) -> None:
    parsed: list[bytes] = []
    module = load_with_feedparser(_STEP2, lambda body: parsed.append(body))
    monkeypatch.setattr(module, "RETRY_LIMIT", 1)
    monkeypatch.setattr(
        module,
        "requests_request_bounded",
        lambda *_args, **_kwargs: _bounded_response(_XML_ENTITY_CANARY),
    )

    result = module.fetch_month("202607010000", "202607312359")

    assert result.completed is False
    assert result.next_offset == 0
    assert parsed == []


def test_step3_preserves_http_400_one_by_one_fallback(
    monkeypatch: pytest.MonkeyPatch,
    load_with_feedparser,
) -> None:
    module = load_with_feedparser(_STEP3, lambda _body: None)
    expected = {"2607.00001": {"title": "t", "abstract": "a"}}

    def fetch(*_args: Any, **kwargs: Any) -> BoundedHttpResponse:
        assert kwargs["request_kwargs"]["params"]["max_results"] == 2
        assert kwargs["request_kwargs"]["timeout"] == module.API_TIMEOUT
        raise HttpStatusError(400)

    monkeypatch.setattr(module, "requests_request_bounded", fetch)
    monkeypatch.setattr(module, "_fetch_one_by_one", lambda _ids: expected)

    assert module.fetch_abstracts_batch(["2607.00001", "invalid"]) == expected


def test_step3_entity_payload_never_reaches_feedparser(
    monkeypatch: pytest.MonkeyPatch,
    load_with_feedparser,
) -> None:
    parsed: list[bytes] = []
    module = load_with_feedparser(_STEP3, lambda body: parsed.append(body))
    monkeypatch.setattr(module, "MAX_RETRIES", 1)
    monkeypatch.setattr(
        module,
        "requests_request_bounded",
        lambda *_args, **_kwargs: _bounded_response(_XML_ENTITY_CANARY),
    )

    assert module.fetch_abstracts_batch(["2607.00001"]) == {}
    assert parsed == []


def test_step3_single_fetch_preserves_max_results_and_feed_fields(
    monkeypatch: pytest.MonkeyPatch,
    load_with_feedparser,
) -> None:
    parsed: list[bytes] = []

    def parse(body: bytes) -> Any:
        parsed.append(body)
        return _AttrDict(
            entries=[
                _AttrDict(
                    id="https://arxiv.org/abs/2607.00001v1",
                    title=" Bounded   transport ",
                    summary=" Safe   Atom payload. ",
                )
            ]
        )

    module = load_with_feedparser(_STEP3, parse)
    calls: list[dict[str, Any]] = []

    def fetch(*_args: Any, **kwargs: Any) -> BoundedHttpResponse:
        calls.append(kwargs)
        return _bounded_response(_VALID_ATOM)

    monkeypatch.setattr(module, "requests_request_bounded", fetch)

    result = module.fetch_abstracts_batch_single("2607.00001")

    assert result == {
        "2607.00001": {
            "title": "Bounded transport",
            "abstract": "Safe Atom payload.",
        }
    }
    assert parsed == [_VALID_ATOM]
    assert calls[0]["request_kwargs"]["params"] == {
        "id_list": "2607.00001",
        "max_results": 1,
    }
    assert calls[0]["request_kwargs"]["timeout"] == module.API_TIMEOUT


def test_sync_arxiv_paths_forbid_unbounded_requests_and_response_access() -> None:
    root = Path(__file__).resolve().parents[2]
    paths = (
        root / "plugins" / "arxiv_filter" / "arxiv_today.py",
        root
        / "plugins"
        / "arxiv_filter"
        / "train_model"
        / "data_prep"
        / "step2_fetch_all_astro_ph.py",
        root / "plugins" / "arxiv_filter" / "train_model" / "data_prep" / "step3_build_dataset.py",
    )
    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                receiver = node.func.value
                if (
                    isinstance(receiver, ast.Name)
                    and receiver.id == "requests"
                    and node.func.attr in {"get", "post", "put", "patch", "delete", "request"}
                ):
                    violations.append(f"{path.name}:{node.lineno}: requests.{node.func.attr}")
                if node.func.attr in {"text", "json", "iter_content", "raise_for_status"}:
                    violations.append(f"{path.name}:{node.lineno}: .{node.func.attr}()")
            if isinstance(node, ast.Attribute) and node.attr == "content":
                violations.append(f"{path.name}:{node.lineno}: .content")

    assert violations == []
