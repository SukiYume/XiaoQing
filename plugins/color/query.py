"""在已校验的颜色记录中执行确定性文本与近似颜色查询。"""

from __future__ import annotations

import math
from collections.abc import Sequence

from .data_manager import ColorRecord


def find_by_name(colors: Sequence[ColorRecord], name: str) -> ColorRecord | None:
    return next((color for color in colors if color["name"] == name), None)


def find_by_pinyin(colors: Sequence[ColorRecord], pinyin: str) -> list[ColorRecord]:
    normalized = pinyin.casefold()
    return [color for color in colors if color.get("pinyin", "").casefold() == normalized]


def find_by_rgb(colors: Sequence[ColorRecord], rgb: list[int]) -> ColorRecord | None:
    return next((color for color in colors if color["RGB"] == rgb), None)


def find_by_hex(colors: Sequence[ColorRecord], hex_value: str) -> ColorRecord | None:
    normalized = hex_value.casefold()
    return next((color for color in colors if color["hex"].casefold() == normalized), None)


def find_by_cmyk(colors: Sequence[ColorRecord], cmyk: list[int]) -> ColorRecord | None:
    return next((color for color in colors if color["CMYK"] == cmyk), None)


def find_by_keyword(colors: Sequence[ColorRecord], keyword: str) -> list[ColorRecord]:
    """按名称或拼音搜索，并以精确、前缀、子串的顺序稳定排序。"""

    normalized                                 = keyword.casefold()
    ranked: list[tuple[int, int, ColorRecord]] = []
    for index, color in enumerate(colors):
        name          = color["name"]
        folded_name   = name.casefold()
        folded_pinyin = color.get("pinyin", "").casefold()
        if folded_name == normalized:
            rank = 0
        elif folded_pinyin == normalized:
            rank = 1
        elif folded_name.startswith(normalized):
            rank = 2
        elif folded_pinyin.startswith(normalized):
            rank = 3
        elif normalized in folded_name:
            rank = 4
        elif normalized in folded_pinyin:
            rank = 5
        else:
            continue
        ranked.append((rank, index, color))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [color for _, _, color in ranked]


def _rgb_to_lab(rgb: Sequence[int]) -> tuple[float, float, float]:
    """把 sRGB 转换到 D65 CIE L*a*b*，用于确定性的感知近似。"""

    def linearize(channel: int) -> float:
        normalized = channel / 255
        if normalized <= 0.04045:
            return normalized / 12.92
        return math.pow((normalized + 0.055) / 1.055, 2.4)

    red, green, blue = (linearize(channel) for channel in rgb)
    x = (0.4124564 * red + 0.3575761 * green + 0.1804375 * blue) / 0.95047
    y = 0.2126729 * red + 0.7151522 * green + 0.072175 * blue
    z = (0.0193339 * red + 0.119192 * green + 0.9503041 * blue) / 1.08883

    delta = 6 / 29

    def pivot(value: float) -> float:
        if value > delta**3:
            return math.pow(value, 1 / 3)
        return value / (3 * delta**2) + 4 / 29

    fx, fy, fz = pivot(x), pivot(y), pivot(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def find_nearest_by_rgb(
    colors: Sequence[ColorRecord],
    rgb: Sequence[int],
) -> tuple[ColorRecord, float] | None:
    """返回 CIE76 距离最小的颜色；同距时保持词库顺序。"""

    if not colors:
        return None
    target_lab    = _rgb_to_lab(rgb)
    best_color    = colors[0]
    best_distance = math.dist(target_lab, _rgb_to_lab(best_color["RGB"]))
    for color in colors[1:]:
        distance = math.dist(target_lab, _rgb_to_lab(color["RGB"]))
        if distance < best_distance:
            best_color    = color
            best_distance = distance
    return best_color, best_distance
