from __future__ import annotations

import asyncio
import base64
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.bounded_http import BoundedHttpResponse
from plugins.apod import main as apod
from plugins.astro_tools import formula, redshift
from plugins.wolframalpha import main as wolframalpha
from tests.helpers.settings_snapshot import with_settings_reader

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


def _response(body: bytes, media_type: str) -> BoundedHttpResponse:
    return BoundedHttpResponse(
        url           = "https://api.wolframalpha.com/test",
        status        = 200,
        body          = body,
        media_type    = media_type,
        charset       = "utf-8",
        headers       = {},
        wire_bytes    = len(body),
        decoded_bytes = len(body),
    )


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "1e9999"])
@pytest.mark.parametrize("calculation", ["schwarzschild", "luminosity", "lifetime"])
def test_mass_calculations_reject_non_positive_or_non_finite_values(
    calculation: str,
    value: str,
):
    context = SimpleNamespace()

    result = formula._handle_calculation(f"{calculation} {value}", context)

    assert result == "质量必须是有限的正数"


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "1e9999"])
@pytest.mark.asyncio
async def test_redshift_rejects_every_non_finite_value(value: str):
    context = SimpleNamespace()

    result = redshift.handle_redshift(value, context)

    assert result == "红移值必须是有限数字"


@pytest.mark.parametrize("question", ["show the next step", "copy cp"])
@pytest.mark.asyncio
async def test_natural_step_and_cp_suffixes_remain_part_of_simple_question(
    question: str,
    monkeypatch: pytest.MonkeyPatch,
):
    captured: list[str] = []

    async def request(_session, _method, _url, **kwargs):
        captured.append(kwargs["request_kwargs"]["params"]["i"])
        return _response(b"plain answer", "text/plain")

    monkeypatch.setattr(wolframalpha, "aiohttp_request_bounded", request)
    context = with_settings_reader(
        SimpleNamespace(
            secrets      = {"plugins": {"wolframalpha": {"appid": "appid"}}},
            http_session = object(),
        )
    )

    result = await wolframalpha.handle("alpha", question, {}, context)

    assert captured == [question]
    assert "plain answer" in str(result)
    assert "步骤解答" not in str(result)


@pytest.mark.asyncio
async def test_all_wolfram_modes_share_two_request_concurrency_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    active = 0
    peak   = 0

    async def request(_session, _method, _url, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        mime_policy = kwargs["mime_policy"]
        if mime_policy is wolframalpha.XML_MIME_POLICY:
            return _response(
                b"<queryresult><pod><plaintext>step</plaintext></pod></queryresult>",
                "application/xml",
            )
        if mime_policy is wolframalpha.JSON_MIME_POLICY:
            return _response(
                b'{"queryresult":{"pods":[{"subpods":[{"plaintext":"complete"}]}]}}',
                "application/json",
            )
        return _response(b"simple", "text/plain")

    monkeypatch.setattr(wolframalpha, "aiohttp_request_bounded", request)
    context = SimpleNamespace(http_session=object())
    tasks = []
    for index in range(4):
        tasks.extend(
            (
                wolframalpha._get_answer(f"simple-{index}", "appid", context),
                wolframalpha._query_step(f"step-{index}", "appid", context.http_session),
                wolframalpha._query_complete(
                    f"complete-{index}",
                    "appid",
                    context.http_session,
                ),
            )
        )

    await asyncio.gather(*tasks)

    assert peak == 2
    assert active == 0


@pytest.mark.asyncio
async def test_apod_image_fetch_uses_only_pinned_bounded_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, object] = {}

    async def fetch(url: str, **kwargs):
        captured.update(url=url, **kwargs)
        return SimpleNamespace(
            url     = url,
            body    = PNG_BYTES,
            headers = {"Content-Type": "image/png"},
        )

    monkeypatch.setattr(apod, "fetch_public_bytes", fetch)
    context = with_settings_reader(SimpleNamespace(config={"plugins": {"apod": {}}}))
    target = tmp_path / apod._cache_filename(
        "https://apod.nasa.gov/apod/image/test.png",
        ".png",
    )
    target.write_bytes(b"x" * len(PNG_BYTES))

    path = await apod._safe_download_image(
        "https://apod.nasa.gov/apod/image/test.png",
        tmp_path,
        context,
    )

    assert path is not None
    assert path.read_bytes() == PNG_BYTES
    assert captured["timeout_seconds"] == apod.IMAGE_TIMEOUT_SECONDS
    assert captured["max_bytes"] == apod.MAX_IMAGE_BYTES
    assert captured["allow_transparent_proxy_fake_dns"] is True
    assert "proxy" not in captured


@pytest.mark.asyncio
async def test_apod_image_cache_prunes_old_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    async def fetch(url: str, **_kwargs):
        return SimpleNamespace(
            url     = url,
            body    = PNG_BYTES,
            headers = {"Content-Type": "image/png"},
        )

    monkeypatch.setattr(apod, "fetch_public_bytes", fetch)
    context = with_settings_reader(SimpleNamespace(config={"plugins": {"apod": {}}}))

    downloaded = []
    started    = time.time() - 1000
    for index in range(apod.IMAGE_CACHE_LIMITS.max_entries + 1):
        path = await apod._safe_download_image(
            f"https://apod.nasa.gov/apod/image/image-{index}.png",
            tmp_path,
            context,
        )
        assert path is not None
        # 下载之间的文件时间可能相同；明确排好先后，淘汰顺序才确定。
        os.utime(path, (started + index, started + index))
        downloaded.append(path)

    assert not downloaded[0].exists()
    assert downloaded[-1].exists()
    assert (
        len([path for path in tmp_path.iterdir() if not path.name.startswith(".")])
        == apod.IMAGE_CACHE_LIMITS.max_entries
    )
