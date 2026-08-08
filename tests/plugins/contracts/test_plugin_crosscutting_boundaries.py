"""跨插件共享的租户、资源、并发与日志安全边界。"""

from __future__ import annotations

import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from core.interfaces import PluginCapabilities
from core.safe_http import SafeHttpResponse
from plugins.adnmb import adapi
from plugins.apod import main as apod
from plugins.astro_tools import const as astro_const
from plugins.astro_tools import obj as astro_obj
from plugins.color import data_manager as color_data
from plugins.github import main as github
from plugins.smalltalk import main as smalltalk
from tests.helpers.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT


class _ScopedContext:
    def __init__(self, data_dir: Path, *, user_id: int = 1, group_id: int | None = None):
        self.data_dir = data_dir
        self.plugin_dir = ROOT / "plugins" / "smalltalk"
        self.current_user_id = user_id
        self.current_group_id = group_id
        self.config = {"plugins": {"smalltalk": {"voice_probability": 0}}}
        self.logger = MagicMock()
        self.chat_reply = MagicMock()
        self.chat_reply.reply = AsyncMock(
            return_value=smalltalk.segments("provider response"),
        )
        self.capabilities = PluginCapabilities(chat_reply=self.chat_reply)


@pytest.mark.asyncio
async def test_smalltalk_calls_loaded_plugin_provider_with_actor(tmp_path: Path) -> None:
    context = _ScopedContext(tmp_path, user_id=42, group_id=84)

    result = await smalltalk._call_chat_api("private prompt", context)

    assert result == smalltalk.segments("provider response")
    context.chat_reply.reply.assert_awaited_once_with(
        "private prompt",
        {"user_id": 42, "group_id": 84},
    )


@pytest.mark.asyncio
async def test_smalltalk_qa_is_scoped_bounded_and_audited(tmp_path: Path) -> None:
    first = _ScopedContext(tmp_path, user_id=10, group_id=100)
    second = _ScopedContext(tmp_path, user_id=20, group_id=200)

    assert "成功" in str(await smalltalk._add_qa(first, "问 回答一"))
    assert await smalltalk.get_qa_answer(first, "问") == "回答一"
    assert await smalltalk.get_qa_answer(second, "问") is None
    assert "不能超过" in str(
        await smalltalk._add_qa(
            first,
            f"{'问' * (smalltalk.MAX_QUESTION_LENGTH + 1)} 回答",
        )
    )

    audit = json.loads((tmp_path / "QA_audit.json").read_text(encoding="utf-8"))
    assert audit["entries"][-1]["owner"] == 10
    assert audit["entries"][-1]["scope"] == "group_100"


def test_qa_commands_are_admin_only_and_schedules_have_no_embedded_groups() -> None:
    smalltalk_manifest = json.loads(
        (ROOT / "plugins" / "smalltalk" / "plugin.json").read_text(encoding="utf-8")
    )
    assert all(command["admin_only"] for command in smalltalk_manifest["commands"])

    for plugin_name in ("apod", "earthquake", "github"):
        manifest = json.loads(
            (ROOT / "plugins" / plugin_name / "plugin.json").read_text(encoding="utf-8")
        )
        assert all("group_ids" not in schedule for schedule in manifest["schedule"])


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), (20, 40, 60)).save(output, format="PNG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_adnmb_image_download_is_bounded_validated_and_hashed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _png_bytes()
    fetch = AsyncMock(
        return_value=SafeHttpResponse(
            url="https://image.nmb.best/image/a.png",
            status=200,
            body=body,
            charset=None,
            headers={"Content-Type": "image/png"},
        )
    )
    monkeypatch.setattr(adapi, "fetch_public_bytes", fetch)
    client = adapi.AdnmbClient(MagicMock(), tmp_path)

    result = await client.download_image("folder/a.png")

    assert result is not None
    assert result.name != "a.png"
    assert result.read_bytes() == body
    assert fetch.await_args.kwargs["max_bytes"] == adapi.MAX_IMAGE_BYTES


def test_apod_cache_key_uses_full_url_and_trusted_mime() -> None:
    one = apod._cache_filename("https://apod.nasa.gov/a/shared", ".png")
    two = apod._cache_filename("https://apod.nasa.gov/b/shared", ".png")
    assert one != two
    assert one.endswith(".png")
    assert all(char not in one for char in '<>:"/\\|?*')


@pytest.mark.asyncio
async def test_lightyear_constant_has_quantity_semantics() -> None:
    result = await astro_const.handle_const("ly", MagicMock())
    assert "1.000000e+00" in result
    assert "lyr" in result


@pytest.mark.asyncio
async def test_simbad_total_deadline_returns_without_global_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Warm the optional scientific imports; the assertion below measures the
    # network deadline rather than first-import cost.
    from astropy import units as _units  # noqa: F401
    from astropy.coordinates import SkyCoord as _SkyCoord  # noqa: F401

    def slow_query(_name: str):
        time.sleep(0.05)
        return None

    monkeypatch.setattr(astro_obj, "_query_simbad_object", slow_query)
    monkeypatch.setattr(astro_obj, "SIMBAD_TOTAL_TIMEOUT_SECONDS", 0.005)
    started = time.monotonic()
    result = await astro_obj.handle_obj("M31", MagicMock())
    assert "超时" in result
    assert time.monotonic() - started < 0.04


def test_custom_colors_are_scoped_and_atomic(tmp_path: Path) -> None:
    first = _ScopedContext(tmp_path, group_id=101)
    second = _ScopedContext(tmp_path, group_id=202)
    first_color = {
        "name": "甲",
        "pinyin": "",
        "RGB": [1, 2, 3],
        "hex": "#010203",
        "CMYK": [67, 33, 0, 99],
    }
    second_color = {
        "name": "乙",
        "pinyin": "",
        "RGB": [4, 5, 6],
        "hex": "#040506",
        "CMYK": [33, 17, 0, 98],
    }

    color_data.mutate_custom_colors(first, lambda colors: colors.append(first_color))
    color_data.mutate_custom_colors(second, lambda colors: colors.append(second_color))

    assert color_data.load_custom_colors(first) == [first_color]
    assert color_data.load_custom_colors(second) == [second_color]


def test_github_history_remains_valid_under_concurrent_writers(tmp_path: Path) -> None:
    context = MagicMock()
    context.data_dir = tmp_path

    def save(index: int) -> None:
        github._save_history(
            [{"full_name": f"owner/repo-{index}", "url": "https://github.com/o/r"}],
            "daily",
            context,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(save, range(24)))

    latest = json.loads((tmp_path / "trending_daily_latest.json").read_text(encoding="utf-8"))
    assert latest["count"] == 1
    assert latest["repositories"][0]["full_name"].startswith("owner/repo-")


def test_sensitive_log_regressions_are_absent() -> None:
    expectations = {
        "echo/main.py": ("Echo command: %s",),
        "choice/main.py": ("问题='{question", "选择结果: {choices}"),
        "guess_number/main.py": ("target=%d",),
        "chat/main.py": ("使用代理: {proxy}",),
        "voice/main.py": ("{error_text}",),
        "url_parser/main.py": (
            "标题、描述和图片: {url}",
            "else url}",
        ),
    }
    for relative_path, forbidden in expectations.items():
        source = (ROOT / "plugins" / relative_path).read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in source
