"""Pendo Web 像素图标：样式表由位图脚本生成，两者保持一致。"""

from __future__ import annotations

import importlib.util

import pytest

from tests.helpers.paths import REPOSITORY_ROOT

SCRIPT = REPOSITORY_ROOT / "plugins" / "pendo" / "scripts" / "build_pixel_icons.py"


def _load_builder():
    spec = importlib.util.spec_from_file_location("pendo_build_pixel_icons", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_icon_stylesheet_matches_the_bitmaps_and_rejects_malformed_bitmaps() -> None:
    """改了位图没重新生成、或位图不是 12×12 时都要被发现。"""

    builder = _load_builder()

    assert builder.ICONS_CSS.read_text(encoding="utf-8") == builder.render()

    square = "\n".join(["#..........."] * 12)
    assert "M0 0h1v1h-1z" in builder.mask_url(square)
    for malformed in (
        "\n".join(["#..........."] * 11),
        "\n".join(["#.........."] * 12),
        square.replace("#", "x"),
    ):
        with pytest.raises(ValueError):
            builder.mask_url(malformed)
