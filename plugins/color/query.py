"""在已校验的颜色记录中执行确定性精确或子串查询。"""

from __future__ import annotations

from collections.abc import Sequence

from .data_manager import ColorRecord


def find_by_name(colors: Sequence[ColorRecord], name: str) -> ColorRecord | None:
    return next((color for color in colors if color["name"] == name), None)


def find_by_rgb(colors: Sequence[ColorRecord], rgb: list[int]) -> ColorRecord | None:
    return next((color for color in colors if color["RGB"] == rgb), None)


def find_by_hex(colors: Sequence[ColorRecord], hex_value: str) -> ColorRecord | None:
    normalized = hex_value.casefold()
    return next((color for color in colors if color["hex"].casefold() == normalized), None)


def find_by_cmyk(colors: Sequence[ColorRecord], cmyk: list[int]) -> ColorRecord | None:
    return next((color for color in colors if color["CMYK"] == cmyk), None)


def find_by_keyword(colors: Sequence[ColorRecord], keyword: str) -> list[ColorRecord]:
    return [color for color in colors if keyword in color["name"]]
