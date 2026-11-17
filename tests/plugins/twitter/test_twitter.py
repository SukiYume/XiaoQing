"""Twitter 图片插件的配置、传输、缓存与命令契约测试。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.bounded_file_cache import FileCacheLimits
from core.bounded_http import ResponseFormatError
from core.safe_http import SafeHttpResponse
from plugins.twitter import main as twitter
from tests.helpers.assertions import logged_events
from tests.helpers.http import AiohttpBodyStream, QueuedAiohttpSession
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.payloads import image_bytes as _image_bytes
from tests.helpers.settings_snapshot import with_settings_reader

ROOT = REPOSITORY_ROOT


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        status: int       = 200,
        content_type: str = "application/json",
        url: str          = twitter._TIMELINE_URL,
    ) -> None:
        self.status         = status
        self.url            = url
        self.headers        = {"Content-Type": content_type}
        self.content_length = None
        self.content        = AiohttpBodyStream(body)
        self.closed         = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def close(self) -> None:
        self.closed = True


class _RaisingSession:
    def request(self, *_args, **_kwargs):
        raise RuntimeError("network detail must stay private")


def _json_response(payload: object, *, status: int = 200) -> _Response:
    return _Response(json.dumps(payload).encode("utf-8"), status=status)


class _PendingResponse:
    """``session.request(...)`` 返回的异步上下文；进入时可先等一个测试给的闸门。"""

    def __init__(self, response: _Response, gate: Callable[[], Awaitable[None]] | None) -> None:
        self._response = response
        self._gate     = gate

    async def __aenter__(self) -> _Response:
        if self._gate is not None:
            await self._gate()
        return self._response

    async def __aexit__(self, *_args: object) -> None:
        return None


class _TwitterSession:
    """按地址应答的 aiohttp 会话替身：时间线按页排队，媒体按请求地址给字节。

    ``pages`` 的每一项是时间线 JSON 载荷（或现成的响应）；``media`` 把规范化后的媒体请求地址
    映射到图片字节，值为 ``None`` 表示远端返回 404。``gate`` 只在时间线请求上等待，
    用来在网络这条边界上停住一轮抓取。
    """

    def __init__(
        self,
        *pages: object,
        media: Mapping[str, bytes | None] | None   = None,
        gate: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.pages                                      = list(pages)
        self.media                                      = dict(media or {})
        self.gate                                       = gate
        self.timeline_requests: list[dict[str, object]] = []
        self.media_urls: list[str]                      = []

    def request(self, method: str, url: str, **kwargs: object) -> _PendingResponse:
        assert method == "GET"
        if url == twitter._TIMELINE_URL:
            self.timeline_requests.append(kwargs)
            page     = self.pages.pop(0)
            response = page if isinstance(page, _Response) else _json_response(page)
            return _PendingResponse(response, self.gate)
        self.media_urls.append(url)
        body = self.media[url]
        if body is None:
            return _PendingResponse(_Response(b"", status=404, url=url), None)
        return _PendingResponse(_Response(body, content_type="image/png", url=url), None)

    def timeline_variables(self, index: int) -> dict[str, object]:
        params = self.timeline_requests[index]["params"]
        assert isinstance(params, dict)
        variables: dict[str, object] = json.loads(params["variables"])
        return variables


def _photo_url(name: str) -> str:
    """时间线里一张照片的原始地址。"""

    return f"https://pbs.twimg.com/media/{name}.jpg"


def _large_photo_url(name: str) -> str:
    """插件下载这张照片时实际请求的地址（规范化成 large 尺寸）。"""

    return f"https://pbs.twimg.com/media/{name}?format=jpg&name=large"


def _cursor_entry(value: str) -> dict:
    return {"entryId": "cursor-bottom-next", "content": {"value": value}}


def _distinct_image(index: int) -> bytes:
    """第 ``index`` 张内容互不相同的小图。"""

    payload: bytes = _image_bytes("PNG", (index % 256, index // 256, 9))
    return payload


def _seed_cached_image(context: SimpleNamespace, payload: bytes) -> None:
    """把一张图按内容哈希命名放进缓存，让同样内容的下载被认作“已存在”。"""

    image_dir = context.data_dir / "images"
    image_dir.mkdir(exist_ok=True)
    (image_dir / f"{hashlib.sha256(payload).hexdigest()}.png").write_bytes(payload)


def _oversized_png() -> bytes:
    """像素数刚好超过插件上限的 1 位 PNG；压缩后只有几 KB。"""

    from PIL import Image

    width  = 8000
    output = io.BytesIO()
    Image.new("1", (width, twitter.MAX_IMAGE_PIXELS // width + 1)).save(output, format="PNG")
    return output.getvalue()


def _tweet(*urls: str) -> dict:
    return {
        "entryId": "tweet-1",
        "content": {
            "itemContent": {
                "tweet_results": {
                    "result": {
                        "legacy": {
                            "extended_entities": {
                                "media": [{"type": "photo", "media_url_https": url} for url in urls]
                            }
                        }
                    }
                }
            }
        },
    }


def _timeline_payload(entries: list[object]) -> dict:
    return {
        "data": {
            "user": {
                "result": {
                    "timeline": {
                        "timeline": {
                            "instructions": [{"type": "TimelineAddEntries", "entries": entries}]
                        }
                    }
                }
            }
        }
    }


@pytest.fixture
def context(tmp_path: Path) -> SimpleNamespace:
    return with_settings_reader(
        SimpleNamespace(
            data_dir = tmp_path,
            secrets  = {
                "plugins": {
                    "twitter": {
                        "user_id"  : "123456789",
                        "headers"  : {"authorization": "Bearer test-token"},
                        "cookies"  : {"ct0": "csrf-token"},
                        "proxy"    : "http://proxy.example.com:8080",
                        "max_pages": 50,
                    }
                }
            },
            http_session     = None,
            current_user_id  = 123,
            current_group_id = 456,
            send_action      = AsyncMock(),
        )
    )


@pytest.fixture
def install_media_fetch(monkeypatch: pytest.MonkeyPatch):
    def install(
        payload: bytes | None = None,
        *,
        error: Exception | None = None,
    ) -> AsyncMock:
        fetch = AsyncMock(return_value=payload, side_effect=error)
        monkeypatch.setattr(twitter, "_fetch_media_bytes", fetch)
        return fetch

    return install


@pytest.mark.parametrize(
    "secrets",
    [None, [], {"plugins": []}, {"plugins": {"twitter": []}}],
)
def test_malformed_config_layers_fall_back_to_empty(
    context: SimpleNamespace,
    secrets: object,
) -> None:
    context.secrets = secrets
    assert twitter._get_config(context) == {}


def test_api_headers_keep_only_safe_explicit_string_values(context: SimpleNamespace) -> None:
    context.secrets["plugins"]["twitter"]["headers"] = {
        "Authorization": "  Bearer dummy-secret  ",
        "user-agent"   : "Custom Agent",
        "Host"         : "attacker.example",
        "Bad Header"   : "value",
        "X-Number"     : 7,
        "X-Newline"    : "first\r\nsecond",
        "X-Empty"      : "  ",
    }

    headers = twitter._get_headers(context)

    folded = {key.casefold(): value for key, value in headers.items()}
    assert folded == {
        "accept"       : "application/json",
        "user-agent"   : "Custom Agent",
        "authorization": "Bearer dummy-secret",
    }


def test_non_mapping_headers_do_not_add_credentials(context: SimpleNamespace) -> None:
    context.secrets["plugins"]["twitter"]["headers"] = "Bearer hidden"
    headers                                          = twitter._get_headers(context)
    assert {key.casefold() for key in headers} == {"accept", "user-agent"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://proxy.example.com:8080", "http://proxy.example.com:8080"),
        (" https://user:pass@proxy.example.com ", "https://user:pass@proxy.example.com"),
        (None, None),
        (123, None),
        ("", None),
        ("socks5://proxy.example.com", None),
        ("http://", None),
        ("http://proxy.example.com?query=1", None),
        ("http://proxy.example.com#fragment", None),
        ("http://proxy.example.com:0", None),
        ("http://proxy.example.com:bad", None),
        ("http://proxy.example.com\n", None),
    ],
)
def test_proxy_configuration_is_strict(
    context: SimpleNamespace,
    value: object,
    expected: str | None,
) -> None:
    context.secrets["plugins"]["twitter"]["proxy"] = value
    assert twitter._get_proxy(context) == expected


def test_cookies_keep_only_bounded_string_pairs(context: SimpleNamespace) -> None:
    context.secrets["plugins"]["twitter"]["cookies"] = {
        "ct0"      : " csrf ",
        "nested"   : {"secret": "value"},
        "empty"    : "",
        "bad\nname": "value",
        "bad-value": "one\rtwo",
    }
    assert twitter._get_cookies(context) == {"ct0": "csrf"}

    context.secrets["plugins"]["twitter"]["cookies"] = ["not", "a", "mapping"]
    assert twitter._get_cookies(context) == {}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, 1),
        (0, 1),
        (-20, 1),
        (12, 12),
        (999, twitter.MAX_PAGES_TO_CHECK),
        ("12", twitter.MAX_PAGES_TO_CHECK),
        (True, twitter.MAX_PAGES_TO_CHECK),
        (1.5, twitter.MAX_PAGES_TO_CHECK),
        (None, twitter.MAX_PAGES_TO_CHECK),
    ],
)
def test_max_pages_accepts_only_clamped_integers(
    context: SimpleNamespace,
    value: object,
    expected: int,
) -> None:
    context.secrets["plugins"]["twitter"]["max_pages"] = value
    assert twitter._get_max_pages(context) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://pbs.twimg.com/media/a.jpg",
        "https://ton.twitter.com/media/a.png",
        "https://video.twimg.com/media/a.webp",
        "https://pbs.twimg.com:443/media/a.jpg",
    ],
)
def test_media_url_allowlist_accepts_exact_https_hosts(url: str) -> None:
    assert twitter._is_allowed_media_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://pbs.twimg.com/media/a.jpg",
        "https://evil.example/media/a.jpg",
        "https://pbs.twimg.com.evil.example/media/a.jpg",
        "https://user@pbs.twimg.com/media/a.jpg",
        "https://pbs.twimg.com:444/media/a.jpg",
        "https://pbs.twimg.com:bad/media/a.jpg",
        "https://pbs.twimg.com",
        "https://pbs.twimg.com/media/a.jpg\n",
        "x" * (twitter.MAX_MEDIA_URL_CHARS + 1),
        "",
    ],
)
def test_media_url_allowlist_rejects_unsafe_values(url: str) -> None:
    assert not twitter._is_allowed_media_url(url)


def test_original_media_url_is_normalized_without_remote_filename_use() -> None:
    assert (
        twitter._original_media_url("https://pbs.twimg.com/media/ABC.jpg?name=small#ignored")
        == "https://pbs.twimg.com/media/ABC?format=jpg&name=large"
    )
    assert (
        twitter._original_media_url("https://pbs.twimg.com/media/ABC.jpeg")
        == "https://pbs.twimg.com/media/ABC?format=jpg&name=large"
    )
    assert (
        twitter._original_media_url("https://ton.twitter.com/media/ABC.png?token=public#ignored")
        == "https://ton.twitter.com/media/ABC.png?token=public"
    )


@pytest.mark.asyncio
async def test_timeline_request_uses_bounded_get_and_extracts_entries(
    context: SimpleNamespace,
) -> None:
    entries = [
        None,
        _tweet("https://pbs.twimg.com/media/a.jpg"),
        {"entryId": "cursor-bottom-empty", "content": {"value": ""}},
        {"entryId": "cursor-bottom-invalid", "content": {"value": "bad\ncursor"}},
        {"entryId": "cursor-bottom-next", "content": {"value": "next-token"}},
        {"entryId": "cursor-bottom-later", "content": {"value": "ignored"}},
    ]
    session              = QueuedAiohttpSession(_json_response(_timeline_payload(entries)))
    context.http_session = session

    tweets, cursor = await twitter._fetch_timeline(context, 'cursor"\\value')

    assert tweets == [entries[1]]
    assert cursor == "next-token"
    method, url, kwargs = session.requests[0]
    assert (method, url) == ("GET", twitter._TIMELINE_URL)
    assert kwargs["allow_redirects"] is False
    assert kwargs["auto_decompress"] is False
    variables = json.loads(kwargs["params"]["variables"])
    assert variables["userId"] == "123456789"
    assert variables["cursor"] == 'cursor"\\value'
    assert kwargs["cookies"] == {"ct0": "csrf-token"}
    assert kwargs["proxy"] == "http://proxy.example.com:8080"
    assert kwargs["headers"]["authorization"] == "Bearer test-token"


@pytest.mark.asyncio
async def test_timeline_ignores_invalid_cursor_and_rejects_malformed_entries(
    context: SimpleNamespace,
) -> None:
    payload = {
        "data": {
            "user": {
                "result": {
                    "timeline": {
                        "timeline": {
                            "instructions": [
                                None,
                                {"type": "Other", "entries": []},
                                {"type": "TimelineAddEntries", "entries": "bad"},
                            ]
                        }
                    }
                }
            }
        }
    }
    session              = QueuedAiohttpSession(_json_response(payload))
    context.http_session = session

    # 已识别的 TimelineAddEntries 结构畸形时明确失败，不能当成空时间线标记回填完成。
    with pytest.raises(twitter.TwitterFetchError):
        await twitter._fetch_timeline(context, "bad\ncursor")
    variables = json.loads(session.requests[0][2]["params"]["variables"])
    assert "cursor" not in variables


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"data": []},
        {"data": {"user": {"result": {"timeline": {"timeline": {"instructions": {}}}}}}},
    ],
)
async def test_timeline_malformed_shapes_fail_closed(
    context: SimpleNamespace,
    payload: object,
) -> None:
    context.http_session = QueuedAiohttpSession(_json_response(payload))
    with pytest.raises(twitter.TwitterFetchError, match="request failed"):
        await twitter._fetch_timeline(context)


@pytest.mark.asyncio
async def test_timeline_http_and_transport_errors_are_reported(context: SimpleNamespace) -> None:
    context.http_session = QueuedAiohttpSession(_json_response({"private": "detail"}, status=429))
    with pytest.raises(twitter.TwitterFetchError, match="HTTP 429"):
        await twitter._fetch_timeline(context)

    context.http_session = _RaisingSession()
    with pytest.raises(twitter.TwitterFetchError, match="request failed"):
        await twitter._fetch_timeline(context)


def test_image_extraction_keeps_valid_photos_and_skips_bad_items() -> None:
    tweet = _tweet(" https://pbs.twimg.com/media/a.png ")
    media = tweet["content"]["itemContent"]["tweet_results"]["result"]["legacy"][
        "extended_entities"
    ]["media"]
    media.extend(
        [
            None,
            {"type": "photo"},
            {"type": "photo", "media_url_https": None},
            {"type": "video", "media_url_https": "https://pbs.twimg.com/video.mp4"},
        ]
    )
    assert twitter._extract_image_urls(tweet) == ["https://pbs.twimg.com/media/a.png"]
    assert twitter._extract_image_urls({"content": []}) == []


@pytest.mark.parametrize(
    ("image_format", "extension"),
    [("JPEG", ".jpg"), ("PNG", ".png"), ("WEBP", ".webp")],
)
def test_image_validation_uses_actual_content_format(
    image_format: str,
    extension: str,
) -> None:
    assert twitter._detect_image_extension(_image_bytes(image_format)) == extension


def test_image_validation_rejects_invalid_unsupported_and_oversized_images() -> None:
    with pytest.raises(ResponseFormatError, match="valid supported image"):
        twitter._detect_image_extension(b"not an image")
    with pytest.raises(ResponseFormatError, match="unsupported"):
        twitter._detect_image_extension(_image_bytes("GIF"))
    with pytest.raises(ResponseFormatError, match="dimensions"):
        twitter._detect_image_extension(_oversized_png())


@pytest.mark.asyncio
async def test_download_validates_and_commits_by_content_hash(
    context: SimpleNamespace,
    tmp_path: Path,
    install_media_fetch,
) -> None:
    payload  = _image_bytes("PNG")
    fetch    = install_media_fetch(payload)
    save_dir = tmp_path / "images"

    assert await twitter._download_image(
        "https://pbs.twimg.com/media/unsafe-name.jpg?name=small",
        save_dir,
        context,
    )

    expected = save_dir / f"{hashlib.sha256(payload).hexdigest()}.png"
    assert expected.read_bytes() == payload
    assert not (save_dir / "unsafe-name.jpg").exists()
    assert fetch.await_args.args[0] == (
        "https://pbs.twimg.com/media/unsafe-name?format=jpg&name=large"
    )
    assert fetch.await_args.args[1] is context

    install_media_fetch(payload)
    assert not await twitter._download_image(
        "https://pbs.twimg.com/media/another.png",
        save_dir,
        context,
    )
    assert [path.name for path in save_dir.glob("*.png")] == [expected.name]


@pytest.mark.asyncio
async def test_media_fetch_uses_safe_direct_transport_without_proxy(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context.secrets["plugins"]["twitter"]["proxy"] = ""
    payload                                        = _image_bytes()
    public_fetch                                   = AsyncMock(
        return_value=SafeHttpResponse(
            url     = "https://pbs.twimg.com/media/final",
            status  = 200,
            body    = payload,
            charset = None,
            headers = {"Content-Type": "image/png"},
        )
    )
    monkeypatch.setattr(twitter, "fetch_public_bytes", public_fetch)

    url = "https://pbs.twimg.com/media/a.png"
    assert await twitter._fetch_media_bytes(url, context) == payload
    options = public_fetch.await_args.kwargs
    assert options["allowed_hosts"] == twitter._ALLOWED_TWITTER_MEDIA_HOSTS
    assert options["allowed_schemes"] == ("https",)
    assert options["max_bytes"] == twitter.MAX_IMAGE_BYTES
    media_headers = {key.casefold(): value for key, value in options["headers"].items()}
    assert "authorization" not in media_headers
    assert "cookie" not in media_headers
    assert media_headers["accept-encoding"] == "identity"


@pytest.mark.asyncio
async def test_media_fetch_uses_configured_proxy_with_bounded_reader(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _image_bytes()
    context.http_session = QueuedAiohttpSession(_Response(payload, content_type="image/png"))
    public_fetch = AsyncMock()
    monkeypatch.setattr(twitter, "fetch_public_bytes", public_fetch)

    url = "https://pbs.twimg.com/media/a.png"
    assert await twitter._fetch_media_bytes(url, context) == payload
    method, requested_url, options = context.http_session.requests[0]
    assert (method, requested_url) == ("GET", url)
    assert options["proxy"] == "http://proxy.example.com:8080"
    assert options["timeout"] == twitter.REQUEST_TIMEOUT_SECONDS
    assert options["allow_redirects"] is False
    assert options["auto_decompress"] is False
    media_headers = {key.casefold(): value for key, value in options["headers"].items()}
    assert "authorization" not in media_headers
    assert "cookie" not in media_headers
    assert media_headers["accept-encoding"] == "identity"
    public_fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://pbs.twimg.com/media/a.jpg",
        "https://example.com/media/a.jpg",
        "https://user@pbs.twimg.com/media/a.jpg",
        "https://pbs.twimg.com:444/media/a.jpg",
    ],
)
async def test_download_rejects_untrusted_media_before_network(
    context: SimpleNamespace,
    tmp_path: Path,
    install_media_fetch,
    url: str,
) -> None:
    fetch = install_media_fetch(_image_bytes())
    assert not await twitter._download_image(url, tmp_path / "images", context)
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_failures_leave_no_committed_image(
    context: SimpleNamespace,
    tmp_path: Path,
    install_media_fetch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_dir = tmp_path / "images"
    url      = "https://pbs.twimg.com/media/a.png"

    install_media_fetch(None)
    assert not await twitter._download_image(url, save_dir, context)
    install_media_fetch(error=RuntimeError("private download failure"))
    assert not await twitter._download_image(url, save_dir, context)
    install_media_fetch(b"not an image")
    assert not await twitter._download_image(url, save_dir, context)
    install_media_fetch(b"")
    assert not await twitter._download_image(url, save_dir, context)

    payload = _image_bytes()
    install_media_fetch(payload + bytes(twitter.MAX_IMAGE_BYTES))
    assert not await twitter._download_image(url, save_dir, context)
    install_media_fetch(payload)
    monkeypatch.setattr(
        twitter,
        "IMAGE_CACHE_LIMITS",
        FileCacheLimits(max_entries=1, max_bytes=1, ttl_seconds=60),
    )
    assert not await twitter._download_image(url, save_dir, context)
    assert list(save_dir.glob("*.png")) == []


@pytest.mark.asyncio
async def test_fetch_paginates_and_deduplicates_urls(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _distinct_image(1)
    session = _TwitterSession(
        _timeline_payload([_tweet(_photo_url("a"), _photo_url("a")), _cursor_entry("cursor-1")]),
        _timeline_payload([_tweet(_photo_url("a"), _photo_url("b"))]),
        media={_large_photo_url("a"): payload, _large_photo_url("b"): payload},
    )
    context.http_session = session
    monkeypatch.setattr(twitter, "_FETCH_LOCK", asyncio.Lock())

    # 第二张图与第一张内容相同：按内容哈希认作已存在，不算新增。
    assert await twitter._fetch_twitter_images(context) == 1
    assert session.media_urls == [_large_photo_url("a"), _large_photo_url("b")]
    assert "cursor" not in session.timeline_variables(0)
    assert session.timeline_variables(1)["cursor"] == "cursor-1"
    assert len(session.timeline_requests) == 2


@pytest.mark.asyncio
async def test_fetch_stops_after_two_pages_without_new_images(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _distinct_image(1)
    session = _TwitterSession(
        *(
            _timeline_payload([_tweet(_photo_url(str(index))), _cursor_entry(f"c{index}")])
            for index in (1, 2, 3)
        ),
        media={_large_photo_url(str(index)): payload for index in (1, 2, 3)},
    )
    context.http_session = session
    monkeypatch.setattr(twitter, "_FETCH_LOCK", asyncio.Lock())
    _seed_cached_image(context, payload)
    twitter._write_backfill_state(
        context.data_dir / twitter.BACKFILL_STATE_FILENAME,
        twitter._get_user_id(context),
    )

    assert await twitter._fetch_twitter_images(context) == 0
    assert len(session.timeline_requests) == 2
    assert session.timeline_variables(1)["cursor"] == "c1"


@pytest.mark.asyncio
async def test_fetch_reports_when_every_media_download_fails(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context.http_session = _TwitterSession(
        _timeline_payload([_tweet(_photo_url("a"), _photo_url("b"))]),
        media={_large_photo_url("a"): None, _large_photo_url("b"): None},
    )
    monkeypatch.setattr(twitter, "_FETCH_LOCK", asyncio.Lock())

    with pytest.raises(twitter.TwitterMediaFetchError) as raised:
        await twitter._fetch_twitter_images(context)
    assert raised.value.attempted == 2
    assert not (context.data_dir / twitter.BACKFILL_STATE_FILENAME).exists()


@pytest.mark.asyncio
async def test_fetch_stops_on_empty_timeline_and_repeated_cursor(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _TwitterSession(
        _timeline_payload([]),
        _timeline_payload([_tweet(_photo_url("a")), _cursor_entry("same")]),
        _timeline_payload([_tweet(_photo_url("b")), _cursor_entry("same")]),
        media={
            _large_photo_url("a"): _distinct_image(1),
            _large_photo_url("b"): _distinct_image(2),
        },
    )
    context.http_session = session
    monkeypatch.setattr(twitter, "_FETCH_LOCK", asyncio.Lock())

    assert await twitter._fetch_twitter_images(context) == 0
    assert len(session.timeline_requests) == 1

    assert await twitter._fetch_twitter_images(context) == 2
    assert len(session.timeline_requests) == 3


@pytest.mark.asyncio
async def test_initial_backfill_downloads_all_media_and_records_completion(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names  = [str(index) for index in range(101)]
    cached = _distinct_image(500)
    media  = {_large_photo_url(name): _distinct_image(int(name)) for name in names}
    media |= {_large_photo_url("old-a"): cached, _large_photo_url("old-b"): cached}
    session = _TwitterSession(
        _timeline_payload([_tweet(_photo_url("old-a")), _cursor_entry("a")]),
        _timeline_payload([_tweet(_photo_url("old-b")), _cursor_entry("b")]),
        _timeline_payload([_tweet(*(_photo_url(name) for name in names))]),
        media=media,
    )
    context.http_session = session
    monkeypatch.setattr(twitter, "_FETCH_LOCK", asyncio.Lock())
    _seed_cached_image(context, cached)

    assert await twitter._fetch_twitter_images(context) == len(names)
    assert len(session.media_urls) == len(names) + 2
    assert twitter._backfill_is_complete(
        context.data_dir / twitter.BACKFILL_STATE_FILENAME,
        twitter._get_user_id(context),
    )


@pytest.mark.asyncio
async def test_fetch_honors_one_page_limit_without_requiring_end_cursor(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context.secrets["plugins"]["twitter"]["max_pages"] = 1
    session                                            = _TwitterSession(
        _timeline_payload([_tweet(_photo_url("a")), _cursor_entry("next")]),
        media={_large_photo_url("a"): _distinct_image(1)},
    )
    context.http_session = session
    monkeypatch.setattr(twitter, "_FETCH_LOCK", asyncio.Lock())

    assert await twitter._fetch_twitter_images(context) == 1
    assert len(session.timeline_requests) == 1
    assert "cursor" not in session.timeline_variables(0)


@pytest.mark.asyncio
async def test_duplicate_fetch_rounds_are_serialized(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    peak   = 0

    async def slow_page() -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1

    context.http_session = _TwitterSession(
        _timeline_payload([]),
        _timeline_payload([]),
        gate=slow_page,
    )
    monkeypatch.setattr(twitter, "_FETCH_LOCK", asyncio.Lock())

    assert await asyncio.gather(
        twitter._fetch_twitter_images(context),
        twitter._fetch_twitter_images(context),
    ) == [0, 0]
    assert peak == 1


@pytest.mark.asyncio
async def test_random_images_are_sent_once_per_round(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_dir = context.data_dir / "images"
    image_dir.mkdir()
    (image_dir / "a.jpg").write_bytes(_image_bytes("JPEG"))
    (image_dir / "b.png").write_bytes(_image_bytes("PNG"))
    (image_dir / "ignored.txt").write_text("not media", encoding="utf-8")
    (image_dir / "unsafe name.jpg").write_bytes(b"unsafe")
    (image_dir / ".hidden.jpg").write_bytes(b"hidden")
    (image_dir / "directory.webp").mkdir()
    monkeypatch.setattr(twitter.random, "choice", lambda values: values[0])

    first = await twitter._get_random_image(context)
    assert first is not None
    await first.delivery_receipt.record(True)
    second = await twitter._get_random_image(context)
    assert second is not None
    await second.delivery_receipt.record(True)
    third = await twitter._get_random_image(context)
    assert third is not None
    await third.delivery_receipt.record(True)

    assert [Path(value[0]["data"]["file"]).name for value in (first, second, third)] == [
        "a.jpg",
        "b.png",
        "a.jpg",
    ]
    assert (context.data_dir / "posted.txt").read_text(encoding="utf-8") == "a.jpg\n"


@pytest.mark.asyncio
async def test_random_image_advances_posted_state_only_after_delivery_ack(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_dir = context.data_dir / "images"
    image_dir.mkdir()
    (image_dir / "retry.jpg").write_bytes(_image_bytes("JPEG"))
    monkeypatch.setattr(twitter.random, "choice", lambda values: values[0])

    first   = await twitter.handle("twimg", "", {}, context)
    receipt = getattr(first, "delivery_receipt", None)

    assert receipt is not None
    assert not (context.data_dir / "posted.txt").exists()

    await receipt.record(False)
    retry         = await twitter.handle("twimg", "", {}, context)
    retry_receipt = getattr(retry, "delivery_receipt", None)

    assert retry_receipt is not None
    assert retry[0]["data"]["file"] == first[0]["data"]["file"]
    assert not (context.data_dir / "posted.txt").exists()

    await retry_receipt.record(True)
    assert (context.data_dir / "posted.txt").read_text(encoding="utf-8") == "retry.jpg\n"


@pytest.mark.asyncio
async def test_pending_random_image_deliveries_reserve_distinct_files(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_dir = context.data_dir / "images"
    image_dir.mkdir()
    (image_dir / "a.jpg").write_bytes(_image_bytes("JPEG"))
    (image_dir / "b.jpg").write_bytes(_image_bytes("JPEG"))
    monkeypatch.setattr(twitter.random, "choice", lambda values: values[0])

    first  = await twitter._get_random_image(context)
    second = await twitter._get_random_image(context)

    assert first is not None and second is not None
    assert first[0]["data"]["file"] != second[0]["data"]["file"]
    assert not (context.data_dir / "posted.txt").exists()

    await first.delivery_receipt.record(False)
    retry = await twitter._get_random_image(context)
    assert retry is not None
    assert retry[0]["data"]["file"] == first[0]["data"]["file"]

    await second.delivery_receipt.record(False)
    await retry.delivery_receipt.record(False)


@pytest.mark.asyncio
async def test_random_image_repairs_oversized_and_stale_posted_state(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_dir = context.data_dir / "images"
    image_dir.mkdir()
    (image_dir / "current.jpg").write_bytes(_image_bytes("JPEG"))
    posted_file = context.data_dir / "posted.txt"
    posted_file.write_bytes(b"x" * (twitter.MAX_POSTED_STATE_BYTES + 1))
    monkeypatch.setattr(twitter.random, "choice", lambda values: values[0])

    selected = await twitter._get_random_image(context)

    assert selected is not None
    assert Path(selected[0]["data"]["file"]).name == "current.jpg"
    assert posted_file.read_bytes() == b"x" * (twitter.MAX_POSTED_STATE_BYTES + 1)
    await selected.delivery_receipt.record(True)
    assert posted_file.read_text(encoding="utf-8") == "current.jpg\n"

    posted_file.write_bytes(b"\xff")
    assert twitter._read_posted_names(posted_file, {"current.jpg"}) == set()
    posted_file.write_text("current.jpg\nstale.png\n", encoding="utf-8")
    assert twitter._read_posted_names(posted_file, {"current.jpg"}) == {"current.jpg"}


@pytest.mark.asyncio
async def test_random_image_retries_when_selected_file_disappears(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_dir = context.data_dir / "images"
    image_dir.mkdir()
    (image_dir / "a.jpg").write_bytes(_image_bytes("JPEG"))
    (image_dir / "b.jpg").write_bytes(_image_bytes("JPEG"))

    def choose_then_lose_it(values: list[str]) -> str:
        # 选中之后、取文件之前，被选中的文件被别处删掉。
        chosen = values[0]
        if chosen == "a.jpg":
            (image_dir / chosen).unlink()
        return chosen

    monkeypatch.setattr(twitter.random, "choice", choose_then_lose_it)

    selected = await twitter._get_random_image(context)
    assert selected is not None
    assert Path(selected[0]["data"]["file"]).name == "b.jpg"
    await selected.delivery_receipt.record(True)
    assert (context.data_dir / "posted.txt").read_text(encoding="utf-8") == "b.jpg\n"


@pytest.mark.asyncio
async def test_random_image_returns_none_when_all_candidates_disappear(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_dir = context.data_dir / "images"
    image_dir.mkdir()
    (image_dir / "gone.jpg").write_bytes(_image_bytes("JPEG"))

    def choose_then_lose_it(values: list[str]) -> str:
        chosen = values[0]
        (image_dir / chosen).unlink(missing_ok=True)
        return chosen

    monkeypatch.setattr(twitter.random, "choice", choose_then_lose_it)

    assert await twitter._get_random_image(context) is None


def test_cache_listing_ignores_entries_that_fail_stat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class VanishingEntry:
        """修剪缓存时还在，随后列目录时已经消失的条目。"""

        name   = "broken.jpg"
        suffix = ".jpg"

        def __init__(self) -> None:
            self.symlink_checks = 0

        def is_symlink(self) -> bool:
            self.symlink_checks += 1
            if self.symlink_checks > 1:
                raise OSError("entry disappeared")
            return False

        def stat(self) -> os.stat_result:
            raise FileNotFoundError("entry disappeared")

    entry = VanishingEntry()
    monkeypatch.setattr(Path, "iterdir", lambda _path: iter([entry]))
    assert twitter._list_cached_image_names(tmp_path) == []
    assert entry.symlink_checks == 2


def test_posted_reader_rejects_file_that_grows_during_read() -> None:
    grown = b"x" * (twitter.MAX_POSTED_STATE_BYTES + 1)

    class GrowingFile:
        def is_symlink(self) -> bool:
            return False

        def is_file(self) -> bool:
            return True

        def stat(self) -> SimpleNamespace:
            return SimpleNamespace(st_size=0)

        def open(self, _mode: str):
            return io.BytesIO(grown)

    assert twitter._read_posted_names(GrowingFile(), {grown.decode()}) == set()


@pytest.mark.asyncio
async def test_random_image_returns_none_for_empty_cache(context: SimpleNamespace) -> None:
    assert await twitter._get_random_image(context) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "args", "marker"),
    [
        ("twimg", "help", "只读取本地缓存"),
        ("twimg", "帮助", "只读取本地缓存"),
        ("tw_fetch", "?", "仅限管理员"),
    ],
)
async def test_command_help_matches_runtime_behavior(
    context: SimpleNamespace,
    command: str,
    args: str,
    marker: str,
) -> None:
    result = await twitter.handle(command, args, {}, context)
    assert marker in result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_manual_fetch_returns_immediately_and_notifies_on_completion(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def hold_timeline_request() -> None:
        started.set()
        await release.wait()

    names   = [str(index) for index in range(5)]
    session = _TwitterSession(
        _timeline_payload([_tweet(*(_photo_url(name) for name in names))]),
        media = {_large_photo_url(name): _distinct_image(int(name)) for name in names},
        gate  = hold_timeline_request,
    )
    context.http_session = session
    monkeypatch.setattr(twitter, "_FETCH_LOCK", asyncio.Lock())
    monkeypatch.setattr(twitter, "_FETCH_TASK", None)
    monkeypatch.setattr(twitter, "_MANUAL_NOTIFICATION_TASK", None)

    result = await twitter.handle("tw_fetch", "", {}, context)

    assert "已开始后台抓取" in result[0]["data"]["text"]
    assert result.command_outcome.state == "accepted"
    await asyncio.wait_for(started.wait(), timeout=1.0)
    notification = twitter._MANUAL_NOTIFICATION_TASK
    assert notification is not None and not notification.done()

    duplicate = await twitter.handle("tw_fetch", "", {}, context)
    assert "正在后台抓取" in duplicate[0]["data"]["text"]
    assert "完成后会通知" in duplicate[0]["data"]["text"]
    assert duplicate.command_outcome.state == "accepted"
    assert len(session.timeline_requests) == 1

    release.set()
    await asyncio.wait_for(notification, timeout=1.0)
    action = context.send_action.await_args.args[0]
    assert action["action"] == "send_group_msg"
    assert action["params"]["group_id"] == 456
    assert "新下载 5 张" in action["params"]["message"][0]["data"]["text"]

    context.current_group_id = None
    context.current_user_id  = None
    context.send_action.reset_mock()
    no_target = await twitter.handle("tw_fetch", "", {}, context)
    assert "无法确定" in no_target[0]["data"]["text"]
    assert len(session.timeline_requests) == 1
    context.send_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_fetch_reports_remote_http_failure(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    context.http_session = _TwitterSession(_json_response({"private": "detail"}, status=403))
    monkeypatch.setattr(twitter, "_FETCH_LOCK", asyncio.Lock())
    monkeypatch.setattr(twitter, "_FETCH_TASK", None)
    monkeypatch.setattr(twitter, "_MANUAL_NOTIFICATION_TASK", None)

    with caplog.at_level(logging.WARNING):
        result = await twitter.handle("tw_fetch", "", {}, context)
        assert "已开始后台抓取" in result[0]["data"]["text"]

        notification = twitter._MANUAL_NOTIFICATION_TASK
        assert notification is not None
        await asyncio.wait_for(notification, timeout=1.0)
    action  = context.send_action.await_args.args[0]
    message = action["params"]["message"][0]["data"]["text"]

    assert "抓取失败" in message
    assert "HTTP 403" in message
    assert "抓取完成" not in message
    # 远端 HTTP 错误是上游故障：记 WARNING，不走 ERROR 级的 public_error。
    assert logged_events(caplog, "fetch.failed")
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_background_fetch_reports_media_transport_failure(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names                = ["a", "b", "c"]
    context.http_session = _TwitterSession(
        _timeline_payload([_tweet(*(_photo_url(name) for name in names))]),
        media={_large_photo_url(name): None for name in names},
    )
    monkeypatch.setattr(twitter, "_FETCH_LOCK", asyncio.Lock())

    outcome = await twitter._run_background_fetch(context)

    assert outcome.succeeded is False
    assert outcome.count == 0
    assert "图片下载全部失败" in outcome.message
    assert "代理" in outcome.message


@pytest.mark.asyncio
async def test_random_and_unknown_commands_have_explicit_routes(
    context: SimpleNamespace,
) -> None:
    image_dir = context.data_dir / "images"
    image_dir.mkdir()
    (image_dir / "image.png").write_bytes(_image_bytes("PNG"))
    result = await twitter.handle("twimg", "", {}, context)
    assert result[0]["type"] == "image"
    assert result[0]["data"]["file"].startswith("file:")

    await result.delivery_receipt.record(False)
    (image_dir / "image.png").unlink()
    result = await twitter.handle("twimg", "", {}, context)
    assert "缓存为空" in result[0]["data"]["text"]
    assert "/tw_fetch" in result[0]["data"]["text"]

    result = await twitter.handle("not-twitter", "", {}, context)
    assert "未知 Twitter 命令" in result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_unexpected_command_error_uses_public_message(
    context: SimpleNamespace,
    tmp_path: Path,
) -> None:
    # 数据目录建在一个普通文件下面，建缓存目录时会抛出带完整路径的 OSError。
    blocker = tmp_path / "private-secret"
    blocker.write_text("not a directory", encoding="utf-8")
    context.data_dir = blocker / "data"

    result = await twitter.handle("twimg", "", {}, context)
    rendered = json.dumps(result, ensure_ascii=False)
    assert "private-secret" not in rendered
    assert "失败" in rendered


@pytest.mark.asyncio
async def test_scheduled_fetch_is_silent_and_contains_failures(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(twitter, "_FETCH_LOCK", asyncio.Lock())
    monkeypatch.setattr(twitter, "_FETCH_TASK", None)
    monkeypatch.setattr(twitter, "_MANUAL_NOTIFICATION_TASK", None)
    session = _TwitterSession(
        _timeline_payload([_tweet(_photo_url("a"), _photo_url("b"))]),
        media={
            _large_photo_url("a"): _distinct_image(1),
            _large_photo_url("b"): _distinct_image(2),
        },
    )
    context.http_session = session
    assert await twitter.scheduled_fetch(context) is None
    first = twitter._FETCH_TASK
    assert first is not None
    assert (await asyncio.wait_for(first, timeout=1.0)).count == 2
    assert len(session.timeline_requests) == 1

    # 时间线没有新图，而本地缓存也是空的。
    for cached in (context.data_dir / "images").iterdir():
        cached.unlink()
    context.http_session = _TwitterSession(_timeline_payload([]))
    assert await twitter.scheduled_fetch(context) is None
    second = twitter._FETCH_TASK
    assert second is not None
    empty_outcome = await asyncio.wait_for(second, timeout=1.0)
    assert empty_outcome.count == 0
    assert empty_outcome.succeeded is False
    assert "缓存仍为空" in empty_outcome.message

    # 数据目录建在普通文件下面：抓取在建缓存目录时抛出带路径的意外错误。
    blocker = tmp_path / "private-scheduled-failure"
    blocker.write_text("not a directory", encoding="utf-8")
    context.data_dir = blocker / "data"
    assert await twitter.scheduled_fetch(context) is None
    third = twitter._FETCH_TASK
    assert third is not None
    outcome = await asyncio.wait_for(third, timeout=1.0)
    assert outcome.succeeded is False
    assert "private-scheduled-failure" not in outcome.message
    context.send_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_cancels_owned_background_fetch(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()

    async def never_finishes() -> None:
        started.set()
        await asyncio.Event().wait()

    context.http_session = _TwitterSession(_timeline_payload([]), gate=never_finishes)
    monkeypatch.setattr(twitter, "_FETCH_LOCK", asyncio.Lock())
    monkeypatch.setattr(twitter, "_FETCH_TASK", None)
    monkeypatch.setattr(twitter, "_MANUAL_NOTIFICATION_TASK", None)
    monkeypatch.setattr(twitter, "_POSTED_RESERVATIONS", {})

    # 随机发图还没拿到送达回执：这张图被预留着。
    image_dir = context.data_dir / "images"
    image_dir.mkdir()
    (image_dir / "image.jpg").write_bytes(_image_bytes("JPEG"))
    pending = await twitter.handle("twimg", "", {}, context)
    assert pending[0]["type"] == "image"
    assert twitter._POSTED_RESERVATIONS

    assert await twitter.scheduled_fetch(context) is None
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task = twitter._FETCH_TASK
    assert task is not None

    await twitter.shutdown(context)

    assert task.cancelled()
    assert twitter._FETCH_TASK is None
    assert twitter._MANUAL_NOTIFICATION_TASK is None
    assert twitter._POSTED_RESERVATIONS == {}


def test_manifest_resolves_schedules_and_admin_commands() -> None:
    manifest = json.loads(
        (ROOT / "plugins" / "twitter" / "plugin.json").read_text(encoding="utf-8")
    )

    assert manifest["concurrency"] == "parallel"
    assert {command["name"] for command in manifest["commands"]} == {"twimg", "tw_fetch"}
    assert (
        next(command for command in manifest["commands"] if command["name"] == "tw_fetch")[
            "admin_only"
        ]
        is True
    )
    assert all("group_ids" not in schedule for schedule in manifest["schedule"])
    assert all(callable(getattr(twitter, schedule["handler"])) for schedule in manifest["schedule"])


@pytest.mark.asyncio
async def test_twitter_error_never_completes_backfill(tmp_path, monkeypatch):
    from core.bounded_http import BoundedHttpResponse
    from plugins.twitter import main

    body     = json.dumps({"errors": [{"message": "temporary"}]}).encode()
    response = BoundedHttpResponse(
        "https://x.com/", 200, body, "application/json", "utf-8", {}, len(body), len(body)
    )
    settings = SimpleNamespace(plugin_secrets=lambda name: {"user_id": "123"})
    context = SimpleNamespace(
        data_dir=tmp_path, http_session=object(), get_settings_snapshot=lambda: settings
    )
    monkeypatch.setattr(main, "aiohttp_request_bounded", AsyncMock(return_value=response))
    with pytest.raises(main.TwitterFetchError):
        await main._fetch_twitter_images(context)
    assert not main._backfill_is_complete(tmp_path / main.BACKFILL_STATE_FILENAME, "123")
