"""Flickr 命令、会话、图片缓存与公开错误测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.flickr import main as flickr
from plugins.flickr.client import FlickrPhoto
from tests.helpers.http import aiohttp_json_response
from tests.helpers.payloads import image_bytes
from tests.helpers.settings_snapshot import with_settings_reader

_OWNER_ID = "98765432@N01"


def _photo(number: int, *, license_id: str = "0") -> FlickrPhoto:
    return FlickrPhoto(
        photo_id    = str(number),
        owner_id    = _OWNER_ID,
        owner_name  = "Astro Photographer",
        title       = f"Night Sky {number}",
        description = f"Description {number}",
        license_id  = license_id,
        taken_at    = "2026-08-18 03:04:05",
        tags        = ("night", "sky"),
        media_url   = f"https://live.staticflickr.com/66/{number}_abc_c.jpg",
        page_url    = f"https://www.flickr.com/photos/{_OWNER_ID}/{number}/",
    )


def _photo_item(number: int) -> dict[str, Any]:
    """Flickr 列表接口里的一张照片。"""

    return {
        "id"         : str(number),
        "owner"      : _OWNER_ID,
        "ownername"  : "Astro Photographer",
        "title"      : f"Night Sky {number}",
        "description": {"_content": f"Description {number}"},
        "license"    : "0",
        "datetaken"  : "2026-08-18 03:04:05",
        "tags"       : "night sky",
        "media"      : "photo",
        "url_c"      : f"https://live.staticflickr.com/66/{number}_abc_c.jpg",
    }


def _list_payload(count: int = 3, *, container: str = "photos") -> dict[str, Any]:
    return {
        "stat"   : "ok",
        container: {
            "page" : 1,
            "pages": 1,
            "total": count,
            "photo": [_photo_item(number) for number in range(1, count + 1)],
        },
    }


def _info_payload(number: int, *, license_id: str) -> dict[str, Any]:
    return {
        "stat" : "ok",
        "photo": {
            "id"         : str(number),
            "owner"      : {"nsid": _OWNER_ID, "realname": "Astro Photographer"},
            "title"      : {"_content": f"Night Sky {number}"},
            "description": {"_content": f"Description {number}"},
            "license"    : license_id,
            "dates"      : {"taken": "2026-08-18 03:04:05"},
            "tags"       : {"tag": [{"raw": "night"}, {"raw": "sky"}]},
            "url_c"      : f"https://live.staticflickr.com/66/{number}_abc_c.jpg",
        },
    }


class _ApiSession:
    """Flickr REST 的 HTTP 会话替身：按请求参数里的 ``method`` 返回预置的 JSON。"""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {
            "flickr.interestingness.getList": _list_payload(),
            "flickr.photos.search"          : _list_payload(),
            "flickr.people.findByUsername"  : {"stat": "ok", "user": {"id": _OWNER_ID}},
            "flickr.urls.lookupUser"        : {"stat": "ok", "user": {"id": _OWNER_ID}},
            "flickr.people.getPublicPhotos" : _list_payload(),
            "flickr.photosets.getPhotos"    : _list_payload(container="photoset"),
            "flickr.photos.getInfo"         : _info_payload(9, license_id="4"),
        }
        self.calls: list[dict[str, Any]] = []

    def request(self, _method: str, url: str, **kwargs: Any):
        params = dict(kwargs["params"])
        self.calls.append(params)
        payload = self.routes[params["method"]]
        if isinstance(payload, Exception):
            raise payload
        return aiohttp_json_response(payload, url=url)

    def calls_to(self, method: str) -> list[dict[str, Any]]:
        return [params for params in self.calls if params["method"] == method]


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


@pytest.fixture
def api(context: SimpleNamespace) -> _ApiSession:
    session              = _ApiSession()
    context.http_session = session
    return session


@pytest.fixture(autouse=True)
def image_download(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """图片下载边界：``fetch_public_bytes`` 返回一张有效的小 PNG；真实的校验与缓存照常运行。"""

    fetch = AsyncMock(
        return_value=SimpleNamespace(body=image_bytes("PNG"), url="https://live.staticflickr.com/")
    )
    monkeypatch.setattr(flickr, "fetch_public_bytes", fetch)
    return fetch


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
    api: _ApiSession,
) -> None:
    result = await flickr.handle("flickr", "", {"user_id": 123, "group_id": 456}, context)

    assert [segment["type"] for segment in result] == ["image", "text"]
    message = _reply_text(result)
    assert "Night Sky 1" in message
    assert "All Rights Reserved" in message
    assert "第 1/3 张" in message
    assert [params["page"] for params in api.calls_to("flickr.interestingness.getList")] == [1]


@pytest.mark.asyncio
async def test_search_defaults_to_all_licenses_and_parses_filters(
    context: SimpleNamespace,
    api: _ApiSession,
) -> None:
    await flickr.handle(
        "flickr",
        "search Milky Way --tags night,sky --sort interesting --date 2026-08",
        {},
        context,
    )

    (options,) = api.calls_to("flickr.photos.search")
    assert options["text"] == "Milky Way"
    assert options["tags"] == "night,sky"
    assert options["sort"] == "interestingness-desc"
    assert "license" not in options
    assert options["min_taken_date"] == "2026-08-01 00:00:00"
    assert options["max_taken_date"] == "2026-08-31 23:59:59"
    assert "is_commons" not in options


@pytest.mark.asyncio
async def test_cc_and_commons_filters_are_explicit(
    context: SimpleNamespace,
    api: _ApiSession,
) -> None:
    await flickr.handle("flickr", "典藏 moon --license cc", {}, context)

    (options,) = api.calls_to("flickr.photos.search")
    assert options["license"] == "1,2,3,4,5,6,9,10"
    assert options["is_commons"] == 1


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
    api: _ApiSession,
    args: str,
    expected: str,
) -> None:
    result = await flickr.handle("flickr", args, {}, context)

    assert expected in _reply_text(result)
    assert api.calls == []


@pytest.mark.asyncio
async def test_user_and_album_commands_resolve_owner(
    context: SimpleNamespace,
    api: _ApiSession,
) -> None:
    await flickr.handle("flickr", "user Display Name", {}, context)
    assert [p["username"] for p in api.calls_to("flickr.people.findByUsername")] == ["Display Name"]
    assert [p["user_id"] for p in api.calls_to("flickr.people.getPublicPhotos")] == [_OWNER_ID]

    await flickr.handle(
        "flickr",
        "album https://www.flickr.com/photos/example/albums/72100000000000000",
        {},
        context,
    )
    assert [p["username"] for p in api.calls_to("flickr.people.findByUsername")] == [
        "Display Name",
        "example",
    ]
    (album_request,) = api.calls_to("flickr.photosets.getPhotos")
    assert album_request["user_id"] == _OWNER_ID
    assert album_request["photoset_id"] == "72100000000000000"


@pytest.mark.asyncio
async def test_more_advances_without_repeats_and_stops_at_end(
    context: SimpleNamespace,
    api: _ApiSession,
) -> None:
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
    api: _ApiSession,
) -> None:
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
    api: _ApiSession,
) -> None:
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
    api: _ApiSession,
) -> None:
    await flickr.handle("flickr", "", {}, context)

    current = await flickr.handle("flickr", "info", {}, context)
    assert "Description 1" in _reply_text(current)
    assert api.calls_to("flickr.photos.getInfo") == []

    explicit = await flickr.handle("flickr", "info 123456789", {}, context)
    assert "CC BY 2.0" in _reply_text(explicit)
    assert [p["photo_id"] for p in api.calls_to("flickr.photos.getInfo")] == ["123456789"]


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
    api: _ApiSession,
) -> None:
    context.secrets = {}
    missing         = await flickr.handle("flickr", "", {}, context)
    assert "Flickr API Key 未配置" in _reply_text(missing)

    context.secrets = {"plugins": {"flickr": {"api_key": "test-api-key"}}}
    api.routes["flickr.interestingness.getList"] = {"stat": "fail", "code": 100}
    invalid = await flickr.handle("flickr", "", {}, context)
    assert "API Key 无效" in _reply_text(invalid)

    api.routes["flickr.interestingness.getList"] = {"stat": "fail", "code": 105}
    unavailable                                  = await flickr.handle("flickr", "", {}, context)
    assert "暂时不可用" in _reply_text(unavailable)


@pytest.mark.asyncio
async def test_image_download_is_validated_cached_and_host_pinned(
    context: SimpleNamespace,
    image_download: AsyncMock,
    tmp_path: Path,
) -> None:
    fetch = image_download
    photo = _photo(1)

    first  = await flickr._download_photo(photo, context)
    second = await flickr._download_photo(photo, context)

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
    image_download: AsyncMock,
) -> None:
    image_download.side_effect = ValueError("private image detail")

    result = await flickr._render_photo(_photo(1), context, index=0, total=1)

    message = _reply_text(result)
    assert "图片下载暂时失败" in message
    assert "Astro Photographer" in message
    assert "https://www.flickr.com/photos/" in message
    assert "private image detail" not in message


@pytest.mark.asyncio
async def test_unexpected_error_uses_public_redacted_response(
    context: SimpleNamespace,
    api: _ApiSession,
) -> None:
    context.secrets = {"plugins": {"flickr": {"api_key": "secret-canary-key"}}}
    # 网络层出了插件没有预料到的异常（不是传输 / 协议类错误）。
    api.routes["flickr.interestingness.getList"] = LookupError(
        "Bearer secret-canary-key C:\\private\\file"
    )

    result = await flickr.handle("flickr", "", {}, context)

    serialized = json.dumps(result, ensure_ascii=False)
    assert "XQ-PLUGIN-UNEXPECTED" in serialized
    assert "flickr-test-request" in serialized
    assert "secret-canary-key" not in serialized
    assert "C:\\private" not in serialized


@pytest.mark.asyncio
async def test_flickr_empty_queries_do_not_accumulate_locks(tmp_path):
    from plugins.flickr import main

    context = SimpleNamespace(state={}, current_user_id=1, current_group_id=None)
    for user in range(1, 1100):
        context.current_user_id = user
        await main._more(1, event={}, context=context)
    runtime = main._runtime(context)
    main._prune_runtime(runtime, now=float("inf"))
    assert runtime["sessions"] == runtime["locks"] == {}
