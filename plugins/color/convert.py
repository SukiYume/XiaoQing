"""验证并转换 RGB、CMYK 与 HEX 颜色值。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import cast

RGB_MIN = 0
RGB_MAX = 255
CMYK_MIN = 0
CMYK_MAX = 100
_HEX_PATTERN = re.compile(r"#?(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})")


def _validate_channels(
    values: Sequence[object],
    *,
    label: str,
    expected_count: int,
    minimum: int,
    maximum: int,
) -> tuple[bool, str | None]:
    if len(values) != expected_count:
        return False, f"{label} 需要 {expected_count} 个值，实际提供了 {len(values)} 个"

    for index, value in enumerate(values, start=1):
        if type(value) is not int:
            return False, f"{label} 第 {index} 个值必须是整数"
        if not minimum <= value <= maximum:
            return False, f"{label} 值必须在 {minimum}-{maximum} 范围内，第 {index} 个值为 {value}"
    return True, None


def validate_rgb(rgb: Sequence[object]) -> tuple[bool, str | None]:
    """验证三个精确整数 RGB 通道，布尔值不视为整数。"""

    return _validate_channels(
        rgb,
        label="RGB",
        expected_count=3,
        minimum=RGB_MIN,
        maximum=RGB_MAX,
    )


def validate_cmyk(cmyk: Sequence[object]) -> tuple[bool, str | None]:
    """验证四个精确整数 CMYK 通道，布尔值不视为整数。"""

    return _validate_channels(
        cmyk,
        label="CMYK",
        expected_count=4,
        minimum=CMYK_MIN,
        maximum=CMYK_MAX,
    )


def _require_rgb(rgb: Sequence[object]) -> tuple[int, int, int]:
    valid, error = validate_rgb(rgb)
    if not valid:
        raise ValueError(error or "RGB 颜色值无效")
    return cast(tuple[int, int, int], tuple(rgb))


def rgb_to_cmyk(rgb: Sequence[object]) -> list[int]:
    """把有效 RGB 转成 0-100 的整数 CMYK。"""

    red, green, blue = _require_rgb(rgb)
    if (red, green, blue) == (0, 0, 0):
        return [0, 0, 0, CMYK_MAX]

    cyan = 1 - red / RGB_MAX
    magenta = 1 - green / RGB_MAX
    yellow = 1 - blue / RGB_MAX
    black = min(cyan, magenta, yellow)
    scale = 1 - black
    return [
        round((cyan - black) / scale * CMYK_MAX),
        round((magenta - black) / scale * CMYK_MAX),
        round((yellow - black) / scale * CMYK_MAX),
        round(black * CMYK_MAX),
    ]


def cmyk_to_rgb(cmyk: Sequence[object]) -> list[int]:
    """按标准设备无关近似把 0-100 CMYK 转成整数 sRGB。"""

    valid, error = validate_cmyk(cmyk)
    if not valid:
        raise ValueError(error or "CMYK 颜色值无效")
    cyan, magenta, yellow, black = cast(tuple[int, int, int, int], tuple(cmyk))
    return [
        round(RGB_MAX * (1 - channel / CMYK_MAX) * (1 - black / CMYK_MAX))
        for channel in (cyan, magenta, yellow)
    ]


def hex_to_rgb(hex_value: str) -> list[int]:
    """解析一个可带单个 ``#`` 的三位或六位 ASCII HEX 值。"""

    if not isinstance(hex_value, str) or _HEX_PATTERN.fullmatch(hex_value) is None:
        raise ValueError("HEX 颜色值必须是 #RGB、RGB、#RRGGBB 或 RRGGBB")
    value = hex_value.removeprefix("#")
    if len(value) == 3:
        value = "".join(character * 2 for character in value)
    return [int(value[index : index + 2], 16) for index in range(0, 6, 2)]


def rgb_to_hex(rgb: Sequence[object]) -> str:
    """把有效 RGB 规范化为小写 ``#rrggbb``。"""

    return "#{:02x}{:02x}{:02x}".format(*_require_rgb(rgb))
