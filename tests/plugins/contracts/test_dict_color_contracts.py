"""CR-277 regressions for Dict assets and Color mutation/cache contracts."""

from __future__ import annotations

import hashlib
import json
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.bounded_file_cache import FileCacheLimits
from plugins.color import data_manager as color_data
from plugins.color import image_gen as color_images
from plugins.color import main as color_main
from plugins.dict import main as dict_main
from tests.helpers.assertions import text_segments_text
from tests.helpers.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT


def test_dict_manifest_covers_full_bundled_assets_and_usage_terms():
    asset_dir = ROOT / "plugins" / "dict" / "assets"
    manifest = json.loads((asset_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 1
    assert manifest["source_version"] == "r241020"
    assert manifest["source_archive"].endswith("astrodict_241020.zip")
    assert len(manifest["source_archive_sha256"]) == 64
    assert manifest["ownership"] == "中国天文学会"
    assert manifest["usage_terms"].startswith("https://nadc.china-vo.org/")
    assert "MIT" not in manifest["license"]
    assert {spec["filename"] for spec in manifest["files"].values()} == {
        "astrodict_ce.txt",
        "astrodict_ec.txt",
    }

    expected_entries = {
        "english_to_chinese": 30_094,
        "chinese_to_english": 26_770,
    }
    for direction, spec in manifest["files"].items():
        path    = asset_dir / spec["filename"]
        payload = path.read_bytes()
        assert len(payload) == spec["bytes"]
        assert hashlib.sha256(payload).hexdigest() == spec["sha256"]
        assert len(payload.decode("utf-8").splitlines()) == spec["entries"]
        assert spec["entries"] == expected_entries[direction]


def test_dict_uses_standard_library_and_package_inventory_is_complete(monkeypatch):
    monkeypatch.setitem(sys.modules, "pandas", None)
    dict_main._load_dictionary.cache_clear()
    plugin_dir = ROOT / "plugins" / "dict"

    result = dict_main._query_astrodict_sync(
        "1-mirror telescope",
        plugin_dir,
        True,
        10,
    )

    assert "单反光面望远镜" in result
    assert "pandas" not in (plugin_dir / "main.py").read_text(encoding="utf-8").casefold()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["tool"]["setuptools"]["package-data"]["plugins.dict"] == [
        "assets/*.txt",
        "assets/manifest.json",
    ]
    for relative in (
        "assets/astrodict_ce.txt",
        "assets/astrodict_ec.txt",
        "assets/manifest.json",
    ):
        assert (plugin_dir / relative).is_file()


def test_color_manifest_count_and_docs_match_real_palette():
    plugin_dir = ROOT / "plugins" / "color"
    palette = json.loads((plugin_dir / "color.json").read_text(encoding="utf-8"))
    manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
    readme = (plugin_dir / "README.md").read_text(encoding="utf-8")

    assert len(palette) == 526
    assert "builtin_color_count" not in manifest
    assert "526" in manifest["description"]
    assert "8000" not in manifest["description"]
    assert "526" in readme
    assert "8000" not in readme


def _color_context(tmp_path: Path, *, admin: bool) -> SimpleNamespace:
    actor = 42
    return SimpleNamespace(
        current_group_id = 1001,
        current_user_id  = actor,
        data_dir         = tmp_path / "color-data",
        is_global_admin=lambda user_id=None: admin and int(user_id) == actor,
        logger     = MagicMock(),
        plugin_dir = ROOT / "plugins" / "color",
    )


@pytest.mark.asyncio
async def test_color_mutations_require_current_authenticated_bot_admin(monkeypatch, tmp_path):
    context = _color_context(tmp_path, admin=False)

    async def no_image(*_args, **_kwargs):
        return None

    monkeypatch.setattr(color_main.image_gen, "generate_color_image", no_image)

    denied = await color_main.handle(
        "color",
        "-w 审核红 1 2 3",
        {"user_id": 42, "group_id": 1001},
        context,
    )
    assert "只有 Bot 全局管理员" in text_segments_text(denied)
    assert not color_data._custom_file(context).exists()

    context.is_global_admin = lambda user_id=None: int(user_id) == 42
    added = await color_main.handle(
        "color",
        "-w 审核红 1 2 3",
        {"user_id": 42, "group_id": 1001},
        context,
    )
    assert "添加成功" in text_segments_text(added)
    assert color_data._custom_file(context).is_file()

    context.is_global_admin = lambda _user_id=None: False
    visible = await color_main.handle(
        "color",
        "-n 审核红",
        {"user_id": 99, "group_id": 1001},
        context,
    )
    denied_delete = await color_main.handle(
        "color",
        "-d 审核红",
        {"user_id": 99, "group_id": 1001},
        context,
    )
    assert "审核红" in text_segments_text(visible)
    assert "只有 Bot 全局管理员" in text_segments_text(denied_delete)
    assert any(color["name"] == "审核红" for color in color_data.load_custom_colors(context))


def test_color_custom_store_rejects_oversized_manual_scope_file(tmp_path):
    context = _color_context(tmp_path, admin=True)
    custom_file = color_data._custom_file(context)
    custom_file.parent.mkdir(parents=True)
    original = [{"name": str(index)} for index in range(201)]
    custom_file.write_text(json.dumps(original), encoding="utf-8")

    with pytest.raises(ValueError, match="count exceeds"):
        color_data.mutate_custom_colors(context, lambda colors: colors.clear())

    assert json.loads(custom_file.read_text(encoding="utf-8")) == original


@pytest.mark.asyncio
async def test_color_image_cache_is_content_addressed_and_lru_bounded(monkeypatch, tmp_path):
    calls: list[str] = []

    def fake_render(name: str, _rgb: list[int]) -> bytes:
        calls.append(name)
        return name.encode("utf-8")

    async def inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(color_images, "MATPLOTLIB_AVAILABLE", True)
    monkeypatch.setattr(color_images, "_render_color_image", fake_render)
    monkeypatch.setattr(color_images, "run_sync", inline)
    monkeypatch.setattr(
        color_images,
        "IMAGE_CACHE_LIMITS",
        FileCacheLimits(max_entries=2, max_bytes=100, ttl_seconds=60),
    )
    context = SimpleNamespace(logger=MagicMock())
    output_dir = tmp_path / "images"

    first = await color_images.generate_color_image("first", [1, 2, 3], output_dir, context)
    await color_images.generate_color_image("second", [4, 5, 6], output_dir, context)
    assert await color_images.generate_color_image("first", [1, 2, 3], output_dir, context) == first
    await color_images.generate_color_image("third", [7, 8, 9], output_dir, context)

    assert calls == ["first", "second", "third"]
    assert len(list(output_dir.glob("*.png"))) == 2
    assert Path(first).is_file()
