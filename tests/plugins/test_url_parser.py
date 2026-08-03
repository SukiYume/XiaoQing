"""URL 预览插件的元数据、网络边界、图片缓存与调度契约测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit

import pytest

from core.bounded_file_cache import FileCacheLimits
from core.safe_http import SafeHttpError, SafeHttpResponse, UnsafeUrlError
from plugins.url_parser import main as url_parser
from tests.helpers.payloads import image_bytes as _image_bytes

ROOT = Path(__file__).resolve().parents[2]


def _html_response(
    html: str | bytes,
    *,
    url: str = "https://example.com/page",
    charset: str | None = "utf-8",
) -> SafeHttpResponse:
    body = html.encode("utf-8") if isinstance(html, str) else html
    return SafeHttpResponse(
        url=url,
        status=200,
        body=body,
        charset=charset,
        headers={"Content-Type": "text/html"},
    )


def _image_response(
    payload: bytes,
    *,
    content_type: str = "image/png",
) -> SafeHttpResponse:
    return SafeHttpResponse(
        url="https://example.com/assets/final",
        status=200,
        body=payload,
        charset=None,
        headers={"Content-Type": content_type},
    )


@pytest.fixture
def context(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        data_dir=tmp_path,
        http_session=None,
        request_id="req-url-preview",
        secrets={"plugins": {"url_parser": {}}},
        logger=MagicMock(),
    )


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    page = AsyncMock(return_value=None)
    image = AsyncMock(return_value=None)

    async def validate(url: str):
        return urlsplit(url), ("93.184.216.34",)

    validator = AsyncMock(side_effect=validate)
    monkeypatch.setattr(url_parser, "fetch_public_html", page)
    monkeypatch.setattr(url_parser, "fetch_public_bytes", image)
    monkeypatch.setattr(url_parser, "validate_public_fetch_target", validator)
    return SimpleNamespace(page=page, image=image, validator=validator)


def test_init_records_plugin_load(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger=url_parser.__name__):
        assert url_parser.init() is None
    assert "已加载" in caplog.text


def test_text_compaction_normalizes_whitespace_and_enforces_budget() -> None:
    assert url_parser._compact_text("  alpha\n beta  ", 20) == "alpha beta"
    assert url_parser._compact_text(None, 20) == ""
    compact = url_parser._compact_text("x" * 30, 10)
    assert compact == "xxxxxxx..."
    assert len(compact) == 10


def test_html_parser_uses_metadata_priority_and_real_text_bounds() -> None:
    html = f"""
    <html><head>
      <title>  Nested page title  </title>
      <meta name="description" content="{"d" * 150}">
      <meta property="og:description" content="lower priority">
      <meta name="twitter:description" content="lowest priority">
      <meta property="og:image" content=" /assets/cover.png ">
      <meta name="twitter:image" content="/fallback.png">
    </head></html>
    """

    title, description, image_reference = url_parser._parse_preview_html(html)

    assert title == "Nested page title"
    assert len(description) == url_parser.MAX_DESC_LENGTH
    assert description.endswith("...")
    assert image_reference == "/assets/cover.png"


@pytest.mark.parametrize(
    ("markup", "expected_description", "expected_image"),
    [
        (
            '<meta property="og:description" content="Open Graph">'
            '<meta name="twitter:image" content="twitter.png">',
            "Open Graph",
            "twitter.png",
        ),
        (
            '<meta name="twitter:description" content="Twitter Card">'
            '<meta property="og:image" content="og.png">',
            "Twitter Card",
            "og.png",
        ),
        ("<meta name=description><meta property=og:image>", "", ""),
    ],
)
def test_html_parser_supports_fallback_metadata(
    markup: str,
    expected_description: str,
    expected_image: str,
) -> None:
    _, description, image_reference = url_parser._parse_preview_html(
        f"<html><head>{markup}</head></html>"
    )
    assert (description, image_reference) == (expected_description, expected_image)


def test_html_parser_drops_overlong_image_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(url_parser, "MAX_IMAGE_URL_LENGTH", 8)
    _, _, image_reference = url_parser._parse_preview_html(
        '<meta property="og:image" content="/much-too-long.png">'
    )
    assert image_reference == ""


def test_html_decoder_uses_declared_charset_and_utf8_fallback() -> None:
    assert url_parser._decode_html("中文".encode("gbk"), "gbk") == "中文"
    assert url_parser._decode_html(b"<title>ok</title>", "unknown-charset") == ("<title>ok</title>")
    assert "�" in url_parser._decode_html(b"\xff", None)


@pytest.mark.parametrize(
    ("image_format", "extension"),
    [("JPEG", ".jpg"), ("PNG", ".png"), ("WEBP", ".webp")],
)
def test_image_validation_uses_actual_format(image_format: str, extension: str) -> None:
    assert url_parser._detect_image_extension(_image_bytes(image_format)) == extension


def test_image_validation_rejects_invalid_unsupported_and_oversized_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="valid image"):
        url_parser._detect_image_extension(b"not an image")
    with pytest.raises(ValueError, match="unsupported"):
        url_parser._detect_image_extension(_image_bytes("GIF"))

    monkeypatch.setattr(url_parser, "MAX_IMAGE_PIXELS", 3)
    with pytest.raises(ValueError, match="dimensions"):
        url_parser._detect_image_extension(_image_bytes())


def test_cached_preview_rejects_mismatched_and_oversized_files(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = url_parser._preview_cache(context)
    cache.directory.mkdir()
    mismatched = cache.directory / "mismatch.jpg"
    mismatched.write_bytes(_image_bytes("PNG"))
    assert url_parser._validated_cached_preview(cache, (mismatched.name,)) is None
    assert not mismatched.exists()

    oversized = cache.directory / "oversized.png"
    oversized.write_bytes(_image_bytes("PNG"))
    monkeypatch.setattr(url_parser, "MAX_IMAGE_BYTES", 1)
    assert url_parser._validated_cached_preview(cache, (oversized.name,)) is None
    assert not oversized.exists()


def test_cached_preview_cleanup_failure_is_best_effort(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cache = url_parser._preview_cache(context)
    cache.directory.mkdir()
    corrupted = cache.directory / "corrupted.jpg"
    corrupted.write_bytes(b"not an image")

    monkeypatch.setattr(Path, "unlink", MagicMock(side_effect=OSError("locked")))
    with caplog.at_level("DEBUG", logger=url_parser.__name__):
        assert url_parser._validated_cached_preview(cache, (corrupted.name,)) is None
    assert "暂时无法删除" in caplog.text


@pytest.mark.asyncio
async def test_text_preview_does_not_depend_on_shared_http_session(
    context: SimpleNamespace,
    network: SimpleNamespace,
) -> None:
    network.page.return_value = _html_response(
        "<html><head><title> Example   Page </title>"
        '<meta name="description" content="A useful summary"></head></html>'
    )

    result = await url_parser.handle_url("https://example.com/page", {}, context)

    assert result == [
        {
            "type": "text",
            "data": {"text": "🔗 Example Page\nA useful summary\n\n链接: https://example.com/page"},
        }
    ]
    network.page.assert_awaited_once_with(
        "https://example.com/page",
        headers={"User-Agent": url_parser._USER_AGENT},
        timeout_seconds=url_parser.REQUEST_TIMEOUT,
    )


@pytest.mark.asyncio
async def test_description_only_preview_uses_nonempty_heading(
    context: SimpleNamespace,
    network: SimpleNamespace,
) -> None:
    network.page.return_value = _html_response(
        '<meta name="description" content="Description only">'
    )
    result = await url_parser.handle_url("https://example.com", {}, context)
    assert result[0]["data"]["text"].startswith("🔗 网页预览\nDescription only")


@pytest.mark.asyncio
async def test_relative_preview_image_is_validated_cached_and_reused(
    context: SimpleNamespace,
    network: SimpleNamespace,
) -> None:
    page_url = "https://example.com/posts/1"
    image_url = "https://example.com/assets/cover.jpg"
    network.page.return_value = _html_response(
        '<title>With image</title><meta property="og:image" content="/assets/cover.jpg">',
        url=page_url,
    )
    payload = _image_bytes("PNG")
    network.image.return_value = _image_response(payload, content_type="image/jpeg")
    digest = hashlib.sha256(image_url.encode()).hexdigest()
    cache_dir = context.data_dir / "url_previews"
    cache_dir.mkdir()
    stale_path = cache_dir / f"{digest}.jpg"
    stale_path.write_bytes(b"legacy invalid image")

    first = await url_parser.handle_url("https://example.com/input", {}, context)
    second = await url_parser.handle_url("https://example.com/input", {}, context)

    expected_name = f"{digest}.png"
    expected_path = context.data_dir / "url_previews" / expected_name
    assert expected_path.read_bytes() == payload
    assert not stale_path.exists()
    assert first[1]["type"] == "image"
    assert first[1]["data"]["file"].startswith("file:")
    assert second[1] == first[1]
    assert network.validator.await_count == 2
    assert [call.args[0] for call in network.validator.await_args_list] == [image_url, image_url]
    network.image.assert_awaited_once()
    options = network.image.await_args.kwargs
    assert options["max_bytes"] == url_parser.MAX_IMAGE_BYTES
    assert options["allowed_content_type_prefixes"] == ("image/",)
    assert options["allowed_schemes"] == ("http", "https")


@pytest.mark.asyncio
async def test_image_only_metadata_returns_an_image_segment(
    context: SimpleNamespace,
    network: SimpleNamespace,
) -> None:
    network.page.return_value = _html_response(
        '<meta name="twitter:image" content="https://example.com/image.webp">'
    )
    network.image.return_value = _image_response(
        _image_bytes("WEBP"),
        content_type="image/webp",
    )

    result = await url_parser.handle_url("https://example.com", {}, context)
    assert len(result) == 1
    assert result[0]["type"] == "image"


@pytest.mark.asyncio
async def test_empty_and_oversized_pages_return_no_preview(
    context: SimpleNamespace,
    network: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    network.page.return_value = None
    assert await url_parser.handle_url("https://example.com", {}, context) == []

    network.page.return_value = _html_response("<html><body>no metadata</body></html>")
    assert await url_parser.handle_url("https://example.com", {}, context) == []

    monkeypatch.setattr(url_parser, "MAX_HTML_BYTES", 3)
    network.page.return_value = _html_response(b"1234")
    assert await url_parser.handle_url("https://example.com", {}, context) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [SafeHttpError("bounded"), UnsafeUrlError("private")])
async def test_safe_page_failures_return_no_preview(
    context: SimpleNamespace,
    network: SimpleNamespace,
    error: Exception,
) -> None:
    network.page.side_effect = error
    assert await url_parser.handle_url("https://example.com", {}, context) == []


@pytest.mark.asyncio
async def test_unexpected_page_failure_uses_public_error_boundary(
    context: SimpleNamespace,
    network: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    network.page.side_effect = RuntimeError("unexpected page failure")
    with caplog.at_level("ERROR"):
        result = await url_parser.handle_url("https://example.com", {}, context)
    assert result == []
    assert "url_parser.handle_url" in "\n".join(record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_unsafe_or_failed_optional_image_keeps_text_preview(
    context: SimpleNamespace,
    network: SimpleNamespace,
) -> None:
    network.page.return_value = _html_response(
        '<title>Safe text</title><meta property="og:image" content="http://127.0.0.1/a.png">'
    )
    network.validator.side_effect = UnsafeUrlError("private image")
    result = await url_parser.handle_url("https://example.com", {}, context)
    assert len(result) == 1 and result[0]["type"] == "text"
    network.image.assert_not_awaited()

    network.validator.side_effect = None
    network.validator.return_value = (urlsplit("https://example.com/a.png"), ("93.184.216.34",))
    network.image.side_effect = SafeHttpError("bad image")
    result = await url_parser.handle_url("https://example.com", {}, context)
    assert len(result) == 1 and result[0]["type"] == "text"


@pytest.mark.asyncio
async def test_invalid_or_unexpected_optional_image_keeps_text_preview(
    context: SimpleNamespace,
    network: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    network.page.return_value = _html_response(
        '<title>Text survives</title><meta property="og:image" content="/bad.png">'
    )
    network.image.return_value = _image_response(b"not an image")
    result = await url_parser.handle_url("https://example.com", {}, context)
    assert len(result) == 1 and result[0]["type"] == "text"

    network.image.side_effect = RuntimeError("unexpected image failure")
    with caplog.at_level("ERROR"):
        result = await url_parser.handle_url("https://example.com", {}, context)
    assert len(result) == 1 and result[0]["type"] == "text"
    assert "url_parser.preview_image" in "\n".join(record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_empty_oversized_or_uncacheable_image_keeps_text(
    context: SimpleNamespace,
    network: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    network.page.return_value = _html_response(
        '<title>Text only</title><meta property="og:image" content="/image.png">'
    )
    network.image.return_value = _image_response(b"")
    result = await url_parser.handle_url("https://example.com", {}, context)
    assert len(result) == 1

    payload = _image_bytes()
    network.image.return_value = _image_response(payload)
    monkeypatch.setattr(url_parser, "MAX_IMAGE_BYTES", len(payload) - 1)
    result = await url_parser.handle_url("https://example.com", {}, context)
    assert len(result) == 1

    monkeypatch.setattr(url_parser, "MAX_IMAGE_BYTES", 5 * 1024 * 1024)
    monkeypatch.setattr(
        url_parser,
        "PREVIEW_CACHE_LIMITS",
        FileCacheLimits(max_entries=1, max_bytes=1, ttl_seconds=60),
    )
    result = await url_parser.handle_url("https://example.com", {}, context)
    assert len(result) == 1
    assert list((context.data_dir / "url_previews").glob("*.png")) == []


@pytest.mark.asyncio
async def test_overlong_resolved_image_url_is_not_validated(
    context: SimpleNamespace,
    network: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(url_parser, "MAX_IMAGE_URL_LENGTH", 20)
    assert (
        await url_parser._cache_preview_image(
            "https://example.com",
            "/a-very-long-image-name.png",
            context,
        )
        is None
    )
    network.validator.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    ["", None, pytest.param("x" * (url_parser.MAX_INPUT_URL_LENGTH + 1), id="overlong")],
)
async def test_invalid_input_url_is_rejected_before_network(
    context: SimpleNamespace,
    network: SimpleNamespace,
    url: object,
) -> None:
    assert await url_parser.handle_url(url, {}, context) == []
    network.page.assert_not_awaited()


@pytest.mark.asyncio
async def test_largest_accepted_url_stays_within_message_budget(
    context: SimpleNamespace,
    network: SimpleNamespace,
) -> None:
    prefix = "https://example.com/"
    url = prefix + "x" * (url_parser.MAX_INPUT_URL_LENGTH - len(prefix))
    title = "t" * 300
    description = "d" * 200
    network.page.return_value = _html_response(
        f'<title>{title}</title><meta name="description" content="{description}">'
    )

    result = await url_parser.handle_url(url, {}, context)

    assert len(result[0]["data"]["text"]) <= 3000


@pytest.mark.asyncio
async def test_preview_concurrency_is_bounded(
    context: SimpleNamespace,
    network: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    peak = 0

    async def slow_page(url: str, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return _html_response(f"<title>{url}</title>", url=url)

    network.page.side_effect = slow_page
    monkeypatch.setattr(url_parser, "_PREVIEW_SEMAPHORE", asyncio.Semaphore(2))
    results = await asyncio.gather(
        *(url_parser.handle_url(f"https://example.com/{index}", {}, context) for index in range(6))
    )
    assert all(result and result[0]["type"] == "text" for result in results)
    assert peak == 2


@pytest.mark.asyncio
async def test_command_placeholder_and_manifest_match_dispatcher_contract(
    context: SimpleNamespace,
) -> None:
    manifest = json.loads(
        (ROOT / "plugins" / "url_parser" / "plugin.json").read_text(encoding="utf-8")
    )
    readme = (ROOT / "plugins" / "url_parser" / "README.md").read_text(encoding="utf-8")

    assert await url_parser.handle("unused", "unused", {}, context) == []
    assert manifest["commands"] == []
    assert manifest["schedule"] == []
    assert manifest["concurrency"] == "parallel"
    for marker in ("完整的单 URL", "2 MiB", "5 MiB", "128", "7 天", "4"):
        assert marker in readme
