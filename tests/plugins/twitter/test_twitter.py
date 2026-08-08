"""Twitter 图片插件的配置、传输、缓存与命令契约测试。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.bounded_file_cache import BoundedFileCache, FileCacheLimits
from core.safe_http import SafeHttpResponse
from plugins.twitter import main as twitter
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.payloads import image_bytes as _image_bytes
from tests.helpers.settings_snapshot import with_settings_reader

ROOT = REPOSITORY_ROOT


class _AsyncContent:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def iter_chunked(self, size: int):
        for offset in range(0, len(self.body), max(1, size)):
            yield self.body[offset : offset + size]


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json",
    ) -> None:
        self.status = status
        self.url = twitter._TIMELINE_URL
        self.headers = {"Content-Type": content_type}
        self.content_length = None
        self.content = _AsyncContent(body)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self, *responses: _Response) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class _RaisingSession:
    def request(self, *_args, **_kwargs):
        raise RuntimeError("network detail must stay private")


def _json_response(payload: object, *, status: int = 200) -> _Response:
    return _Response(json.dumps(payload).encode("utf-8"), status=status)


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
            data_dir=tmp_path,
            secrets={
                "plugins": {
                    "twitter": {
                        "user_id": "123456789",
                        "headers": {"authorization": "Bearer test-token"},
                        "cookies": {"ct0": "csrf-token"},
                        "proxy": "http://proxy.example.com:8080",
                        "max_pages": 50,
                    }
                }
            },
            http_session=None,
            current_user_id=123,
            current_group_id=456,
            send_action=AsyncMock(),
            logger=MagicMock(),
        )
    )


@pytest.fixture
def install_media_fetch(monkeypatch: pytest.MonkeyPatch):
    def install(
        payload: bytes | None = None,
        *,
        error: Exception | None = None,
    ) -> AsyncMock:
        response = None
        if payload is not None:
            response = SafeHttpResponse(
                url="https://pbs.twimg.com/media/final",
                status=200,
                body=payload,
                charset=None,
                headers={"Content-Type": "image/png"},
            )
        fetch = AsyncMock(return_value=response, side_effect=error)
        monkeypatch.setattr(twitter, "fetch_public_bytes", fetch)
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
        "user-agent": "Custom Agent",
        "Host": "attacker.example",
        "Bad Header": "value",
        "X-Number": 7,
        "X-Newline": "first\r\nsecond",
        "X-Empty": "  ",
    }

    headers = twitter._get_headers(context)

    folded = {key.casefold(): value for key, value in headers.items()}
    assert folded == {
        "accept": "application/json",
        "user-agent": "Custom Agent",
        "authorization": "Bearer dummy-secret",
    }


def test_non_mapping_headers_do_not_add_credentials(context: SimpleNamespace) -> None:
    context.secrets["plugins"]["twitter"]["headers"] = "Bearer hidden"
    headers = twitter._get_headers(context)
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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" exact-id ", "exact-id"),
        ('id"\\Unicode-用户', 'id"\\Unicode-用户'),
        (123456, "123456"),
        (0, twitter.DEFAULT_USER_ID),
        (True, twitter.DEFAULT_USER_ID),
        (None, twitter.DEFAULT_USER_ID),
        ("", twitter.DEFAULT_USER_ID),
        ("bad\nvalue", twitter.DEFAULT_USER_ID),
        ("x" * (twitter.MAX_USER_ID_CHARS + 1), twitter.DEFAULT_USER_ID),
    ],
)
def test_user_id_normalization(
    context: SimpleNamespace,
    value: object,
    expected: str,
) -> None:
    context.secrets["plugins"]["twitter"]["user_id"] = value
    assert twitter._get_user_id(context) == expected


def test_cookies_keep_only_bounded_string_pairs(context: SimpleNamespace) -> None:
    context.secrets["plugins"]["twitter"]["cookies"] = {
        "ct0": " csrf ",
        "nested": {"secret": "value"},
        "empty": "",
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
    session = _Session(_json_response(_timeline_payload(entries)))
    context.http_session = session

    tweets, cursor, has_next = await twitter._fetch_timeline(context, 'cursor"\\value')

    assert tweets == [entries[1]]
    assert (cursor, has_next) == ("next-token", True)
    method, url, kwargs = session.calls[0]
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
async def test_timeline_ignores_invalid_cursor_and_malformed_instructions(
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
    session = _Session(_json_response(payload))
    context.http_session = session

    assert await twitter._fetch_timeline(context, "bad\ncursor") == ([], None, False)
    variables = json.loads(session.calls[0][2]["params"]["variables"])
    assert "cursor" not in variables


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "is_format_error"),
    [
        ([], True),
        ({"data": []}, False),
        (
            {"data": {"user": {"result": {"timeline": {"timeline": {"instructions": {}}}}}}},
            False,
        ),
    ],
)
async def test_timeline_malformed_shapes_fail_closed(
    context: SimpleNamespace,
    payload: object,
    is_format_error: bool,
) -> None:
    context.http_session = _Session(_json_response(payload))
    if is_format_error:
        with pytest.raises(twitter.TwitterFetchError, match="request failed"):
            await twitter._fetch_timeline(context)
    else:
        assert await twitter._fetch_timeline(context) == ([], None, False)


@pytest.mark.asyncio
async def test_timeline_http_and_transport_errors_are_reported(context: SimpleNamespace) -> None:
    context.http_session = _Session(_json_response({"private": "detail"}, status=429))
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


def test_image_validation_rejects_invalid_unsupported_and_oversized_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="valid supported image"):
        twitter._detect_image_extension(b"not an image")
    with pytest.raises(ValueError, match="unsupported"):
        twitter._detect_image_extension(_image_bytes("GIF"))

    monkeypatch.setattr(twitter, "MAX_IMAGE_PIXELS", 3)
    with pytest.raises(ValueError, match="dimensions"):
        twitter._detect_image_extension(_image_bytes())


@pytest.mark.asyncio
async def test_download_validates_and_commits_by_content_hash(
    context: SimpleNamespace,
    tmp_path: Path,
    install_media_fetch,
) -> None:
    payload = _image_bytes("PNG")
    fetch = install_media_fetch(payload)
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
    options = fetch.await_args.kwargs
    assert options["allowed_hosts"] == twitter._ALLOWED_TWITTER_MEDIA_HOSTS
    assert options["allowed_schemes"] == ("https",)
    assert options["max_bytes"] == twitter.MAX_IMAGE_BYTES
    media_headers = {key.casefold(): value for key, value in options["headers"].items()}
    assert "authorization" not in media_headers
    assert "cookie" not in media_headers
    assert media_headers["accept-encoding"] == "identity"

    install_media_fetch(payload)
    assert not await twitter._download_image(
        "https://pbs.twimg.com/media/another.png",
        save_dir,
        context,
    )
    assert [path.name for path in save_dir.glob("*.png")] == [expected.name]


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
    url = "https://pbs.twimg.com/media/a.png"

    install_media_fetch(None)
    assert not await twitter._download_image(url, save_dir, context)
    install_media_fetch(error=RuntimeError("private download failure"))
    assert not await twitter._download_image(url, save_dir, context)
    install_media_fetch(b"not an image")
    assert not await twitter._download_image(url, save_dir, context)
    install_media_fetch(b"")
    assert not await twitter._download_image(url, save_dir, context)

    payload = _image_bytes()
    install_media_fetch(payload)
    monkeypatch.setattr(twitter, "MAX_IMAGE_BYTES", len(payload) - 1)
    assert not await twitter._download_image(url, save_dir, context)
    monkeypatch.setattr(twitter, "MAX_IMAGE_BYTES", 10 * 1024 * 1024)
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
    first = "https://pbs.twimg.com/media/a.jpg"
    second = "https://pbs.twimg.com/media/b.jpg"
    pages = {
        None: ([_tweet(first, first)], "cursor-1", True),
        "cursor-1": ([_tweet(first, second)], None, False),
    }

    async def fetch_page(_context, cursor=None):
        return pages[cursor]

    download = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(twitter, "_fetch_timeline", fetch_page)
    monkeypatch.setattr(twitter, "_download_image", download)
    monkeypatch.setattr(twitter, "_FETCH_LOCK", asyncio.Lock())

    assert await twitter._fetch_twitter_images(context) == 1
    assert [call.args[0] for call in download.await_args_list] == [first, second]


@pytest.mark.asyncio
async def test_fetch_stops_after_two_pages_without_new_images(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str | None] = []

    async def fetch_page(_context, cursor=None):
        calls.append(cursor)
        index = len(calls)
        return ([_tweet(f"https://pbs.twimg.com/media/{index}.jpg")], f"c{index}", True)

    monkeypatch.setattr(twitter, "_fetch_timeline", fetch_page)
    monkeypatch.setattr(twitter, "_download_image", AsyncMock(return_value=False))
    monkeypatch.setattr(twitter, "_FETCH_LOCK", asyncio.Lock())

    assert await twitter._fetch_twitter_images(context) == 0
    assert calls == [None, "c1"]


@pytest.mark.asyncio
async def test_fetch_stops_on_empty_timeline_and_repeated_cursor(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline = AsyncMock(return_value=([], None, False))
    monkeypatch.setattr(twitter, "_fetch_timeline", timeline)
    monkeypatch.setattr(twitter, "_FETCH_LOCK", asyncio.Lock())
    assert await twitter._fetch_twitter_images(context) == 0

    timeline.side_effect = [
        ([_tweet("https://pbs.twimg.com/media/a.jpg")], "same", True),
        ([_tweet("https://pbs.twimg.com/media/b.jpg")], "same", True),
    ]
    monkeypatch.setattr(twitter, "_download_image", AsyncMock(return_value=True))
    assert await twitter._fetch_twitter_images(context) == 2
    assert timeline.await_count == 3


@pytest.mark.asyncio
async def test_fetch_limits_each_run_to_one_hundred_images(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls = [f"https://pbs.twimg.com/media/{index}.jpg" for index in range(101)]
    monkeypatch.setattr(
        twitter,
        "_fetch_timeline",
        AsyncMock(return_value=([_tweet(*urls)], "unused", True)),
    )
    download = AsyncMock(return_value=True)
    monkeypatch.setattr(twitter, "_download_image", download)
    monkeypatch.setattr(twitter, "_FETCH_LOCK", asyncio.Lock())

    assert await twitter._fetch_twitter_images(context) == twitter.MAX_IMAGES_PER_FETCH
    assert download.await_count == twitter.MAX_IMAGES_PER_FETCH


@pytest.mark.asyncio
async def test_fetch_honors_one_page_limit_without_requiring_end_cursor(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context.secrets["plugins"]["twitter"]["max_pages"] = 1
    timeline = AsyncMock(return_value=([_tweet("https://pbs.twimg.com/media/a.jpg")], "next", True))
    monkeypatch.setattr(twitter, "_fetch_timeline", timeline)
    monkeypatch.setattr(twitter, "_download_image", AsyncMock(return_value=True))
    monkeypatch.setattr(twitter, "_FETCH_LOCK", asyncio.Lock())

    assert await twitter._fetch_twitter_images(context) == 1
    timeline.assert_awaited_once_with(context, None)


@pytest.mark.asyncio
async def test_duplicate_fetch_rounds_are_serialized(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    peak = 0

    async def slow_empty_page(_context, _cursor=None):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return [], None, False

    monkeypatch.setattr(twitter, "_fetch_timeline", slow_empty_page)
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

    first = await twitter.handle("twimg", "", {}, context)
    receipt = getattr(first, "delivery_receipt", None)

    assert receipt is not None
    assert not (context.data_dir / "posted.txt").exists()

    await receipt.record(False)
    retry = await twitter.handle("twimg", "", {}, context)
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

    first = await twitter._get_random_image(context)
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
    original_get_any = BoundedFileCache.get_any

    def flaky_get_any(cache: BoundedFileCache, names: tuple[str, ...]):
        if names == ("a.jpg",):
            (image_dir / "a.jpg").unlink(missing_ok=True)
            return None
        return original_get_any(cache, names)

    monkeypatch.setattr(BoundedFileCache, "get_any", flaky_get_any)
    monkeypatch.setattr(twitter.random, "choice", lambda values: values[0])

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
    (image_dir / "gone.jpg").write_bytes(b"gone")
    monkeypatch.setattr(BoundedFileCache, "get_any", lambda *_args: None)

    assert await twitter._get_random_image(context) is None


def test_cache_listing_ignores_entries_that_fail_stat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenEntry:
        name = "broken.jpg"
        suffix = ".jpg"

        def is_symlink(self) -> bool:
            raise OSError("entry disappeared")

    monkeypatch.setattr(BoundedFileCache, "prune", lambda _cache: None)
    monkeypatch.setattr(Path, "iterdir", lambda _path: iter([BrokenEntry()]))
    assert twitter._list_cached_image_names(tmp_path) == []


def test_posted_reader_rejects_file_that_grows_during_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class GrowingFile:
        def is_symlink(self) -> bool:
            return False

        def is_file(self) -> bool:
            return True

        def stat(self) -> SimpleNamespace:
            return SimpleNamespace(st_size=0)

        def open(self, _mode: str):
            return io.BytesIO(b"xx")

    monkeypatch.setattr(twitter, "MAX_POSTED_STATE_BYTES", 1)
    assert twitter._read_posted_names(GrowingFile(), {"xx"}) == set()


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
    calls = 0

    async def fetch(_context):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return 5

    monkeypatch.setattr(twitter, "_fetch_twitter_images", fetch)
    monkeypatch.setattr(twitter, "_FETCH_TASK", None)
    monkeypatch.setattr(twitter, "_MANUAL_NOTIFICATION_TASK", None)

    result = await twitter.handle("tw_fetch", "", {}, context)

    assert "已开始后台抓取" in result[0]["data"]["text"]
    await asyncio.wait_for(started.wait(), timeout=1.0)
    notification = twitter._MANUAL_NOTIFICATION_TASK
    assert notification is not None and not notification.done()

    duplicate = await twitter.handle("tw_fetch", "", {}, context)
    assert "正在后台抓取" in duplicate[0]["data"]["text"]
    assert "/twimg" in duplicate[0]["data"]["text"]
    assert calls == 1

    release.set()
    await asyncio.wait_for(notification, timeout=1.0)
    action = context.send_action.await_args.args[0]
    assert action["action"] == "send_group_msg"
    assert action["params"]["group_id"] == 456
    assert "新下载 5 张" in action["params"]["message"][0]["data"]["text"]

    context.current_group_id = None
    context.current_user_id = None
    context.send_action.reset_mock()
    no_target = await twitter.handle("tw_fetch", "", {}, context)
    assert "无法确定" in no_target[0]["data"]["text"]
    assert calls == 1
    context.send_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_fetch_reports_remote_http_failure(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch = AsyncMock(side_effect=twitter.TwitterFetchError(status=403))
    monkeypatch.setattr(twitter, "_fetch_twitter_images", fetch)
    monkeypatch.setattr(twitter, "_FETCH_TASK", None)
    monkeypatch.setattr(twitter, "_MANUAL_NOTIFICATION_TASK", None)

    result = await twitter.handle("tw_fetch", "", {}, context)
    assert "已开始后台抓取" in result[0]["data"]["text"]

    notification = twitter._MANUAL_NOTIFICATION_TASK
    assert notification is not None
    await asyncio.wait_for(notification, timeout=1.0)
    action = context.send_action.await_args.args[0]
    message = action["params"]["message"][0]["data"]["text"]

    assert "抓取失败" in message
    assert "HTTP 403" in message
    assert "抓取完成" not in message


@pytest.mark.asyncio
async def test_random_and_unknown_commands_have_explicit_routes(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"image")
    monkeypatch.setattr(
        twitter,
        "_get_random_image",
        AsyncMock(return_value=[twitter.image(str(image_path))]),
    )
    result = await twitter.handle("twimg", "", {}, context)
    assert result[0]["type"] == "image"
    assert result[0]["data"]["file"].startswith("file:")

    monkeypatch.setattr(twitter, "_get_random_image", AsyncMock(return_value=None))
    result = await twitter.handle("twimg", "", {}, context)
    assert "无法获取" in result[0]["data"]["text"]

    result = await twitter.handle("not-twitter", "", {}, context)
    assert "未知 Twitter 命令" in result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_unexpected_command_error_uses_public_message(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(twitter, "parse", MagicMock(side_effect=RuntimeError("private-secret")))
    result = await twitter.handle("twimg", "", {}, context)
    rendered = json.dumps(result, ensure_ascii=False)
    assert "private-secret" not in rendered
    assert "失败" in rendered


@pytest.mark.asyncio
async def test_scheduled_fetch_is_silent_and_contains_failures(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch = AsyncMock(return_value=2)
    monkeypatch.setattr(twitter, "_fetch_twitter_images", fetch)
    monkeypatch.setattr(twitter, "_FETCH_TASK", None)
    monkeypatch.setattr(twitter, "_MANUAL_NOTIFICATION_TASK", None)
    assert await twitter.scheduled_fetch(context) == []
    first = twitter._FETCH_TASK
    assert first is not None
    assert (await asyncio.wait_for(first, timeout=1.0)).count == 2
    fetch.assert_awaited_once_with(context)

    fetch.return_value = 0
    assert await twitter.scheduled_fetch(context) == []
    second = twitter._FETCH_TASK
    assert second is not None
    assert (await asyncio.wait_for(second, timeout=1.0)).count == 0

    fetch.side_effect = RuntimeError("private scheduled failure")
    assert await twitter.scheduled_fetch(context) == []
    third = twitter._FETCH_TASK
    assert third is not None
    outcome = await asyncio.wait_for(third, timeout=1.0)
    assert outcome.succeeded is False
    assert "private scheduled failure" not in outcome.message
    context.send_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_cancels_owned_background_fetch(
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()

    async def never_finishes(_context):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(twitter, "_fetch_twitter_images", never_finishes)
    monkeypatch.setattr(twitter, "_FETCH_TASK", None)
    monkeypatch.setattr(twitter, "_MANUAL_NOTIFICATION_TASK", None)
    monkeypatch.setattr(twitter, "_POSTED_RESERVATIONS", {"pending": {"image.jpg"}})

    assert await twitter.scheduled_fetch(context) == []
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task = twitter._FETCH_TASK
    assert task is not None

    await twitter.shutdown(context)

    assert task.cancelled()
    assert twitter._FETCH_TASK is None
    assert twitter._MANUAL_NOTIFICATION_TASK is None
    assert twitter._POSTED_RESERVATIONS == {}


def test_manifest_and_docs_describe_the_same_bounded_behavior() -> None:
    manifest = json.loads(
        (ROOT / "plugins" / "twitter" / "plugin.json").read_text(encoding="utf-8")
    )
    readme = (ROOT / "plugins" / "twitter" / "README.md").read_text(encoding="utf-8")

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
    assert (
        "后台"
        in next(command for command in manifest["commands"] if command["name"] == "tw_fetch")[
            "help"
        ]
    )
    assert "提交后台抓取，并在完成后私聊通知结果" in readme
    for command in manifest["commands"]:
        for trigger in command["triggers"]:
            assert f"/{trigger}" in readme
    for marker in ("5000", "512 MiB", "90", "10 MiB", "4000 万", "1 MiB", "03:00"):
        assert marker in readme
