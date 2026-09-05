"""Flickr 命令、会话、图片缓存与公开错误测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from plugins.flickr import main as flickr
from plugins.flickr.client import FlickrApiError, FlickrPage, FlickrPhoto
from tests.helpers.payloads import image_bytes
from tests.helpers.settings_snapshot import with_settings_reader

_REAL_DOWNLOAD_PHOTO = flickr._download_photo


def _photo(number: int, *, license_id: str = "0") -> FlickrPhoto:
    return FlickrPhoto(
        photo_id    = str(number),
        owner_id    = "98765432@N01",
        owner_name  = "Astro Photographer",
        title       = f"Night Sky {number}",
        description = f"Description {number}",
        license_id  = license_id,
        taken_at    = "2026-08-18 03:04:05",
        tags        = ("night", "sky"),
        media_url   = f"https://live.staticflickr.com/66/{number}_abc_c.jpg",
        page_url    = f"https://www.flickr.com/photos/98765432@N01/{number}/",
    )


def _page(count: int = 3) -> FlickrPage:
    return FlickrPage(
        photos = tuple(_photo(number) for number in range(1, count + 1)),
        page   = 1,
        pages  = 1,
        total  = count,
    )


@pytest.fixture
def context(tmp_path: Path) -> SimpleNamespace:
    return with_settings_reader(
        SimpleNamespace(
            data_dir         = tmp_path,
            config           = {},
            secrets          = {"plugins": {"flickr": {"api_key": "test-api-key"}}},
            http_session     = None,
            current_user_id  = 123,
            current_group_id = 456,
            logger           = MagicMock(),
            state            = {},
            request_id       = "flickr-test-request",
        )
    )


@pytest.fixture(autouse=True)
def local_image(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    target = tmp_path / "flickr.jpg"
    target.write_bytes(image_bytes())
    monkeypatch.setattr(flickr, "_download_photo", AsyncMock(return_value=target))
    return target


def _client(monkeypatch: pytest.MonkeyPatch, **methods: object) -> SimpleNamespace:
    values = {
        "interesting": AsyncMock(return_value=_page()),
        "search": AsyncMock(return_value=_page()),
        "resolve_user": AsyncMock(return_value="98765432@N01"),
        "public_photos": AsyncMock(return_value=_page()),
        "album_photos": AsyncMock(return_value=_page()),
        "photo_info": AsyncMock(return_value=_photo(9, license_id="4")),
    }
    values.update(methods)
    client = SimpleNamespace(**values)
    monkeypatch.setattr(flickr, "FlickrClient", lambda _context: client)
    return client


def _reply_text(result: list[dict]) -> str:
    return "\n".join(
        segment.get("data", {}).get("text", "")
        for segment in result
        if segment.get("type") == "text"
    )


@pytest.mark.asyncio
async def test_help_is_compact_and_does_not_require_api_key(context: SimpleNamespace) -> None:
    context.secrets = {}

    result = await flickr.handle("flickr", "help", {}, context)

    message = _reply_text(result)
    assert "/flickr search" in message
    assert "/flickr more [1-5]" in message
    assert "license=any" in message


@pytest.mark.asyncio
async def test_default_command_opens_interesting_session(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)

    result = await flickr.handle("flickr", "", {"user_id": 123, "group_id": 456}, context)

    assert [segment["type"] for segment in result] == ["image", "text"]
    message = _reply_text(result)
    assert "Night Sky 1" in message
    assert "All Rights Reserved" in message
    assert "第 1/3 张" in message
    client.interesting.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_search_defaults_to_all_licenses_and_parses_filters(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)

    await flickr.handle(
        "flickr",
        "search Milky Way --tags night,sky --sort interesting --date 2026-08",
        {},
        context,
    )

    options = client.search.await_args.kwargs
    assert options["query"] == "Milky Way"
    assert options["tags"] == "night,sky"
    assert options["sort"] == "interestingness-desc"
    assert options["license_ids"] is None
    assert options["min_taken_date"] == "2026-08-01 00:00:00"
    assert options["max_taken_date"] == "2026-08-31 23:59:59"
    assert options["commons_only"] is False


@pytest.mark.asyncio
async def test_cc_and_commons_filters_are_explicit(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)

    await flickr.handle("flickr", "典藏 moon --license cc", {}, context)

    options = client.search.await_args.kwargs
    assert options["license_ids"] == "1,2,3,4,5,6,9,10"
    assert options["commons_only"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("search", "请提供搜索关键词"),
        ("search moon --sort random", "--sort 可选"),
        ("search moon --license private", "--license 可选"),
        ("search moon --date 2026-02-30", "--date 需使用"),
        ("search moon --unknown value", "未知选项"),
        ("more 0", "1 到 5"),
        ("more 6", "1 到 5"),
        ("info one two", "用法"),
        ("unknown", "未知 Flickr 命令"),
    ],
)
async def test_invalid_inputs_return_actionable_errors(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    args: str,
    expected: str,
) -> None:
    _client(monkeypatch)

    result = await flickr.handle("flickr", args, {}, context)

    assert expected in _reply_text(result)


@pytest.mark.asyncio
async def test_user_and_album_commands_resolve_owner(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)

    await flickr.handle("flickr", "user Display Name", {}, context)
    client.resolve_user.assert_awaited_with("Display Name")
    client.public_photos.assert_awaited_once_with("98765432@N01")

    client.resolve_user.reset_mock()
    await flickr.handle(
        "flickr",
        "album https://www.flickr.com/photos/example/albums/72100000000000000",
        {},
        context,
    )
    client.resolve_user.assert_awaited_with("example")
    client.album_photos.assert_awaited_once_with(
        user_id  = "98765432@N01",
        album_id = "72100000000000000",
    )


@pytest.mark.asyncio
async def test_more_advances_without_repeats_and_stops_at_end(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _client(monkeypatch)
    await flickr.handle("flickr", "", {}, context)

    result  = await flickr.handle("flickr", "more 2", {}, context)
    message = _reply_text(result)
    assert "Night Sky 2" in message
    assert "Night Sky 3" in message
    assert "Night Sky 1" not in message

    finished = await flickr.handle("flickr", "more", {}, context)
    assert "已经浏览完" in _reply_text(finished)


@pytest.mark.asyncio
async def test_session_isolated_by_group_and_user(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _client(monkeypatch)
    await flickr.handle("flickr", "", {}, context)

    context.current_group_id = 999
    other_group              = await flickr.handle("flickr", "more", {}, context)
    assert "当前没有可继续" in _reply_text(other_group)

    context.current_group_id = 456
    context.current_user_id  = 777
    other_user               = await flickr.handle("flickr", "more", {}, context)
    assert "当前没有可继续" in _reply_text(other_user)


@pytest.mark.asyncio
async def test_expired_session_is_removed(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _client(monkeypatch)
    await flickr.handle("flickr", "", {}, context)
    runtime            = context.state["flickr_runtime"]
    session            = next(iter(runtime["sessions"].values()))
    session.expires_at = 0

    result = await flickr.handle("flickr", "more", {}, context)

    assert "当前没有可继续" in _reply_text(result)
    assert runtime["sessions"] == {}


@pytest.mark.asyncio
async def test_info_uses_current_or_explicit_photo(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)
    await flickr.handle("flickr", "", {}, context)

    current = await flickr.handle("flickr", "info", {}, context)
    assert "Description 1" in _reply_text(current)
    client.photo_info.assert_not_awaited()

    explicit = await flickr.handle("flickr", "info 123456789", {}, context)
    assert "CC BY 2.0" in _reply_text(explicit)
    client.photo_info.assert_awaited_once_with("123456789")


def test_photo_and_album_reference_parsers_cover_supported_urls() -> None:
    assert flickr._parse_photo_reference("123456") == "123456"
    assert (
        flickr._parse_photo_reference("https://www.flickr.com/photos/example/123456/") == "123456"
    )
    assert int(flickr._parse_photo_reference("https://flic.kr/p/2")) > 0
    assert flickr._parse_album_reference(["owner", "album55"]) == ("owner", "album55")


@pytest.mark.asyncio
async def test_configuration_and_api_errors_have_stable_messages(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context.secrets = {}
    missing         = await flickr.handle("flickr", "", {}, context)
    assert "Flickr API Key 未配置" in _reply_text(missing)

    _client(monkeypatch, interesting=AsyncMock(side_effect=FlickrApiError("100")))
    invalid = await flickr.handle("flickr", "", {}, context)
    assert "API Key 无效" in _reply_text(invalid)

    _client(monkeypatch, interesting=AsyncMock(side_effect=FlickrApiError("105")))
    unavailable = await flickr.handle("flickr", "", {}, context)
    assert "暂时不可用" in _reply_text(unavailable)


@pytest.mark.asyncio
async def test_image_download_is_validated_cached_and_host_pinned(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = image_bytes("PNG")
    fetch   = AsyncMock(
        return_value=SimpleNamespace(
            body = payload,
            url  = "https://live.staticflickr.com/66/1_abc_c.jpg",
        )
    )
    monkeypatch.setattr(flickr, "fetch_public_bytes", fetch)

    async def direct(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(flickr, "run_sync", direct)
    photo = _photo(1)

    first  = await _REAL_DOWNLOAD_PHOTO(photo, context)
    second = await _REAL_DOWNLOAD_PHOTO(photo, context)

    assert first == second
    assert first.parent == tmp_path / "images"
    assert first.suffix == ".png"
    assert fetch.await_count == 1
    options = fetch.await_args.kwargs
    assert options["allowed_hosts"] == frozenset({"live.staticflickr.com"})
    assert options["allowed_schemes"] == ("https",)
    assert options["max_bytes"] == flickr.MAX_IMAGE_BYTES
    assert options["allow_transparent_proxy_fake_dns"] is True


@pytest.mark.asyncio
async def test_image_failure_degrades_to_attributed_text(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        flickr,
        "_download_photo",
        AsyncMock(side_effect=ValueError("private image detail")),
    )

    result = await flickr._render_photo(_photo(1), context, index=0, total=1)

    message = _reply_text(result)
    assert "图片下载暂时失败" in message
    assert "Astro Photographer" in message
    assert "https://www.flickr.com/photos/" in message
    assert "private image detail" not in message


@pytest.mark.asyncio
async def test_unexpected_error_uses_public_redacted_response(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context.secrets = {"plugins": {"flickr": {"api_key": "secret-canary-key"}}}
    monkeypatch.setattr(
        flickr,
        "parse",
        Mock(side_effect=RuntimeError("Bearer secret-canary-key C:\\private\\file")),
    )

    result = await flickr.handle("flickr", "ordinary", {}, context)

    serialized = json.dumps(result, ensure_ascii=False)
    assert "XQ-PLUGIN-UNEXPECTED" in serialized
    assert "flickr-test-request" in serialized
    assert "secret-canary-key" not in serialized
    assert "C:\\private" not in serialized
