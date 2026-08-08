"""CR-278 regressions for Twitter structure/cache and Voice lock/config drift."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from core.bounded_file_cache import FileCacheLimits
from core.safe_http import SafeHttpResponse
from plugins.twitter import main as twitter
from plugins.voice import main as voice
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.settings_snapshot import with_settings_reader

ROOT = REPOSITORY_ROOT


def _png(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(output, format="PNG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_twitter_graphql_params_are_real_json_with_exact_strings(monkeypatch):
    captured = {}

    async def fake_request(*_args, **kwargs):
        captured.update(kwargs["request_kwargs"])
        return object()

    monkeypatch.setattr(twitter, "aiohttp_request_bounded", fake_request)
    monkeypatch.setattr(twitter, "parse_bounded_json", lambda *_args, **_kwargs: {"data": {}})
    context = with_settings_reader(
        SimpleNamespace(
            http_session=object(),
            secrets={
                "plugins": {
                    "twitter": {
                        "user_id": 'id"\\Unicode-用户',
                        "headers": {},
                        "cookies": {},
                    }
                }
            },
        )
    )

    assert await twitter._fetch_timeline(context, cursor='cursor"\\value') == (
        [],
        None,
        False,
    )

    variables = json.loads(captured["params"]["variables"])
    features = json.loads(captured["params"]["features"])
    toggles = json.loads(captured["params"]["fieldToggles"])
    assert variables["userId"] == 'id"\\Unicode-用户'
    assert variables["cursor"] == 'cursor"\\value'
    assert variables["includePromotedContent"] is False
    assert features["articles_preview_enabled"] is True
    assert toggles == {"withArticlePlainText": False}


def test_twitter_media_extraction_skips_malformed_items_individually():
    tweet = {
        "content": {
            "itemContent": {
                "tweet_results": {
                    "result": {
                        "legacy": {
                            "extended_entities": {
                                "media": [
                                    None,
                                    {"type": "photo"},
                                    {"type": "photo", "media_url_https": None},
                                    {
                                        "type": "photo",
                                        "media_url_https": " https://pbs.twimg.com/ok.png ",
                                    },
                                    {"type": "video"},
                                ]
                            }
                        }
                    }
                }
            }
        }
    }

    assert twitter._extract_image_urls(tweet) == ["https://pbs.twimg.com/ok.png"]
    assert twitter._extract_image_urls({"content": []}) == []


@pytest.mark.asyncio
async def test_twitter_downloads_are_globally_bounded_and_cache_commit_is_atomic(
    monkeypatch, tmp_path
):
    payloads = [_png((index, 2, 3)) for index in range(1, 7)]
    active = 0
    peak = 0

    async def fake_fetch(url: str, **_kwargs):
        nonlocal active, peak
        index = int(url.rsplit("img", 1)[1].split(".", 1)[0])
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return SafeHttpResponse(
            url=url,
            status=200,
            body=payloads[index],
            charset=None,
            headers={"Content-Type": "image/png"},
        )

    tweets = [
        {
            "content": {
                "itemContent": {
                    "tweet_results": {
                        "result": {
                            "legacy": {
                                "extended_entities": {
                                    "media": [
                                        {
                                            "type": "photo",
                                            "media_url_https": (
                                                f"https://pbs.twimg.com/media/img{index}.png"
                                            ),
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
            }
        }
        for index in range(len(payloads))
    ]
    monkeypatch.setattr(twitter, "fetch_public_bytes", fake_fetch)
    monkeypatch.setattr(twitter, "_fetch_timeline", AsyncMock(return_value=(tweets, None, False)))
    monkeypatch.setattr(twitter, "_FETCH_LOCK", asyncio.Lock())
    monkeypatch.setattr(twitter, "MAX_CONCURRENT_IMAGE_DOWNLOADS", 2)
    monkeypatch.setattr(
        twitter,
        "IMAGE_CACHE_LIMITS",
        FileCacheLimits(
            max_entries=2,
            max_bytes=2 * max(map(len, payloads)),
            ttl_seconds=60,
        ),
    )
    context = with_settings_reader(
        SimpleNamespace(
            data_dir=tmp_path,
            logger=MagicMock(),
            secrets={"plugins": {"twitter": {"max_pages": 1}}},
        )
    )

    assert await twitter._fetch_twitter_images(context) == len(payloads)
    files = list((tmp_path / "images").glob("*.png"))
    assert peak <= 2
    assert len(files) == 2
    assert sum(path.stat().st_size for path in files) <= twitter.IMAGE_CACHE_LIMITS.max_bytes


def _voice_context(tmp_path: Path) -> SimpleNamespace:
    return with_settings_reader(
        SimpleNamespace(
            data_dir=tmp_path / "voice-data",
            http_session=object(),
            logger=MagicMock(),
            secrets={
                "plugins": {
                    "voice": {
                        "subscription_key": "secret",
                        "region": "southeastasia",
                        "voice_name": "zh-CN-XiaomoNeural",
                        "style": "cheerful",
                        "role": "Girl",
                    }
                }
            },
        )
    )


@pytest.mark.asyncio
async def test_voice_same_key_is_singleflight_and_lock_registry_returns_to_zero(
    monkeypatch, tmp_path
):
    calls = 0

    async def fake_request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return SimpleNamespace(body=b"ID3valid-audio")

    monkeypatch.setattr(voice, "aiohttp_request_bounded", fake_request)
    context = _voice_context(tmp_path)

    results = await asyncio.gather(*(voice.text_to_speech("same text", context) for _ in range(20)))

    assert calls == 1
    assert len(set(results)) == 1
    assert results[0] is not None
    assert voice._TTS_LOCKS.active_key_count == 0


@pytest.mark.asyncio
async def test_voice_many_unique_completed_keys_do_not_accumulate(monkeypatch, tmp_path):
    async def fake_request(*_args, **_kwargs):
        return SimpleNamespace(body=b"ID3valid-audio")

    monkeypatch.setattr(voice, "aiohttp_request_bounded", fake_request)
    context = _voice_context(tmp_path)

    results = await asyncio.gather(
        *(voice.text_to_speech(f"text-{index}", context) for index in range(100))
    )

    assert all(result is not None for result in results)
    assert voice._TTS_LOCKS.active_key_count == 0


def test_root_voice_and_twitter_examples_match_runtime_schema():
    document = json.loads((ROOT / "config" / "secrets.json.example").read_text(encoding="utf-8"))
    voice_config = document["plugins"]["voice"]
    twitter_config = document["plugins"]["twitter"]

    assert set(voice_config) == {
        "subscription_key",
        "region",
        "voice_name",
        "style",
        "role",
        "proxy",
    }
    assert not {"appid", "api_secret", "api_key"} & set(voice_config)
    assert {"user_id", "headers", "cookies", "proxy", "max_pages"} <= set(twitter_config)
    assert "nitter_host" not in twitter_config
