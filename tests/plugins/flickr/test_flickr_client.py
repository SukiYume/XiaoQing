"""Flickr 固定 API 客户端的传输与响应契约测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.flickr import client as flickr_client
from tests.helpers.settings_snapshot import with_settings_reader


class _AsyncContent:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def iter_chunked(self, size: int):
        for offset in range(0, len(self.body), max(1, size)):
            yield self.body[offset : offset + size]


class _Response:
    def __init__(
        self,
        payload: object,
        *,
        status: int       = 200,
        content_type: str = "application/json",
    ) -> None:
        self.status         = status
        self.url            = flickr_client.API_ENDPOINT
        self.headers        = {"Content-Type": content_type}
        self.content_length = None
        self.content        = _AsyncContent(json.dumps(payload).encode("utf-8"))
        self.closed         = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self, *responses: _Response) -> None:
        self.responses                          = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def _photo_item(**updates: object) -> dict[str, object]:
    item: dict[str, object] = {
        "id": "123456789",
        "owner": "98765432@N01",
        "ownername": "Astro Photographer",
        "secret": "abcdef1234",
        "server": "65535",
        "title": "Night Sky",
        "description": {"_content": "A quiet night."},
        "license": "4",
        "datetaken": "2026-08-18 03:04:05",
        "tags": "night sky telescope",
        "media": "photo",
        "url_c": "https://live.staticflickr.com/65535/123456789_abcdef1234_c.jpg",
    }
    item.update(updates)
    return item


def _page_payload(*items: object, container: str = "photos") -> dict[str, object]:
    return {
        "stat": "ok",
        container: {"page": 1, "pages": 2, "total": "3", "photo": list(items)},
    }


@pytest.fixture
def context(tmp_path: Path) -> SimpleNamespace:
    return with_settings_reader(
        SimpleNamespace(
            data_dir     = tmp_path,
            config       = {},
            secrets      = {"plugins": {"flickr": {"api_key": "test-api-key"}}},
            http_session = None,
            logger       = MagicMock(),
            state        = {},
        )
    )


@pytest.mark.parametrize(
    "secrets",
    [
        {},
        {"plugins": {}},
        {"plugins": {"flickr": {}}},
        {"plugins": {"flickr": {"api_key": "bad key\n"}}},
    ],
)
def test_api_key_is_required_and_bounded(context: SimpleNamespace, secrets: dict) -> None:
    context.secrets = secrets

    with pytest.raises(flickr_client.FlickrConfigurationError):
        flickr_client.FlickrClient(context)


@pytest.mark.asyncio
async def test_interesting_uses_fixed_bounded_endpoint_and_parses_photo(
    context: SimpleNamespace,
) -> None:
    context.http_session = _Session(_Response(_page_payload(_photo_item())))
    client               = flickr_client.FlickrClient(context)

    page = await client.interesting()

    assert page.total == 3
    assert len(page.photos) == 1
    photo = page.photos[0]
    assert photo.title == "Night Sky"
    assert photo.owner_name == "Astro Photographer"
    assert photo.license_id == "4"
    assert photo.media_url.startswith("https://live.staticflickr.com/")
    method, url, options = context.http_session.calls[0]
    assert (method, url) == ("GET", flickr_client.API_ENDPOINT)
    assert options["allow_redirects"] is False
    assert options["auto_decompress"] is False
    assert options["params"]["method"] == "flickr.interestingness.getList"
    assert options["params"]["api_key"] == "test-api-key"
    assert options["params"]["nojsoncallback"] == "1"


@pytest.mark.asyncio
async def test_search_omits_license_for_any_and_passes_explicit_filters(
    context: SimpleNamespace,
) -> None:
    context.http_session = _Session(
        _Response(_page_payload()),
        _Response(_page_payload()),
    )
    client = flickr_client.FlickrClient(context)

    await client.search(
        query          = "aurora",
        tags           = "sky,night",
        sort           = "interestingness-desc",
        license_ids    = None,
        min_taken_date = "2026-08-01 00:00:00",
        max_taken_date = "2026-08-31 23:59:59",
    )
    first = context.http_session.calls[0][2]["params"]
    assert "license" not in first
    assert first["text"] == "aurora"
    assert first["tags"] == "sky,night"
    assert first["media"] == "photos"
    assert first["safe_search"] == 1

    await client.search(
        query          = "moon",
        tags           = "",
        sort           = "relevance",
        license_ids    = "1,2,3,4,5,6,9,10",
        min_taken_date = None,
        max_taken_date = None,
        commons_only   = True,
    )
    second = context.http_session.calls[1][2]["params"]
    assert second["license"] == "1,2,3,4,5,6,9,10"
    assert second["is_commons"] == 1


@pytest.mark.asyncio
async def test_commons_sized_response_fits_json_string_budget(
    context: SimpleNamespace,
) -> None:
    items = [
        _photo_item(
            id          = str(index),
            description = {"_content": "x" * 1_500},
        )
        for index in range(1, 101)
    ]
    context.http_session = _Session(_Response(_page_payload(*items)))
    client               = flickr_client.FlickrClient(context)

    page = await client.search(
        query          = "moon",
        tags           = "",
        sort           = "interestingness-desc",
        license_ids    = None,
        min_taken_date = None,
        max_taken_date = None,
        commons_only   = True,
    )

    assert len(page.photos) == 100


@pytest.mark.asyncio
async def test_api_failure_is_typed_without_remote_message_or_key(
    context: SimpleNamespace,
) -> None:
    context.http_session = _Session(
        _Response(
            {
                "stat": "fail",
                "code": 100,
                "message": "private upstream detail test-api-key",
            }
        )
    )
    client = flickr_client.FlickrClient(context)

    with pytest.raises(flickr_client.FlickrApiError) as caught:
        await client.interesting()

    assert caught.value.code == "100"
    assert "test-api-key" not in str(caught.value)
    assert "private upstream detail" not in str(caught.value)


@pytest.mark.asyncio
async def test_wrong_mime_http_error_and_malformed_json_fail_closed(
    context: SimpleNamespace,
) -> None:
    client = flickr_client.FlickrClient(context)
    context.http_session = _Session(_Response({"stat": "ok"}, content_type="text/html"))
    with pytest.raises(flickr_client.FlickrTransportError):
        await client.interesting()

    context.http_session = _Session(_Response({"stat": "ok"}, status=429))
    with pytest.raises(flickr_client.FlickrTransportError):
        await client.interesting()

    context.http_session = _Session(_Response({"photos": {}}))
    with pytest.raises(flickr_client.FlickrProtocolError):
        await client.interesting()


def test_photo_parser_skips_malformed_video_and_untrusted_media_urls() -> None:
    page = flickr_client._parse_photo_page(
        _page_payload(
            None,
            _photo_item(media="video"),
            _photo_item(id="bad/id"),
            _photo_item(url_c="https://example.com/image.jpg", server="", secret=""),
            _photo_item(url_c="", id="55", server="66", secret="abc"),
        ),
        container_name="photos",
    )

    assert len(page.photos) == 1
    assert page.photos[0].media_url == "https://live.staticflickr.com/66/55_abc_c.jpg"


@pytest.mark.asyncio
async def test_user_resolution_supports_nsid_username_and_profile_url(
    context: SimpleNamespace,
) -> None:
    client       = flickr_client.FlickrClient(context)
    client._call = AsyncMock(
        side_effect=[
            {"stat": "ok", "user": {"id": "11@N22"}},
            {"stat": "ok", "user": {"id": "33@N44"}},
        ]
    )

    assert await client.resolve_user("123@N45") == "123@N45"
    assert await client.resolve_user("Display Name") == "11@N22"
    assert await client.resolve_user("https://www.flickr.com/photos/example/") == "33@N44"
    assert client._call.await_args_list[0].args == ("flickr.people.findByUsername",)
    assert client._call.await_args_list[1].args == ("flickr.urls.lookupUser",)


@pytest.mark.asyncio
async def test_public_photos_album_and_info_use_expected_contracts(
    context: SimpleNamespace,
) -> None:
    client       = flickr_client.FlickrClient(context)
    info_payload = {
        "stat": "ok",
        "photo": {
            "id": "123456789",
            "server": "65535",
            "secret": "abcdef1234",
            "license": "4",
            "owner": {
                "nsid": "98765432@N01",
                "username": "Astro Photographer",
            },
            "title": {"_content": "Night Sky"},
            "description": {"_content": "Long description"},
            "dates": {"taken": "2026-08-18 03:04:05"},
            "tags": {"tag": [{"raw": "night"}, {"raw": "sky"}]},
        },
    }
    client._call = AsyncMock(
        side_effect=[
            _page_payload(_photo_item(owner="")),
            _page_payload(_photo_item(owner=""), container="photoset"),
            info_payload,
        ]
    )

    public_page = await client.public_photos("98765432@N01")
    album_page = await client.album_photos(user_id="98765432@N01", album_id="album55")
    info = await client.photo_info("123456789")

    assert public_page.photos[0].owner_id == "98765432@N01"
    assert album_page.photos[0].owner_id == "98765432@N01"
    assert info.description == "Long description"
    assert info.tags == ("night", "sky")
    assert client._call.await_args_list[1].kwargs["photoset_id"] == "album55"
    assert client._call.await_args_list[2].kwargs["photo_id"] == "123456789"
